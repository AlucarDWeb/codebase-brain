"""Is this symbol used? One verdict per symbol from the compiled graph and a text search.

The graph is exact for what the build compiled, but deleting a symbol also breaks callers it
never saw: Objective-C the index skipped, interface builder files, plists, string lookups. So
every symbol is checked twice, in one pass: its uses in the graph (and through any protocol
requirement or base method it implements), and `git grep` for its bare name in tracked files
the graph has no records for. A text match in a file the graph did index is ignored, because
there the graph already knows whether the name means this symbol.
"""
import os, re, subprocess

R_DECL, R_DEF, R_IMPLICIT, R_EXTENDEDBY = 1, 2, 256, 1 << 14
TEST_HINTS = ("test", "spec", "snapshot", "mock", "fixture")
TEST_DIRS = ("/Tests/", "/SnapshotTests/", "/UITests/", "/TestHelpers/", "/Mocks/", "/Testing/")
SOURCE_EXT = (".swift", ".m", ".mm", ".h", ".c", ".cpp")
RESOURCE_EXT = (".xib", ".storyboard", ".plist", ".strings", ".json", ".intentdefinition", ".entitlements")
DOC_EXT = (".md", ".txt", ".rst", ".html")
# Names whose text search would match half the repository.
NO_TEXT = {"init", "deinit", "subscript", "callAsFunction", "description", "id", "name", "type", "value"}
ACCESSOR_WORDS = ("getter", "setter", "modify", "didSet", "willSet")


def is_test(rel, module=None):
    if any(d in "/" + (rel or "") for d in TEST_DIRS):
        return True
    base = os.path.basename(rel or "")
    return base.endswith(("Spec.swift", "Tests.swift", "Test.swift", "Mock.swift", "Mocks.swift")) or \
        any(h in (module or "").lower() for h in TEST_HINTS)


def bare_name(name):
    base = (name or "").split("(")[0]
    return base.split(":")[0] if ":" in base else base


def resolve_one(db, ident, resolve):
    """(symbol row, how many in-repo symbols share the name) for `Module.Type.member` or a
    definition site `path/File.swift:line`."""
    m = re.match(r"^(.+\.(?:swift|m|mm|h)):(\d+)$", ident.strip())
    if m:
        rows = db.execute("""SELECT s.* FROM symbols s JOIN files f ON f.path_hash = s.def_path_hash
                             WHERE (f.rel = ? OR f.rel GLOB ?) AND s.def_line = ? AND s.in_repo = 1
                             ORDER BY (s.in_deg + s.out_deg) DESC""",
                          (m.group(1), "*/" + m.group(1).lstrip("/"), int(m.group(2)))).fetchall()
        return (rows[0], len(rows) - 1) if rows else (None, 0)
    rows = [r for r in resolve(db, ident.strip()) if r["in_repo"]]
    return (rows[0], len(rows) - 1) if rows else (None, 0)


def graph_uses(db, uh):
    """Occurrences of a symbol that are not its own declaration or an extension of it, plus
    the uses of every requirement or base method it implements (they dispatch to it)."""
    targets = [(uh, None)] + [(r["dst"], r["dst"]) for r in db.execute(
        "SELECT e.dst FROM edges e WHERE e.src = ? AND e.kind = 'OVERRIDES'", (uh,))]
    uses = []
    for target, via in targets:
        for r in db.execute("""SELECT o.line, o.roles, f.rel, f.module FROM occurrences o
                               LEFT JOIN files f ON f.path_hash = o.path_hash
                               WHERE o.usr_hash = ?""", (target,)):
            if r["roles"] & (R_DECL | R_DEF | R_EXTENDEDBY):
                continue
            uses.append({"site": f"{r['rel']}:{r['line']}", "rel": r["rel"], "test": is_test(r["rel"], r["module"]),
                         "implicit": bool(r["roles"] & R_IMPLICIT), "via": via})
    return uses


def text_hits(root, names, indexed):
    """{name: [(rel, line, kind, test)]} for word matches in tracked files the graph has no
    records for, one `git grep` for all names."""
    names = sorted({n for n in names if n and n not in NO_TEXT})
    out = {n: [] for n in names}
    if not names:
        return out
    args = ["git", "-C", root, "grep", "-n", "-I", "-w", "-F"]
    for n in names:
        args += ["-e", n]
    res = subprocess.run(args, capture_output=True, text=True)
    pats = {n: re.compile(r"(?<![\w$])" + re.escape(n) + r"(?![\w$])") for n in names}
    for line in res.stdout.splitlines():
        rel, _, rest = line.partition(":")
        ln, _, text = rest.partition(":")
        if rel in indexed or not ln.isdigit():
            continue
        ext = os.path.splitext(rel)[1].lower()
        kind = "source" if ext in SOURCE_EXT else "resource" if ext in RESOURCE_EXT else \
            "doc" if ext in DOC_EXT else "other"
        for n, p in pats.items():
            if p.search(text):
                out[n].append((rel, int(ln), kind, is_test(rel)))
    return out


def check(db, root, idents, resolve):
    """One record per identifier: the symbol, its verdict and the evidence behind it."""
    resolved = [(ident,) + resolve_one(db, ident, resolve) for ident in idents]
    indexed = {r[0] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1 AND rel IS NOT NULL")}

    def text_name(sym):
        n = sym["name"] or ""
        if n.startswith(tuple(w + ":" for w in ACCESSOR_WORDS)):
            n = n.split(":", 1)[1]
        return bare_name(n)

    hits = text_hits(root, [text_name(s) for _, s, _ in resolved if s], indexed)
    out = []
    for ident, sym, others in resolved:
        if not sym:
            out.append({"ident": ident, "verdict": "NOT FOUND", "symbol": None})
            continue
        def_rel = db.execute("SELECT rel FROM files WHERE path_hash = ?", (sym["def_path_hash"],)).fetchone()
        def_rel = def_rel[0] if def_rel else None
        uses = graph_uses(db, sym["usr_hash"])
        name = text_name(sym)
        text = [h for h in hits.get(name, []) if not (h[0] == def_rel and h[1] == sym["def_line"])]
        prod = [u for u in uses if not u["test"]]
        tests = [u for u in uses if u["test"]]
        text_prod = [h for h in text if h[2] in ("source", "resource", "other") and not h[3]]
        text_test = [h for h in text if h[2] in ("source", "resource", "other") and h[3]]
        if prod:
            verdict = "USED IN PRODUCTION"
        elif text_prod:
            verdict = "USED IN PRODUCTION (text only)"
        elif tests or text_test:
            verdict = "USED ONLY BY TESTS"
        else:
            verdict = "UNUSED"
        out.append({"ident": ident, "symbol": sym, "def": f"{def_rel}:{sym['def_line']}", "verdict": verdict,
                    "others": others, "prod": prod, "tests": tests, "text_prod": text_prod,
                    "text_test": text_test, "docs": [h for h in text if h[2] == "doc"],
                    "text_searched": name not in NO_TEXT})
    return out
