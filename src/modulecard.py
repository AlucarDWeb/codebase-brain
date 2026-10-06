"""Module card: what a module is made of, what it uses, who uses it, and where a change spreads.

Every figure comes from the compiled graph, so it describes what the last build compiled. Files
under the module's folder that the build never compiled are counted and reported, never guessed
about, and a module imported without any symbol used is listed apart from the ones in use.
"""
import os, re, subprocess

TYPE_KINDS = ("Class", "Struct", "Enum", "Protocol")
# Structural edges say where a symbol lives, not who depends on it. RECEIVED_BY ties a
# receiver type to a method called on it, and its site is the caller's (a snapshot test
# calling a helper on a view), so it would credit the caller's dependency to the view.
STRUCTURAL = ("CONTAINS", "EXTENDS", "ACCESSOR_OF", "RECEIVED_BY")
TEST_HINTS = ("test", "spec", "snapshot", "mock", "fixture")
SOURCE_EXT = (".swift", ".m", ".mm", ".h", ".c", ".cpp")
SWIFT_IMPORT = re.compile(r"^[ \t]*(?:@\w+(?:\([^)]*\))?[ \t]+)*import[ \t]+(?:(?:class|struct|enum|protocol|func|typealias|var|let)[ \t]+)?(\w+)", re.M)
OBJC_IMPORT = re.compile(r"^[ \t]*(?:@import[ \t]+(\w+)|#(?:import|include)[ \t]+<(\w+)/)", re.M)


def is_test(module):
    return any(h in (module or "").lower() for h in TEST_HINTS)


def modules(db):
    return sorted(r[0] for r in db.execute(
        "SELECT DISTINCT module FROM files WHERE in_repo = 1 AND module IS NOT NULL AND module != ''"))


def resolve(db, name):
    """(module, suggestions): an exact or case-insensitive match, else names that contain it."""
    known = modules(db)
    for m in known:
        if m == name:
            return m, []
    for m in known:
        if m.lower() == name.lower():
            return m, []
    near = [m for m in known if name.lower() in m.lower()]
    return None, sorted(near, key=lambda m: (is_test(m), len(m), m))[:12]


def folder(db, module):
    """(module root, layer base, repo-relative files) from where the module's sources live."""
    rels = [r[0] for r in db.execute("SELECT rel FROM files WHERE module = ? AND in_repo = 1 AND rel IS NOT NULL",
                                     (module,))]
    if not rels:
        return None, None, []
    common = os.path.commonpath([os.path.dirname(r) or "." for r in rels])
    if common in ("", "."):
        # Files spread across the repo (an app target with generated sources): its main folder.
        tops = {}
        for r in rels:
            tops[r.split("/", 1)[0]] = tops.get(r.split("/", 1)[0], 0) + 1
        common = max(tops, key=tops.get)
    parts = common.split("/")
    if "Sources" in parts:
        i = parts.index("Sources")
        return "/".join(parts[:i]) or ".", "/".join(parts[:i + 1]), rels
    return common, common, rels


def layers(rels, base):
    out = {}
    for r in rels:
        sub = r[len(base) + 1:] if base and r.startswith(base + "/") else r
        seg = sub.split("/", 1)[0] if "/" in sub else "(top level)"
        out[seg] = out.get(seg, 0) + 1
    return sorted(out.items(), key=lambda kv: -kv[1])


def type_ranking(db, module):
    """Per type: distinct symbols outside it that it uses and that use it, with its extensions'
    and members' edges folded in. A temporary table keeps the member test indexed."""
    db.execute("DROP TABLE IF EXISTS temp.card_mem")
    db.execute("CREATE TEMP TABLE card_mem(t INTEGER, m INTEGER, PRIMARY KEY(t, m))")
    db.execute(f"""INSERT OR IGNORE INTO temp.card_mem
        WITH RECURSIVE types(t) AS (SELECT usr_hash FROM symbols WHERE module = ? AND in_repo = 1
                                    AND kind IN ({",".join("?" * len(TYPE_KINDS))})),
        roots(t, m) AS (SELECT t, t FROM types
                        UNION SELECT types.t, e.src FROM types JOIN edges e ON e.dst = types.t AND e.kind = 'EXTENDS'),
        mem(t, m) AS (SELECT t, m FROM roots
                      UNION SELECT mem.t, e.dst FROM mem JOIN edges e ON e.src = mem.m AND e.kind = 'CONTAINS')
        SELECT t, m FROM mem""", (module,) + TYPE_KINDS)
    db.execute("CREATE INDEX temp.ix_card_mem_m ON card_mem(m)")
    skip = ",".join("?" * len(STRUCTURAL))
    rows = db.execute(f"""
        WITH ext(t, other, inbound) AS (
          SELECT c.t, e.dst, 0 FROM temp.card_mem c JOIN edges e ON e.src = c.m AND e.kind NOT IN ({skip})
          UNION ALL
          SELECT c.t, e.src, 1 FROM temp.card_mem c JOIN edges e ON e.dst = c.m AND e.kind NOT IN ({skip}))
        SELECT s.name, s.kind, f.rel, COUNT(DISTINCT CASE WHEN inbound = 1 THEN other END) used_by,
               COUNT(DISTINCT CASE WHEN inbound = 0 THEN other END) uses
        FROM ext JOIN symbols s ON s.usr_hash = ext.t LEFT JOIN files f ON f.path_hash = s.def_path_hash
        WHERE NOT EXISTS (SELECT 1 FROM temp.card_mem c2 WHERE c2.t = ext.t AND c2.m = ext.other)
        GROUP BY ext.t ORDER BY used_by + uses DESC""", STRUCTURAL + STRUCTURAL).fetchall()
    db.execute("DROP TABLE temp.card_mem")
    return [dict(name=r[0], kind=r[1], rel=r[2], used_by=r[3], uses=r[4]) for r in rows]


def depends_on(db, module):
    skip = ",".join("?" * len(STRUCTURAL))
    return db.execute(f"""SELECT d.module, COUNT(DISTINCT d.usr_hash) symbols, COUNT(*) edges
        FROM symbols s JOIN edges e ON e.src = s.usr_hash JOIN symbols d ON d.usr_hash = e.dst
        WHERE s.module = ? AND d.module != ? AND d.in_repo = 1 AND e.kind NOT IN ({skip})
        GROUP BY d.module ORDER BY symbols DESC""", (module, module) + STRUCTURAL).fetchall()


def used_by(db, module):
    """{dependent module: [(symbol, kind, uses, first site)]} for every module that uses this one."""
    skip = ",".join("?" * len(STRUCTURAL))
    rows = db.execute(f"""SELECT s.module, d.name, d.kind, COUNT(*) n, MIN(f.rel || ':' || e.line) site
        FROM symbols d JOIN edges e ON e.dst = d.usr_hash JOIN symbols s ON s.usr_hash = e.src
        LEFT JOIN files f ON f.path_hash = e.path_hash
        WHERE d.module = ? AND d.in_repo = 1 AND s.module IS NOT NULL AND s.module != ? AND e.kind NOT IN ({skip})
        GROUP BY s.module, d.usr_hash ORDER BY n DESC""", (module, module) + STRUCTURAL).fetchall()
    out = {}
    for mod, name, kind, n, site in rows:
        out.setdefault(mod, []).append((name, kind, n, site))
    return out


def imports(root, rels):
    """Modules named by import statements in the given files."""
    found = set()
    for r in rels:
        try:
            with open(os.path.join(root, r), errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        rx = SWIFT_IMPORT if r.endswith(".swift") else OBJC_IMPORT
        for m in rx.finditer(text):
            found.add(next(g for g in m.groups() if g))
    return found


def coverage(root, mod_root, indexed):
    """(tracked source files under the module root, how many of them have index records)."""
    out = subprocess.run(["git", "-C", root, "ls-files", "--", mod_root], capture_output=True, text=True)
    tracked = [p for p in out.stdout.splitlines() if p.endswith(SOURCE_EXT)]
    have = set(indexed)
    return len(tracked), sum(1 for p in tracked if p in have)
