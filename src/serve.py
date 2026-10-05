"""Local server for the explorer: serves the page and answers its lookups from the whole graph.

The page embeds only the most connected symbols. Served from here, its graph tab can search
every symbol and load any symbol's neighbours on demand. Opened as a file, the page works as
before, limited to what it embeds.
"""
import json, os, re, sqlite3, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEARCH_CAP = 40
NEIGHBOUR_CAP = 150

_NODE_SQL = """SELECT s.usr_hash, s.name, s.kind, s.module, f.rel, s.def_line, s.in_deg, s.out_deg,
                      s.call_in, s.call_out, s.ref_count, s.in_repo
               FROM symbols s LEFT JOIN files f ON f.path_hash = s.def_path_hash"""


_cache_lock = threading.Lock()


def _cached(cache, db_path, key, fn):
    # One lock, so a request that arrives while the warm-up is computing waits for it instead
    # of running the same multi-second query a second time.
    with _cache_lock:
        stamp = os.path.getmtime(db_path)
        hit = cache.get(key)
        if not hit or hit[0] != stamp:
            hit = cache[key] = (stamp, fn())
        return hit[1]


def _regexp(pattern, value):
    try:
        return value is not None and re.search(pattern, value, re.I) is not None
    except re.error:
        return False


def _node(r):
    return {"h": str(r[0]), "n": r[1], "k": r[2], "m": r[3] or "", "f": r[4] or "", "l": r[5] or 0,
            "i": r[6], "o": r[7], "ci": r[8], "co": r[9], "rc": r[10], "x": 1}


def search(db, text, limit=SEARCH_CAP):
    """In-repo symbols whose name contains `text`, case-insensitively, most connected first.
    `Module.name` narrows to modules whose name contains the part before the last dot."""
    q = text.strip().lower()
    dot = q.rfind(".")
    mq, nq = (q[:dot], q[dot + 1:]) if dot > 0 else ("", q)
    if not nq and not mq:
        return {"nodes": [], "more": False}
    # instr on lower() matches the page's own substring search exactly; LIKE would also treat
    # % and _ in a name as wildcards.
    where, args = "s.in_repo = 1 AND instr(lower(s.name), ?) > 0", [nq]
    if mq:
        where += " AND instr(lower(COALESCE(s.module, '')), ?) > 0"
        args.append(mq)
    rows = db.execute(f"{_NODE_SQL} WHERE {where} ORDER BY s.in_deg + s.out_deg DESC LIMIT ?",
                      args + [limit + 1]).fetchall()
    return {"nodes": [_node(r) for r in rows[:limit]], "more": len(rows) > limit}


def neighbours(db, usr_hash, limit=NEIGHBOUR_CAP):
    """Every edge into and out of one symbol, grouped by (src, dst, kind) and heaviest first,
    with the node at the other end of each."""
    me = int(usr_hash)
    rows = db.execute("""SELECT src, dst, kind, COUNT(*) n, MIN(path_hash), MIN(line) FROM edges
                         WHERE src = ? OR dst = ? GROUP BY src, dst, kind ORDER BY n DESC""",
                      (me, me)).fetchall()
    total = len(rows)
    rows = rows[:limit]
    others = {d if s == me else s for s, d, *_ in rows}
    nodes, paths = {}, {}
    for h in others | {me}:
        r = db.execute(f"{_NODE_SQL} WHERE s.usr_hash = ?", (h,)).fetchone()
        if r:
            nodes[h] = _node(r)
    for ph in {r[4] for r in rows if r[4] is not None}:
        r = db.execute("SELECT COALESCE(rel, path) FROM files WHERE path_hash = ?", (ph,)).fetchone()
        paths[ph] = r[0] if r else ""
    edges = [[str(s), str(d), k, paths.get(ph, ""), line or 0, n]
             for s, d, k, n, ph, line in rows if s in nodes and d in nodes]
    return {"nodes": list(nodes.values()), "edges": edges, "total": total, "kept": len(edges)}


SORTS = {"deg": "s.in_deg + s.out_deg DESC", "ci": "s.call_in DESC", "co": "s.call_out DESC",
         "rc": "s.ref_count DESC", "name": "lower(s.name)"}
SYMBOL_CAP = 400


def symbols(db, q="", kind="", module="", sort="deg", limit=SYMBOL_CAP):
    """The symbols tab's filter over the whole graph: a substring of the name or file path, or
    /regex/ on the name, plus exact kind and module. Returns the first `limit` and the total."""
    where, args = ["s.in_repo = 1"], []
    q = q.strip()
    if q.startswith("/") and q.rfind("/") > 0:
        where.append("regexp(?, s.name)")
        args.append(q[1:q.rfind("/")])
    elif q:
        # Matching paths first: the files table is small, and joining it per symbol made this
        # the slowest filter by a factor of ten.
        where.append("""(instr(lower(s.name), ?) > 0 OR s.def_path_hash IN
                         (SELECT path_hash FROM files WHERE instr(lower(rel), ?) > 0))""")
        args += [q.lower(), q.lower()]
    if kind:
        where.append("s.kind = ?"); args.append(kind)
    if module:
        where.append("s.module = ?"); args.append(module)
    cond = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM symbols s WHERE {cond}", args).fetchone()[0]
    rows = db.execute(f"{_NODE_SQL} WHERE {cond} ORDER BY {SORTS.get(sort, SORTS['deg'])} LIMIT ?",
                      args + [limit]).fetchall()
    return {"nodes": [_node(r) for r in rows], "total": total}


def facets(db):
    kinds = [r[0] for r in db.execute("SELECT DISTINCT kind FROM symbols WHERE in_repo = 1 ORDER BY kind")]
    mods = [r[0] for r in db.execute("""SELECT DISTINCT module FROM symbols WHERE in_repo = 1
                                        AND module IS NOT NULL AND module != '' ORDER BY module""")]
    return {"kinds": kinds, "modules": mods}


def dead(db):
    """Every dead-code and test-only candidate, in the page's row shape, instead of the first few
    hundred the page embeds."""
    import deadcode
    prior = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        total, rows = deadcode.candidates(db, limit=-1)
        cand = [[r["name"], r["kind"], r["module"] or "", r["rel"] or "", r["def_line"] or 0] for r in rows]
        to = [[r["name"], r["kind"], r["module"] or "", r["rel"] or "", r["def_line"] or 0, r["test_modules"]]
              for r in deadcode.test_only(db, limit=-1)]
    finally:
        db.row_factory = prior
    return {"dead": cand, "dead_total": total, "test_only": to}


def docs(history_path, q, limit=60):
    """Full-text matches in the repository's markdown docs, best first, with a snippet."""
    if not q.strip() or not os.path.exists(history_path):
        return {"docs": []}
    import history
    hdb = sqlite3.connect(f"file:{history_path}?mode=ro", uri=True)
    try:
        rows = history.docs_search(hdb, q, limit=limit)
    except sqlite3.Error:
        rows = []
    finally:
        hdb.close()
    return {"docs": [{"path": r[0], "title": r[1], "kind": r[2], "module": r[3], "published": r[4],
                      "snip": r[5]} for r in rows]}


class _Handler(BaseHTTPRequestHandler):
    html_path = None
    db_path = None
    local = threading.local()

    def _db(self):
        db = getattr(self.local, "db", None)
        if db is None:
            # Read-only, so a build swapping the file underneath never meets a writer here.
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            db.create_function("regexp", 2, _regexp, deterministic=True)
            self.local.db = db
        return db

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                with open(self.html_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/api/ping":
                return self._send(200, json.dumps({"ok": True}), "application/json")
            if url.path == "/api/search":
                return self._send(200, json.dumps(search(self._db(), qs.get("q", ""))), "application/json")
            if url.path == "/api/symbols":
                return self._json(symbols(self._db(), qs.get("q", ""), qs.get("kind", ""),
                                          qs.get("module", ""), qs.get("sort", "deg")))
            if url.path == "/api/facets":
                return self._json(facets(self._db()))
            if url.path == "/api/dead":
                return self._json(self._cached("dead", lambda: dead(self._db())))
            if url.path == "/api/docs":
                import history
                return self._json(docs(history.history_db_for(self.db_path), qs.get("q", "")))
            if url.path == "/api/neighbours" and qs.get("h", "").lstrip("-").isdigit():
                return self._send(200, json.dumps(neighbours(self._db(), qs["h"])), "application/json")
            self._send(404, "not found", "text/plain")
        except sqlite3.Error as e:
            # A rebuild swaps the database file, so drop the handle and let the next call reopen it.
            self.local.db = None
            self._send(503, json.dumps({"error": str(e)}), "application/json")

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json")

    def _cached(self, key, fn):
        # Keyed on the db file's mtime, so a rebuild swapping the file recomputes it.
        return _cached(self.cache, self.db_path, key, fn)

    def log_message(self, *args):
        pass


def serve(html_path, db_path, port=0, on_ready=None):
    """Serve until interrupted. Port 0 picks a free one; on_ready gets the URL."""
    handler = type("Handler", (_Handler,), {"html_path": html_path, "db_path": os.path.abspath(db_path),
                                            "local": threading.local(), "cache": {}})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    # The dead-code queries take seconds on a large graph, so they run before anyone opens the tab.
    def warm():
        try:
            db = sqlite3.connect(f"file:{handler.db_path}?mode=ro", uri=True)
            _cached(handler.cache, handler.db_path, "dead", lambda: dead(db))
            db.close()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()
    if on_ready:
        on_ready(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
