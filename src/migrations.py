"""Long-running migrations, counted down to zero, plus the tickets that plan them.

A migration is something the team wants gone, measured one way, with an optional list of
Jira keys for the planned work. Its definition lives in the project's registry entry on this
machine (`idxg migrations add`) and stores an absolute start date, so the start never drifts.

Text in git (`imports`, `path`, `pattern`) is counted at the last first-parent commit before
each week boundary, so a folder that moved shows what a checkout of that week would show. The
commits that moved a count are read from one `git log -G` (or `--name-status` for a path),
each with what it added and removed. Starting from the first sample and adding every commit's
change has to land on the current count; `reconciled` records whether it does, and the done
date is only taken from commits when it does.

The code graph (`uses`, `inherits`) counts what the compiler resolved, in compiled files only.
The graph keeps no past, so those start on the day they are added and gain one count per graph
build, recorded with the checkout the build saw.

Tickets count as merged once a commit on the history branch names them in its subject or its
pull request title. Merged is what git can say; whether the ticket is closed is Jira's to say.
"""
import concurrent.futures, datetime, hashlib, json, os, re, sqlite3, subprocess

import history
import usage

SOURCE_SPECS = ("*.swift", "*.m", "*.mm", "*.h")
DEFAULT_WINDOW_DAYS = 365
GIT_KINDS = ("imports", "path", "pattern")
GRAPH_KINDS = ("uses", "inherits")
MEASURES = GIT_KINDS + GRAPH_KINDS
TYPE_KINDS = ("Class", "Struct", "Enum")
R_DECL, R_DEF, R_IMPLICIT = 1, 2, 256
TICKET_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY, spec TEXT, spec_hash TEXT, kind TEXT,
  since TEXT, start_sha TEXT, head_sha TEXT, synced_at TEXT, counted TEXT, graph_built TEXT);
CREATE TABLE IF NOT EXISTS migration_samples(name TEXT, day TEXT, sha TEXT, count INTEGER,
  ref TEXT, detail TEXT, PRIMARY KEY(name, day));
CREATE TABLE IF NOT EXISTS migration_commits(name TEXT, ord INTEGER, sha TEXT, day TEXT, subject TEXT,
  added INTEGER, removed INTEGER, PRIMARY KEY(name, sha));
CREATE TABLE IF NOT EXISTS migration_left(name TEXT, area TEXT, in_graph INTEGER, count INTEGER,
  tests INTEGER, files INTEGER);
"""

POSIX_CLASSES = {"space": r"\s", "blank": r" \t", "digit": "0-9", "alpha": "A-Za-z", "alnum": "A-Za-z0-9",
                 "upper": "A-Z", "lower": "a-z", "punct": r"!-/:-@\[-`{-~", "xdigit": "0-9A-Fa-f"}


# ---------------------------------------------------------------- definitions

def split_list(value):
    """Comma- or space-separated items from a string or a list of strings."""
    if not value:
        return []
    parts = [value] if isinstance(value, str) else list(value)
    return [x for p in parts for x in str(p).replace(",", " ").split() if x]


def _ticket_order(key):
    prefix, _, n = key.partition("-")
    return prefix, int(n)


def normalize(spec):
    """Validate a definition and fill its defaults: at most one measure, tickets optional, at
    least one of the two."""
    name = (spec.get("name") or "").strip()
    if not name:
        raise ValueError("a migration needs a name")
    kinds = [k for k in MEASURES if spec.get(k)]
    if len(kinds) > 1:
        raise ValueError("pass at most one of --imports, --path, --pattern, --uses, --inherits")
    raw = split_list(spec.get("tickets"))
    bad = [t for t in raw if not TICKET_KEY.match(t)]
    if bad:
        raise ValueError(f"not a ticket key: {', '.join(bad)}")
    tickets = sorted(set(raw), key=_ticket_order)
    if not kinds and not tickets:
        raise ValueError("pass something to count (--imports, --path, --pattern, --uses, --inherits), "
                         "--tickets, or both")
    out = {"name": name}
    k = kinds[0] if kinds else None
    if k in ("imports", "uses"):
        mods = split_list(spec[k])
        bad = [m for m in mods if not IDENT.match(m)]
        if bad:
            raise ValueError(f"not a module name: {', '.join(bad)}")
        out[k] = mods
    elif k == "inherits":
        types = split_list(spec[k])
        bad = [t for t in types if not all(IDENT.match(p) for p in t.split("."))]
        if bad:
            raise ValueError(f"not a type name: {', '.join(bad)}")
        out[k] = types
        if spec.get("inherits_usr"):
            out["inherits_usr"] = list(spec["inherits_usr"])
    elif k == "path":
        out["path"] = spec["path"].strip().strip("/")
    elif k == "pattern":
        re.compile(_ere_to_py(spec["pattern"]))
        out["pattern"] = spec["pattern"]
    if spec.get("files"):
        if k not in ("imports", "pattern"):
            raise ValueError("--files narrows --imports and --pattern only")
        out["files"] = list(spec["files"])
    if spec.get("in"):
        if k not in ("imports", "pattern", "uses", "inherits"):
            raise ValueError("--in limits --imports, --pattern, --uses and --inherits; a --path is its own scope")
        names, paths = split_list(spec["in"]), [x.strip("/") for x in (spec.get("in_paths") or split_list(spec["in"]))]
        pairs = list(dict(zip(paths, names)).items())
        out["in"], out["in_paths"] = [n for _, n in pairs], [f for f, _ in pairs]
        if any("/" in f for f in out.get("files", []) if not f.startswith(":(exclude") and not f.startswith(":!")):
            raise ValueError("with --in, --files takes file name globs such as *.swift")
    today = datetime.date.today()
    default = today if k in GRAPH_KINDS or not k else today - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    since = spec.get("since") or default.isoformat()
    datetime.date.fromisoformat(since)
    out["since"] = since
    if tickets:
        out["tickets"] = tickets
    if spec.get("epic"):
        if not TICKET_KEY.match(spec["epic"]):
            raise ValueError(f"not a ticket key: {spec['epic']}")
        out["epic"] = spec["epic"]
    return out


def kind_of(spec):
    return next((k for k in MEASURES if spec.get(k)), "tickets")


def unit(spec):
    return {"imports": "imports", "path": "files", "pattern": "matching lines", "uses": "uses",
            "inherits": "types", "tickets": "tickets"}[kind_of(spec)]


def describe(spec):
    text = _describe(spec)
    if spec.get("in"):
        shown = [n if n == p else f"{n} ({p}/)" for n, p in zip(spec["in"], spec["in_paths"])]
        text += f", only in {', '.join(shown)}"
    return text


def _describe(spec):
    k = kind_of(spec)
    where = ", ".join(spec.get("files") or SOURCE_SPECS)
    if k == "imports":
        return f"import lines naming {', '.join(spec['imports'])} in {where}"
    if k == "path":
        return f"tracked files under {spec['path']}/"
    if k == "pattern":
        return f"lines matching /{spec['pattern']}/ in {where}"
    if k == "uses":
        return (f"lines of compiled code outside {', '.join(spec['uses'])} that name one of their symbols, "
                f"as the compiler resolved them (written uses only, not what it implied)")
    if k == "inherits":
        return f"types that inherit from or conform to {', '.join(spec['inherits'])}, directly or through other types"
    return describe_tickets(spec)


def describe_tickets(spec):
    epic = f" of {spec['epic']}" if spec.get("epic") else ""
    return (f"{_n(len(spec['tickets']), 'ticket')}{epic}; a ticket counts as merged once a commit on the history "
            f"branch names it in its subject or its pull request title")


def measure_hash(spec):
    """What the counts depend on; tickets and the epic can change without losing them."""
    keep = {k: v for k, v in spec.items() if k not in ("name", "tickets", "epic")}
    return hashlib.blake2b(json.dumps(keep, sort_keys=True).encode(), digest_size=8).hexdigest()


def _ere_to_py(pattern):
    """git's -G and grep -E read POSIX extended regexes; Python has no [[:space:]] classes."""
    return re.sub(r"\[:([a-z]+):\]", lambda m: POSIX_CLASSES.get(m.group(1), m.group(0)), pattern)


def _import_ere(mods):
    alt = "|".join(mods)
    swift = (r"^[[:space:]]*(@[A-Za-z_]+(\([^)]*\))?[[:space:]]+)*"
             r"((public|package|internal|fileprivate|private)[[:space:]]+)?import[[:space:]]+"
             r"((typealias|struct|class|enum|protocol|let|var|func)[[:space:]]+)?(" + alt + r")([[:space:].;]|$)")
    objc = r"^[[:space:]]*@import[[:space:]]+(" + alt + r")([[:space:].;]|$)"
    header = r"^[[:space:]]*#(import|include)[[:space:]]+<(" + alt + r")/"
    return "|".join((swift, objc, header))


def regex(spec):
    """(POSIX ERE for git, compiled Python regex for diff lines), or (None, None) for a path."""
    k = kind_of(spec)
    if k == "path":
        return None, None
    ere = _import_ere(spec["imports"]) if k == "imports" else spec["pattern"]
    return ere, re.compile(_ere_to_py(ere))


def pathspecs(spec):
    if spec.get("path"):
        return [spec["path"]]
    specs = list(spec.get("files") or SOURCE_SPECS)
    folders = spec.get("in_paths") or []
    if not folders:
        return specs
    # git cannot intersect two pathspecs, so each name glob is rewritten under each folder.
    excludes = [p for p in specs if p.startswith(":(exclude") or p.startswith(":!")]
    names = [p for p in specs if p not in excludes]
    return [f":(glob){f}/**/{p}" for f in folders for p in names] + excludes


def in_scope(spec, rel):
    folders = spec.get("in_paths")
    return not folders or any(rel == f or rel.startswith(f + "/") for f in folders)


def resolve_scope(root, ref, graph_db, areas):
    """[(area as given, folder)]: a module the graph knows maps to the directory its files share,
    anything else has to be a directory tracked on `ref`."""
    prefixes = history.module_prefixes(graph_db) if graph_db and os.path.exists(graph_db) else {}
    lower = {}
    for m in prefixes:
        lower.setdefault(m.lower(), []).append(m)
    out = []
    for a in split_list(areas):
        a = a.strip("/")
        if a in prefixes:
            out.append((a, prefixes[a]))
        elif len(lower.get(a.lower(), [])) == 1:
            m = lower[a.lower()][0]
            out.append((m, prefixes[m]))
        elif _git(root, ["ls-tree", "-d", "--name-only", ref, "--", a], ok=(0, 128)).strip() == a:
            out.append((a, a))
        else:
            raise SystemExit(f"{a} is neither a module the graph knows nor a directory on {ref}")
    return out


# ---------------------------------------------------------------- git

def _git(root, args, ok=(0,)):
    r = subprocess.run(["git", "-C", root, "-c", "core.quotepath=false"] + args, capture_output=True,
                       text=True, errors="replace")
    if r.returncode not in ok:
        raise SystemExit(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def breakdown_at(root, sha, spec):
    """{path: count} at one commit."""
    if kind_of(spec) == "path":
        return {p: 1 for p in _git(root, ["ls-tree", "-r", "--name-only", sha, "--"] + pathspecs(spec)).splitlines() if p}
    ere, _ = regex(spec)
    out = {}
    # git grep exits 1 when nothing matches.
    for line in _git(root, ["grep", "-c", "-I", "-E", "-e", ere, sha, "--"] + pathspecs(spec), ok=(0, 1)).splitlines():
        path, _, n = line[len(sha) + 1:].rpartition(":")
        if path and n.isdigit():
            out[path] = int(n)
    return out


def count_at(root, sha, spec):
    return sum(breakdown_at(root, sha, spec).values())


def _utc(iso):
    # git writes Z for commits made in UTC, which fromisoformat accepts only from 3.11.
    iso = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc)


def week_points(root, branch, since, head):
    """([(day, sha)], {sha: order}): the first-parent commit just before `since` and before every
    Monday after it (UTC midnight), then the head on its own day; and the first-parent commits
    from the start to the head, numbered oldest first. sha is None before the branch's first commit."""
    start = datetime.datetime.combine(datetime.date.fromisoformat(since), datetime.time(), datetime.timezone.utc)
    floor = (start - datetime.timedelta(days=60)).date().isoformat()
    log = [l.split("\x1f") for l in _git(root, ["log", "--first-parent", "--format=%H\x1f%cI", f"--since={floor}",
                                                head]).splitlines() if l]
    commits = [(sha, _utc(iso)) for sha, iso in log]
    # Outside the 60-day floor, ask git for the one commit before the start.
    before_start = next((sha for sha, t in commits if t < start), None)
    if before_start is None:
        before_start = _git(root, ["rev-list", "-1", "--first-parent", f"--before={start.isoformat()}", head]).strip() or None
    head_day = _utc(_git(root, ["log", "-1", "--format=%cI", head]).strip()).date().isoformat()
    points = [(since, before_start)]
    b = start + datetime.timedelta(days=(7 - start.weekday()) % 7 or 7)
    now = datetime.datetime.now(datetime.timezone.utc)
    while b <= now:
        day = b.date().isoformat()
        if day >= head_day:
            break
        points.append((day, next((sha for sha, t in commits if t < b), before_start)))
        b += datetime.timedelta(days=7)
    if head_day > since:
        points.append((head_day, head))
    order = {sha: len(commits) - i for i, (sha, _) in enumerate(commits)}
    if before_start and before_start not in order:
        order[before_start] = 0
    return points, order


def derive_counts(points, order, start_count, moved):
    """The count at every point from the start count plus each commit's change, or None when a
    commit or a point falls outside the first-parent order."""
    deltas = []
    for sha, added, removed in moved:
        if sha not in order:
            return None
        deltas.append((order[sha], added - removed))
    deltas.sort()
    out = []
    for _, sha in points:
        if sha is None:
            out.append(0)
            continue
        if sha not in order:
            return None
        out.append(start_count + sum(d for o, d in deltas if o <= order[sha]))
    return out


def scan_commits(root, revrange, spec):
    """[(sha, day, subject, added, removed)] oldest first, for first-parent commits that changed
    the count. revrange is `a..b`, or a single sha for everything reachable."""
    fmt = "\x01%H\x1f%cI\x1f%s"
    base = ["log", revrange, "--first-parent", "-m", "--no-color", "--no-ext-diff", f"--format={fmt}"]
    k = kind_of(spec)
    if k == "path":
        text = _git(root, base + ["--no-renames", "--name-status", "--"] + pathspecs(spec))
    else:
        ere, rx = regex(spec)
        text = _git(root, base + ["-G" + ere, "-p", "-U0", "--"] + pathspecs(spec))
    out = []
    for rec in text.split("\x01")[1:]:
        head, _, body = rec.partition("\n")
        sha, iso, subject = (head.split("\x1f") + ["", ""])[:3]
        added = removed = 0
        if k == "path":
            for line in body.splitlines():
                st = line[:1]
                if st == "A":
                    added += 1
                elif st == "D":
                    removed += 1
        else:
            in_hunk = False
            for line in body.splitlines():
                if line.startswith("diff --git "):
                    in_hunk = False
                elif line.startswith("@@"):
                    in_hunk = True
                elif in_hunk and line[:1] == "+" and rx.search(line[1:]):
                    added += 1
                elif in_hunk and line[:1] == "-" and rx.search(line[1:]):
                    removed += 1
        if added != removed:
            out.append((sha, _utc(iso).date().isoformat(), subject, added, removed))
    out.reverse()
    return out


def _is_ancestor(root, a, b):
    return subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------- code graph

def _graph(graph_db):
    return sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)


def _marks(seq):
    return ",".join("?" * len(seq))


def _chunks(seq, n=500):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def usr_module(usr, module):
    """The module a symbol belongs to. A Swift symbol read from an SDK interface has none in the
    graph but its USR names it; an Objective-C one from the SDK names none at all ('')."""
    if module:
        return module
    usr = usr or ""
    m = re.match(r"^s:(\d+)", usr)
    if m:
        return usr[m.end():m.end() + int(m.group(1))]
    m = re.match(r"^c:@M@([^@]+)@", usr)
    return m.group(1) if m else ""


def resolve_types(graph_db, names):
    """[(name as shown, usr)] for each name, or SystemExit listing the candidates when a name could
    mean several types: a migration must not count the wrong one."""
    g = _graph(graph_db)
    out = []
    try:
        for n in names:
            mod, _, base = n.rpartition(".")
            rows = g.execute("SELECT usr, module, kind, in_repo FROM symbols WHERE name = ? AND kind IN "
                             "('Class','Struct','Enum','Protocol')", (base,)).fetchall()
            if mod:
                # An SDK Objective-C class carries no module, so UIKit.UIViewController still finds it.
                rows = [r for r in rows if usr_module(r[0], r[1]) == mod or (not usr_module(r[0], r[1]) and not r[3])]
            if not rows:
                raise SystemExit(f"no class, struct, enum or protocol named {n} in the graph")
            if len(rows) > 1:
                seen = {}
                for u, m, k, r in rows:
                    label = f"{usr_module(u, m) or '(no module)'}.{base}  ({k}, {'in the repo' if r else 'outside the repo'})"
                    seen[label] = seen.get(label, 0) + 1
                cands = "\n".join(f"  {lab}" + (f"  x{c}, nested in different types" if c > 1 else "")
                                  for lab, c in sorted(seen.items()))
                raise SystemExit(f"{n} could mean {len(rows)} types; pass one as Module.Name:\n{cands}")
            m0 = usr_module(rows[0][0], rows[0][1])
            out.append((f"{m0}.{base}" if m0 else base, rows[0][0]))
    finally:
        g.close()
    return out


def _module_targets(g, mods):
    hashes = {r[0] for r in g.execute(f"SELECT usr_hash FROM symbols WHERE module IN ({_marks(mods)})", mods)}
    for m in mods:
        hashes.update(r[0] for r in g.execute(
            "SELECT usr_hash FROM symbols WHERE (module IS NULL OR module = '') AND usr GLOB ?", (f"s:{len(m)}{m}*",)))
    return hashes


def graph_uses(g, mods, mapper, scope=None):
    """(lines where code outside `mods` names one of their symbols, detail, areas).

    Occurrences rather than edges, because only occurrences say what the compiler implied:
    RxSwift's `extension NSObject: ReactiveCompatible` gives every NSObject subclass an `rx`
    member nobody wrote, which on Wallapop was 14,493 of the edges into RxSwift."""
    rels = dict(g.execute("SELECT path_hash, rel FROM files WHERE in_repo = 1 AND rel IS NOT NULL"))
    fmod = dict(g.execute("SELECT path_hash, module FROM files WHERE in_repo = 1"))
    skip, lines, per, occurrences = set(mods), set(), {}, 0
    for chunk in _chunks(_module_targets(g, mods)):
        for ph, line, roles in g.execute(
                f"SELECT path_hash, line, roles FROM occurrences WHERE usr_hash IN ({_marks(chunk)})", chunk):
            rel = rels.get(ph)
            if rel is None or fmod.get(ph) in skip or roles & (R_DECL | R_DEF | R_IMPLICIT) or (scope and not scope(rel)):
                continue
            occurrences += 1
            if (ph, line) in lines:
                continue
            lines.add((ph, line))
            module = fmod.get(ph)
            a = per.setdefault(_area(mapper, rel, module), [0, 0, set()])
            a[0] += 1
            a[1] += 1 if usage.is_test(rel, module) else 0
            a[2].add(ph)
    areas = {k: [v[0], v[1], len(v[2])] for k, v in per.items()}
    files = {ph for ph, _ in lines}
    return len(lines), {"files": len(files), "occurrences": occurrences}, areas


def graph_inherits(g, usrs, mapper, scope=None):
    """(in-repo classes, structs and enums that inherit from or conform to `usrs`, through any
    chain of other types, detail, areas). A conformance declared in an extension belongs to the
    type the extension extends."""
    rels = dict(g.execute("SELECT path_hash, rel FROM files WHERE rel IS NOT NULL"))
    start = [r[0] for r in g.execute(f"SELECT usr_hash FROM symbols WHERE usr IN ({_marks(usrs)})", usrs)]
    seen, frontier, found, direct = set(start), list(start), {}, set()
    first = True
    while frontier:
        nxt, extensions = [], []
        for chunk in _chunks(frontier):
            for src, kind, in_repo, module, def_ph in g.execute(
                    f"""SELECT DISTINCT s.usr_hash, s.kind, s.in_repo, s.module, s.def_path_hash FROM edges e
                        JOIN symbols s ON s.usr_hash = e.src WHERE e.dst IN ({_marks(chunk)}) AND e.kind = 'INHERITS'""",
                    chunk):
                if src in seen:
                    continue
                seen.add(src)
                nxt.append(src)
                if kind == "Extension":
                    extensions.append(src)
                elif kind in TYPE_KINDS and in_repo:
                    found[src] = (module, def_ph)
                    if first:
                        direct.add(src)
        for chunk in _chunks(extensions):
            for t, kind, in_repo, module, def_ph in g.execute(
                    f"""SELECT DISTINCT s.usr_hash, s.kind, s.in_repo, s.module, s.def_path_hash FROM edges e
                        JOIN symbols s ON s.usr_hash = e.dst WHERE e.src IN ({_marks(chunk)}) AND e.kind = 'EXTENDS'""",
                    chunk):
                if t in seen:
                    continue
                seen.add(t)
                nxt.append(t)
                if kind in TYPE_KINDS and in_repo:
                    found[t] = (module, def_ph)
                    if first:
                        direct.add(t)
        frontier, first = nxt, False
    if scope:
        found = {k: v for k, v in found.items() if scope(rels.get(v[1]) or "")}
        direct &= set(found)
    per = {}
    for module, def_ph in found.values():
        rel = rels.get(def_ph) or ""
        a = per.setdefault(_area(mapper, rel, module), [0, 0, set()])
        a[0] += 1
        a[1] += 1 if usage.is_test(rel, module) else 0
        a[2].add(def_ph)
    areas = {k: [v[0], v[1], len(v[2])] for k, v in per.items()}
    files = {ph for _, ph in found.values()}
    return len(found), {"files": len(files), "direct": len(direct)}, areas


def _scope(spec):
    return (lambda rel: in_scope(spec, rel)) if spec.get("in_paths") else None


def path_areas(counts, mapper):
    """{(area, in graph): [count, in test files, files]} from {path: count}."""
    areas = {}
    for path, n in counts.items():
        key = _area(mapper, path)
        a = areas.setdefault(key, [0, 0, 0])
        a[0] += n
        a[1] += n if usage.is_test(path, mapper.map(path)[0] if mapper else None) else 0
        a[2] += 1
    return areas


def _area(mapper, rel, module=None):
    """(area, 1 when it is a module the graph knows, 0 for a plain directory)."""
    mod, component = mapper.map(rel) if mapper and rel else (None, None)
    if mod:
        return mod, 1
    if module and not rel:
        return module, 1
    return component or (rel.split("/")[0] if rel else module or "?"), 0


# ---------------------------------------------------------------- tickets

def ticket_status(db, keys):
    """Per ticket key, the first-parent commits that name it in their subject or PR title."""
    cols = _cols(db, "commits")
    title = "pr_title" if "pr_title" in cols else "NULL"
    release = "release" if "release" in cols else "NULL"
    out = []
    for key in keys:
        rows = db.execute(f"""SELECT sha, day, subject, pr, author, {release}, {title} FROM commits
                              WHERE tickets GLOB ? OR {title} GLOB ? ORDER BY day, seq DESC""",
                          (f"*{key}*", f"*{key}*")).fetchall()
        hits = [r for r in rows if key in history.TICKET_RE.findall(f"{r[2]} {r[6] or ''}")]
        out.append({"key": key, "commits": len(hits),
                    "first_day": hits[0][1] if hits else None, "last_day": hits[-1][1] if hits else None,
                    "first_sha": hits[0][0][:11] if hits else None,
                    "prs": [r[3] or history.pr_number(r[2]) for r in hits if r[3] or history.pr_number(r[2])],
                    "release": hits[0][5] if hits else None, "subject": hits[0][2] if hits else None})
    return out


def ticket_summary(db, spec):
    items = ticket_status(db, spec["tickets"])
    merged = [t for t in items if t["commits"]]
    total = len(items)
    last = max(merged, key=lambda t: t["first_day"]) if merged else None
    return {"total": total, "merged": len(merged),
            "progress": round(100.0 * len(merged) / total, 1) if total else None,
            "done_day": last["first_day"] if last and len(merged) == total else None,
            "latest_day": max(t["last_day"] for t in merged) if merged else None,
            "last": last, "epic": spec.get("epic"), "items": items}


def _n(n, word):
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def ticket_line(t):
    if t["merged"] == t["total"]:
        return (f"{'the ticket has' if t['total'] == 1 else 'all ' + str(t['total']) + ' tickets have'} a merged commit; the last to get one was "
                f"{t['last']['key']} on {t['done_day']}")
    if not t["merged"]:
        return f"{'the ticket has' if t['total'] == 1 else 'none of the ' + str(t['total']) + ' tickets has'} no merged commit yet"
    return f"{t['merged']} of {t['total']} tickets have a merged commit ({t['progress']:g}%), the latest on {t['latest_day']}"


# ---------------------------------------------------------------- sync

def _cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def ensure_schema(db):
    db.executescript(SCHEMA)
    for table, col in (("migrations", "graph_built"), ("migration_samples", "ref"), ("migration_samples", "detail")):
        if col not in _cols(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def sync(db, root, branch, head, mapper, specs, log=print, jobs=6, graph_db=None):
    """Bring every configured migration up to `head` and the current graph; drop the rows of
    removed ones. A definition that no longer parses is skipped with a line in the log, so one
    bad entry never stops a history build."""
    ensure_schema(db)
    good = []
    for s in specs:
        try:
            good.append(normalize(s))
        except (ValueError, re.error) as e:
            log(f"  migration {s.get('name')!r} skipped: {e}")
    names = {s["name"] for s in good}
    for (name,) in db.execute("SELECT name FROM migrations").fetchall():
        if name not in names:
            _wipe(db, name, everything=True)
    for spec in good:
        row = db.execute("SELECT spec_hash, head_sha, start_sha, counted, graph_built FROM migrations WHERE name = ?",
                         (spec["name"],)).fetchone()
        if row and row[0] != measure_hash(spec):
            _wipe(db, spec["name"], everything=True)
            row = None
        kind = kind_of(spec)
        if kind in GIT_KINDS:
            if not (row and row[1] == head):
                _sync_git(db, root, branch, head, mapper, spec, row, log, jobs)
        elif kind in GRAPH_KINDS:
            _sync_graph(db, root, mapper, spec, row, graph_db, log)
        else:
            _save(db, spec, head_sha=head, counted="tickets")
        # The stored definition follows the config even when nothing needed recounting, since
        # tickets and the epic can change without touching the counts.
        _save(db, spec)
        db.commit()
    db.commit()


def _save(db, spec, **cols):
    vals = {"spec": json.dumps(spec), "spec_hash": measure_hash(spec), "kind": kind_of(spec),
            "since": spec["since"], "synced_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), **cols}
    if db.execute("SELECT 1 FROM migrations WHERE name = ?", (spec["name"],)).fetchone():
        db.execute(f"UPDATE migrations SET {', '.join(k + ' = ?' for k in vals)} WHERE name = ?",
                   list(vals.values()) + [spec["name"]])
    else:
        vals["name"] = spec["name"]
        db.execute(f"INSERT INTO migrations({', '.join(vals)}) VALUES({_marks(vals)})", list(vals.values()))


def _sync_git(db, root, branch, head, mapper, spec, row, log, jobs):
    name = spec["name"]
    points, order = week_points(root, branch, spec["since"], head)
    start_sha = points[0][1]
    if row and row[1] and row[2] == start_sha and _is_ancestor(root, row[1], head):
        revrange = f"{row[1]}..{head}"
    else:
        db.execute("DELETE FROM migration_commits WHERE name = ?", (name,))
        revrange = f"{start_sha}..{head}" if start_sha else head
    ord0 = db.execute("SELECT COALESCE(MAX(ord), 0) FROM migration_commits WHERE name = ?", (name,)).fetchone()[0]
    moved = scan_commits(root, revrange, spec)
    db.executemany("INSERT OR REPLACE INTO migration_commits VALUES(?,?,?,?,?,?,?)",
                   [(name, ord0 + i + 1, *c) for i, c in enumerate(moved)])

    known = {d: (s, n) for d, s, n in db.execute(
        "SELECT day, sha, count FROM migration_samples WHERE name = ?", (name,))}
    if start_sha is None:
        start_count = 0
    elif known.get(spec["since"], (None, 0))[0] == start_sha:
        start_count = known[spec["since"]][1]
    else:
        start_count = count_at(root, start_sha, spec)
    here = breakdown_at(root, head, spec)
    head_count = sum(here.values())
    all_moved = db.execute("SELECT sha, added, removed FROM migration_commits WHERE name = ?", (name,)).fetchall()
    counts = derive_counts(points, order, start_count, all_moved)
    counted = "commits"
    if counts is None or counts[-1] != head_count:
        # The commits do not explain the change, so count every week from the tree instead.
        counted = "weekly"
        reuse = bool(row) and row[3] == "weekly"
        fixed = {start_sha: start_count, head: head_count, None: 0}

        def at(point):
            d, s = point
            if s in fixed:
                return fixed[s]
            if reuse and known.get(d, (None,))[0] == s:
                return known[d][1]
            return count_at(root, s, spec)
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            counts = list(pool.map(at, points))
    db.execute("DELETE FROM migration_samples WHERE name = ?", (name,))
    db.executemany("INSERT INTO migration_samples(name, day, sha, count) VALUES(?,?,?,?)",
                   [(name, d, s, n) for (d, s), n in zip(points, counts)])

    db.execute("DELETE FROM migration_left WHERE name = ?", (name,))
    areas = path_areas(here, mapper)
    db.executemany("INSERT INTO migration_left VALUES(?,?,?,?,?,?)",
                   [(name, k[0], k[1], *v) for k, v in areas.items()])
    _save(db, spec, start_sha=start_sha, head_sha=head, counted=counted)
    log(f"  migration {name}: {len(moved)} new commits, {head_count:,} {unit(spec)} left"
        + ("" if counted == "commits" else ", counted week by week because the commits do not add up"))


def _sync_graph(db, root, mapper, spec, row, graph_db, log):
    name = spec["name"]
    if not graph_db or not os.path.exists(graph_db):
        _save(db, spec, counted="graph")
        log(f"  migration {name}: no code graph to count from yet")
        return
    g = _graph(graph_db)
    try:
        gm = dict(g.execute("SELECT key, value FROM meta"))
        built = gm.get("built_at") or ""
        if row and row[4] == built:
            return
        if kind_of(spec) == "uses":
            count, detail, areas = graph_uses(g, spec["uses"], mapper, _scope(spec))
        else:
            try:
                usrs = spec.get("inherits_usr") or [u for _, u in resolve_types(graph_db, spec["inherits"])]
            except SystemExit as e:
                log(f"  migration {name} skipped: {e}")
                return
            count, detail, areas = graph_inherits(g, usrs, mapper, _scope(spec))
    finally:
        g.close()
    detail["coverage"] = [int(gm.get("coverage_covered") or 0), int(gm.get("coverage_tracked") or 0)]
    sha = _git(root, ["rev-parse", "HEAD"], ok=(0, 128)).strip()
    ref = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], ok=(0, 128)).strip()
    day = built[:10] or datetime.date.today().isoformat()
    db.execute("INSERT OR REPLACE INTO migration_samples(name, day, sha, count, ref, detail) VALUES(?,?,?,?,?,?)",
               (name, day, sha, count, ref, json.dumps(detail)))
    db.execute("DELETE FROM migration_left WHERE name = ?", (name,))
    db.executemany("INSERT INTO migration_left VALUES(?,?,?,?,?,?)",
                   [(name, k[0], k[1], *v) for k, v in areas.items()])
    _save(db, spec, head_sha=sha, graph_built=built, counted="graph")
    log(f"  migration {name}: {count:,} {unit(spec)} in the graph built {built.replace('T', ' ')}")


def _wipe(db, name, everything=False):
    for t in ("migration_samples", "migration_commits", "migration_left") + (("migrations",) if everything else ()):
        db.execute(f"DELETE FROM {t} WHERE name = ?", (name,))


def _writer(hdb_path, work):
    db = sqlite3.connect(hdb_path)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        ensure_schema(db)
        work(db)
        db.commit()
    finally:
        db.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(hdb_path + suffix):
                os.remove(hdb_path + suffix)


def refresh(hdb_path, root, branch, graph_db, specs, log=print):
    """Sync migrations alone, outside a history build (after `idxg migrations add`)."""
    head = history.git(root, "rev-parse", branch + "^{commit}").strip()
    mapper = history.PathMapper(history.module_prefixes(graph_db))
    _writer(hdb_path, lambda db: sync(db, root, branch, head, mapper, specs, log=log, graph_db=graph_db))


def preview(root, branch, graph_db, hdb_path, spec, left=8):
    """Today's numbers for a definition without saving it: the start and current count for a git
    measure, the current count for a graph one, the ticket status. Shown before tracking, so
    whoever picked the measure can check it counts what they meant."""
    kind = kind_of(spec)
    mapper = history.PathMapper(history.module_prefixes(graph_db))
    u = unit(spec)
    lines = [f"would track {spec['name']}: {describe(spec)}"]
    areas = {}
    if kind in GIT_KINDS:
        head = history.git(root, "rev-parse", branch + "^{commit}").strip()
        points, _ = week_points(root, branch, spec["since"], head)
        start = count_at(root, points[0][1], spec) if points[0][1] else 0
        here = breakdown_at(root, head, spec)
        now = sum(here.values())
        areas = path_areas(here, mapper)
        lines.append(f"  {start:,} {u} on {spec['since']}, {now:,} now on {branch} ({_signed(now - start)}), "
                     f"in {_n(len(here), 'file')}")
    elif kind in GRAPH_KINDS:
        if not graph_db or not os.path.exists(graph_db):
            raise SystemExit("no code graph to count from; idxg-build first")
        g = _graph(graph_db)
        try:
            built = dict(g.execute("SELECT key, value FROM meta")).get("built_at", "")
            if kind == "uses":
                now, detail, areas = graph_uses(g, spec["uses"], mapper, _scope(spec))
                extra = f"{detail['occurrences']:,} occurrences"
            else:
                usrs = spec.get("inherits_usr") or [x for _, x in resolve_types(graph_db, spec["inherits"])]
                now, detail, areas = graph_inherits(g, usrs, mapper, _scope(spec))
                extra = f"{detail['direct']:,} of them directly"
        finally:
            g.close()
        ref = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], ok=(0, 128)).strip()
        lines.append(f"  {now:,} {u} in {_n(detail['files'], 'file')} ({extra}), in the graph built "
                     f"{built.replace('T', ' ')} from {ref}; the line starts with this count")
    if spec.get("tickets"):
        db = history.connect(hdb_path)
        try:
            t = ticket_summary(db, spec)
        finally:
            db.close()
        lines.append(f"  tickets: {ticket_line(t)}")
        missing = [x["key"] for x in t["items"] if not x["commits"]]
        if missing:
            lines.append(f"    without a merged commit: {', '.join(missing[:12])}")
    if areas:
        top = sorted(areas.items(), key=lambda kv: -kv[1][0])
        lines.append(f"  where, largest first ({_n(len(top), 'area')}):")
        for (area, in_graph), (n, tests, files) in top[:left]:
            lines.append(f"    {n:>7,}  {area}{'' if in_graph else '/'}  (in {_n(files, 'file')}, {tests:,} in test files)")
    lines.append("  nothing is saved yet: track_migration with dry_run false (idxg migrations add without "
                 "--dry-run) tracks it")
    return "\n".join(lines)


def drop(hdb_path, name):
    _writer(hdb_path, lambda db: _wipe(db, name, everything=True))


# ---------------------------------------------------------------- reading

def has_tables(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'migrations'").fetchone() is not None


def names(db):
    if not has_tables(db):
        return []
    return [r[0] for r in db.execute("SELECT name FROM migrations ORDER BY name COLLATE NOCASE")]


def find(db, wanted):
    """The one migration a name, a case-insensitive name or a unique substring picks."""
    all_ = names(db)
    for test in (lambda n: n == wanted, lambda n: n.lower() == wanted.lower(),
                 lambda n: wanted.lower() in n.lower()):
        hits = [n for n in all_ if test(n)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SystemExit(f"{wanted!r} matches {len(hits)} migrations: {', '.join(hits)}")
    raise SystemExit(f"no migration named {wanted!r}" + (f"; tracked: {', '.join(all_)}" if all_ else
                     "; add one with idxg migrations add"))


def _ticket_samples(t, since):
    """Tickets still without a merged commit, after each first merge."""
    days = sorted(x["first_day"] for x in t["items"] if x["first_day"])
    if not days:
        return [[since, t["total"]], [datetime.date.today().isoformat(), t["total"]]]
    start = (datetime.date.fromisoformat(min(days)) - datetime.timedelta(days=1)).isoformat()
    points, left = {start: t["total"]}, t["total"]
    for d in days:
        left -= 1
        points[d] = left
    today = datetime.date.today().isoformat()
    points.setdefault(today, left)
    return [[d, n] for d, n in sorted(points.items())]


def summary(db, name, commits_limit=None, left_limit=None):
    """Every number the tab and the text report show, computed from the stored rows."""
    spec_json, since, head_sha, synced, counted = db.execute(
        "SELECT spec, since, head_sha, synced_at, counted FROM migrations WHERE name = ?", (name,)).fetchone()
    spec = json.loads(spec_json)
    kind = kind_of(spec)
    has_commits = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'commits'").fetchone() is not None
    branch = dict(db.execute("SELECT key, value FROM meta WHERE key = 'branch'")).get("branch") or "main" \
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() else "main"
    tickets = ticket_summary(db, spec) if spec.get("tickets") and has_commits else None
    scols = _cols(db, "migration_samples")
    pick = ", ".join(c if c in scols else "NULL" for c in ("ref", "detail"))
    raw = db.execute(f"SELECT day, count, sha, {pick} FROM migration_samples WHERE name = ? ORDER BY day",
                     (name,)).fetchall()
    samples = [[r[0], r[1]] for r in raw]
    if kind == "tickets" and tickets:
        samples = _ticket_samples(tickets, since)
    rows = []
    if kind in GIT_KINDS:
        extra = "c.pr, c.author, c.release" if has_commits else "NULL, NULL, NULL"
        join = "LEFT JOIN commits c ON c.sha = m.sha" if has_commits else ""
        rows = db.execute(f"""SELECT m.sha, m.day, m.subject, m.added, m.removed, {extra}
                              FROM migration_commits m {join} WHERE m.name = ? ORDER BY m.ord""", (name,)).fetchall()
    start = samples[0][1] if samples else 0
    now = samples[-1][1] if samples else 0
    running, done_at, reached = start, None, None
    for sha, day, subject, added, removed, pr, author, release in rows:
        running += added - removed
        if running == 0 and reached is None:
            reached = (sha, day, subject, pr)
        elif running != 0:
            reached = None
    reconciled = running == now if kind in GIT_KINDS else True
    peak = max(samples, key=lambda x: x[1]) if samples else [since, 0]
    first_day = samples[0][0] if samples else since
    # A path that did not exist yet on the start date (a folder moved in later) is measured
    # from its peak instead, or it would read as nothing to remove.
    base, base_day = (start, since if kind in GIT_KINDS else first_day) if start else (peak[1], peak[0])
    if now == 0 and base > 0:
        if kind == "tickets" and tickets and tickets["last"]:
            last = tickets["last"]
            done_at = {"sha": last["first_sha"], "day": tickets["done_day"], "subject": last["subject"],
                       "pr": last["prs"][0] if last["prs"] else None}
        elif reconciled and reached:
            done_at = {"sha": reached[0][:11], "day": reached[1], "subject": reached[2], "pr": reached[3]}
        else:
            first_zero = next((d for i, (d, n) in enumerate(samples) if all(x == 0 for _, x in samples[i:])), None)
            done_at = {"sha": None, "day": first_zero, "subject": None, "pr": None}

    def change_over(weeks):
        if not samples:
            return None
        cut = (datetime.date.fromisoformat(samples[-1][0]) - datetime.timedelta(weeks=weeks)).isoformat()
        past = [n for d, n in samples if d <= cut]
        return (now - past[-1]) if past else None

    removing = [r for r in rows if r[3] < r[4]]
    adding = [r for r in rows if r[3] > r[4]]
    commits = [{"sha": r[0][:11], "day": r[1], "subject": r[2], "change": r[3] - r[4], "added": r[3],
                "removed": r[4], "pr": r[5] or history.pr_number(r[2] or ""), "author": r[6], "release": r[7]}
               for r in reversed(rows)]
    left = [{"area": a, "in_graph": bool(g), "count": n, "tests": t, "files": f} for a, g, n, t, f in db.execute(
        "SELECT area, in_graph, count, tests, files FROM migration_left WHERE name = ? ORDER BY count DESC, area",
        (name,))]
    what = describe(spec)
    synced_txt = (synced or "").replace("T", " ")
    notes, graph = [], None
    if kind in GIT_KINDS:
        basis = f"counts {what} on {branch}, once a week since {since}; as of {(head_sha or '')[:11]}, read {synced_txt}"
        if not reconciled:
            notes.append("the commits do not add up to the weekly counts (a pattern git and Python read differently, "
                         "or a merge diff), so the commit list is incomplete; the weekly counts come from the tree")
    elif kind in GRAPH_KINDS:
        detail = json.loads(raw[-1][4]) if raw and raw[-1][4] else {}
        covered, tracked = (detail.get("coverage") or [0, 0])
        ref = raw[-1][3] if raw else None
        graph = {"ref": ref, "sha": (raw[-1][2] or "")[:11] if raw else None, "day": samples[-1][0] if samples else None,
                 "files": detail.get("files"), "occurrences": detail.get("occurrences"),
                 "direct": detail.get("direct"),
                 "coverage_pct": round(100.0 * covered / tracked, 1) if tracked else None}
        basis = (f"counts {what}, once per graph build since {first_day}"
                 + (f"; last count {graph['day']} from {ref} at {graph['sha']}" if raw else "; no count yet"))
        if len(samples) == 1:
            notes.append(f"one count so far: the code graph keeps no past, so this line starts on {first_day} "
                         f"and gains a point at every graph build")
        if graph["coverage_pct"] is not None:
            notes.append(f"compiled files only: the graph covers {graph['coverage_pct']:g}% of tracked source files, "
                         f"so a use in a file it never compiled is not counted")
        if ref and ref not in (branch, branch.split("/")[-1]):
            notes.append(f"the last count comes from a graph built on {ref}, not {branch}")
    else:
        basis = f"counts {what}; read {synced_txt}"
    if tickets:
        notes.append(f"merged means a commit on {branch} names the ticket; whether Jira calls it done is not in git")
    return {
        "name": name, "what": what, "unit": unit(spec), "kind": kind, "spec": spec, "branch": branch,
        "since": since, "start": start, "now": now, "now_day": samples[-1][0] if samples else None,
        "peak": {"day": peak[0], "count": peak[1]}, "base": base, "base_day": base_day,
        "base_is_peak": not start and kind == "path",
        "progress": round(100.0 * (base - now) / base, 1) if base else None,
        "done": done_at, "reconciled": reconciled, "counted": counted,
        "change_4w": change_over(4), "change_12w": change_over(12),
        "samples": samples, "commits_total": len(rows),
        "removing": {"commits": len(removing), "count": sum(r[4] - r[3] for r in removing)},
        "adding": {"commits": len(adding), "count": sum(r[3] - r[4] for r in adding)},
        "commits": commits if commits_limit is None else commits[:commits_limit],
        "left_total_areas": len(left),
        "left": left if left_limit is None else left[:left_limit],
        "head_sha": (head_sha or "")[:11], "synced_at": synced,
        "basis": basis, "notes": notes, "graph": graph, "tickets": tickets,
    }


def for_viz(db, commits_limit=150, left_limit=40):
    out = []
    for n in names(db):
        s = summary(db, n, commits_limit, left_limit)
        s["headline"] = headline(s)
        s["ticket_line"] = ticket_line(s["tickets"]) if s["tickets"] else ""
        out.append(s)
    return out


def _signed(n):
    return f"+{n:,}" if n > 0 else f"{n:,}" if n < 0 else "0"


def headline(s):
    """One line per migration: where it stands, in counts."""
    if s["kind"] == "tickets":
        t = s["tickets"]
        if not t:
            return "no history to match the tickets against yet"
        return ("done: " if t["merged"] == t["total"] else "") + ticket_line(t)
    u = s["unit"]
    done = s["done"]
    when = f"at the peak on {s['base_day']}" if s["base_is_peak"] else f"on {s['base_day']}"
    if not s["samples"]:
        return "not counted yet"
    if done:
        how = ""
        if done.get("sha"):
            how = f" with {done['sha']}" + (f" (#{done['pr']})" if done.get("pr") else "")
        return f"done: {s['base']:,} {u} {when}, none since {done['day']}{how}"
    if s["progress"] is None:
        return f"no {u} on {s['base_day']} or since"
    if s["now"] > s["base"]:
        return f"{s['now']:,} {u} left, {s['now'] - s['base']:,} more than the {s['base']:,} {when}"
    return f"{s['progress']:g}% done: {s['now']:,} {u} left of {s['base']:,} {when}"


def report_text(s, web="", commits=20, left=15):
    lines = [f"{s['name']}: {headline(s)}", f"  {s['basis']} (idxg history build updates it)"]
    if s["kind"] != "tickets" and s["samples"]:
        lines.append(f"  peak {s['peak']['count']:,} on {s['peak']['day']}"
                     + (f"; change over the last 4 weeks {_signed(s['change_4w'])}" if s["change_4w"] is not None else "")
                     + (f", last 12 weeks {_signed(s['change_12w'])}" if s["change_12w"] is not None else ""))
    g = s["graph"]
    if g and g.get("files") is not None:
        if g.get("occurrences") is not None:
            lines.append(f"  now in {g['files']:,} files, {g['occurrences']:,} occurrences")
        elif g.get("direct") is not None:
            lines.append(f"  defined in {g['files']:,} files; {g['direct']:,} inherit or conform directly")
    if s["kind"] in GIT_KINDS:
        lines.append(f"  {s['commits_total']:,} commits changed the count: {s['removing']['commits']:,} removed "
                     f"{s['removing']['count']:,}, {s['adding']['commits']:,} added {s['adding']['count']:,}")
    lines += [f"  {n}" for n in s["notes"]]
    t = s["tickets"]
    if t:
        lines += ["", f"tickets{' of ' + t['epic'] if t['epic'] else ''}: {ticket_line(t)}"]
        for x in t["items"]:
            if x["commits"]:
                span = x["first_day"] if x["first_day"] == x["last_day"] else f"{x['first_day']} .. {x['last_day']}"
                prs = " ".join(f"#{p}" for p in x["prs"][:4])
                rel = f"  {x['release']}" if x["release"] else ""
                lines.append(f"  {x['key']:<14} {x['commits']:>2} commit{'s' if x['commits'] != 1 else ' '}  {span}  {prs}{rel}")
            else:
                lines.append(f"  {x['key']:<14} no merged commit yet")
    if s["left"] and s["now"] and s["kind"] != "tickets":
        lines += ["", f"what is left, by module ({s['left_total_areas']} areas; a name not in the graph is a directory):"]
        for a in s["left"][:left]:
            tests = f", {a['tests']:,} of them in test files" if a["tests"] else ""
            lines.append(f"  {a['count']:>7,}  {a['area']}{'' if a['in_graph'] else '/'}  (in {a['files']:,} files{tests})")
        if s["left_total_areas"] > left:
            lines.append(f"  ... {s['left_total_areas'] - left} more areas")
    if s["commits"] and commits:
        lines += ["", f"commits that moved it, newest first ({min(commits, len(s['commits']))} of {s['commits_total']:,}):"]
        for c in s["commits"][:commits]:
            pr = f"  #{c['pr']}" if c.get("pr") else ""
            rel = f"  {c['release']}" if c.get("release") else ""
            lines.append(f"  {c['day']}  {_signed(c['change']):>6}  {c['sha']}  {c['subject'][:90]}{pr}{rel}")
    return "\n".join(lines)
