"""What changing a method's signature breaks: its implementations and overrides, every call of it
or of them, the members of its protocol's extensions, and text in files the build never compiled.

The first three come from the compiled graph and are exact for what the build compiled. A file the
build never compiled has no records, so each tracked file without records that names the member's
type is read as text, and its matching lines are reported as text, never as resolved uses.
"""
import os, re, subprocess

import usage

ACCESSOR_WORDS = ("getter", "setter", "modify", "didSet", "willSet")


def bare(name):
    """`reduce` for `reduce(_:_:)`, `title` for `getter:title`."""
    n = name or ""
    if n.startswith(tuple(w + ":" for w in ACCESSOR_WORDS)):
        n = n.split(":", 1)[1]
    return n.split("(")[0]


def container(db, uh):
    """(type usr_hash, type row) that declares a member, seeing through an extension."""
    row = db.execute("""SELECT p.usr_hash, p.kind FROM edges e JOIN symbols p ON p.usr_hash = e.src
                        WHERE e.dst = ? AND e.kind = 'CONTAINS' LIMIT 1""", (uh,)).fetchone()
    if not row:
        return None, None
    h = row[0]
    if row[1] == "Extension":
        ext = db.execute("SELECT dst FROM edges WHERE src = ? AND kind = 'EXTENDS' LIMIT 1", (h,)).fetchone()
        if ext:
            h = ext[0]
    return h, db.execute("SELECT * FROM symbols WHERE usr_hash = ?", (h,)).fetchone()


def implementations(db, uh, depth=4):
    """[(usr_hash, level)] for every symbol that implements or overrides `uh`, and their overrides."""
    seen, frontier, out = {uh}, [uh], []
    for level in range(1, depth + 1):
        nxt = []
        for h in frontier:
            for (src,) in db.execute("SELECT DISTINCT src FROM edges WHERE dst = ? AND kind = 'OVERRIDES'", (h,)):
                if src not in seen:
                    seen.add(src)
                    nxt.append(src)
                    out.append((src, level))
        frontier = nxt
    return out


def implements(db, uh):
    """The requirements or base methods `uh` itself implements."""
    return [r[0] for r in db.execute("SELECT DISTINCT dst FROM edges WHERE src = ? AND kind = 'OVERRIDES'", (uh,))]


def with_accessors(db, uh):
    """A property's calls land on its getter and setter, so they are targets too."""
    return [uh] + [r[0] for r in db.execute("SELECT src FROM edges WHERE dst = ? AND kind = 'ACCESSOR_OF'", (uh,))]


def sites(db, targets):
    """(calls, references): CALLS edges into any target, and REFERENCES edges at sites where no
    call was recorded (a method passed as a value, a key path)."""
    calls, refs, seen = [], [], set()
    marks = ",".join("?" * len(targets))
    for kind in ("CALLS", "REFERENCES"):
        for r in db.execute(f"""SELECT e.src, e.dst, e.path_hash, e.line, f.rel, s.module FROM edges e
                                JOIN symbols s ON s.usr_hash = e.src LEFT JOIN files f ON f.path_hash = e.path_hash
                                WHERE e.dst IN ({marks}) AND e.kind = ? ORDER BY f.rel, e.line""",
                            list(targets) + [kind]):
            key = (r[1], r[2], r[3])
            if key in seen:
                continue
            seen.add(key)
            (calls if kind == "CALLS" else refs).append(
                dict(caller=r[0], target=r[1], rel=r[4], line=r[3], module=r[5], test=usage.is_test(r[4], r[5])))
    return calls, refs


def extension_members(db, type_hash, skip):
    """Members of the type's extensions other than `skip`, each with the lines where it calls
    one of `skip`: in a protocol these are the helpers that forward to a requirement."""
    marks = ",".join("?" * len(skip))
    out = []
    for r in db.execute("""SELECT DISTINCT s.usr_hash, s.name, s.kind, f.rel, s.def_line, s.module FROM edges x
                           JOIN edges m ON m.src = x.src AND m.kind = 'CONTAINS' JOIN symbols s ON s.usr_hash = m.dst
                           LEFT JOIN files f ON f.path_hash = s.def_path_hash
                           WHERE x.dst = ? AND x.kind = 'EXTENDS' ORDER BY f.rel, s.def_line""", (type_hash,)):
        if r[0] in skip or r[2] in ("TypeAlias", "Extension"):
            continue
        calls = [c[0] for c in db.execute(f"""SELECT DISTINCT line FROM edges WHERE src = ? AND kind = 'CALLS'
                                               AND dst IN ({marks}) ORDER BY line""", [r[0]] + list(skip))]
        out.append(dict(h=r[0], name=r[1], kind=r[2], rel=r[3], line=r[4], module=r[5], calls=calls))
    return out


def unindexed_text(root, db, type_name, member):
    """{rel: [(line, what, text)]} for tracked files without index records that name the type
    (or, with no type, the member), keeping the lines that name either."""
    word = type_name or member
    if not word or word in usage.NO_TEXT:
        return {}, []
    res = subprocess.run(["git", "-C", root, "grep", "-l", "-I", "-w", "-F", "-e", word],
                         capture_output=True, text=True)
    indexed = {r[0] for r in db.execute("SELECT rel FROM files WHERE in_repo = 1 AND rel IS NOT NULL")}
    files = [p for p in res.stdout.splitlines() if p and p not in indexed]
    docs = [p for p in files if os.path.splitext(p)[1].lower() in usage.DOC_EXT]
    code = [p for p in files if p not in docs]
    type_rx = re.compile(r"(?<![\w$])" + re.escape(type_name) + r"(?![\w$])") if type_name else None
    mem_rx = re.compile(r"(?<![\w$])" + re.escape(member) + r"(?![\w$])") if member and member not in usage.NO_TEXT else None
    decl_rx = re.compile(r"\b(?:func|var|let|case)\s+" + re.escape(member) + r"\b") if mem_rx else None
    call_rx = re.compile(r"(?<![\w$])" + re.escape(member) + r"\s*[(\[{]|\.\s*" + re.escape(member) + r"\b") if mem_rx else None
    out = {}
    for rel in code:
        try:
            with open(os.path.join(root, rel), errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        hits = []
        for i, text in enumerate(lines, 1):
            if mem_rx and mem_rx.search(text):
                what = f"declares {member}" if decl_rx.search(text) else \
                    f"calls {member}" if call_rx.search(text) else f"names {member}"
            elif type_rx and type_rx.search(text):
                what = f"names {type_name}"
            else:
                continue
            hits.append((i, what, " ".join(text.split())[:100]))
        if hits:
            out[rel] = hits
    return out, docs
