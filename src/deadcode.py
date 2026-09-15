"""Dead-code candidates: symbols nothing in the indexed build reaches."""
import subprocess

DEAD_INBOUND = "'CALLS','REFERENCES','RECEIVED_BY','OVERRIDES','SPECIALIZES','IB_TYPE_OF','INHERITS'"
DEAD_KINDS = ("InstanceMethod", "StaticMethod", "ClassMethod", "Class", "Struct", "Enum",
              "Protocol", "InstanceProperty", "StaticProperty", "TypeAlias", "Function")

# Conformances whose members the compiler synthesizes uses for: memberwise decoding,
# ==/hash, rawValue round-trips, SwiftUI's body. Members of these types have no explicit
# reference anywhere and are not dead.
SYNTHESIZED_PARENTS = ("Codable", "Decodable", "Encodable", "CodingKey", "Equatable",
                       "Hashable", "Comparable", "RawRepresentable", "CaseIterable",
                       "Identifiable", "View", "ViewModifier", "Sendable")


ENTRY_POINTS = ("main", "applicationDidFinishLaunching", "application:didFinishLaunchingWithOptions:")

# Vendored code: nothing local calls most of it, and it is not ours to delete.
VENDOR_PATTERNS = ("%/third-party/%", "%/third_party/%", "%/Vendor/%", "%/Pods/%",
                   "%/Carthage/%", "%/.build/%")


def candidates(db, kinds=None, module=None, include_tests=False, limit=200, offset=0,
               lang="Swift", include_vendor=False):
    """Symbols nothing in the indexed build reaches.

    Structural edges (CONTAINS, ACCESSOR_OF, EXTENDS) are ignored, since every member has
    a parent. Symbols that satisfy a protocol requirement or an IB outlet are excluded:
    those are reached by dynamic dispatch the index cannot attribute to a call site.
    """
    ks = list(kinds) if kinds else list(DEAD_KINDS)
    where = ["s.in_repo = 1", "s.ref_count <= 1",
             "s.name NOT LIKE 'getter:%'", "s.name NOT LIKE 'setter:%'",
             "s.kind IN (%s)" % ",".join("?" * len(ks))]
    args = list(ks)
    if module:
        where.append("s.module = ?")
        args.append(module)
    if lang and lang != "any":
        # ObjC methods are reached by selector, which no index can attribute to a call
        # site, so including them would drown the list in false positives.
        where.append("s.lang = ?")
        args.append(lang)
    if not include_vendor:
        for pat in VENDOR_PATTERNS:
            where.append("COALESCE(f.rel,'') NOT LIKE ?")
            args.append(pat)
    where.append("s.name NOT IN (%s)" % ",".join("?" * len(ENTRY_POINTS)))
    args += list(ENTRY_POINTS)
    if not include_tests:
        where.append("COALESCE(s.module,'') NOT LIKE '%_Tests'")
        where.append("COALESCE(f.rel,'') NOT LIKE '%/Tests/%'")
        where.append("COALESCE(f.rel,'') NOT LIKE '%/SnapshotTests/%'")
    where.append(f"""NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = s.usr_hash
                     AND e.kind IN ({DEAD_INBOUND}))""")
    where.append("""NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = s.usr_hash
                    AND e.kind IN ('OVERRIDES','IB_TYPE_OF'))""")
    where.append("s.name != 'CodingKeys'")
    where.append("""NOT EXISTS (
                     SELECT 1 FROM edges c
                     JOIN edges i ON i.src = c.src AND i.kind = 'INHERITS'
                     JOIN symbols proto ON proto.usr_hash = i.dst
                     WHERE c.kind = 'CONTAINS' AND c.dst = s.usr_hash
                       AND proto.name IN (%s))""" % ",".join("?" * len(SYNTHESIZED_PARENTS)))
    args += list(SYNTHESIZED_PARENTS)
    q = f"""SELECT s.name, s.kind, s.module, f.rel, s.def_line, s.usr
            FROM symbols s LEFT JOIN files f ON f.path_hash = s.def_path_hash
            WHERE {' AND '.join(where)}
            ORDER BY s.module, f.rel, s.def_line LIMIT ? OFFSET ?"""
    total = db.execute(f"""SELECT COUNT(*) c FROM symbols s
                           LEFT JOIN files f ON f.path_hash = s.def_path_hash
                           WHERE {' AND '.join(where)}""", args).fetchone()["c"]
    return total, db.execute(q, args + [limit, offset]).fetchall()


def mentions(root, name, own_file):
    """(files other than the definition site, count of extra mentions in that file).

    A same-file mention does not disprove deadness: with overloads, the call may resolve
    to a sibling. It is reported so a human can judge.
    """
    # Swift symbol names carry argument labels, e.g. map(input:) or subscript(_:), which
    # never appear as written at call sites. Search the bare identifier.
    needle = name.split("(", 1)[0].split(":", 1)[-1] if name.startswith(("getter:", "setter:")) \
        else name.split("(", 1)[0]
    if not needle:
        return [], 0
    r = subprocess.run(["rg", "-n", "-w", "--glob", "!bazel-*", "--glob", "*.swift",
                        "--glob", "*.m", "--glob", "*.h", "--", needle, "."],
                       cwd=root, capture_output=True, text=True)
    others, same = [], 0
    for line in r.stdout.splitlines():
        path = line.split(":", 1)[0].lstrip("./")
        if path == own_file:
            same += 1
        elif path not in others:
            others.append(path)
    return others, max(0, same - 1)


TEST_MODULE_GLOBS = ("*test*", "*spec*", "*snapshot*", "*mock*", "*fixture*")


def test_only(db, kinds=None, module=None, limit=200, offset=0):
    """Production symbols that something reaches, but only from test code: every inbound
    semantic edge comes from a symbol in a test module. A candidate list like `candidates`,
    bounded by the same coverage caveat: an uncompiled production caller is invisible."""
    kinds = kinds or DEAD_KINDS
    test_clause = " OR ".join("LOWER(c.module) GLOB ?" for _ in TEST_MODULE_GLOBS)
    own_test = " OR ".join("LOWER(s.module) GLOB ?" for _ in TEST_MODULE_GLOBS)
    where = [f"s.in_repo = 1", f"s.kind IN ({','.join('?' * len(kinds))})", f"NOT ({own_test})", "s.module IS NOT NULL"]
    args = list(kinds) + list(TEST_MODULE_GLOBS)
    if module:
        where.append("s.module = ?")
        args.append(module)
    sql = f"""
      WITH cand AS (
        SELECT s.usr_hash, s.name, s.kind, s.module, s.def_path_hash, s.def_line, s.call_in, s.in_deg
        FROM symbols s WHERE {' AND '.join(where)}
      )
      SELECT cand.name, cand.kind, cand.module, f.rel, cand.def_line, cand.call_in,
             (SELECT COUNT(DISTINCT c.module) FROM edges e JOIN symbols c ON c.usr_hash = e.src
              WHERE e.dst = cand.usr_hash AND e.kind IN ({DEAD_INBOUND})) AS test_modules
      FROM cand LEFT JOIN files f ON f.path_hash = cand.def_path_hash
      WHERE EXISTS (SELECT 1 FROM edges e JOIN symbols c ON c.usr_hash = e.src
                    WHERE e.dst = cand.usr_hash AND e.kind IN ({DEAD_INBOUND}) AND ({test_clause}))
        AND NOT EXISTS (SELECT 1 FROM edges e JOIN symbols c ON c.usr_hash = e.src
                        WHERE e.dst = cand.usr_hash AND e.kind IN ({DEAD_INBOUND})
                          AND c.usr_hash <> cand.usr_hash AND NOT ({test_clause}))
      ORDER BY cand.module, cand.name LIMIT ? OFFSET ?"""
    rows = db.execute(sql, args + list(TEST_MODULE_GLOBS) + list(TEST_MODULE_GLOBS) + [limit, offset]).fetchall()
    return rows
