"""Long-running removals on the history branch, counted week by week.

A migration is something the team wants gone: every import of a module, every file under a
path, or every line matching a pattern. Its definition lives in the project's local config
(`idxg migrations add`) and stores an absolute start date, so the start count never drifts.

Counts come from git at the last first-parent commit before each week boundary, so a folder
that moved or a module that was renamed shows what a checkout of that week would show. The
commits that moved a count are read from one `git log -G` (or `--name-status` for a path)
over the window, each with what it added and removed. Starting from the first sample and
adding every commit's change has to land on the current count; `reconciled` records whether
it does, and the done date is only taken from commits when it does.
"""
import concurrent.futures, datetime, hashlib, json, os, re, sqlite3, subprocess

import history
import usage

SOURCE_SPECS = ("*.swift", "*.m", "*.mm", "*.h")
DEFAULT_WINDOW_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY, spec TEXT, spec_hash TEXT, kind TEXT,
  since TEXT, start_sha TEXT, head_sha TEXT, synced_at TEXT, counted TEXT);
CREATE TABLE IF NOT EXISTS migration_samples(name TEXT, day TEXT, sha TEXT, count INTEGER,
  PRIMARY KEY(name, day));
CREATE TABLE IF NOT EXISTS migration_commits(name TEXT, ord INTEGER, sha TEXT, day TEXT, subject TEXT,
  added INTEGER, removed INTEGER, PRIMARY KEY(name, sha));
CREATE TABLE IF NOT EXISTS migration_left(name TEXT, area TEXT, in_graph INTEGER, count INTEGER,
  tests INTEGER, files INTEGER);
"""

POSIX_CLASSES = {"space": r"\s", "blank": r" \t", "digit": "0-9", "alpha": "A-Za-z", "alnum": "A-Za-z0-9",
                 "upper": "A-Z", "lower": "a-z", "punct": r"!-/:-@\[-`{-~", "xdigit": "0-9A-Fa-f"}


# ---------------------------------------------------------------- definitions

def normalize(spec):
    """Validate a definition and fill its defaults. Exactly one of imports, path, pattern."""
    name = (spec.get("name") or "").strip()
    if not name:
        raise ValueError("a migration needs a name")
    kinds = [k for k in ("imports", "path", "pattern") if spec.get(k)]
    if len(kinds) != 1:
        raise ValueError("pass exactly one of --imports, --path, --pattern")
    out = {"name": name}
    if spec.get("imports"):
        mods = spec["imports"]
        mods = [m.strip() for m in (mods.split(",") if isinstance(mods, str) else mods) if m.strip()]
        bad = [m for m in mods if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", m)]
        if bad:
            raise ValueError(f"not a module name: {', '.join(bad)}")
        out["imports"] = mods
    elif spec.get("path"):
        out["path"] = spec["path"].strip().strip("/")
    else:
        re.compile(_ere_to_py(spec["pattern"]))
        out["pattern"] = spec["pattern"]
    if spec.get("files"):
        out["files"] = list(spec["files"])
    since = spec.get("since") or (datetime.date.today() - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
    datetime.date.fromisoformat(since)
    out["since"] = since
    return out


def kind_of(spec):
    return "imports" if spec.get("imports") else "path" if spec.get("path") else "pattern"


def unit(spec):
    return {"imports": "imports", "path": "files", "pattern": "matching lines"}[kind_of(spec)]


def describe(spec):
    k = kind_of(spec)
    where = ", ".join(spec.get("files") or SOURCE_SPECS)
    if k == "imports":
        return f"import lines naming {', '.join(spec['imports'])} in {where}"
    if k == "path":
        return f"tracked files under {spec['path']}/"
    return f"lines matching /{spec['pattern']}/ in {where}"


def spec_hash(spec):
    return hashlib.blake2b(json.dumps(spec, sort_keys=True).encode(), digest_size=8).hexdigest()


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
    return list(spec.get("files") or SOURCE_SPECS)


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


# ---------------------------------------------------------------- sync

def ensure_schema(db):
    db.executescript(SCHEMA)


def sync(db, root, branch, head, mapper, specs, log=print, jobs=6):
    """Bring every configured migration up to `head`; drop the rows of removed ones."""
    ensure_schema(db)
    specs = [normalize(s) for s in specs]
    names = {s["name"] for s in specs}
    for (name,) in db.execute("SELECT name FROM migrations").fetchall():
        if name not in names:
            _wipe(db, name, everything=True)
    for spec in specs:
        name = spec["name"]
        h = spec_hash(spec)
        row = db.execute("SELECT spec_hash, head_sha, start_sha, counted FROM migrations WHERE name = ?",
                         (name,)).fetchone()
        if row and row[0] != h:
            _wipe(db, name, everything=True)
            row = None
        if row and row[1] == head:
            continue
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
        db.executemany("INSERT INTO migration_samples VALUES(?,?,?,?)",
                       [(name, d, s, n) for (d, s), n in zip(points, counts)])

        db.execute("DELETE FROM migration_left WHERE name = ?", (name,))
        areas = {}
        for path, n in here.items():
            module, component = mapper.map(path) if mapper else (None, None)
            key = (module or component or path.split("/")[0], 1 if module else 0)
            a = areas.setdefault(key, [0, 0, 0])
            a[0] += n
            a[1] += n if usage.is_test(path, module) else 0
            a[2] += 1
        db.executemany("INSERT INTO migration_left VALUES(?,?,?,?,?,?)",
                       [(name, k[0], k[1], *v) for k, v in areas.items()])
        db.execute("INSERT OR REPLACE INTO migrations VALUES(?,?,?,?,?,?,?,?,?)",
                   (name, json.dumps(spec), h, kind_of(spec), spec["since"], start_sha, head,
                    datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), counted))
        db.commit()
        log(f"  migration {name}: {len(moved)} new commits, {head_count:,} {unit(spec)} left"
            + ("" if counted == "commits" else ", counted week by week because the commits do not add up"))
    db.commit()


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
    _writer(hdb_path, lambda db: sync(db, root, branch, head, mapper, specs, log=log))


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


def summary(db, name, commits_limit=None, left_limit=None):
    """Every number the tab and the text report show, computed from the stored rows."""
    spec_json, since, head_sha, synced, counted = db.execute(
        "SELECT spec, since, head_sha, synced_at, counted FROM migrations WHERE name = ?", (name,)).fetchone()
    spec = json.loads(spec_json)
    samples = [list(r) for r in db.execute(
        "SELECT day, count FROM migration_samples WHERE name = ? ORDER BY day", (name,))]
    has_commits = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'commits'").fetchone() is not None
    extra = ("c.pr, c.author, c.release" if has_commits else "NULL, NULL, NULL")
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
    reconciled = running == now
    peak = max(samples, key=lambda s: s[1]) if samples else [since, 0]
    # A path that did not exist yet on the start date (a folder moved in later) is measured
    # from its peak instead, or it would read as nothing to remove.
    base, base_day = (start, since) if start else (peak[1], peak[0])
    if now == 0 and base > 0:
        if reconciled and reached:
            done_at = {"sha": reached[0][:11], "day": reached[1], "subject": reached[2], "pr": reached[3]}
        else:
            first_zero = next((d for i, (d, n) in enumerate(samples) if all(x == 0 for _, x in samples[i:])), None)
            done_at = {"sha": None, "day": first_zero, "subject": None, "pr": None}

    def change_over(weeks):
        cut = (datetime.date.fromisoformat(samples[-1][0]) - datetime.timedelta(weeks=weeks)).isoformat() if samples else None
        past = [n for d, n in samples if d <= cut] if cut else []
        return (now - past[-1]) if past else None

    removing = [r for r in rows if r[3] < r[4]]
    adding = [r for r in rows if r[3] > r[4]]
    commits = [{"sha": r[0][:11], "day": r[1], "subject": r[2], "change": r[3] - r[4], "added": r[3],
                "removed": r[4], "pr": r[5] or history.pr_number(r[2] or ""), "author": r[6], "release": r[7]}
               for r in reversed(rows)]
    left = [{"area": a, "in_graph": bool(g), "count": n, "tests": t, "files": f} for a, g, n, t, f in db.execute(
        "SELECT area, in_graph, count, tests, files FROM migration_left WHERE name = ? ORDER BY count DESC, area",
        (name,))]
    return {
        "name": name, "what": describe(spec), "unit": unit(spec), "kind": kind_of(spec), "spec": spec,
        "since": since, "start": start, "now": now, "now_day": samples[-1][0] if samples else None,
        "peak": {"day": peak[0], "count": peak[1]}, "base": base, "base_day": base_day, "base_is_peak": not start,
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
    }


def for_viz(db, commits_limit=150, left_limit=40):
    out = []
    for n in names(db):
        s = summary(db, n, commits_limit, left_limit)
        s["headline"] = headline(s)
        out.append(s)
    return out


def _signed(n):
    return f"+{n:,}" if n > 0 else f"{n:,}" if n < 0 else "0"


def headline(s):
    """One line per migration: where it stands, in counts."""
    u = s["unit"]
    done = s["done"]
    when = f"at the peak on {s['base_day']}" if s["base_is_peak"] else f"on {s['base_day']}"
    if done:
        how = ""
        if done.get("sha"):
            how = f" with {done['sha']}" + (f" (#{done['pr']})" if done.get("pr") else "")
        return f"done: {s['base']:,} {u} {when}, none since {done['day']}{how}"
    if s["progress"] is None:
        return f"no {u} on {s['since']} or since"
    if s["now"] > s["base"]:
        return f"{s['now']:,} {u} left, {s['now'] - s['base']:,} more than the {s['base']:,} {when}"
    return f"{s['progress']:g}% done: {s['now']:,} {u} left of {s['base']:,} {when}"


def report_text(s, web="", commits=20, left=15):
    lines = [f"{s['name']}: {headline(s)}",
             f"  counts {s['what']} on the history branch, once a week since {s['since']}; "
             f"as of {s['head_sha']}, read {(s['synced_at'] or '').replace('T', ' ')} (idxg history build updates it)"]
    lines.append(f"  peak {s['peak']['count']:,} on {s['peak']['day']}"
                 + (f"; change over the last 4 weeks {_signed(s['change_4w'])}" if s["change_4w"] is not None else "")
                 + (f", last 12 weeks {_signed(s['change_12w'])}" if s["change_12w"] is not None else ""))
    lines.append(f"  {s['commits_total']:,} commits changed the count: {s['removing']['commits']:,} removed "
                 f"{s['removing']['count']:,}, {s['adding']['commits']:,} added {s['adding']['count']:,}")
    if not s["reconciled"]:
        lines.append("  the commits do not add up to the weekly counts (a pattern git and Python read differently, "
                     "or a merge diff), so the commit list is incomplete")
    if s["left"] and s["now"]:
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
