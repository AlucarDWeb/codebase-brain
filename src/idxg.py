#!/usr/bin/env python3
"""idxg: query a Swift/clang index-store knowledge graph (codebase-memory shaped)."""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project as prj
import deadcode
import impact
import modulecard
import usage

VERSION = prj.VERSION

KIND_BOOST = {"Function": 10, "InstanceMethod": 10, "ClassMethod": 10, "StaticMethod": 10,
              "Constructor": 8, "Class": 5, "Struct": 5, "Protocol": 5, "Enum": 5, "Extension": 3}
EDGE_KINDS = ["CALLS", "REFERENCES", "CONTAINS", "INHERITS", "OVERRIDES", "EXTENDS",
              "ACCESSOR_OF", "RECEIVED_BY", "SPECIALIZES", "IB_TYPE_OF"]
ROLE_BITS = [(1, "declaration"), (2, "definition"), (4, "reference"), (8, "read"), (16, "write"),
             (32, "call"), (64, "dynamic"), (128, "addressof"), (256, "implicit")]


def path_layer(rel, depth=2):
    """Group a repo-relative path into a layer: its first `depth` segments."""
    if not rel:
        return "(external)"
    parts = rel.split("/")
    if len(parts) <= depth:
        return parts[0]
    return "/".join(parts[:depth])


def db_path(arg):
    if arg and not os.path.isdir(os.path.expanduser(arg)):
        return os.path.expanduser(arg)
    # A folder means the project it belongs to: agents pass the repo they were told about.
    root = prj.find_root(os.path.expanduser(arg) if arg else None)
    db = prj.db_for(root)
    if os.path.exists(db):
        return db
    known = "".join(f"\n    {r}" for r in sorted(prj.load_registry()))
    raise SystemExit(f"no graph for {root}\n"
                     + (f"  indexed projects (pass one as --db, or as db to the MCP tools):{known}\n" if known else "")
                     + "  run: idxg init            (index this project and write its skill)")


def connect(arg, write=False):
    p = db_path(arg)
    uri = f"file:{p}" + ("" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("regexp", 2, lambda pat, val: 1 if val is not None and re.search(pat, val) else 0)
    return db


def check_stale(args):
    """Warn on stderr when the graph is older than the index store; refresh if configured."""
    if getattr(args, "no_stale_check", False):
        return
    try:
        db = connect(getattr(args, "db", None))
        m = meta(db)
        db.close()
    except SystemExit:
        raise
    except Exception:
        return
    stale, reason, _ = prj.staleness(m)
    if not stale:
        return
    root = m.get("repo_root")
    cfg = prj.effective_config(root)
    if cfg.get("auto_refresh_on_query") or getattr(args, "refresh", False):
        print(f"graph is stale ({reason}); reindexing {root}", file=sys.stderr)
        build_now(root, quiet=True)
        return
    print(f"note: graph is stale ({reason}). run `idxg refresh` to reindex {root}", file=sys.stderr)


def build_now(root, jobs=None, quiet=False, viz=None):
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "build.py"),
           "--root", root, "--jobs", str(jobs or prj.effective_config(root).get("jobs", 4))]
    if viz is False:
        cmd.append("--no-viz")
    out = subprocess.run(cmd, capture_output=quiet, text=True)
    if out.returncode != 0:
        if quiet and out.stderr:
            print(out.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"indexing failed for {root}")
    if quiet and out.stdout:
        try:
            return json.loads(out.stdout[out.stdout.index("{"):])
        except (ValueError, json.JSONDecodeError):
            return None
    return None


def meta(db):
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}


REGEX_META = set(".^$*+?()[]{}|\\") | set("[]")


def literal_name_filter(pattern):
    """Translate ^Foo$ / ^Foo / Foo$ into an indexable comparison, else (None, None).

    GLOB, not LIKE: SQLite's LIKE is case-insensitive for ASCII, which would return more
    than the equivalent regex and quietly disagree with the slow path.
    """
    anchored_start = pattern.startswith("^")
    anchored_end = pattern.endswith("$") and not pattern.endswith("\\$")
    body = pattern[1:] if anchored_start else pattern
    if anchored_end:
        body = body[:-1]
    if not body or REGEX_META & set(body):
        return None, None
    if anchored_start and anchored_end:
        return "s.name = ?", body
    if anchored_start:
        return "s.name GLOB ?", body + "*"
    if anchored_end:
        return "s.name GLOB ?", "*" + body
    return "s.name GLOB ?", f"*{body}*"


def source_line(root, rel_path, line, cache):
    """One line of a repo file with its whitespace collapsed, or '' when the file is gone."""
    if rel_path not in cache:
        try:
            with open(os.path.join(root, rel_path or ""), errors="replace") as f:
                cache[rel_path] = f.read().splitlines()
        except OSError:
            cache[rel_path] = []
    lines = cache[rel_path]
    return " ".join(lines[line - 1].split())[:110] if line and 0 < line <= len(lines) else ""


def roles_str(mask):
    return "|".join(n for b, n in ROLE_BITS if mask & b) or str(mask)


def rel(db, path_hash, cache={}):
    if path_hash in cache:
        return cache[path_hash]
    r = db.execute("SELECT COALESCE(rel, path) AS p FROM files WHERE path_hash = ?", (path_hash,)).fetchone()
    cache[path_hash] = r["p"] if r else "?"
    return cache[path_hash]


def qname(db, usr_hash, depth=6):
    """Qualified name by walking CONTAINS parents."""
    parts, seen, cur = [], set(), usr_hash
    for _ in range(depth):
        row = db.execute("SELECT name FROM symbols WHERE usr_hash = ?", (cur,)).fetchone()
        if not row:
            break
        parts.append(row["name"])
        p = db.execute("""SELECT e.src FROM edges e WHERE e.dst = ? AND e.kind = 'CONTAINS' LIMIT 1""",
                       (cur,)).fetchone()
        if not p or p["src"] in seen:
            break
        seen.add(p["src"]); cur = p["src"]
    mod = db.execute("SELECT module FROM symbols WHERE usr_hash = ?", (usr_hash,)).fetchone()
    head = [mod["module"]] if mod and mod["module"] else []
    return ".".join(head + list(reversed(parts)))


def resolve(db, ident, kind=None, limit=25):
    """Resolve a name, Module.name, Type.member, Module.Type.member or USR to symbol rows."""
    if ident.startswith(("s:", "c:")):
        rows = db.execute("SELECT * FROM symbols WHERE usr = ?", (ident,)).fetchall()
        if rows:
            return rows
    *quals, name = ident.split(".")
    kinds = kind.split(",") if kind else []

    def fetch(name_sql, name_arg, parent=None, module=None):
        q, args = "SELECT s.* FROM symbols s", []
        if parent:
            q += (" JOIN edges e ON e.dst = s.usr_hash AND e.kind = 'CONTAINS'"
                  " JOIN symbols p ON p.usr_hash = e.src AND p.name = ?")
            args.append(parent)
        q += f" WHERE s.{name_sql}"
        args.append(name_arg)
        if module:
            q += " AND (s.module = ? OR s.module LIKE ?)"
            args += [module, f"%{module}%"]
        if kinds:
            q += " AND s.kind IN (%s)" % ",".join("?" * len(kinds))
            args += kinds
        q += " GROUP BY s.usr_hash ORDER BY s.in_repo DESC, (s.in_deg + s.out_deg) DESC LIMIT ?"
        args.append(limit)
        return db.execute(q, args).fetchall()

    names = [("name = ?", name)]
    if "(" not in name:
        # Swift members carry their argument labels, so a bare name stands for every overload.
        names.append(("name GLOB ?", name + "(*"))
    if quals:
        # The qualifier right before the name is the enclosing type, or with nothing before it
        # the module; dropping it would rank every same-named member of the repo instead.
        lead = quals[0] if len(quals) > 1 and db.execute(
            "SELECT 1 FROM symbols WHERE module = ? LIMIT 1", (quals[0],)).fetchone() else None
        attempts = [{"parent": quals[-1], "module": lead}]
        if len(quals) == 1:
            attempts.append({"module": quals[0]})
    else:
        attempts = [{}]
    for at in attempts:
        for name_sql, arg in names:
            rows = fetch(name_sql, arg, **at)
            if rows:
                return rows
    if quals:
        return []
    return db.execute("""SELECT * FROM symbols WHERE name LIKE ? ORDER BY in_repo DESC,
                         (in_deg+out_deg) DESC LIMIT ?""", (f"{name}%", limit)).fetchall()


def sym_line(db, r, show_qn=True):
    loc = f"{rel(db, r['def_path_hash'])}:{r['def_line']}" if r["def_path_hash"] else "(no def site)"
    nm = qname(db, r["usr_hash"]) if show_qn else r["name"]
    return f"{nm}  {r['kind']}/{r['lang']}  {loc}  in={r['in_deg']} out={r['out_deg']} refs={r['ref_count']}"


# ---------------------------------------------------------------- commands
def cmd_status(a):
    db = connect(a.db)
    m = meta(db)
    tables = ("symbols", "edges", "occurrences", "defs", "files", "units")
    cached = all(f"count_{t}" in m for t in tables) and not a.exact
    if cached:
        counts = {t: int(m[f"count_{t}"]) for t in tables}
    else:
        counts = {t: db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    if m.get("edge_kinds") and not a.exact:
        per_kind = [{"kind": k, "c": v} for k, v in json.loads(m["edge_kinds"]).items()]
    else:
        per_kind = db.execute("SELECT kind, COUNT(*) c FROM edges GROUP BY kind ORDER BY c DESC").fetchall()
    in_repo = db.execute("SELECT COUNT(*) c FROM files WHERE in_repo = 1").fetchone()["c"]
    stamp = "" if a.exact else "  (counts as of the last build; --exact recounts)"
    swift = int(m.get("count_swift_symbols") or 0) or db.execute(
        "SELECT COUNT(*) c FROM symbols WHERE lang='Swift'").fetchone()["c"]
    root = m.get("repo_root", "")
    tracked = int(m.get("coverage_tracked") or 0)
    covered = int(m.get("coverage_covered") or 0)
    recounted = not cached
    if (not tracked or a.exact) and os.path.isdir(root):
        recounted = True
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m", "*.h", "*.mm",
                              "*.c", "*.cpp"], capture_output=True, text=True).stdout.split("\0")
        out = [f for f in out if f]
        tracked = len(out)
        have = {r["rel"] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1")}
        covered = sum(1 for f in out if f in have)
    if recounted:
        cache_summary(a.db, counts, per_kind, tracked, covered)
    if a.json:
        tag, url = prj.update_available(VERSION)
        print(json.dumps({"meta": m, "counts": counts, "history": _history_status(a.db),
                          "version": VERSION, "update_available": tag, "release_url": url,
                          "edges_by_kind": {r["kind"]: r["c"] for r in per_kind},
                          "files_in_repo": in_repo, "swift_symbols": swift,
                          "coverage": {"tracked_sources": tracked, "covered": covered,
                                       "pct": round(100 * covered / tracked, 1) if tracked else None}}, indent=2))
        return
    print(f"project:   {m.get('project')}")
    print(f"repo root: {m.get('repo_root')}")
    print(f"store:     {m.get('store_path')}")
    print(f"built:     {m.get('built_at')}  (format v{m.get('format_version')}, {m.get('build_seconds')}s)")
    print(f"db:        {db_path(a.db)}  ({os.path.getsize(db_path(a.db))/1e9:.2f} GB)")
    print(f"\ncounts{stamp}")
    for k, v in counts.items():
        print(f"  {k:<12} {v:>10,}")
    print(f"  {'swift syms':<12} {swift:>10,}")
    print(f"  {'files in repo':<12} {in_repo:>10,}")
    print("\nedges by kind")
    for r in per_kind:
        print(f"  {r['kind']:<12} {r['c']:>10,}")
    if tracked:
        print(f"\ncoverage: {covered:,}/{tracked:,} tracked sources have index records "
              f"({100*covered/tracked:.1f}%)")
        print("  a file with no records was never compiled in the indexed build; grep it instead.")
    hist_line = _history_status(a.db)
    if hist_line:
        print(f"\nhistory: {hist_line}")
    tag, url = prj.update_available(VERSION)
    if tag:
        print(f"\nupdate:  codebase-brain {tag} is available (installed {VERSION}). run `idxg update`  {url}")


def _history_status(db_arg):
    hist = _history_module()
    hp = hist.history_db_for(db_path(db_arg))
    if not os.path.exists(hp):
        return "not built (idxg history build adds commit history and repo docs)"
    h = hist.connect(hp)
    m = hist.meta(h)
    h.close()
    return (f"{int(m.get('count_commits') or 0):,} commits on {m.get('branch')} "
            f"({m.get('first_day')} to {m.get('last_day')}), {int(m.get('count_docs') or 0)} docs, "
            f"built {m.get('built_at')} at {m.get('head_sha', '')[:11]}")


def cache_summary(db_arg, counts, per_kind, tracked, covered):
    """Persist the expensive summaries so later calls read them instead of recounting.

    Best effort: a read-only volume or a concurrent build just means the next call
    recounts again.
    """
    try:
        w = sqlite3.connect(db_path(db_arg))
        # No WAL: a lingering journal beside the database breaks the build's atomic swap.
        w.execute("PRAGMA journal_mode=DELETE")
        with w:
            for name, value in counts.items():
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"count_{name}", str(value)))
            kinds = {(r["kind"] if not isinstance(r, dict) else r["kind"]):
                     (r["c"] if not isinstance(r, dict) else r["c"]) for r in per_kind}
            w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("edge_kinds", json.dumps(kinds)))
            if tracked:
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_tracked", str(tracked)))
                w.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("coverage_covered", str(covered)))
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.close()
    except sqlite3.Error:
        pass


def cmd_coverage(a):
    db = connect(a.db)
    m = meta(db)
    root = m.get("repo_root", "")
    out = []
    for p in a.paths:
        rp = os.path.relpath(os.path.realpath(p), root) if os.path.isabs(p) else p
        if os.path.isdir(os.path.join(root, rp)):
            rows = db.execute("""SELECT rel, (SELECT COUNT(*) FROM symbols s WHERE s.def_path_hash = f.path_hash) n
                                 FROM files f WHERE in_repo = 1 AND rel LIKE ?""", (rp.rstrip("/") + "/%",)).fetchall()
            import subprocess
            tracked = subprocess.run(["git", "-C", root, "ls-files", "-z", rp],
                                     capture_output=True, text=True).stdout.split("\0")
            src = [f for f in tracked if f.endswith((".swift", ".m", ".h", ".mm", ".c", ".cpp"))]
            have = {r["rel"] for r in rows}
            miss = [f for f in src if f not in have]
            out.append({"scope": rp, "kind": "dir", "sources": len(src), "covered": len(src) - len(miss),
                        "missing": miss[:40], "missing_count": len(miss)})
        else:
            r = db.execute("""SELECT path_hash, rel FROM files WHERE rel = ? OR path LIKE ?""",
                           (rp, f"%{rp}")).fetchone()
            if not r:
                out.append({"scope": rp, "kind": "file", "covered": False,
                            "reason": "no index record: file not compiled in the indexed build"})
            else:
                n = db.execute("SELECT COUNT(*) c FROM symbols WHERE def_path_hash = ?", (r["path_hash"],)).fetchone()["c"]
                occ = db.execute("SELECT COUNT(*) c FROM occurrences WHERE path_hash = ?", (r["path_hash"],)).fetchone()["c"]
                out.append({"scope": r["rel"], "kind": "file", "covered": True, "definitions": n, "occurrences": occ})
    if a.json:
        print(json.dumps(out, indent=2)); return
    for o in out:
        if o["kind"] == "file":
            if o["covered"]:
                print(f"COVERED  {o['scope']}  defs={o['definitions']} occurrences={o['occurrences']}")
            else:
                print(f"MISSING  {o['scope']}  {o['reason']}")
        else:
            print(f"DIR      {o['scope']}  {o['covered']}/{o['sources']} sources covered")
            for f in o["missing"]:
                print(f"           missing: {f}")
            if o["missing_count"] > len(o["missing"]):
                print(f"           ... {o['missing_count']-len(o['missing'])} more")
    print("\nnote: coverage reflects what the BSP build compiled. Absence is not proof a symbol does not exist.")


def cmd_search(a):
    db = connect(a.db)
    where, args = [], []
    if a.name:
        # An anchored literal is by far the most common pattern an agent sends, and it can
        # use the name index instead of running the regex over every symbol.
        sql, val = literal_name_filter(a.name)
        if sql:
            where.append(sql); args.append(val)
        else:
            where.append("regexp(?, s.name)"); args.append(a.name)
    if a.kind:
        ks = a.kind.split(",")
        where.append("s.kind IN (%s)" % ",".join("?" * len(ks))); args += ks
    if a.module:
        where.append("s.module = ?"); args.append(a.module)
    if a.file:
        where.append("f.rel GLOB ?"); args.append(a.file)
    if a.lang:
        where.append("s.lang = ?"); args.append(a.lang)
    if a.min_degree:
        where.append("(s.in_deg + s.out_deg) >= ?"); args.append(a.min_degree)
    if a.max_degree is not None:
        where.append("(s.in_deg + s.out_deg) <= ?"); args.append(a.max_degree)
    if not a.all:
        where.append("s.in_repo = 1")
    base = """FROM symbols s LEFT JOIN files f ON f.path_hash = s.def_path_hash"""
    if a.query:
        base = """FROM symbols_fts x JOIN fts_map m ON m.rowid = x.rowid
                  JOIN symbols s ON s.usr_hash = m.usr_hash
                  LEFT JOIN files f ON f.path_hash = s.def_path_hash"""
        where.insert(0, "symbols_fts MATCH ?")
        args.insert(0, " OR ".join(a.query.split()))
        order = "bm25(symbols_fts) - (s.in_deg + s.out_deg) / 50.0"
    else:
        order = "-(s.in_deg + s.out_deg)"
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) c {base} {w}", args).fetchone()["c"]
    rows = db.execute(f"""SELECT s.*, f.rel {base} {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                      args + [a.limit, a.offset]).fetchall()
    if a.json:
        print(json.dumps({"total": total, "returned": len(rows), "offset": a.offset,
                          "has_more": total > a.offset + len(rows),
                          "results": [{"qn": qname(db, r["usr_hash"]), "name": r["name"], "kind": r["kind"],
                                       "lang": r["lang"], "usr": r["usr"], "file": r["rel"], "line": r["def_line"],
                                       "in": r["in_deg"], "out": r["out_deg"], "calls_in": r["call_in"],
                                       "calls_out": r["call_out"], "refs": r["ref_count"], "module": r["module"]}
                                      for r in rows]}, indent=2))
        return
    print(f"total: {total}  returned: {len(rows)}  offset: {a.offset}")
    if a.detail == "ids":
        for r in rows:
            print(qname(db, r["usr_hash"]) + f"  [{r['usr']}]")
        print(f"has_more: {total > a.offset + len(rows)}")
        return
    group = None
    for r in rows:
        g = f"{r['module'] or '?'} ({r['rel'] or 'external'})"
        if g != group:
            group = g
            print(f"\n{group}:")
        loc = f":{r['def_line']}" if r["def_line"] else ""
        print(f"  {r['name']}  {r['kind']}{loc}  in={r['in_deg']} out={r['out_deg']} "
              f"calls={r['call_in']}/{r['call_out']} refs={r['ref_count']}")
        if a.usr:
            print(f"      {r['usr']}")
    print(f"\nhas_more: {total > a.offset + len(rows)}")


def _neighbors(db, uh, direction, kinds):
    ks = ",".join("?" * len(kinds))
    if direction == "in":
        q = f"""SELECT e.src AS other, e.kind, e.path_hash, e.line, s.name, s.kind AS skind, s.module,
                s.in_deg, s.out_deg FROM edges e JOIN symbols s ON s.usr_hash = e.src
                WHERE e.dst = ? AND e.kind IN ({ks}) GROUP BY e.src, e.kind, e.path_hash, e.line"""
    else:
        q = f"""SELECT e.dst AS other, e.kind, e.path_hash, e.line, s.name, s.kind AS skind, s.module,
                s.in_deg, s.out_deg FROM edges e JOIN symbols s ON s.usr_hash = e.dst
                WHERE e.src = ? AND e.kind IN ({ks}) GROUP BY e.dst, e.kind, e.path_hash, e.line"""
    return db.execute(q, [uh] + kinds).fetchall()


def cmd_trace(a):
    db = connect(a.db)
    kinds = a.kind.split(",")
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}; try idxg search --name '{a.symbol}'")
    if len(cands) > 1 and not a.first:
        print(f"{len(cands)} candidates for {a.symbol!r} (use --first or a USR):")
        for r in cands[:10]:
            print("  " + sym_line(db, r))
        return
    root = cands[0]
    directions = ["in", "out"] if a.direction == "both" else [a.direction]
    code_root = meta(db).get("repo_root", "") if getattr(a, "code", False) else None
    files_seen = {}

    def code_at(site):
        path, _, ln = site.rpartition(":")
        return source_line(code_root, path, int(ln) if ln.isdigit() else 0, files_seen)
    tree = {"symbol": qname(db, root["usr_hash"]), "usr": root["usr"], "kind": root["kind"],
            "file": rel(db, root["def_path_hash"]) if root["def_path_hash"] else None, "line": root["def_line"]}
    for d in directions:
        seen = set()

        def walk(uh, depth):
            if depth > a.depth or uh in seen:
                return []
            seen.add(uh)
            out = []
            rows = _neighbors(db, uh, d, kinds)
            agg = {}
            for r in rows:
                key = (r["other"], r["kind"])
                agg.setdefault(key, {"row": r, "sites": []})
                if r["path_hash"]:
                    agg[key]["sites"].append(f"{rel(db, r['path_hash'])}:{r['line']}")
            for (other, ekind), v in sorted(agg.items(), key=lambda kv: -len(kv[1]["sites"]))[:a.fanout]:
                r = v["row"]
                node = {"name": r["name"], "kind": r["skind"], "edge": ekind, "module": r["module"],
                        "sites": v["sites"][:3], "site_count": len(v["sites"]),
                        "children": walk(other, depth + 1)}
                if code_root is not None and v["sites"]:
                    node["code"] = code_at(v["sites"][0])
                out.append(node)
            return out

        tree[d] = walk(root["usr_hash"], 1)
    if a.json:
        print(json.dumps(tree, indent=2)); return
    printed = [0]
    spent = [0]
    loc = f"{tree['file']}:{tree['line']}" if tree["file"] else "external"
    print(f"{tree['symbol']}  [{tree['kind']}]  {loc}")
    print(f"edges: {','.join(kinds)}  depth: {a.depth}")

    def show(nodes, prefix=""):
        for i, n in enumerate(nodes):
            if printed[0] >= a.max_rows or spent[0] >= a.max_bytes:
                return
            last = i == len(nodes) - 1
            branch = "└─ " if last else "├─ "
            site = f"  {n['sites'][0]}" if n["sites"] else ""
            extra = f" (x{n['site_count']})" if n["site_count"] > 1 else ""
            line = f"{prefix}{branch}{n['name']}  {n['kind']} <{n['edge']}>{site}{extra}"
            if n.get("code"):
                line += f"\n{prefix}{'   ' if last else '│  '}   {n['code']}"
            print(line)
            printed[0] += 1
            spent[0] += len(line) + 1
            show(n["children"], prefix + ("   " if last else "│  "))

    for d in directions:
        label = "callers / inbound" if d == "in" else "callees / outbound"
        print(f"\n{label}:")
        if not tree[d]:
            print("  (none)")
        before = printed[0]
        show(tree[d])
        if printed[0] >= a.max_rows or spent[0] >= a.max_bytes:
            why = "rows" if printed[0] >= a.max_rows else "size"
            print(f"  ... truncated on {why} ({printed[0]} rows, {spent[0]} bytes). Narrow with "
                  f"--fanout / --depth / --kind, or raise --max-rows / --max-bytes")


def cmd_refs(a):
    db = connect(a.db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    rows = db.execute("""SELECT o.*, f.rel FROM occurrences o LEFT JOIN files f ON f.path_hash = o.path_hash
                         WHERE o.usr_hash = ? ORDER BY f.rel, o.line LIMIT ?""",
                      (r["usr_hash"], a.limit)).fetchall()
    total = db.execute("SELECT COUNT(*) c FROM occurrences WHERE usr_hash = ?", (r["usr_hash"],)).fetchone()["c"]
    if a.json:
        print(json.dumps({"symbol": qname(db, r["usr_hash"]), "usr": r["usr"], "total": total,
                          "occurrences": [{"file": x["rel"], "line": x["line"], "col": x["col"],
                                           "roles": roles_str(x["roles"])} for x in rows]}, indent=2))
        return
    print(f"{qname(db, r['usr_hash'])}  [{r['kind']}]  {total} occurrences")
    cur = None
    for x in rows:
        if x["rel"] != cur:
            cur = x["rel"]; print(f"\n{cur}:")
        print(f"  {x['line']}:{x['col']}  {roles_str(x['roles'])}")


def cmd_snippet(a):
    db = connect(a.db)
    m = meta(db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    if not r["def_path_hash"]:
        raise SystemExit("symbol has no definition site in this index (external or module symbol)")
    path = os.path.join(m["repo_root"], rel(db, r["def_path_hash"]))
    if not os.path.exists(path):
        raise SystemExit(f"file not found on disk: {path}")
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    start = max(0, r["def_line"] - 1)
    end = start
    depth, opened = 0, False
    for i in range(start, min(len(lines), start + a.max_lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            opened = True
        end = i
        if opened and depth <= 0:
            break
    if not opened:
        end = min(len(lines) - 1, start + 2)
    print(f"{rel(db, r['def_path_hash'])}:{r['def_line']}  [{r['kind']} {r['name']}]")
    spent = 0
    for i in range(start, end + 1):
        out = f"{i+1:6}  {lines[i]}"
        if spent + len(out) > a.max_bytes:
            print(f"  ... truncated at {i - start} lines / {spent} bytes "
                  f"(raise with --max-bytes)")
            break
        print(out)
        spent += len(out) + 1


def cmd_sql(a):
    db = connect(a.db)
    q = a.query.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise SystemExit("only SELECT / WITH queries are allowed")
    if not re.search(r"(?i)\blimit\b", q):
        q += f" LIMIT {a.limit}"
    rows = db.execute(q).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=2)); return
    if not rows:
        print("(no rows)"); return
    cols = rows[0].keys()
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\n{len(rows)} rows")


def cmd_arch(a):
    db = connect(a.db)
    mods = db.execute("""SELECT module, COUNT(*) syms, SUM(call_in) cin, SUM(call_out) cout
                         FROM symbols WHERE in_repo = 1 AND module IS NOT NULL
                         GROUP BY module ORDER BY syms DESC LIMIT ?""", (a.limit,)).fetchall()
    db.create_function("layer", 1, path_layer)
    layers = db.execute("""SELECT layer(rel) layer, COUNT(*) files FROM files
                           WHERE in_repo = 1 GROUP BY layer ORDER BY files DESC LIMIT 15""").fetchall()
    hot_in = db.execute("""SELECT name, kind, module, call_in FROM symbols WHERE in_repo = 1
                           ORDER BY call_in DESC LIMIT 10""").fetchall()
    hot_out = db.execute("""SELECT name, kind, module, call_out FROM symbols WHERE in_repo = 1
                            ORDER BY call_out DESC LIMIT 10""").fetchall()
    targets = db.execute("SELECT target, COUNT(*) n FROM units GROUP BY target ORDER BY n DESC").fetchall()
    if a.json:
        print(json.dumps({"modules": [dict(r) for r in mods], "layers": [dict(r) for r in layers],
                          "top_called": [dict(r) for r in hot_in], "top_callers": [dict(r) for r in hot_out],
                          "targets": [dict(r) for r in targets]}, indent=2))
        return
    print("layers (indexed files)")
    w = max([len(r["layer"] or "") for r in layers] + [10])
    for r in layers:
        print(f"  {(r['layer'] or ''):<{w}} {r['files']:>7,}")
    print("\ntop modules by symbol count")
    for r in mods:
        print(f"  {r['module']:<32} syms={r['syms']:>7,}  calls in/out={r['cin']:>7,}/{r['cout']:>7,}")
    print("\nmost-called symbols")
    for r in hot_in:
        print(f"  {r['name']:<40} {r['kind']:<16} {r['module'] or '':<24} {r['call_in']:>6,}")
    print("\nbiggest callers")
    for r in hot_out:
        print(f"  {r['name']:<40} {r['kind']:<16} {r['module'] or '':<24} {r['call_out']:>6,}")
    print("\nbuild targets")
    for r in targets:
        print(f"  {r['target']:<40} {r['n']:>6,} units")


def cmd_viz(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import viz
    db = connect(a.db)
    m = meta(db)
    cfgv = prj.effective_config(m.get("repo_root") or prj.find_root())
    scope = a.scope or (cfgv.get("viz_scope") or None)
    limit = a.limit if a.limit is not None else (20000 if scope else cfgv.get("viz_limit", 1500))
    data = viz.slice_data(db, scope=scope, limit=limit, edge_cap=a.edge_cap,
                          per_node_cap=a.per_node_cap if a.per_node_cap is not None
                          else cfgv.get("viz_per_node_cap", 25))
    root = m.get("repo_root", "")
    if os.path.isdir(root):
        import subprocess
        tracked = subprocess.run(["git", "-C", root, "ls-files", "-z", "*.swift", "*.m", "*.h", "*.mm"],
                                 capture_output=True, text=True).stdout.split("\0")
        tracked = [f for f in tracked if f]
        have = {r["rel"] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1")}
        cov = sum(1 for f in tracked if f in have)
        data["meta"]["coverage_pct"] = round(100 * cov / len(tracked), 1) if tracked else None
        data["meta"]["coverage"] = f"{cov}/{len(tracked)}"
    db_file = db_path(a.db)
    viz.attach_history(data, db_file, weeks=cfgv.get("viz_history_weeks", 26))
    # Derive the default from the database path, the same way build.py does, so viz and a
    # build cannot write two different explorer files for one project.
    default_out = os.path.join(os.path.dirname(db_file),
                               os.path.basename(db_file).replace(".db", "-explorer.html"))
    out = os.path.expanduser(a.out) if a.out else default_out
    viz.render(data, out, title=a.title)
    if not a.out and root:
        entry = prj.load_registry().get(root, {})
        prj.register(root, entry.get("db", db_file), entry.get("stores") or [], {"html": out})
    print(f"wrote {out}  ({os.path.getsize(out)/1e6:.1f} MB)")
    print(f"nodes: {len(data['nodes']):,} (slice {data['slice_size']:,})  edges: {len(data['edges']):,}")
    if a.open:
        subprocess_open = __import__("subprocess")
        subprocess_open.run(["open", out])


def cmd_module(a):
    """One module's card: folder and layers, what it uses, who uses it and through which symbols,
    its most connected types, and how much of its folder the build compiled."""
    db = connect(a.db)
    root = meta(db).get("repo_root", "")
    mod, near = modulecard.resolve(db, a.module)
    if not mod:
        raise SystemExit(f"no module named {a.module!r} in the graph" + (f"; close: {', '.join(near)}" if near else ""))
    mod_root, base, rels = modulecard.folder(db, mod)
    ranking = modulecard.type_ranking(db, mod)
    by_type = modulecard.type_users(db, mod)
    aliases = modulecard.top_aliases(db, mod)
    namesakes = modulecard.namesakes(db, mod)
    owners = modulecard.owners(db)
    root_of = {o: r for o, r in owners.values()}
    deps = modulecard.depends_on(db, mod)
    users = modulecard.used_by(db, mod)
    known = set(modulecard.modules(db))
    used = {r[0] for r in deps}
    unused_imports = sorted(modulecard.imports(root, rels) & known - used - {mod})
    siblings = db.execute("""SELECT module, COUNT(*) FROM files WHERE in_repo = 1 AND rel GLOB ? AND module != ?
                             AND module IS NOT NULL AND module != '' GROUP BY module ORDER BY 2 DESC""",
                          (mod_root + "/*", mod)).fetchall()
    sibling_names = {s for s, _ in siblings}
    under = [r[0] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1 AND rel GLOB ?", (mod_root + "/*",))]
    tracked, covered = modulecard.coverage(root, mod_root, under)
    importing, importing_indexed, import_only = modulecard.importers(db, root, mod, mod_root)
    symbols = db.execute("SELECT COUNT(*) FROM symbols WHERE module = ? AND in_repo = 1", (mod,)).fetchone()[0]
    basenames = {}
    for (r,) in db.execute("SELECT rel FROM files WHERE in_repo = 1 AND rel IS NOT NULL"):
        b = os.path.basename(r)
        basenames[b] = basenames.get(b, 0) + 1

    def short_site(site, owner_root):
        """A file name when it is unique in the repo, else the path inside the owner's folder."""
        rel_path, line = site
        b = os.path.basename(rel_path)
        if basenames.get(b) == 1:
            return f"{b}:{line}"
        if owner_root and rel_path.startswith(owner_root + "/"):
            return f"{rel_path[len(owner_root) + 1:]}:{line}"
        return f"{rel_path}:{line}"

    def layer_of(rel):
        sub = rel[len(base) + 1:] if rel and base and rel.startswith(base + "/") else (rel or "")
        return sub.split("/", 1)[0] if "/" in sub else "(top level)"

    groups = {"outside": {}, "folder": {}, "tests": {}}
    for k, v in users.items():
        key = "folder" if k in sibling_names else "tests" if modulecard.is_test(k) else "outside"
        groups[key][k] = v

    def type_groups(t):
        """{folder above the using module: {using module: first site}}: a module inside another's
        folder counts as that one, test modules go under 'tests' and this module's neighbours in
        its own folder under 'same folder'."""
        out = {}
        for user in t["users"]:
            owner, owner_root = owners.get(user, (user, user))
            if modulecard.is_test(user):
                key, name = "tests", user
            elif owner == mod or user in sibling_names:
                key, name = "same folder", user
            else:
                key, name = os.path.dirname(owner_root) or owner_root, owner
            group = out.setdefault(key, {})
            site = t["sites"].get(user)
            if name not in group or (site and (group[name] is None or site < group[name])):
                group[name] = site
        return out

    def group_text(t):
        found = dict(t["groups"])
        tests = len(found.pop("tests", {}))
        parts = []
        for g, v in sorted(found.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            names = [f"{n} ({short_site(site, root_of.get(n))})" if site else n for n, site in sorted(v.items())]
            parts.append(f"{g} {len(v)}: {', '.join(names[:30])}" + (" ..." if len(v) > 30 else ""))
        return "; ".join(parts) + (f"; tests {tests}" if tests else "")
    for t in by_type + aliases:
        t["groups"] = type_groups(t)
        t["outside"] = sum(len(v) for g, v in t["groups"].items() if g != "tests")
    by_type.sort(key=lambda t: (-t["outside"], -len(t["groups"].get("tests", ())), t["name"]))
    if getattr(a, "json", False):
        print(json.dumps({"module": mod, "root": mod_root, "files": len(rels), "symbols": symbols,
                          "coverage": {"tracked": tracked, "indexed": covered},
                          "same_folder": dict(siblings), "layers": modulecard.layers(rels, base),
                          "depends_on": [dict(module=d, symbols=s, edges=e) for d, s, e in deps],
                          "imported_unused": unused_imports,
                          "used_by": {g: {k: [dict(symbol=n, kind=kd, uses=c, site=st) for n, kd, c, st in v[:5]]
                                          for k, v in mods.items()} for g, mods in groups.items()},
                          "types": ranking[:a.types],
                          "type_users": [dict(type=t["name"], kind=t["kind"], file=t["rel"], line=t["line"],
                                              used_by={g: {n: f"{st[0]}:{st[1]}" if st else None for n, st in v.items()}
                                                       for g, v in t["groups"].items()})
                                         for t in by_type + aliases],
                          "same_name_elsewhere": namesakes,
                          "importers": {"files": len(importing), "compiled": len(importing_indexed),
                                        "never_compiled": sorted(set(importing) - set(importing_indexed)),
                                        "import_only": import_only}}, indent=1))
        return
    out = [f"{mod}  {mod_root}  {len(rels)} source files, {symbols:,} symbols"]
    if siblings:
        out.append("same folder: " + ", ".join(f"{s} ({n} file{'s' * (n != 1)})" for s, n in siblings))
    out.append(f"coverage: {covered}/{tracked} tracked source files under {mod_root}/ have index records"
               + ("; the others were never compiled, so nothing here speaks for them" if covered < tracked else ""))
    by_layer = {}
    for t in ranking:
        by_layer.setdefault(layer_of(t["rel"]), []).append(t["name"])
    out += ["", f"layers (folders under {base}/), with their most connected types:"]
    for seg, n in modulecard.layers(rels, base):
        noun = "files" if n != 1 else "file "
        out.append(f"  {seg:<18} {n:>4} {noun}  {', '.join(by_layer.get(seg, [])[:4]) or '-'}")
    out += ["", f"depends on {len(deps)} first-party modules (distinct symbols used):"]
    out += textwrap.wrap(", ".join(f"{d} {n}" for d, n, _ in deps), 100, initial_indent="  ", subsequent_indent="  ")
    if unused_imports:
        out.append("  imported, no symbol used: " + ", ".join(unused_imports))
    total = lambda v: sum(c for _, _, c, _ in v)
    out += ["", f"used by {len(groups['outside'])} modules outside its folder (symbols they use, one call site):"]
    ranked = sorted(groups["outside"].items(), key=lambda kv: -total(kv[1]))
    for k, v in ranked[:a.users]:
        out.append(f"  {k}: " + ", ".join(f"{n} x{c}" for n, _, c, _ in v[:3]) + f"  ({v[0][3]})")
    if len(ranked) > a.users:
        rest = ranked[a.users:]
        out.append(f"  and {len(rest)} more, using {sum(total(v) for _, v in rest):,} times in all: "
                   + ", ".join(k for k, _ in rest[:15]) + (" ..." if len(rest) > 15 else ""))
    for label in ("folder", "tests"):
        if groups[label]:
            out.append(f"  {'same folder' if label == 'folder' else 'tests'}: "
                       + ", ".join(f"{k} ({total(v)})" for k, v in sorted(groups[label].items(), key=lambda kv: -total(kv[1]))))
    if importing:
        prod = [f for f in importing if not usage.is_test(f)]
        never = sorted(set(importing) - set(importing_indexed), key=lambda f: (usage.is_test(f), f))
        out.append(f"  imported by {len(importing)} source files outside its folder ({len(prod)} production, "
                   f"{len(importing) - len(prod)} tests); {len(importing_indexed)} of them compiled")
        if never:
            out += textwrap.wrap(f"never compiled, so what they use is unknown ({len(never)}): "
                                 + ", ".join(never[:12]) + (" ..." if len(never) > 12 else ""),
                                 110, initial_indent="    ", subsequent_indent="      ")
        only = [f for f in import_only if not usage.is_test(f)]
        if only:
            out += textwrap.wrap(f"compiled production files that import it and use none of its symbols ({len(only)}): "
                                 + ", ".join(os.path.basename(f) for f in only[:15]) + (" ..." if len(only) > 15 else ""),
                                 110, initial_indent="    ", subsequent_indent="      ")
    out += ["", "most connected types (distinct symbols that use it / that it uses; members and extensions "
                "folded in, structural edges ignored):"]
    for i, t in enumerate(ranking[:a.types], 1):
        out.append(f"  {i:>2}. {t['name']:<40} {t['kind']:<8} used by {t['used_by']:>4}  uses {t['uses']:>4}  {layer_of(t['rel'])}")
    used_types = [t for t in by_type if t["outside"]]
    cap = getattr(a, "type_users", 30)
    out += ["", f"types other modules use: {len(used_types)} of {len(by_type)}, each with the modules that use it and "
                "the first place each one does, grouped by the folder above them (a use of a member or an extension "
                "counts for its type; a module inside another's folder counts as that one):"]
    for t in used_types[:cap]:
        where = t["rel"][len(base) + 1:] if t["rel"] and base and t["rel"].startswith(base + "/") else t["rel"]
        name = qname(db, t["h"]).split(".", 1)[-1]
        site = f"{where}:{t['line']}" if where else "no definition site"
        out += textwrap.wrap(f"{name} ({t['kind']}, {site})  " + group_text(t), 110,
                             initial_indent="  ", subsequent_indent="      ")
    if len(used_types) > cap:
        out.append(f"  and {len(used_types) - cap} more; raise type_users to list them")
    for label, names in (("used outside only by tests", [t for t in by_type if not t["outside"] and t["users"]]),
                         ("used by no other module in the compiled build", [t for t in by_type if not t["users"]])):
        if names:
            listed = ", ".join(qname(db, t["h"]).split(".", 1)[-1] for t in names[:40])
            out += textwrap.wrap(f"{label} ({len(names)}): {listed}" + (" ..." if len(names) > 40 else ""),
                                 110, initial_indent="  ", subsequent_indent="      ")
    if aliases:
        out += ["", f"typealiases declared outside any type ({len(aliases)}; an alias is a name, not a type, and a "
                    "nested alias already counts for its type):"]
        for t in aliases[:20]:
            where = t["rel"][len(base) + 1:] if t["rel"] and base and t["rel"].startswith(base + "/") else t["rel"]
            out += textwrap.wrap(f"{t['name']} ({where}:{t['line']})  " + (group_text(t) or "no other module uses it"),
                                 110, initial_indent="  ", subsequent_indent="      ")
    if namesakes:
        listed = [f"{n} ({', '.join(v[:4])}{' ...' if len(v) > 4 else ''})" for n, v in sorted(namesakes.items())]
        out += ["", *textwrap.wrap(f"names another module also gives a type ({len(namesakes)}); the uses above come "
                                   "from the compiler, so they never mix these up, but a text search does: "
                                   + ", ".join(listed[:20]) + (" ..." if len(listed) > 20 else ""),
                                   110, subsequent_indent="  ")]
    spent = 0
    for i, line in enumerate(out):
        if spent + len(line) + 1 > a.max_bytes:
            print(f"... {a.max_bytes:,} byte budget reached after {i} of {len(out)} lines; raise max_bytes, "
                  "or lower users or type_users")
            break
        print(line)
        spent += len(line) + 1


def cmd_usage(a):
    """Whether each symbol can be deleted: its uses in the compiled build, through protocol
    requirements it implements, and in tracked files the build never indexed."""
    db = connect(a.db)
    m = meta(db)
    rows = usage.check(db, m.get("repo_root", ""), a.symbols, resolve)
    if getattr(a, "json", False):
        print(json.dumps([{k: (dict(v) if k == "symbol" and v else v) for k, v in r.items()} for r in rows],
                         indent=1, default=str))
        return
    lines_cache = {}
    for i, r in enumerate(rows, 1):
        if not r["symbol"]:
            print(f"{i}. {r['ident']}\n   NOT FOUND in the graph; check the spelling, or pass its definition site as path:line\n")
            continue
        sym = r["symbol"]
        print(f"{i}. {qname(db, sym['usr_hash'])}  {sym['kind']}  {r['def']}")
        prod, tests = r["prod"], r["tests"]
        files = lambda us: len({u["rel"] for u in us})
        n = lambda count, word: f"{count} {word}{'s' * (count != 1)}"
        if r["verdict"] == "USED IN PRODUCTION":
            print(f"   USED IN PRODUCTION: {n(len(prod), 'use')} in {n(files(prod), 'production file')}, {len(tests)} in tests")
        elif r["verdict"] == "USED IN PRODUCTION (text only)":
            print("   USED IN PRODUCTION (text only): no use in the compiled build, but files the index has no "
                  "records for name it; check they are built")
        elif r["verdict"] == "USED ONLY BY TESTS":
            print(f"   USED ONLY BY TESTS: {n(len(tests), 'use')} in {n(files(tests), 'test file')}"
                  + (f", plus {len(r['text_test'])} text match(es) in unindexed test files" if r["text_test"] else ""))
        else:
            print("   UNUSED: no use in the compiled build"
                  + ("; the name appears in no other tracked file outside the indexed sources"
                     if r["text_searched"] else "; text search skipped, the name is too common to grep"))
        shown = prod[:3] if prod else tests[:3]
        for u in shown:
            caller = usage.enclosing(db, u)
            print(f"     {u['site']}" + (f"  in {qname(db, caller)}" if caller else "")
                  + (f"  (through {qname(db, u['via'])})" if u["via"] else "")
                  + ("  (implicit)" if u["implicit"] else ""))
            code = source_line(m.get("repo_root", ""), u["rel"], u["line"], lines_cache)
            if code:
                print(f"         {code}")
        # Text matches are evidence only where the graph found nothing stronger.
        text = r["text_prod"] if not prod else []
        text += r["text_test"] if not prod and not r["text_prod"] and not tests else []
        for rel, ln, kind, test, code in text[:3]:
            print(f"     {rel}:{ln}  (text, {kind} file without index records{', test' if test else ''})\n         {code}")
        if r["docs"]:
            print("     mentioned in docs: " + ", ".join(f"{d[0]}:{d[1]}" for d in r["docs"][:2]))
        if r["others"]:
            print(f"   {r['others']} other symbol(s) share this name; pass the definition site as path:line to pick another")
        print()
    cov_t, cov_c = m.get("coverage_tracked"), m.get("coverage_covered")
    if cov_t:
        print(f"the build indexed {int(cov_c):,} of {int(cov_t):,} tracked sources; the text search covers the rest, "
              "but a use built from a string at runtime is invisible to both")


def cmd_impact(a):
    """What a signature change to a method or property breaks: implementations and overrides,
    calls of it or of them, the members of its protocol's extensions, and the lines of files the
    build never compiled that name its type."""
    db = connect(a.db)
    m = meta(db)
    root = m.get("repo_root", "")
    cands = [r for r in resolve(db, a.symbol) if r["in_repo"]]
    if not cands:
        raise SystemExit(f"no symbol in this repo matched {a.symbol!r}; try Type.member, Module.Type.member or a USR")
    # A type's extensions carry its name, so a bare type name matches them too.
    if len(cands) > 1 and all(r["kind"] in modulecard.TYPE_KINDS + ("Extension",) for r in cands):
        cands = [r for r in cands if r["kind"] != "Extension"] or cands[:1]
    if len(cands) > 1:
        print(f"{len(cands)} symbols match {a.symbol!r}; pass one as Module.Type.member or by USR:")
        for r in cands[:12]:
            print(f"  {sym_line(db, r)}\n    usr {r['usr']}")
        return
    sym = cands[0]
    if sym["kind"] in modulecard.TYPE_KINDS + ("Extension",):
        raise SystemExit(f"{a.symbol} is a {sym['kind']}; impact_of takes a method or property. For a type, "
                         "describe_module lists who uses each type, find_references every occurrence")
    lines_cache = {}
    uh = sym["usr_hash"]
    short = lambda h: qname(db, h).split(".", 1)[-1]
    type_hash, type_row = impact.container(db, uh)
    impls = impact.implementations(db, uh)
    targets = [t for h in [uh] + [h for h, _ in impls] for t in impact.with_accessors(db, h)]
    calls, refs = impact.sites(db, targets)
    call_keys = {(c["rel"], c["line"]) for c in calls}
    refs = [r for r in refs if (r["rel"], r["line"]) not in call_keys]
    helpers = impact.extension_members(db, type_hash, set(targets)) \
        if type_row is not None and type_row["kind"] == "Protocol" else []
    text, docs = impact.unindexed_text(root, db, type_row["name"] if type_row is not None else None,
                                       impact.bare(sym["name"]))
    def_rel = rel(db, sym["def_path_hash"]) if sym["def_path_hash"] else None
    if getattr(a, "json", False):
        info = lambda h: dict(symbol=qname(db, h), file=rel(db, db.execute(
            "SELECT def_path_hash FROM symbols WHERE usr_hash = ?", (h,)).fetchone()[0] or 0))
        print(json.dumps({"symbol": qname(db, uh), "usr": sym["usr"], "file": def_rel, "line": sym["def_line"],
                          "implements": [qname(db, h) for h in impact.implements(db, uh)],
                          "implementations": [dict(info(h), level=lv) for h, lv in impls],
                          "calls": [dict(c, caller=qname(db, c["caller"]), target=qname(db, c["target"])) for c in calls],
                          "references": [dict(r, caller=qname(db, r["caller"]), target=qname(db, r["target"])) for r in refs],
                          "extension_members": [dict(h, h=None, symbol=qname(db, h["h"])) for h in helpers],
                          "unindexed_text": {k: [list(x) for x in v] for k, v in text.items()}, "docs": docs},
                         indent=1, default=str))
        return

    out = [f"{qname(db, uh)}  {sym['kind']}  {def_rel}:{sym['def_line']}" if def_rel else
           f"{qname(db, uh)}  {sym['kind']}  (no definition site in the index)"]
    decl = source_line(root, def_rel, sym["def_line"], lines_cache) if def_rel else ""
    if decl:
        out.append(f"    {decl}")
    if type_row is not None and type_row["kind"] == "Protocol":
        out.append(f"  a requirement of protocol {type_row['name']}: every implementation below changes with it")
    for req in impact.implements(db, uh):
        out.append(f"  it implements {qname(db, req)}; a signature change breaks that conformance, so run "
                   f"impact_of on the requirement for the whole picture")

    def listing(items, fmt):
        """Items grouped by module, production first, up to max_rows; the rest as counts per module."""
        groups = {}
        for it in items:
            groups.setdefault((it["test"], it["module"] or "(no module)"), []).append(it)
        lines, shown, rest = [], 0, {}
        for (test, mod), group in sorted(groups.items()):
            for i, it in enumerate(group):
                if shown >= a.max_rows:
                    rest[mod] = len(group) - i
                    break
                if i == 0:
                    lines.append(f"  {mod}" + ("  (tests)" if test else ""))
                lines += fmt(it)
                shown += 1
        if rest:
            summary = ", ".join(f"{k} {v}" for k, v in sorted(rest.items(), key=lambda kv: -kv[1]))
            lines += textwrap.wrap(f"... and {sum(rest.values())} more in {len(rest)} modules: {summary} (raise max_rows "
                                   "to list them)", 110, initial_indent="  ", subsequent_indent="      ")
        return lines

    rows = []
    for h, level in impls:
        s = db.execute("SELECT * FROM symbols WHERE usr_hash = ?", (h,)).fetchone()
        r_ = rel(db, s["def_path_hash"]) if s["def_path_hash"] else ""
        rows.append(dict(h=h, level=level, module=s["module"], rel=r_, line=s["def_line"], test=usage.is_test(r_, s["module"])))
    prod = sum(1 for r in rows if not r["test"])
    out += ["", f"implementations and overrides: {len(rows)} in the compiled build ({prod} production, "
                f"{len(rows) - prod} tests)" + (":" if rows else "")]
    out += listing(rows, lambda r: [f"    {short(r['h'])}  {r['rel']}:{r['line']}"
                                    + (f"  (overrides an override, level {r['level']})" if r["level"] > 1 else "")])

    def site(c, verb):
        what = "the requirement" if c["target"] == uh and type_row is not None and type_row["kind"] == "Protocol" \
            else "it" if c["target"] == uh else short(c["target"])
        code = source_line(root, c["rel"], c["line"], lines_cache)
        return [f"    {c['rel']}:{c['line']}  in {short(c['caller'])}  {verb} {what}"] + ([f"        {code}"] if code else [])

    prod = sum(1 for c in calls if not c["test"])
    out += ["", f"call sites of it or of an implementation: {len(calls)} in the compiled build ({prod} production, "
                f"{len(calls) - prod} tests)" + (":" if calls else "")]
    out += listing(calls, lambda c: site(c, "calls"))
    if refs:
        out += ["", f"referenced without a call ({len(refs)}: passed as a value, a key path or a selector):"]
        out += listing(refs, lambda c: site(c, "references"))
    if helpers:
        out += ["", f"other members of extensions of {type_row['name']} (helpers that may forward to it or repeat its "
                    f"parameters):"]
        for h in helpers:
            where = f"{h['rel']}:{h['line']}" if h["rel"] else "no definition site"
            does = f"calls it at line{'s' * (len(h['calls']) > 1)} {', '.join(map(str, h['calls']))}" if h["calls"] \
                else "no call to it"
            out.append(f"  {h['name']}  {h['kind']}  {where}  {does}")
    label = type_row["name"] if type_row is not None else impact.bare(sym["name"])
    if text:
        out += ["", f"tracked files the build never compiled that name {label} (text matches, not resolved; read "
                    f"each line):"]
        for path, hits in sorted(text.items()):
            out.append(f"  {path}" + ("  (test)" if usage.is_test(path) else ""))
            for ln, what, code in hits[:12]:
                out.append(f"    {ln:>5}  {what:<24} {code}")
            if len(hits) > 12:
                out.append(f"    ... {len(hits) - 12} more lines in this file")
    else:
        out += ["", f"no tracked file outside the compiled build names {label}"]
    if docs:
        out.append(f"  docs naming {label}: " + ", ".join(docs[:6]) + (" ..." if len(docs) > 6 else ""))
    cov_t, cov_c = m.get("coverage_tracked"), m.get("coverage_covered")
    if cov_t:
        out.append(f"\nthe build indexed {int(cov_c):,} of {int(cov_t):,} tracked sources; the text search covers the "
                   "rest, but a call built from a string at runtime is invisible to both")
    spent = 0
    for i, line in enumerate(out):
        if spent + len(line) + 1 > a.max_bytes:
            print(f"... {a.max_bytes:,} byte budget reached after {i} of {len(out)} lines; raise max_bytes or max_rows")
            break
        print(line)
        spent += len(line) + 1


def cmd_dead(a):
    db = connect(a.db)
    if getattr(a, "test_only", False):
        kinds = tuple(a.kind.split(",")) if a.kind else None
        rows = deadcode.test_only(db, kinds=kinds, module=a.module, limit=a.limit, offset=a.offset)
        if a.json:
            print(json.dumps([dict(r) for r in rows], indent=1))
            return
        print(f"{len(rows)} production symbols that only test code reaches"
              + (f" in {a.module}" if a.module else "") + (f" (offset {a.offset})" if a.offset else ""))
        print("no production caller in the compiled build; a caller in an uncompiled file is invisible, so verify before deleting")
        mod = None
        for r in rows:
            if r["module"] != mod:
                mod = r["module"]
                print(f"\n{mod}")
            print(f"  {r['name']:<50} {r['kind']:<16} {(r['rel'] or '').split('/')[-1]}:{r['def_line']}"
                  f"  used by {r['test_modules']} test module{'s' if r['test_modules'] != 1 else ''}")
        return
    m = meta(db)
    root = m.get("repo_root", "")
    total, rows = deadcode.candidates(db, kinds=a.kind.split(",") if a.kind else None,
                                      module=a.module, include_tests=a.include_tests,
                                      limit=a.limit, offset=a.offset, lang=a.lang,
                                      include_vendor=a.include_vendor)
    out = []
    for r in rows:
        item = {"name": r["name"], "kind": r["kind"], "module": r["module"],
                "file": r["rel"], "line": r["def_line"], "usr": r["usr"]}
        if a.verify and root:
            others, same_file = deadcode.mentions(root, r["name"], r["rel"] or "")
            item["mentions"] = others[:5]
            item["mention_count"] = len(others)
            item["same_file_mentions"] = same_file
            if others:
                continue
        out.append(item)
    if a.json:
        print(json.dumps({"total_candidates": total, "returned": len(out),
                          "verified": bool(a.verify), "candidates": out}, indent=2))
        return
    cov = ""
    print(f"dead-code candidates: {total}" + (f", {len(out)} survive the text check" if a.verify else ""))
    print("only as good as the index: a symbol used solely from files the build never")
    print("compiled looks unreachable here. Check `idxg status` coverage first.\n")
    group = None
    for item in out:
        g = f"{item['module'] or '?'} ({item['file'] or 'external'})"
        if g != group:
            group = g
            print(f"\n{group}:")
        same = item.get("same_file_mentions") or 0
        hint = f"  ({same} same-file mention{'s' if same != 1 else ''}, check overloads)" if same else ""
        print(f"  {item['name']}  {item['kind']}:{item['line']}{hint}")
    if not a.verify:
        print("\nadd --verify to drop candidates whose name appears in any other file")


def cmd_schema(a):
    db = connect(a.db)
    for r in db.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        print(f"-- {r['type']}: {r['name']}\n{r['sql']};\n")
    print("-- edge kinds:", ", ".join(EDGE_KINDS))
    print("-- occurrence role bits:", ", ".join(f"{b}={n}" for b, n in ROLE_BITS))
    print(textwrap.dedent("""
    -- edge direction: src --kind--> dst
    --   CALLS       caller -> callee (from index-store calledBy relations)
    --   REFERENCES  enclosing symbol -> referenced symbol (containedBy)
    --   CONTAINS    parent -> child (childOf: type -> member, func -> local type)
    --   INHERITS    subclass/conformer -> base class/protocol (baseOf)
    --   OVERRIDES   override -> overridden requirement
    --   EXTENDS     extension -> extended type (extendedBy)
    --   ACCESSOR_OF getter/setter -> property
    -- every edge row carries the source location of the relation (path_hash, line, col)
    """).strip())
    hist = _history_module()
    hp = hist.history_db_for(db_path(a.db))
    if not os.path.exists(hp):
        print("\n-- history db: not built (idxg history build)")
        return
    h = hist.connect(hp)
    print(f"\n-- history db: {hp}")
    for r in h.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"):
        print(f"-- {r['type']}: {r['name']}\n{r['sql']};\n")
    print(textwrap.dedent("""
    -- commits: one row per first-parent commit of the history branch; pr_* columns come from
    --   GitHub when gh is logged in (pr_body NULL = not fetched, '' = PR not found)
    -- commit_files.path equals files.rel in the graph db: that is the join between the two
    -- commit_files.module comes from the graph's module directory prefixes, NULL when uncompiled
    -- components: module-depth directories; alive=0 means the branch no longer has the directory
    -- module_rank: cross-module CALLS in/out per module, used to order digest areas
    """).strip())
    h.close()



# ---------------------------------------------------------------- project setup
# ---------------------------------------------------------------- history and docs
def _history_module():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import history
    return history


def history_db(a):
    """Read-only connection to the history db that belongs to the graph in scope."""
    hist = _history_module()
    return hist, hist.connect(hist.history_db_for(db_path(getattr(a, "db", None))))


def _history_config(a):
    root = meta(connect(a.db)).get("repo_root") or prj.find_root()
    return root, prj.effective_config(root)


def cmd_history_build(a):
    hist = _history_module()
    graph = db_path(a.db)
    root, cfg = _history_config(a)
    manifest = cfg.get("docs_manifest") or None
    if manifest and not os.path.isabs(os.path.expanduser(manifest)):
        manifest = os.path.join(root, manifest)
    first_parent = cfg.get("history_first_parent", True) and not getattr(a, "all_commits", False)
    path, counts = hist.build(root, graph, branch=a.branch or (cfg.get("history_branch") or None),
                              first_parent=first_parent, full=a.full, since=a.since,
                              docs=a.docs, prs=a.prs and cfg.get("history_prs", True), manifest_path=manifest,
                              release_tags=cfg.get("history_release_tags") or None,
                              release_branches=cfg.get("history_release_branches") or None)
    if getattr(a, "json", False):
        print(json.dumps({"db": path, **counts}))


def _fmt_commit(r, web="", release=True):
    pr = f"  #{r['pr']}" if r["pr"] else ""
    tickets = f"  [{r['tickets']}]" if r["tickets"] else ""
    rel = f"  release {r['release']}" if release and "release" in r.keys() and r["release"] else ""
    return (f"{r['day']}  {r['short']}  {r['subject'][:100]}{pr}{tickets}\n"
            f"            {r['author']}  {r['files']} files  +{r['ins']:,} -{r['del']:,}{rel}")


def _symbol_paths(a):
    """The definition file of a symbol, so its history is the history of that file."""
    db = connect(a.db)
    cands = resolve(db, a.symbol)
    if not cands:
        raise SystemExit(f"no symbol matched {a.symbol!r}")
    r = cands[0]
    if not r["def_path_hash"]:
        raise SystemExit(f"{a.symbol} has no definition site in the index, so no file to follow")
    path = rel(db, r["def_path_hash"])
    db.close()
    return path, r


def cmd_history_log(a):
    hist, hdb = history_db(a)
    paths = list(getattr(a, "paths", None) or [])
    note = ""
    if getattr(a, "symbol", None):
        path, sym = _symbol_paths(a)
        paths.append(path)
        note = (f"following {sym['name']} through its definition file {path}; line-level history "
                f"is not tracked, so unrelated edits to the file appear too\n")
    release = getattr(a, "release", None)
    tags = [t.strip() for t in (release if isinstance(release, list) else (release or "").split(",")) if t.strip()]
    if tags:
        return _history_by_release(a, hist, hdb, tags, paths, note)
    rows = hist.commits_for(hdb, paths=paths or None, module=a.module, component=a.component,
                            author=a.author, since=a.since, until=a.until, query=a.grep, limit=a.limit)
    m = hist.meta(hdb)
    if getattr(a, "json", False):
        out = [dict(r) for r in rows]
        if a.files:
            for o in out:
                o["changed"] = [dict(f) for f in hist.files_of(hdb, o["sha"], 60)]
        if getattr(a, "narrate", False):
            for o in out:
                full = hdb.execute("SELECT * FROM commits WHERE sha = ?", (o["sha"],)).fetchone()
                o["narrative"] = hist.narrate_commit(full, hist.files_of(hdb, o["sha"]), m.get("remote_web", ""), hdb)
        print(json.dumps({"branch": m.get("branch"), "head": m.get("head_sha"), "commits": out}, indent=1))
        return
    scope = [x for x in paths + [a.module, a.component] if x]
    print(f"{m.get('branch')} @ {m.get('head_sha', '')[:11]}, built {m.get('built_at')}"
          + (f"  (scope: {', '.join(scope)})" if scope else ""))
    if note:
        print(note.rstrip())
    if not rows:
        print("no commits matched. Module filters only see files the compiled index attributes; "
              "try a path or directory instead.")
        return
    spent, shown = 0, 0
    budget = getattr(a, "max_bytes", 12000)
    web = m.get("remote_web", "")
    for r in rows:
        if getattr(a, "narrate", False):
            full = hdb.execute("SELECT * FROM commits WHERE sha = ?", (r["sha"],)).fetchone()
            block = [textwrap.fill(hist.narrate_commit(full, hist.files_of(hdb, r["sha"]), web, hdb), 100)
                     + (f"\n  {web}/pull/{r['pr']}" if web and r["pr"] else "")]
        else:
            block = [_fmt_commit(r)]
        if a.files:
            for f in hist.files_of(hdb, r["sha"], 8):
                mod = f"  [{f['module']}]" if f["module"] else ""
                block.append(f"              +{f['ins']:<5} -{f['del']:<5} {f['path']}{mod}")
            if r["files"] > 8:
                block.append(f"              ... {r['files'] - 8} more files (idxg history show {r['short']})")
        text = "\n".join(block)
        if spent + len(text) > budget and shown:
            print(f"... {len(rows) - shown} more commits not printed: {budget:,} byte budget reached "
                  f"(raise --max-bytes, or narrow with --since, --module or a path)")
            break
        print(text)
        spent += len(text) + 1
        shown += 1
    if shown == len(rows) == a.limit:
        print(f"(showing {a.limit}; raise --limit or narrow with --since, --module or a path)")


def _history_by_release(a, hist, hdb, tags, paths, note):
    """What first shipped in each release, oldest release first, each under the point where it
    left the branch, and each commit with what and why from its PR description."""
    m = hist.meta(hdb)
    branch, web = m.get("branch"), m.get("remote_web", "")
    windows = {t: hist.release_window(hdb, t) for t in tags}
    missing = [t for t, w in windows.items() if not w]
    filters = dict(paths=paths or None, module=a.module, component=a.component, author=a.author,
                   since=a.since, until=a.until, query=a.grep, limit=a.limit)
    groups = []
    for w in sorted((w for w in windows.values() if w), key=lambda w: (w["cut"], w["tag"])):
        every = hist.commits_for(hdb, release=w["tag"], **dict(filters, limit=-1))
        earlier = [r for r in hist.commits_for(hdb, release=w["tag"], release_contains=True,
                                               **dict(filters, limit=-1))
                   if r["hotfix"] and r["hotfix"] != w["tag"]]
        groups.append((dict(w, total=len(every), picked_here=sum(1 for r in every if r["hotfix"] == w["tag"])),
                       list(reversed(every[:a.limit])), earlier))
    if getattr(a, "json", False):
        print(json.dumps({"branch": branch, "head": m.get("head_sha"), "missing": missing, "releases": [
            dict(w, commits=[dict(r, why=hist.commit_why(hdb, r["sha"])) for r in rows],
                 shipped_earlier=[dict(r) for r in earlier]) for w, rows, earlier in groups]}, indent=1))
        return
    scope = [x for x in paths + [a.module, a.component] if x]
    print(f"{branch} @ {m.get('head_sha', '')[:11]}, built {m.get('built_at')}"
          + (f"  (scope: {', '.join(scope)})" if scope else ""))
    if note:
        print(note.rstrip())
    print(f"a commit first ships in the first release cut from {branch} after it was merged, or in an earlier "
          "release whose branch received it as a cherry-pick; cut times are UTC")
    if missing:
        print(f"no release named {', '.join(missing)}; get_releases (idxg history releases) lists them")
    spent, budget, done = 0, getattr(a, "max_bytes", 12000), False
    for w, rows, earlier in groups:
        if w["source"] == "branch":
            state = f"release branch, not tagged, head {(w['tag_sha'] or '')[:11]}"
        else:
            state = f"tagged {w['tag_date']}, pointing at {(w['tag_sha'] or '')[:11]}"
        head = [f"\n{w['tag']}: cut from {branch} at {(w['base_sha'] or '')[:11]} on {w['cut']}, {state}",
                f"  window: merged after {w['previous']} was cut ({w['previous_cut']}) up to this cut"
                if w["previous"] else "  window: everything merged up to this cut (no earlier release known)"]
        head.append(f"  {w['total']} commit{'s' * (w['total'] != 1)} in scope first shipped here"
                    + (f", {w['picked_here']} of them cherry-picked onto its branch after the cut" if w["picked_here"] else "")
                    + (f"; {len(earlier)} more from the window shipped earlier as picks: "
                       + ", ".join(f"#{r['pr'] or r['short']} in {r['hotfix']}" for r in earlier) if earlier else ""))
        if not rows:
            head.append("  (none)")
        blocks = ["\n".join(head)]
        for r in rows:
            if getattr(a, "narrate", False):
                full = hdb.execute("SELECT * FROM commits WHERE sha = ?", (r["sha"],)).fetchone()
                block = [textwrap.fill(hist.narrate_commit(full, hist.files_of(hdb, r["sha"]), web, hdb), 100,
                                       initial_indent="  ", subsequent_indent="  ")]
            else:
                block = ["  " + _fmt_commit(r, release=False).replace("\n", "\n  ")]
                if r["hotfix"] == w["tag"]:
                    block.append(f"              {hist.shipped(hdb, r['release'], r['hotfix'])}")
                why = hist.commit_why(hdb, r["sha"])
                block.append(textwrap.fill(f"why: {why}" if why else "why: no PR description or message body "
                                           "(idxg history build fetches PR descriptions when gh is logged in)",
                                           100, initial_indent="              ", subsequent_indent="              "))
            if a.files:
                for f in hist.files_of(hdb, r["sha"], 8):
                    mod = f"  [{f['module']}]" if f["module"] else ""
                    block.append(f"              +{f['ins']:<5} -{f['del']:<5} {f['path']}{mod}")
            blocks.append("\n".join(block))
        for text in blocks:
            if spent + len(text) > budget and spent:
                print(f"... {budget:,} byte budget reached (raise --max-bytes, or narrow with --module or a path)")
                done = True
                break
            print(text)
            spent += len(text) + 1
        if done:
            break
        if w["total"] > len(rows):
            print(f"  (showing the newest {len(rows)} of {w['total']} for {w['tag']}; raise --limit or narrow the scope)")


def cmd_history_show(a):
    hist, hdb = history_db(a)
    ident = a.sha.strip()
    if ident.startswith("#") and ident[1:].isdigit():
        r = hdb.execute("SELECT * FROM commits WHERE pr = ? ORDER BY committed DESC", (int(ident[1:]),)).fetchone()
    else:
        r = hdb.execute("SELECT * FROM commits WHERE sha = ? OR sha GLOB ? ORDER BY committed DESC",
                        (ident, ident + "*")).fetchone()
    if not r:
        raise SystemExit(f"no commit {ident} in the history of {hist.meta(hdb).get('branch')}")
    files = hist.files_of(hdb, r["sha"], a.max_files)
    web = hist.meta(hdb).get("remote_web", "")
    if getattr(a, "json", False):
        d = dict(r)
        d["changed"] = [dict(f) for f in files]
        print(json.dumps(d, indent=1))
        return
    print(f"{r['sha']}  {r['day']}  {r['author']} <{r['email']}>")
    print(f"{r['subject']}")
    if r["pr"]:
        print(f"pr: #{r['pr']}" + (f"  {web}/pull/{r['pr']}" if web else ""))
    if r["tickets"]:
        print(f"tickets: {r['tickets']}")
    if "release" in r.keys():
        if r["release"]:
            rel = hdb.execute("SELECT tag_date, base_day FROM releases WHERE tag = ?", (r["release"],)).fetchone()
            print(f"release: first shipped in {r['release']}" + (f" (branched from {hist.meta(hdb).get('branch')} on {rel['base_day']}, tagged {rel['tag_date']})" if rel else ""))
        else:
            print("release: not in any tagged release yet")
    print(f"{r['files']} files, +{r['ins']:,} -{r['del']:,}, {r['parents']} parent(s)")
    print("\n" + textwrap.fill(hist.narrate_commit(r, hist.files_of(hdb, r["sha"], 2000), web, hdb), 100))
    if r["pr_body"]:
        print(f"\npull request description" + (f" ({r['pr_labels']})" if r["pr_labels"] else ""))
        print(textwrap.indent(r["pr_body"][:a.max_body], "  "))
        if len(r["pr_body"]) > a.max_body:
            print(f"  ... truncated at {a.max_body:,} bytes (raise --max-body)")
    if r["body"]:
        print("\ncommit body")
        print(textwrap.indent(r["body"], "  "))
    print("\nfiles")
    for f in files:
        mod = f"  [{f['module']}]" if f["module"] else ""
        old = f"  (was {f['old_path']})" if f["old_path"] else ""
        print(f"  +{f['ins']:<5} -{f['del']:<5} {f['path']}{mod}{old}")
    if r["files"] > len(files):
        print(f"  ... {r['files'] - len(files)} more (raise --max-files)")


def cmd_history_churn(a):
    hist, hdb = history_db(a)
    import datetime as _dt
    since = a.since or (_dt.date.today() - _dt.timedelta(days=365)).isoformat()
    rows = hist.churn(hdb, since=since, by=a.by, limit=a.limit, ext=a.ext)
    if getattr(a, "json", False):
        print(json.dumps({"since": since, "by": a.by, "rows": [dict(r) for r in rows]}, indent=1))
        return
    print(f"change since {since}, by {a.by}" + (f", {a.ext} files only" if a.ext else ""))
    if a.by == "module":
        print("modules come from the compiled index; a file the build never compiled is counted nowhere here")
    w = max([len(str(r["key"])) for r in rows] + [10])
    w = min(w, 60)
    print(f"  {'':<{w}} {'commits':>8} {'+lines':>9} {'-lines':>9} {'authors':>8}  last")
    for r in rows:
        key = str(r["key"])
        key = key if len(key) <= w else "..." + key[-(w - 3):]
        print(f"  {key:<{w}} {r['commits']:>8,} {r['ins'] or 0:>9,} {r['del'] or 0:>9,} {r['authors']:>8}  {r['last']}")


def cmd_history_timeline(a):
    hist, hdb = history_db(a)
    m = hist.meta(hdb)
    granularity, all_eras = hist.eras(hdb, granularity=a.granularity)
    shown = all_eras[-a.periods:] if a.periods else all_eras
    paras = hist.narrate(m, granularity, shown, all_eras)
    overview = hist.overview_text(hdb)
    if getattr(a, "json", False):
        print(json.dumps({"overview": overview, "granularity": granularity,
                          "periods": [{**e, "text": p["text"]} for e, p in zip(shown, paras)]}, indent=1))
        return
    print(textwrap.fill(overview, 100))
    print(f"\none paragraph per {granularity}, {len(shown)} of {len(all_eras)} shown"
          + ("" if len(shown) == len(all_eras) else " (--periods 0 for all)"))
    for p in paras:
        print()
        print(textwrap.fill(p["text"], 100))


def _resolve_window(a, hist, hdb):
    m = hist.meta(hdb)
    if getattr(a, "week", None):
        return hist.week_bounds(a.week)
    if getattr(a, "since", None) or getattr(a, "until", None):
        return a.since or m.get("first_day"), a.until or m.get("last_day")
    return hist.week_bounds(hist.iso_week(m.get("last_day")))


def cmd_history_digest(a):
    hist, hdb = history_db(a)
    m = hist.meta(hdb)
    if getattr(a, "list", False):
        for w, n in hist.weeks(hdb, limit=a.limit):
            s, e = hist.week_bounds(w)
            print(f"  {w}  {s} to {e}  {n:>4} commits")
        return
    if getattr(a, "release", None):
        data = hist.release_digest(hdb, a.release, m.get("remote_web", ""))
        if not data:
            raise SystemExit(f"no commits first shipped in release {a.release!r}; idxg history releases lists the tags")
        start, end = data["window"]["start"], data["window"]["end"]
    else:
        start, end = _resolve_window(a, hist, hdb)
        data = hist.week_digest(hdb, start, end, m.get("remote_web", ""))
    if getattr(a, "json", False):
        print(json.dumps(data, indent=1, ensure_ascii=False))
        return
    html_arg = getattr(a, "html", None)
    if html_arg:
        graph = db_path(a.db)
        out = (os.path.expanduser(html_arg) if html_arg != "auto" else
               os.path.join(os.path.dirname(graph), os.path.basename(graph).replace(
                   ".db", f"-digest-{getattr(a, 'release', None) or start}.html")))
        with open(out, "w") as f:
            f.write(hist.render_digest(data))
        print(f"wrote {out}  ({os.path.getsize(out) / 1024:.0f} KB, {data['window']['commits']} changes)")
        if getattr(a, "open", False):
            subprocess.run(["open", out])
        return
    text = hist.digest_text(data)
    budget = getattr(a, "max_bytes", 0)
    if budget and len(text) > budget:
        print(text[:budget])
        print(f"\n... truncated at {budget:,} of {len(text):,} bytes (raise --max-bytes, or --json for the data)")
    else:
        print(text)


REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True)


def release_tags():
    """Release tags in this checkout, newest first."""
    tags = [t for t in _git("tag", "-l", "v*").stdout.split() if prj._version_tuple(t)]
    return sorted(tags, key=prj._version_tuple, reverse=True)


def previous_release():
    """The newest release tag older than the running version, or None."""
    older = [t for t in release_tags() if prj._version_tuple(t) < prj._version_tuple(VERSION)]
    return older[0] if older else None


def cmd_update(a):
    """Check out a release tag in the checkout this CLI runs from, then reinstall links."""
    tag, url = prj.update_available(VERSION, force=True)
    latest = prj.latest_release(force=False).get("tag") or "unknown"
    print(f"installed: {VERSION}   latest release: {latest}")
    if a.check:
        if tag:
            print(f"update available: {tag}  {url}\nrun `idxg update` to install it")
        else:
            print("up to date")
        return
    target = ("v" + a.to.lstrip("v")) if a.to else (tag or (latest if a.force and latest != "unknown" else None))
    if not target:
        print("up to date; pass --force to reinstall the latest release, or --to <version> for another one")
        return
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        raise SystemExit(f"{REPO_DIR} is not a git checkout; update it the way you installed it")
    if _git("status", "--porcelain").stdout.strip():
        raise SystemExit(f"{REPO_DIR} has uncommitted changes; commit or discard them, then run idxg update")
    r = _git("fetch", "--quiet", "--tags", "--force", "origin")
    if r.returncode != 0:
        raise SystemExit(f"git fetch failed: {(r.stderr or r.stdout).strip()[:400]}")
    if target not in release_tags():
        known = ", ".join(release_tags()[:5]) or "none"
        raise SystemExit(f"no release {target}; the newest ones are {known}")
    # A detached checkout of the tag means a later update never depends on the branch state,
    # and a commit pushed to main without a release never reaches anyone.
    before = _git("rev-parse", "HEAD").stdout.strip()
    r = _git("checkout", "--quiet", "--detach", target)
    if r.returncode != 0:
        raise SystemExit(f"git checkout {target} failed: {(r.stderr or r.stdout).strip()[:400]}")
    print(f"checked out {target} in {REPO_DIR}")
    # Try the new code before relinking anything, and step back when it does not start, because
    # a broken idxg could not run another update.
    smoke = os.path.join(REPO_DIR, "tests", "smoke.py")
    check = [smoke] if os.path.exists(smoke) else [os.path.join(REPO_DIR, "src", "idxg.py"), "--version"]
    r = subprocess.run([sys.executable, *check], capture_output=True, text=True)
    if r.returncode != 0:
        _git("checkout", "--quiet", before)
        raise SystemExit(f"{target} does not start on {sys.executable}, so the checkout is back where it "
                         f"was ({VERSION}):\n{(r.stdout + r.stderr).strip()[-600:]}")
    now = subprocess.run([sys.executable, os.path.join(REPO_DIR, "src", "idxg.py"), "--version"],
                         capture_output=True, text=True)
    install = os.path.join(REPO_DIR, "install.sh")
    if os.path.exists(install):
        r = subprocess.run(["sh", install], capture_output=True, text=True)
        print(r.stdout.strip().splitlines()[0] if r.stdout.strip() else "install.sh ran")
    print(f"now: {now.stdout.strip()}")
    print("restart Claude Code so the MCP server picks up the new code; graphs and history need no rebuild")


def cmd_crash(a):
    """Map a stack trace onto the graph and the history: symbol, callers and recent commits per frame."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import crash
    text = a.text if getattr(a, "text", None) else (sys.stdin.read() if a.trace == "-" else open(a.trace).read())
    db = connect(a.db)
    m = meta(db)
    root = m.get("repo_root", "")
    hist = _history_module()
    hp = hist.history_db_for(db_path(a.db))
    hdb = hist.connect(hp) if os.path.exists(hp) else None
    branch = (hist.meta(hdb).get("branch") if hdb else None) or "HEAD"
    since_day, since_after = crash.since_point(root, a.since, branch)
    if a.since and not (since_day or since_after):
        raise SystemExit(f"--since {a.since!r} is neither a date nor a git ref in {root}")
    since = (f"{a.since} (commits it does not contain)" if since_after else since_day) if a.since else None
    frames = crash.parse(text)
    resolved, skipped_system, unresolved, seen = [], 0, [], set()
    for fr in frames:
        if crash.is_system(fr):
            skipped_system += 1
            continue
        sym, how = crash.resolve_frame(db, fr)
        if not sym:
            unresolved.append(fr)
            continue
        # Recursion and Sentry's compiler-generated twins repeat a frame; once is enough.
        if sym["usr_hash"] in seen:
            continue
        seen.add(sym["usr_hash"])
        entry = {"frame": fr["index"], "raw": fr["raw"], "symbol": sym["name"], "kind": sym["kind"],
                 "module": sym["module"], "file": sym["rel"], "def_line": sym["def_line"],
                 "trace_line": fr["line"], "resolved_by": how,
                 "callers": [dict(r) for r in crash.callers(db, sym["usr_hash"], a.callers)], "commits": []}
        if hdb and sym["rel"]:
            rows = hist.commits_for(hdb, paths=[sym["rel"]], since=since_day, after=since_after, limit=a.commits)
            entry["commits"] = [dict(r) for r in rows]
            for c in entry["commits"]:
                c["shipped"] = hist.shipped(hdb, c["release"], c["hotfix"])
                body = hdb.execute("SELECT pr_body FROM commits WHERE sha = ?", (c["sha"],)).fetchone() \
                    if "pr_body" in {r[1] for r in hdb.execute("PRAGMA table_info(commits)")} else None
                c["pr_summary"] = hist.pr_summary(body[0]) if body else ""
        resolved.append(entry)
        if len(resolved) >= a.frames:
            break
    if getattr(a, "json", False):
        print(json.dumps({"frames_total": len(frames), "system_frames": skipped_system, "since": since,
                          "resolved": resolved, "unresolved": [f["raw"] for f in unresolved]}, indent=1))
        return
    web = hist.meta(hdb).get("remote_web", "") if hdb else ""
    print(f"{len(frames)} frames parsed, {skipped_system} in system images, {len(resolved)} resolved to this repo"
          + (f", {len(unresolved)} unresolved" if unresolved else "")
          + (f"; commits since {since}" if since else "; commits: most recent")
          + ("" if hdb else "; no history db (idxg history build)"))
    if not resolved:
        print("nothing in this trace resolves to indexed code. Check idxg coverage for the files named, "
              "and that the trace is symbolicated.")
        return
    spent, budget, shown = 0, a.max_bytes, set()
    for e in resolved:
        block = [f"\n#{e['frame']}  {e['symbol']}  {e['kind']}  {e['module'] or ''}",
                 f"    {e['file']}:{e['def_line']}" + (f"  (trace line {e['trace_line']})" if e["trace_line"] else "")
                 + f"  resolved by {e['resolved_by']}"]
        if e["callers"]:
            block.append(f"    callers ({len(e['callers'])} shown):")
            for c in e["callers"]:
                site = f"{(c['rel'] or '').split('/')[-1]}:{c['line']}" if c["rel"] else ""
                block.append(f"      {c['name']}  {c['module'] or ''}  {site}" + (f"  (x{c['n']})" if c["n"] > 1 else ""))
        else:
            block.append("    callers: none in the indexed build (entry point, dynamic dispatch, or uncompiled caller)")
        if hdb:
            if e["commits"]:
                block.append(f"    commits touching {e['file'].split('/')[-1]}" + (f" since {since}" if since else "") + ":")
                for c in e["commits"]:
                    pr = f"  #{c['pr']}" if c["pr"] else ""
                    subject = hist.PR_RE.sub("", c["subject"]).strip()[:90]
                    block.append(f"      {c['day']}  {c['short']}  {subject}{pr}  ({c['author']})")
                    # A commit that touched several frames' files is described once.
                    if c["sha"] in shown:
                        block.append("          (described above)")
                        continue
                    shown.add(c["sha"])
                    block.append(f"          shipped: {c['shipped'] or 'not in any release or release branch yet'}")
                    if c["pr_summary"]:
                        block.append(f"          PR: {c['pr_summary']}")
            else:
                block.append(f"    commits touching {e['file'].split('/')[-1]}" + (f" since {since}" if since else "") + ": none")
        text = "\n".join(block)
        if spent + len(text) > budget:
            print(f"\n... {budget:,} byte budget reached; raise --max-bytes or lower --frames")
            break
        print(text)
        spent += len(text)
    if unresolved and spent < budget:
        print(f"\nunresolved ({len(unresolved)}):")
        for f in unresolved[:8]:
            print(f"  {f['raw'][:110]}")
    hot = [e for e in resolved if e["commits"]]
    if hdb and hot:
        latest = max(hot, key=lambda e: e["commits"][0]["day"])
        c = latest["commits"][0]
        print(f"\nmost recently changed frame: {latest['symbol']} in {latest['file'].split('/')[-1]}, "
              f"{c['day']} {c['short']} {hist.PR_RE.sub('', c['subject']).strip()[:80]}"
              + (f"  {web}/pull/{c['pr']}" if web and c["pr"] else ""))
    print("this is what changed near the crash, not why it crashed; read the callers and the PR bodies "
          "(idxg history show) before deciding.")


def cmd_history_releases(a):
    hist, hdb = history_db(a)
    rows = hist.releases(hdb, limit=a.limit)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    m = hist.meta(hdb)
    if not rows:
        print("no version tags found; set idxg config history_release_tags=<regex> if yours look different")
        return
    print(f"releases by the point where their branch left {m.get('branch')}; commits counts what first shipped in each,"
          f" picked what a release received after its cut (a hotfix)")
    print(f"  {'release':<14} {'branched':<11} {'tagged':<22} {'commits':>8} {'picked':>7}")
    for r in rows:
        tagged = r["tag_date"] or "" if r["source"] == "tag" else "not tagged (branch)"
        print(f"  {r['tag']:<14} {r['base_day'] or '':<11} {tagged:<22} {r['commits']:>8,} {r['picks'] or '':>7}")
    print("  idxg history log --release <tag> lists what first shipped in one")


def cmd_history_vault(a):
    hist = _history_module()
    graph = db_path(a.db)
    hdb_path = hist.history_db_for(graph)
    root, cfg = _history_config(a)
    out = a.out or cfg.get("history_vault") or os.path.join(
        os.path.dirname(graph), os.path.basename(graph).replace(".db", "-vault"))
    written, superseded = hist.export_vault(hdb_path, out, docs=a.docs, history=a.history,
                                            modules_min_commits=a.min_commits, dry=a.dry_run)
    for w in written[:40]:
        print(f"  {w}")
    if len(written) > 40:
        print(f"  ... {len(written) - 40} more")
    for new, old in superseded[:20]:
        print(f"  {new} supersedes {old}")
    print(f"\nClippings/ follows the knowledge-vault contract: files are never rewritten, a changed source "
          f"becomes a date-suffixed clipping. Point a vault's compile step at {out}/Clippings, or pass "
          f"--out <vault> to write into the vault directly.")


def cmd_docs_list(a):
    hist, hdb = history_db(a)
    rows = hist.docs_list(hdb, module=a.module, kind=a.kind, path_glob=a.path, limit=a.limit)
    m = hist.meta(hdb)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    total = int(m.get("count_docs") or 0)
    print(f"{len(rows)} of {total} repo docs (branch {m.get('branch')}, synced {m.get('built_at')})")
    kind = None
    for r in rows:
        if r["kind"] != kind:
            kind = r["kind"]
            print(f"\n{kind}")
        mod = f"  [{r['module']}]" if r["module"] else ""
        refreshed = f"  generated sections as of {r['mechanical_refreshed']}" if r["mechanical_refreshed"] else ""
        print(f"  {r['published'] or '----------'}  {r['path']}{mod}  {r['bytes'] // 1024}k{refreshed}")
    if len(rows) == a.limit:
        print(f"\n(showing {a.limit}; raise --limit or filter with --module, --kind, --path)")
    print("\n`published` is the file's last commit date on the history branch, not the date its content is true.")


def cmd_docs_search(a):
    hist, hdb = history_db(a)
    rows = hist.docs_search(hdb, a.query, limit=a.limit)
    if getattr(a, "json", False):
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    if not rows:
        print(f"no doc matched {a.query!r}. Docs are the repo's tracked markdown; try idxg docs list.")
        return
    for r in rows:
        mod = f"  [{r['module']}]" if r["module"] else ""
        print(f"{r['path']}{mod}  ({r['kind']}, {r['published'] or 'undated'})")
        print("    " + " ".join(r["snip"].split())[:220])


def cmd_docs_show(a):
    hist, hdb = history_db(a)
    r = hist.doc_get(hdb, a.path)
    if isinstance(r, list):
        if not r:
            raise SystemExit(f"no doc at {a.path!r}; idxg docs search finds one by content")
        print(f"{a.path!r} matches several docs:")
        for c in r:
            print(f"  {c['path']}")
        return
    body = r["content"]
    print(f"{r['path']}  ({r['kind']}, last commit {r['published'] or 'unknown'}"
          + (f", generated sections as of {r['mechanical_refreshed']}" if r["mechanical_refreshed"] else "")
          + (f", module {r['module']}" if r["module"] else "") + f", {r['bytes']:,} bytes)\n")
    if len(body) > a.max_bytes:
        print(body[:a.max_bytes])
        print(f"\n... truncated at {a.max_bytes:,} of {len(body):,} bytes (raise --max-bytes)")
    else:
        print(body)


def cmd_history(a):
    a.hfn(a)


def cmd_docs(a):
    a.hfn(a)


CLAUDE_START = "<!-- codebase-brain:start -->"
CLAUDE_END = "<!-- codebase-brain:end -->"
# Markers and label the tool wrote before it was renamed; init and deinit still recognise them.
LEGACY_CLAUDE_START = "<!-- ios-codebase-indexer:start -->"
LEGACY_CLAUDE_END = "<!-- ios-codebase-indexer:end -->"
LAUNCH_LABEL = "com.codebase-brain.autoindex"
LEGACY_LAUNCH_LABEL = "com.ios-codebase-indexer.autoindex"
# The per-project skill is named after the project so it never collides with the global
# skill installed under ~/.claude/skills/codebase-brain.
GLOBAL_SKILL = "codebase-brain"
LEGACY_PROJECT_SKILLS = ("codebase-index", "codebase-brain", "project-brain")
# Every skill this tool generates carries this phrase in its frontmatter description, which
# is how init and deinit recognise one written under a name the project no longer has.
SKILL_SIGNATURE = "as indexed by codebase-brain"
NOTES_MARKER = "## Project notes"


def project_skill_name(project):
    """Skill directory for a project: its slug plus -brain, never the global skill's name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (project or "").lower()).strip("-") or "project"
    slug = re.sub(r"-?brain$", "", slug).strip("-") or "project"
    skill = f"{slug}-brain"
    return skill if skill != GLOBAL_SKILL else f"{slug}-project-brain"


def _generated_skill_dirs(root, keep=None):
    """Skill directories this tool wrote: the legacy names, plus anything carrying the
    signature, minus `keep`. A renamed project leaves its old skill behind otherwise."""
    base = os.path.join(root, ".claude", "skills")
    found = []
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if name == keep:
            continue
        d = os.path.join(base, name)
        skill = os.path.join(d, "SKILL.md")
        if not os.path.exists(skill):
            continue
        if name in LEGACY_PROJECT_SKILLS:
            found.append(d)
            continue
        with open(skill) as f:
            if SKILL_SIGNATURE in f.read():
                found.append(d)
    return found


def _markers_in(text):
    for start, end in ((CLAUDE_START, CLAUDE_END), (LEGACY_CLAUDE_START, LEGACY_CLAUDE_END)):
        if start in text and end in text:
            return start, end
    return None, None


def _remove_skill_dir(path, keep_notes_into=None):
    """Delete a project skill directory the tool wrote under an older name, carrying its
    Project notes section over to the new skill file so nothing the user wrote is lost."""
    skill = os.path.join(path, "SKILL.md")
    if not os.path.exists(skill):
        return
    if keep_notes_into:
        with open(skill) as f:
            old = f.read()
        if NOTES_MARKER in old:
            notes = old[old.index(NOTES_MARKER) + len(NOTES_MARKER):].lstrip("\n")
            target = os.path.join(keep_notes_into, "SKILL.md")
            if notes.strip() and not os.path.exists(target):
                with open(target, "w") as f:
                    f.write(NOTES_MARKER + "\n\n" + notes)
    shutil.rmtree(path, ignore_errors=True)


def project_stats(db_file):
    db = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    m = meta(db)
    n = {t: db.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
         for t in ("symbols", "edges", "occurrences", "files")}
    mods = [r["module"] for r in db.execute(
        """SELECT module FROM symbols WHERE in_repo=1 AND module IS NOT NULL AND module != ''
           GROUP BY module ORDER BY COUNT(*) DESC LIMIT 12""")]
    langs = {r["lang"]: r["c"] for r in db.execute(
        "SELECT lang, COUNT(*) c FROM symbols WHERE in_repo=1 GROUP BY lang")}
    db.close()
    return m, n, mods, langs


def install_project_skill(root, db_file):
    m, n, mods, langs = project_stats(db_file)
    name = m.get("project") or os.path.basename(root)
    skill_name = project_skill_name(name)
    d = os.path.join(root, ".claude", "skills", skill_name)
    os.makedirs(d, exist_ok=True)
    for old_dir in _generated_skill_dirs(root, keep=skill_name):
        _remove_skill_dir(old_dir, keep_notes_into=d)
    tracked, covered = int(m.get("coverage_tracked") or 0), int(m.get("coverage_covered") or 0)
    coverage = (f"{100 * covered / tracked:.0f}% of tracked source files ({covered:,} of {tracked:,}) were compiled "
                f"into the graph; symbols that live only in the rest cannot be found or traced here."
                if tracked else "coverage unknown; run `idxg status`.")
    hist = _history_module()
    hp = hist.history_db_for(db_file)
    history_line = "History is not built yet; `idxg history build` adds it."
    if os.path.exists(hp):
        h = hist.connect(hp)
        hm = hist.meta(h)
        h.close()
        history_line = (f"History covers {int(hm.get('count_commits') or 0):,} commits on `{hm.get('branch')}` from "
                        f"{hm.get('first_day')} to {hm.get('last_day')}, with pull request descriptions, and "
                        f"{int(hm.get('count_docs') or 0)} markdown documents from the repository.")
    lang_line = ", ".join(f"{k} {v:,}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1]) if k)
    body = f"""---
name: {skill_name}
description: Facts about {name}'s code graph, commit history and docs as indexed by codebase-brain (coverage, largest modules, history reach) plus this project's own notes. Use together with the codebase-brain skill for who calls X, who changed X and why, what shipped, crash triage, or what the repo's docs say.
---

# {name} in codebase-brain

The rules, the tool table and the caveats are in the `codebase-brain` skill; this file
holds only what is specific to {name}. Prefer the `codebase-brain` MCP tools when the
server is connected; `idxg` in a shell is the fallback. If `idxg` is missing, install it
from https://github.com/AlucarDWeb/codebase-brain and run `idxg init` here.

## What the graph holds for this project

- {n['symbols']:,} symbols and {n['edges']:,} edges across {n['files']:,} indexed files ({lang_line}),
  as of the last build on this machine; `index_status` gives the current numbers.
- {coverage}
- {history_line}
- Largest modules: {', '.join(mods[:12])}.

Explorer: `idxg open` (overview, module graph, symbols, dead code, history, docs).

{NOTES_MARKER}

Anything below this line is yours: project-specific gotchas, build quirks, which targets
the index actually covers, names the team uses. `idxg init` regenerates everything above
it and leaves this section untouched.
"""
    path = os.path.join(d, "SKILL.md")
    keep = ""
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
        if NOTES_MARKER in old:
            keep = old[old.index(NOTES_MARKER) + len(NOTES_MARKER):].lstrip("\n")
    if keep:
        body = body[:body.index(NOTES_MARKER) + len(NOTES_MARKER)] + "\n\n" + keep
    with open(path, "w") as f:
        f.write(body)
    return path


def install_claude_md(root, db_file, target=None):
    m, n, _, _ = project_stats(db_file)
    name = m.get("project") or os.path.basename(root)
    note = f"""{CLAUDE_START}
## Code graph, history and docs (codebase-brain)

codebase-brain is optional here: some people have it installed and some do not. Check
before using it. It is available when the `codebase-brain` MCP tools are connected
(`describe_module`, `trace_path`, `get_history`, `triage_crash`, `search_docs`, ...) or when
`idxg` runs in a shell. If neither is there, skip this section and work as usual: do not
install it, do not ask about it, and do not mention it.

When it is available, it holds a compiler-accurate code graph, the main branch's commit
history with pull request descriptions, and the repository's own docs. For questions about
who calls what, where something is used, what a change would affect, who changed something
and why, what shipped, or what a crash report points at, ask it before reading or grepping
code. Prefer the MCP tools; `idxg` in a shell is the fallback. The graph is a snapshot of the
last compile and covers only what was compiled, so treat a file with no index records as
unproven rather than unused. Rules and tool table: the `codebase-brain` skill. This
project's facts and notes, when present: `.claude/skills/{project_skill_name(name)}/SKILL.md`.
{CLAUDE_END}"""
    path = target or os.path.join(root, "CLAUDE.md")
    existing = ""
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read()
    start, end = _markers_in(existing)
    if start:
        head = existing[:existing.index(start)]
        tail = existing[existing.index(end) + len(end):]
        new = head + note + tail
    else:
        sep = "" if not existing else ("" if existing.endswith("\n\n") else
                                       "\n" if existing.endswith("\n") else "\n\n")
        new = existing + sep + note + "\n"
    with open(path, "w") as f:
        f.write(new)
    tracked = subprocess.run(["git", "-C", root, "ls-files", "--error-unmatch",
                              os.path.relpath(path, root)],
                             capture_output=True, text=True).returncode == 0
    return path, tracked


def cmd_deinit(a):
    """Undo everything init put in a project, optionally the graph itself."""
    root = os.path.realpath(os.path.expanduser(a.path)) if a.path else prj.find_root()
    reg = prj.load_registry()
    entry = reg.get(root, {})
    removed, kept = [], []

    for skill_dir in _generated_skill_dirs(root):
        skill = os.path.join(skill_dir, "SKILL.md")
        with open(skill) as f:
            had_notes = NOTES_MARKER in f.read()
        if had_notes and not a.force:
            kept.append(f"{skill}  (has a Project notes section; pass --force to delete)")
        else:
            os.remove(skill)
            removed.append(skill)
            d = os.path.dirname(skill)
            for probe in (d, os.path.dirname(d)):
                try:
                    os.rmdir(probe)
                except OSError:
                    break

    # The CLAUDE.md block is usually committed and shared, so one person's deinit must not strip it.
    candidates = {os.path.join(root, "CLAUDE.md"),
                  os.path.join(root, prj.effective_config(root).get("claude_md_path") or "CLAUDE.md")}
    for candidate in candidates if a.claude_md else ():
        if not os.path.exists(candidate):
            continue
        with open(candidate) as f:
            text = f.read()
        start, end = _markers_in(text)
        if start:
            head = text[:text.index(start)]
            tail = text[text.index(end) + len(end):]
            new = (head.rstrip("\n") + "\n") + tail.lstrip("\n")
            with open(candidate, "w") as f:
                f.write(new)
            removed.append(f"{candidate}  (agent note stripped)")

    if a.purge:
        db_file = entry.get("db") or prj.db_for(root)
        stem = db_file[:-3] if db_file.endswith(".db") else db_file
        targets = [db_file, entry.get("html") or stem + "-explorer.html", stem + "-history.db",
                   db_file + ".building", db_file + ".lock"]
        targets += [t + sfx for t in list(targets) for sfx in ("-wal", "-shm")]
        if os.path.isdir(os.path.dirname(db_file)):
            targets += [os.path.join(os.path.dirname(db_file), f) for f in os.listdir(os.path.dirname(db_file))
                        if f.startswith(os.path.basename(stem) + "-digest-") and f.endswith(".html")]
        for path in dict.fromkeys(targets):
            if os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        for d in (db_file + ".building.shards", stem + "-vault"):
            if os.path.isdir(d):
                shutil.rmtree(d)
                removed.append(d + "/")
        external_vault = prj.effective_config(root).get("history_vault")
        if external_vault and os.path.isdir(os.path.expanduser(external_vault)):
            kept.append(f"{external_vault}  (your vault; clippings written there stay)")

    if root in reg:
        del reg[root]
        prj.save_registry(reg)
        removed.append(f"registry entry for {root}")

    print(f"project: {root}")
    if removed:
        print("removed")
        for r in removed:
            print("  " + r)
    if kept:
        print("kept")
        for k in kept:
            print("  " + k)
    if not a.purge and entry.get("db"):
        print(f"\nkept the graph, history and explorer beside {entry['db']} (pass --purge to delete them)")
    print("\nre-index any time with: idxg init")


def cmd_config(a):
    root = prj.find_root()
    scope_root = None if a.scope == "global" else root
    if a.unset:
        for key in a.unset:
            where = prj.unset_config(key, scope_root)
            print(f"unset {key}" + (f" ({where})" if where else " (was not set)"))
    for pair in a.assign or []:
        if "=" not in pair:
            raise SystemExit(f"expected key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        try:
            where, val = prj.set_config(key.strip(), value.strip(), scope_root)
        except ValueError as e:
            raise SystemExit(str(e))
        print(f"set {key.strip()} = {val}  ({where})")
    if a.assign or a.unset:
        print()
    eff = prj.effective_config(root, with_source=True)
    if a.json:
        print(json.dumps({k: {"value": v, "source": src} for k, (v, src) in eff.items()}, indent=2))
        return
    print(f"project: {root}")
    print(f"global:  {prj.CONFIG}")
    w = max(len(k) for k in eff)
    print(f"\n{'key'.ljust(w)}  {'value':<12} {'source':<8} what it does")
    for k, (v, src) in sorted(eff.items()):
        note = prj.CONFIG_HELP.get(k, "")
        if k in prj.GLOBAL_ONLY:
            note += " (global only)"
        print(f"{k.ljust(w)}  {str(v):<12} {src:<8} {note}")
    print("\nset for this project:  idxg config jobs=8 viz_limit=2500")
    print("set globally:          idxg config --global poll_minutes=20")
    print("clear an override:     idxg config --unset viz_limit")


def cmd_init(a):
    root = os.path.realpath(os.path.expanduser(a.path)) if a.path else prj.find_root()
    stores, _ = prj.detect_stores(root)
    print(f"project: {root}")
    if not stores:
        raise SystemExit(
            "no index store found for this project.\n"
            "  SwiftPM/Xcode: open it once in an editor with sourcekit-lsp background indexing,\n"
            "                 or build it so the compiler writes an index store\n"
            "  Bazel:         build with --features=swift.index_while_building, or set up\n"
            "                 sourcekit-bazel-bsp, then run idxg init again")
    for st in stores:
        print(f"  store: {st}")
    db = prj.db_for(root)
    if a.build:
        print("indexing (this can take a couple of minutes on a large repo)")
        build_now(root, jobs=a.jobs, quiet=False, viz=None if a.viz else False)
    elif not os.path.exists(db):
        raise SystemExit("no database yet; run without --no-build")
    entry = prj.load_registry().get(root, {})
    db = entry.get("db", db)
    done = [f"database: {db}"]
    if entry.get("html"):
        done.append(f"explorer: {entry['html']}")
    hist = _history_module()
    if os.path.exists(hist.history_db_for(db)):
        done.append(f"history:  {hist.history_db_for(db)}")
    if a.skill:
        done.append(f"skill:    {install_project_skill(root, db)}")
    if a.claude_md:
        target = a.claude_md_path or (prj.effective_config(root).get("claude_md_path") or None)
        if target and not os.path.isabs(target):
            target = os.path.join(root, target)
        path, tracked = install_claude_md(root, db, target)
        done.append(f"note:     {path}" + ("  (git-tracked, review before committing)" if tracked else ""))
    print("\ninstalled")
    for line in done:
        print("  " + line)
    print("\nnext")
    print("  idxg status            what the index covers")
    print("  idxg history timeline  the project's history in prose")
    print("  idxg docs search <q>   the repo's own docs")
    print("  idxg open              the visual explorer")
    if os.path.exists(plist_path()):
        watched = install_agent(watch=agent_watches(), quiet=True)
        print("\nthe background agent refreshes this project too" +
              (f", and now watches {watched} index store dir(s)" if watched else
               f", every {prj.effective_config().get('poll_minutes', 15)} min"))
    else:
        print("  idxg autoindex --install --watch   keep it fresh in the background")


def _root_for(db_arg):
    """The project a --db argument names, whether it is the project folder or its graph file."""
    if db_arg:
        p = os.path.expanduser(db_arg)
        if os.path.isdir(p):
            return prj.find_root(p)
        for root, e in prj.load_registry().items():
            if e.get("db") == p:
                return root
    return prj.find_root()


def cmd_refresh(a):
    reg = prj.load_registry()
    roots = list(reg) if a.all else [_root_for(getattr(a, "db", None))]
    for root in roots:
        entry = reg.get(root, {})
        db = entry.get("db") or prj.db_for(root)
        stale, reason, _ = (True, "forced", {})
        if not a.force and os.path.exists(db):
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            stale, reason, _ = prj.staleness(meta(d))
            d.close()
        if not stale:
            print(f"up to date: {root}")
            continue
        print(f"reindexing {root}  ({reason})")
        build_now(root, jobs=a.jobs, quiet=not a.verbose)
        print(f"  done: {prj.load_registry().get(root, {}).get('db')}")


def cmd_projects(a):
    reg = prj.load_registry()
    if not reg:
        print("no projects indexed yet; run idxg init inside one")
        return
    rows = []
    for root, e in sorted(reg.items()):
        db = e.get("db", "")
        stale = "?"
        if os.path.exists(db):
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            st, reason, _ = prj.staleness(meta(d))
            d.close()
            stale = reason if st else "fresh"
        else:
            stale = "missing db"
        rows.append((root, e.get("symbols", 0), e.get("indexed_at", ""), stale, db))
    if a.json:
        print(json.dumps([{"root": r, "symbols": s, "indexed_at": t, "state": st, "db": d}
                          for r, s, t, st, d in rows], indent=2))
        return
    w = max(len(r[0]) for r in rows)
    print(f"{'project'.ljust(w)}  {'symbols':>9}  {'indexed':<19}  state")
    for r, syms, t, st, d in rows:
        print(f"{r.ljust(w)}  {syms:>9,}  {t:<19}  {st}\n{''.ljust(w)}  graph: {d}")
    print("\nPass a project folder as db to query it from anywhere.")


def cmd_open(a):
    root = prj.find_root()
    entry = prj.load_registry().get(root, {})
    html = entry.get("html")
    if not html or not os.path.exists(html):
        db = entry.get("db") or db_path(a.db)
        html = os.path.join(os.path.dirname(db),
                            os.path.basename(db).replace(".db", "-explorer.html"))
        if not os.path.exists(html):
            raise SystemExit("no explorer yet; run idxg viz")
    if a.static:
        print(html)
        subprocess.run(["open", html])
        return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import serve
    db = entry.get("db") or db_path(a.db)

    def ready(url):
        print(f"explorer at {url}\nthe graph tab can now reach every symbol in {os.path.basename(db)}; "
              "Ctrl-C stops the server", flush=True)
        subprocess.run(["open", url])
    serve.serve(html, db, port=a.port, on_ready=ready)


def store_settled(stores, quiet_seconds, max_wait):
    """True once the index store has stopped changing for quiet_seconds.

    A build writes units continuously, so reindexing on the first write would snapshot a
    half-compiled state and then be stale again immediately.
    """
    import time as _t
    waited = 0
    last = prj.store_signature(stores)
    while waited < max_wait:
        _t.sleep(quiet_seconds)
        waited += quiet_seconds
        now = prj.store_signature(stores)
        if now == last:
            return True
        last = now
    return False


def plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_LABEL}.plist")


def _remove_legacy_agent():
    """Unload the agent installed under the tool's previous name, so two agents never
    reindex the same projects."""
    old = os.path.expanduser(f"~/Library/LaunchAgents/{LEGACY_LAUNCH_LABEL}.plist")
    if os.path.exists(old):
        subprocess.run(["launchctl", "unload", old], capture_output=True)
        os.remove(old)
        print(f"removed the agent installed under the old name: {old}")


def cmd_autoindex(a):
    log = os.path.join(prj.CACHE_DIR, "autoindex.log")
    if a.run_once:
        reg = prj.load_registry()
        for root in list(reg):
            db = reg[root].get("db") or prj.db_for(root)
            if not os.path.exists(db) or not os.path.isdir(root):
                continue
            d = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            d.row_factory = sqlite3.Row
            stale, reason, _ = prj.staleness(meta(d))
            d.close()
            stamp = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
            if not stale:
                print(f"{stamp} fresh {root}", flush=True)
                continue
            settle = a.settle if a.settle is not None else 45
            if settle > 0 and not store_settled(reg[root].get("stores") or [], settle, a.settle_max):
                print(f"{stamp} {root} still being written after {a.settle_max}s, leaving it "
                      f"for the next trigger", flush=True)
                continue
            print(f"{stamp} reindexing {root} ({reason})", flush=True)
            try:
                build_now(root, jobs=a.jobs, quiet=True)
                print(f"{stamp} done {root}", flush=True)
            except SystemExit as e:
                print(f"{stamp} failed: {e}", flush=True)
        return
    if a.status:
        p = plist_path()
        print(f"plist: {p}  ({'installed' if os.path.exists(p) else 'not installed'})")
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        print("loaded:", "yes" if LAUNCH_LABEL in out else "no")
        if os.path.exists(log):
            print(f"log: {log}")
            with open(log) as f:
                for line in f.readlines()[-5:]:
                    print("  " + line.rstrip())
        return
    if a.uninstall:
        _remove_legacy_agent()
        p = plist_path()
        subprocess.run(["launchctl", "unload", p], capture_output=True)
        if os.path.exists(p):
            os.remove(p)
        prj.set_config("autoindex", False, None)
        print(f"removed {p}")
        print("  install.sh will leave it off; `idxg autoindex --install` turns it back on")
        return
    if a.if_enabled and not prj.effective_config().get("autoindex", True):
        print("autoindex is off in your config; leaving it off")
        return
    if a.every:
        prj.set_config("poll_minutes", a.every, None)
    if not a.if_enabled:
        prj.set_config("autoindex", True, None)
    install_agent(minutes=a.every, watch=a.watch or agent_watches())


def agent_watches():
    """Whether the installed agent triggers on index-store writes, so a reinstall keeps
    the choice the user made the first time."""
    p = plist_path()
    if not os.path.exists(p):
        return False
    with open(p) as f:
        return "<key>WatchPaths</key>" in f.read()


def install_agent(minutes=None, watch=False, quiet=False):
    log = os.path.join(prj.CACHE_DIR, "autoindex.log")
    _remove_legacy_agent()
    minutes = minutes or prj.effective_config().get("poll_minutes", 15)
    os.makedirs(prj.CACHE_DIR, exist_ok=True)
    p = plist_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idxg.py")
    watch_paths = []
    if watch:
        for entry in prj.load_registry().values():
            for st in entry.get("stores") or []:
                units = os.path.join(st, "v5", "units")
                watch_paths.append(units if os.path.isdir(units) else st)
    watch_block = ""
    if watch_paths:
        joined = "\n".join(f"        <string>{w}</string>" for w in dict.fromkeys(watch_paths))
        watch_block = (f"    <key>WatchPaths</key>\n    <array>\n{joined}\n    </array>\n"
                       f"    <key>ThrottleInterval</key><integer>120</integer>\n")
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script}</string>
        <string>autoindex</string>
        <string>--run-once</string>
    </array>
{watch_block}    <key>StartInterval</key><integer>{int(minutes) * 60}</integer>
    <key>RunAtLoad</key><false/>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>5</integer>
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    with open(p, "w") as f:
        f.write(body)
    subprocess.run(["launchctl", "unload", p], capture_output=True)
    r = subprocess.run(["launchctl", "load", p], capture_output=True, text=True)
    if quiet:
        return len(set(watch_paths))
    print(f"installed {p}")
    if watch_paths:
        print(f"  watches {len(set(watch_paths))} index store dir(s); a build that writes records")
        print(f"  wakes it, it waits for writes to stop, then reindexes")
    print(f"  also checks every {minutes} min as a fallback")
    print(f"  log: {log}")
    if r.returncode != 0:
        print("  launchctl load said:", (r.stderr or r.stdout).strip())
    return len(set(watch_paths))


def build_parser():
    ap = argparse.ArgumentParser(prog="idxg", description="codebase-brain: a code graph, its history and its docs")
    ap.add_argument("--version", action="version", version=f"codebase-brain {VERSION}")
    ap.add_argument("--db", help="graph db path (default ~/.cache/codebase-brain/<project>.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="index status + coverage summary")
    p.add_argument("--json", action="store_true")
    p.add_argument("--exact", action="store_true",
                   help="recount rows instead of reading the values cached at build time")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("coverage", help="check which paths the index actually covers")
    p.add_argument("paths", nargs="+"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_coverage)

    p = sub.add_parser("search", help="find symbols (regex, full-text, filters)")
    p.add_argument("query", nargs="?", help="full-text query (BM25 over camel-split names)")
    p.add_argument("--name", help="regex on the symbol name")
    p.add_argument("--kind", help="comma list, e.g. Struct,Class,InstanceMethod")
    p.add_argument("--module"); p.add_argument("--file", help="glob on repo-relative path")
    p.add_argument("--lang", help="Swift|ObjC|C|C++")
    p.add_argument("--min-degree", type=int, default=0); p.add_argument("--max-degree", type=int)
    p.add_argument("--limit", type=int, default=40); p.add_argument("--offset", type=int, default=0)
    p.add_argument("--all", action="store_true", help="include symbols defined outside the repo")
    p.add_argument("--usr", action="store_true", help="print USRs")
    p.add_argument("--detail", choices=["ids", "default"], default="default")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_search)

    p = sub.add_parser("trace", help="walk call/reference edges from a symbol")
    p.add_argument("symbol"); p.add_argument("--direction", choices=["in", "out", "both"], default="both")
    p.add_argument("--depth", type=int, default=2); p.add_argument("--fanout", type=int, default=25)
    p.add_argument("--kind", default="CALLS", help="edge kinds, comma list (%s)" % ",".join(EDGE_KINDS))
    p.add_argument("--first", action="store_true", help="use the best match instead of listing candidates")
    p.add_argument("--code", action="store_true", help="print the source line of each edge's first site")
    p.add_argument("--max-rows", type=int, default=120,
                   help="cap printed rows so a wide trace stays readable (default 120)")
    p.add_argument("--max-bytes", type=int, default=8000,
                   help="cap printed bytes, keeping one call affordable for an agent")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_trace)

    p = sub.add_parser("refs", help="every occurrence of a symbol with roles")
    p.add_argument("symbol"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_refs)

    p = sub.add_parser("snippet", help="print a symbol's definition from disk")
    p.add_argument("symbol"); p.add_argument("--max-lines", type=int, default=200)
    p.add_argument("--max-bytes", type=int, default=6000,
                   help="cap printed bytes for a large definition (default 6000)")
    p.set_defaults(fn=cmd_snippet)

    p = sub.add_parser("sql", help="read-only SQL over the graph")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_sql)

    p = sub.add_parser("arch", help="layers, modules, hotspots, targets")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_arch)

    p = sub.add_parser("viz", help="generate the self-contained HTML explorer")
    p.add_argument("--out", help="output html path")
    p.add_argument("--scope", help="module name or path glob to slice on")
    p.add_argument("--limit", type=int, default=None,
                   help="symbols in the slice (default: all in scope, or top 1500 repo-wide; 0 = all)")
    p.add_argument("--edge-cap", type=int, default=30000)
    p.add_argument("--per-node-cap", type=int, default=None,
                   help="max edges kept per symbol per direction (default: config viz_per_node_cap)")
    p.add_argument("--title"); p.add_argument("--open", action="store_true")
    p.set_defaults(fn=cmd_viz)

    p = sub.add_parser("module", help="one module's card: layers, dependencies, dependents, hotspots")
    p.add_argument("module")
    p.add_argument("--types", type=int, default=10, help="how many most connected types to list")
    p.add_argument("--users", type=int, default=20, help="how many dependent modules to detail")
    p.add_argument("--type-users", type=int, default=30, help="how many used types to list with their users")
    p.add_argument("--max-bytes", type=int, default=24000)
    p.set_defaults(fn=cmd_module)

    p = sub.add_parser("usage", help="can these symbols be deleted: graph uses plus a text search of unindexed files")
    p.add_argument("symbols", nargs="+", help="Module.Type.member, Type.member, or a definition site path:line")
    p.set_defaults(fn=cmd_usage)

    p = sub.add_parser("impact", help="what a signature change to a method or property breaks")
    p.add_argument("symbol", help="Type.member, Module.Type.member or a USR")
    p.add_argument("--max-rows", type=int, default=40, help="cap the rows of each list; the rest are counted per module")
    p.add_argument("--max-bytes", type=int, default=12000)
    p.set_defaults(fn=cmd_impact)

    p = sub.add_parser("dead", help="symbols nothing in the indexed build reaches")
    p.add_argument("--kind", help="comma list, default: types, methods and properties")
    p.add_argument("--module")
    p.add_argument("--include-tests", action="store_true")
    p.add_argument("--include-vendor", action="store_true",
                   help="include vendored trees (third-party, Vendor, Pods)")
    p.add_argument("--lang", default="Swift",
                   help="Swift (default), ObjC, C, or any. ObjC selectors are dispatched "
                        "dynamically, so ObjC candidates are mostly false positives")
    p.add_argument("--verify", action="store_true",
                   help="drop candidates whose name appears in any other file (ripgrep)")
    p.add_argument("--test-only", dest="test_only", action="store_true",
                   help="instead: production symbols reached only from test modules")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_dead)

    p = sub.add_parser("schema", help="print the db schema and edge/role vocabulary")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("history", help="commit history of the main branch, joined to the graph")
    hs = p.add_subparsers(dest="hcmd", required=True)
    h = hs.add_parser("build", help="extract git log (incremental) and sync repo docs")
    h.add_argument("--full", action="store_true", help="start over instead of continuing from the last head")
    h.add_argument("--branch", help="history branch (default: config history_branch, then main, master)")
    h.add_argument("--since", help="only commits after YYYY-MM-DD (cheaper first build)")
    h.add_argument("--all-commits", action="store_true",
                   help="every commit reachable from the branch, not one per merge")
    h.add_argument("--no-docs", dest="docs", action="store_false", default=True)
    h.add_argument("--no-prs", dest="prs", action="store_false", default=True,
                   help="skip fetching pull request descriptions through gh")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_build)
    h = hs.add_parser("log", help="commits touching paths, a symbol's file, a module, an author")
    h.add_argument("paths", nargs="*", help="repo-relative files, directories (trailing /) or globs")
    h.add_argument("--symbol", help="follow the file that defines this symbol")
    h.add_argument("--module"); h.add_argument("--component", help="module-depth directory")
    h.add_argument("--author"); h.add_argument("--since"); h.add_argument("--until")
    h.add_argument("--grep", help="substring of subject, body or ticket")
    h.add_argument("--release", help="only commits that first shipped in this release tag")
    h.add_argument("--files", action="store_true", help="list changed files per commit")
    h.add_argument("--narrate", action="store_true",
                   help="one plain paragraph per commit: who, what, why (from the PR), which files")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--max-bytes", type=int, default=12000,
                   help="cap printed bytes, keeping one call affordable for an agent")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_log)
    h = hs.add_parser("show", help="one commit in full, by sha or #PR")
    h.add_argument("sha"); h.add_argument("--max-files", type=int, default=80)
    h.add_argument("--max-body", type=int, default=4000, help="cap the printed PR description")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_show)
    h = hs.add_parser("releases", help="version tags, when each branched off, what first shipped in each")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_releases)
    h = hs.add_parser("digest", help="weekly digest: every change narrated, grouped by area")
    h.add_argument("--week", help="ISO week, e.g. 2026-W36 (default: the week of the last commit)")
    h.add_argument("--release", help="digest of everything that first shipped in this release tag")
    h.add_argument("--since"); h.add_argument("--until")
    h.add_argument("--list", action="store_true", help="list weeks with commit counts")
    h.add_argument("--limit", type=int, default=30, help="weeks shown by --list")
    h.add_argument("--html", nargs="?", const="auto", metavar="PATH",
                   help="write the digest page (the vault's weekly-digest layout); default path beside the db")
    h.add_argument("--open", action="store_true")
    h.add_argument("--max-bytes", type=int, default=0, help="cap the text output (0 = no cap)")
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_digest)
    h = hs.add_parser("churn", help="where change concentrates over a window")
    h.add_argument("--since", help="YYYY-MM-DD (default: 365 days ago)")
    h.add_argument("--by", choices=["module", "component", "file", "author"], default="module")
    h.add_argument("--ext", help="one extension, e.g. swift")
    h.add_argument("--limit", type=int, default=30)
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_churn)
    h = hs.add_parser("timeline", help="narrative history, one paragraph per period")
    h.add_argument("--periods", type=int, default=6, help="most recent periods (0 = all)")
    h.add_argument("--granularity", choices=["year", "quarter", "month"])
    h.add_argument("--json", action="store_true")
    h.set_defaults(hfn=cmd_history_timeline)
    h = hs.add_parser("vault", help="write history and docs as knowledge-vault clippings")
    h.add_argument("--out", help="vault directory (default: config history_vault, else beside the db)")
    h.add_argument("--no-docs", dest="docs", action="store_false", default=True)
    h.add_argument("--no-history", dest="history", action="store_false", default=True)
    h.add_argument("--min-commits", type=int, default=3, help="modules with fewer commits get no note")
    h.add_argument("--dry-run", action="store_true")
    h.set_defaults(hfn=cmd_history_vault)
    p.set_defaults(fn=cmd_history, no_stale_check=True)

    p = sub.add_parser("docs", help="the repository's own markdown docs, searchable")
    ds = p.add_subparsers(dest="dcmd", required=True)
    d = ds.add_parser("list", help="docs by kind, module or path")
    d.add_argument("--module"); d.add_argument("--kind", help="readme, agent-note, skill, guide, doc")
    d.add_argument("--path", help="glob on the repo path")
    d.add_argument("--limit", type=int, default=100)
    d.add_argument("--json", action="store_true")
    d.set_defaults(hfn=cmd_docs_list)
    d = ds.add_parser("search", help="full-text search over doc content")
    d.add_argument("query"); d.add_argument("--limit", type=int, default=10)
    d.add_argument("--json", action="store_true")
    d.set_defaults(hfn=cmd_docs_search)
    d = ds.add_parser("show", help="print one doc")
    d.add_argument("path", help="repo path or a unique suffix of it")
    d.add_argument("--max-bytes", type=int, default=12000)
    d.set_defaults(hfn=cmd_docs_show)
    p.set_defaults(fn=cmd_docs, no_stale_check=True)

    p = sub.add_parser("init", help="index a project and install its skill")
    p.add_argument("path", nargs="?", help="project root (default: detected from the cwd)")
    p.add_argument("--jobs", type=int)
    p.add_argument("--no-build", dest="build", action="store_false", default=True)
    p.add_argument("--no-viz", dest="viz", action="store_false", default=True)
    p.add_argument("--no-skill", dest="skill", action="store_false", default=True)
    p.add_argument("--claude-md", dest="claude_md", action="store_true", default=False,
                   help="also write the agent note into the project's CLAUDE.md "
                        "(once per repo; commit it so everyone gets it)")
    p.add_argument("--no-claude-md", dest="claude_md", action="store_false", help=argparse.SUPPRESS)
    p.add_argument("--claude-md-path", dest="claude_md_path",
                   help="with --claude-md, write the note here instead of <project>/CLAUDE.md")
    p.set_defaults(fn=cmd_init, no_stale_check=True)

    p = sub.add_parser("deinit", help="remove what init installed in a project")
    p.add_argument("path", nargs="?")
    p.add_argument("--purge", action="store_true",
                   help="also delete the graph, history db, explorer, digests and the default vault export")
    p.add_argument("--force", action="store_true",
                   help="delete the project skill even when it has a Project notes section")
    p.add_argument("--claude-md", action="store_true",
                   help="also strip the agent note from the project's CLAUDE.md")
    p.set_defaults(fn=cmd_deinit, no_stale_check=True)

    p = sub.add_parser("config", help="show or change settings for this project")
    p.add_argument("assign", nargs="*", metavar="key=value")
    p.add_argument("--global", dest="scope", action="store_const", const="global",
                   default="project", help="write to the global config instead of this project")
    p.add_argument("--unset", nargs="+", metavar="key")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_config, no_stale_check=True)

    p = sub.add_parser("refresh", help="reindex when the index store has moved on")
    p.add_argument("--all", action="store_true", help="every registered project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--jobs", type=int)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_refresh, no_stale_check=True)

    p = sub.add_parser("projects", help="list indexed projects and whether they are fresh")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_projects, no_stale_check=True)

    p = sub.add_parser("crash", help="triage a stack trace: symbol, callers and recent commits per frame")
    p.add_argument("trace", help="file with the symbolicated trace, or - for stdin")
    p.add_argument("--since", help="only commits after this date (YYYY-MM-DD) or git ref, e.g. a release tag")
    p.add_argument("--frames", type=int, default=6, help="in-repo frames to expand (default 6)")
    p.add_argument("--callers", type=int, default=5)
    p.add_argument("--commits", type=int, default=5)
    p.add_argument("--max-bytes", type=int, default=12000)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_crash)

    p = sub.add_parser("update", help="install the latest release, or another one with --to")
    p.add_argument("--check", action="store_true", help="only report whether a newer release exists")
    p.add_argument("--force", action="store_true", help="reinstall the latest release even when it is installed")
    p.add_argument("--to", metavar="VERSION", help="install this release instead, e.g. 0.2.16 to go back")
    p.set_defaults(fn=cmd_update, no_stale_check=True)

    p = sub.add_parser("open", help="serve the HTML explorer locally and open it, so it can reach every symbol")
    p.add_argument("--static", action="store_true", help="open the file instead; it holds only the most connected symbols")
    p.add_argument("--port", type=int, default=0, help="port to serve on (default: a free one)")
    p.set_defaults(fn=cmd_open, no_stale_check=True)

    p = sub.add_parser("autoindex", help="background refresh via a launchd agent")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true")
    g.add_argument("--uninstall", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--run-once", action="store_true", help="what the agent runs on each tick")
    g.add_argument("--if-enabled", action="store_true",
                   help="install only while autoindex is on in the config; what install.sh runs")
    p.add_argument("--every", type=int, metavar="MINUTES")
    p.add_argument("--jobs", type=int)
    p.add_argument("--watch", action="store_true",
                   help="also trigger on index-store writes, so a build refreshes the graph")
    p.add_argument("--settle", type=int, metavar="SECONDS",
                   help="quiet period the store must hold before reindexing (default 45)")
    p.add_argument("--settle-max", type=int, default=1800, metavar="SECONDS",
                   help="give up waiting for quiet after this long (default 1800)")
    p.set_defaults(fn=cmd_autoindex, no_stale_check=True)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        check_stale(args)
        args.fn(args)
    except BrokenPipeError:
        pass
