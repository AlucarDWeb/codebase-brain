#!/usr/bin/env python3
"""codebase-brain MCP stdio server: the code graph, its history and its docs as tools."""
import argparse, io, json, os, sys, traceback
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A release that cannot import would otherwise make the server vanish without a word; serving
# one tool that explains the failure lets the agent tell the user how to get back.
try:
    import idxg
    BROKEN = None
except Exception:
    idxg, BROKEN = None, traceback.format_exc(limit=2)

SERVER = {"name": "codebase-brain", "version": idxg.VERSION if idxg else "unknown"}
if BROKEN:
    SERVER["degraded"] = True


# Sent once at start-up, so an agent that never loads the skill still knows where to begin.
INSTRUCTIONS = """codebase-brain answers questions about an indexed Swift/ObjC repository from the compiler's own records, its commit history and its docs. When the working directory is not the project, pass the project folder as `db` (list_projects shows the indexed ones).

Where to start:
- How a module is organised, what it uses, who uses it, where a change spreads: describe_module.
- A crash report or stack trace: triage_crash with the pasted trace and since=<previous release tag>. Per frame it gives the code, its callers and the commits that release lacks, each with the release it first shipped in (hotfix branches included) and the opening of its PR. Call get_commit only for a full PR description.
- Who calls X, what X calls: trace_path (Type.member names work; call-site code is included). Every occurrence: find_references.
- Whether given symbols can be deleted: check_usage with the list; it already searches the unindexed files, so do not grep again.
- Who changed X and why: get_history with narrate. What a release shipped: get_releases, get_digest with release.
- What the repo's own docs say: search_docs, then get_doc.
- Anything else: query_graph with SQL, after get_schema.

The graph is a snapshot of the last compile: index_status says whether it is behind and how much of the repo it covers. A file with no index records was never compiled, so search its text instead; check_usage does that search for you."""


def recovery_hint():
    if idxg:
        prev = idxg.previous_release()
        back = f"`idxg update --to {prev}` goes back to the previous release" if prev else \
            "`idxg update --to <version>` installs an earlier release"
        return (f"\n\nThis is codebase-brain {idxg.VERSION}. If the error looks like a bug in the tool "
                f"rather than in the arguments: `idxg update` installs a fix if one is out, {back}. "
                f"Restart Claude Code after either. Report it at "
                f"https://github.com/AlucarDWeb/codebase-brain/issues")
    return ("\n\nThe installed codebase-brain cannot start on this Python, so `idxg` does not run either. "
            f"Go back to the previous release with:\n  git -C {REPO_DIR} fetch --tags && "
            f"git -C {REPO_DIR} checkout --detach $(git -C {REPO_DIR} describe --tags --abbrev=0 HEAD^)\n"
            "then restart Claude Code. Report it at https://github.com/AlucarDWeb/codebase-brain/issues")

DB_ARG = {"type": "string", "description": "the project folder or its graph .db path; defaults to the "
                                            "project around the working directory (list_projects shows both)"}

TOOLS = [
    {"name": "index_status",
     "description": "Index-store graph status: counts, edge kinds, build time, how much of the repo "
                    "the compiled index actually covers, the history db, and whether a newer "
                    "codebase-brain release exists. Call this first in a session.",
     "inputSchema": {"type": "object", "properties": {"db": DB_ARG}}},
    {"name": "search_graph",
     "description": "Find symbols by full-text query (BM25 over camel-split names), name regex, kind, "
                    "module, file glob, or degree. Results carry exact definition file:line and "
                    "in/out degrees from the compiler index, not a parser heuristic.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "natural-language / keyword full-text search"},
         "name_pattern": {"type": "string", "description": "regex on the symbol name"},
         "kind": {"type": "string", "description": "comma list: Struct,Class,Protocol,InstanceMethod,..."},
         "module": {"type": "string"}, "file_pattern": {"type": "string", "description": "glob on repo path"},
         "lang": {"type": "string", "enum": ["Swift", "ObjC", "C", "C++"]},
         "min_degree": {"type": "integer"}, "max_degree": {"type": "integer"},
         "limit": {"type": "integer", "default": 40}, "offset": {"type": "integer", "default": 0},
         "include_external": {"type": "boolean", "description": "include symbols defined outside the repo"},
         "db": DB_ARG}}},
    {"name": "trace_path",
     "description": "Walk resolved call/reference edges from a symbol. direction in=callers, out=callees, "
                    "both. Every edge carries the exact source location of the call site.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string", "description": "name, Module.Name, Type.member, Module.Type.member, or USR"},
         "direction": {"type": "string", "enum": ["in", "out", "both"], "default": "both"},
         "depth": {"type": "integer", "default": 2}, "fanout": {"type": "integer", "default": 25},
         "edge_kinds": {"type": "string", "default": "CALLS",
                        "description": "CALLS,REFERENCES,CONTAINS,INHERITS,OVERRIDES,EXTENDS,ACCESSOR_OF"},
         "first": {"type": "boolean", "default": True, "description": "take best match instead of listing"},
         "code": {"type": "boolean", "default": True,
                  "description": "print the source line at each edge's first site, so the call reads without opening the file"},
         "max_rows": {"type": "integer", "default": 120,
                      "description": "cap printed rows; a wide trace is truncated with a note"},
         "max_bytes": {"type": "integer", "default": 8000,
                       "description": "cap the payload size of one call"},
         "db": DB_ARG},
         "required": ["symbol"]}},
    {"name": "find_references",
     "description": "Every recorded occurrence of a symbol with its role (definition, reference, read, "
                    "write, call, dynamic), grouped by file.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "limit": {"type": "integer", "default": 200},
         "db": DB_ARG}, "required": ["symbol"]}},
    {"name": "get_code_snippet",
     "description": "Print a symbol's definition from disk, using the index's definition line and a "
                    "brace-balanced extent.",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "max_lines": {"type": "integer", "default": 200},
         "max_bytes": {"type": "integer", "default": 6000},
         "db": DB_ARG}, "required": ["symbol"]}},
    {"name": "query_graph",
     "description": "Read-only SQL over the graph. Tables: symbols(usr_hash,usr,name,kind,lang,module,"
                    "def_path_hash,def_line,in_deg,out_deg,call_in,call_out,ref_count,in_repo), "
                    "edges(src,dst,kind,path_hash,line,col), occurrences(usr_hash,path_hash,line,col,roles), "
                    "defs, files(path_hash,path,rel,in_repo,module), units. Call get_schema for details.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 200},
         "db": DB_ARG}, "required": ["query"]}},
    {"name": "check_index_coverage",
     "description": "For each path (file or directory), report whether the compiled index covers it. "
                    "A file with no records was never compiled in the indexed build: grep it instead. "
                    "Absence is never proof a symbol does not exist.",
     "inputSchema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}}, "db": DB_ARG},
         "required": ["paths"]}},
    {"name": "get_architecture",
     "description": "Layers, modules by symbol count, cross-module call hotspots, and build targets.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 20}, "db": DB_ARG}}},
    {"name": "describe_module",
     "description": "One module's card in a single call: its folder and layers with their most connected "
                    "types, the first-party modules it uses (and those it imports without using any symbol), "
                    "every module that uses it with the symbols they call and one call site, the types a "
                    "change would spread furthest from, and how much of its folder the build compiled. "
                    "Start here for 'how is module X organised', 'what depends on X', 'what does X need'.",
     "inputSchema": {"type": "object", "properties": {
         "module": {"type": "string", "description": "module name as the compiler knows it, e.g. SearchFeature"},
         "types": {"type": "integer", "default": 10, "description": "how many most connected types to list"},
         "users": {"type": "integer", "default": 20, "description": "how many dependent modules to detail"},
         "max_bytes": {"type": "integer", "default": 12000}, "db": DB_ARG}, "required": ["module"]}},
    {"name": "check_usage",
     "description": "Can these symbols be deleted? For each one: USED IN PRODUCTION, USED ONLY BY TESTS or "
                    "UNUSED, with the evidence, from its uses in the compiled build (including calls through a "
                    "protocol requirement or base method it implements) and a text search of every tracked "
                    "file the build did not index (Objective-C the index skipped, xibs, storyboards, plists). "
                    "One call answers what otherwise takes a graph lookup and a repository grep per symbol; do "
                    "not repeat the grep afterwards. A use built from a string at runtime is invisible to both.",
     "inputSchema": {"type": "object", "properties": {
         "symbols": {"type": "array", "items": {"type": "string"},
                     "description": "Module.Type.member, Type.member, or a definition site path/File.swift:line"},
         "db": DB_ARG}, "required": ["symbols"]}},
    {"name": "find_dead_code",
     "description": "Symbols nothing in the indexed build reaches: no call, no reference, no "
                    "override, no occurrence beyond their own definition. Structural edges are "
                    "ignored, and synthesis-driven members, protocol witnesses, IB outlets, entry "
                    "points, vendored trees and ObjC are excluded by default. Pass verify to "
                    "cross-check each candidate with a text search. Bounded by index coverage: "
                    "report survivors as candidates to check, never as unused code.",
     "inputSchema": {"type": "object", "properties": {
         "module": {"type": "string"}, "kind": {"type": "string", "description": "comma list"},
         "verify": {"type": "boolean", "description": "drop candidates named in another file"},
         "test_only": {"type": "boolean", "description": "instead list production symbols that only test "
                                                          "modules reach: candidates for code kept alive by tests"},
         "include_tests": {"type": "boolean"}, "include_vendor": {"type": "boolean"},
         "lang": {"type": "string", "enum": ["Swift", "ObjC", "C", "any"]},
         "limit": {"type": "integer", "default": 100}, "offset": {"type": "integer", "default": 0},
         "db": DB_ARG}}},
    {"name": "refresh_index",
     "description": "Reindex the project when the compiler's index store has moved on, which it "
                    "does after any build. Takes minutes on a large repo, so call it when a trace "
                    "looks stale rather than routinely; index_status reports staleness for free.",
     "inputSchema": {"type": "object", "properties": {
         "force": {"type": "boolean", "description": "reindex even when nothing changed"},
         "db": DB_ARG}}},
    {"name": "list_projects",
     "description": "Every indexed project on this machine, with symbol counts, whether each graph is "
                    "fresh or behind its index store, and its graph path. Any other tool queries one of "
                    "them from anywhere when given its folder as db.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_schema",
     "description": "Full db schema plus the edge-kind and occurrence-role vocabulary.",
     "inputSchema": {"type": "object", "properties": {"db": DB_ARG}}},
    {"name": "get_history",
     "description": "Commits on the project's main branch that touched a path, a symbol's file, a "
                    "module, or matched an author or subject text. Each row: date, short sha, author, "
                    "subject, PR number, tickets, files and line counts. Attribution to modules follows "
                    "the compiled index, so uncompiled files have none. Pass with_files to list the "
                    "paths each commit changed.",
     "inputSchema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"},
                   "description": "repo-relative files, directories (trailing /) or globs"},
         "symbol": {"type": "string", "description": "history of the file defining this symbol"},
         "module": {"type": "string"}, "component": {"type": "string", "description": "module-depth directory"},
         "author": {"type": "string"}, "since": {"type": "string", "description": "YYYY-MM-DD"},
         "until": {"type": "string"}, "query": {"type": "string", "description": "substring of subject, body or ticket"},
         "release": {"type": "string", "description": "only commits that first shipped in this release tag"},
         "with_files": {"type": "boolean"},
         "narrate": {"type": "boolean", "description": "one plain paragraph per commit: who, what, why "
                                                        "(from the PR description), which files and modules"},
         "limit": {"type": "integer", "default": 30},
         "max_bytes": {"type": "integer", "default": 12000, "description": "cap the payload of one call"},
         "db": DB_ARG}}},
    {"name": "get_commit",
     "description": "One commit in full: message body, PR, tickets, and every file it changed with "
                    "line counts and module attribution.",
     "inputSchema": {"type": "object", "properties": {
         "sha": {"type": "string", "description": "full or short sha, or a PR number as #123"},
         "max_files": {"type": "integer", "default": 80},
         "max_body": {"type": "integer", "default": 4000, "description": "cap the PR description"},
         "db": DB_ARG},
         "required": ["sha"]}},
    {"name": "get_digest",
     "description": "Weekly or per-release digest of the main branch: headline and stats for the window, the changes "
                    "that stood out, then every change narrated in plain language and grouped by area "
                    "(tooling first, then modules ordered by how much the rest of the code depends on "
                    "them, features, tests last). Defaults to the week of the last commit. Pass a "
                    "since/until pair for any window.",
     "inputSchema": {"type": "object", "properties": {
         "week": {"type": "string", "description": "ISO week, e.g. 2026-W36"},
         "release": {"type": "string", "description": "digest of everything that first shipped in this release tag"},
         "since": {"type": "string"}, "until": {"type": "string"},
         "max_bytes": {"type": "integer", "default": 16000}, "db": DB_ARG}}},
    {"name": "triage_crash",
     "description": "Map a symbolicated stack trace (Apple crash report, lldb backtrace, Sentry frames, or "
                    "any text with Type.method(labels:) and File.swift:line) onto the graph and the "
                    "history. For each in-repo frame: the symbol and its definition, its callers with call "
                    "sites, and the commits that touched its file since a date or release tag, each with its "
                    "PR number, the release it first shipped in (hotfix release branches included) and the "
                    "opening of its PR description. Call get_commit only for the full description. This "
                    "gathers what changed near the crash; it does not know why it crashed.",
     "inputSchema": {"type": "object", "properties": {
         "trace": {"type": "string", "description": "the stack trace text"},
         "since": {"type": "string", "description": "YYYY-MM-DD or a git ref such as the previous release tag"},
         "frames": {"type": "integer", "default": 6}, "callers": {"type": "integer", "default": 5},
         "commits": {"type": "integer", "default": 5}, "max_bytes": {"type": "integer", "default": 12000},
         "db": DB_ARG}, "required": ["trace"]}},
    {"name": "get_releases",
     "description": "Version tags of the project: when each release branched off the history branch, "
                    "when it was tagged, and how many commits first shipped in it. Every commit from "
                    "get_history and get_commit carries its release, so 'which release has this "
                    "feature' is one call.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "default": 30}, "db": DB_ARG}}},
    {"name": "get_churn",
     "description": "Where change concentrates: commits, lines and authors per module, component "
                    "directory, file or author over a window (default the last 365 days).",
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "YYYY-MM-DD"},
         "by": {"type": "string", "enum": ["module", "component", "file", "author"], "default": "module"},
         "ext": {"type": "string", "description": "restrict to one extension, e.g. swift"},
         "limit": {"type": "integer", "default": 25}, "db": DB_ARG}}},
    {"name": "get_timeline",
     "description": "Narrative history of the project: an overview paragraph, then one paragraph per "
                    "period (year, quarter or month by span) with commit and author counts, most "
                    "touched modules, distinctive subject words, directories that appeared or "
                    "disappeared, and the largest change. Every sentence is computed from git log.",
     "inputSchema": {"type": "object", "properties": {
         "periods": {"type": "integer", "default": 6, "description": "most recent periods to narrate; 0 = all"},
         "granularity": {"type": "string", "enum": ["year", "quarter", "month"]},
         "db": DB_ARG}}},
    {"name": "list_docs",
     "description": "Markdown documentation tracked in the repository (READMEs, CLAUDE.md notes, "
                    "skills, design docs), each with kind, module attribution and last-commit date. "
                    "Filter by module to find a module's own docs without knowing their paths.",
     "inputSchema": {"type": "object", "properties": {
         "module": {"type": "string"}, "kind": {"type": "string",
                    "description": "readme, agent-note, skill, guide or doc"},
         "path_glob": {"type": "string"}, "limit": {"type": "integer", "default": 60},
         "db": DB_ARG}}},
    {"name": "search_docs",
     "description": "Full-text search (BM25) over the repository's markdown docs, with a snippet "
                    "per hit. Use it before reading a doc file, and for 'how does this project do X' "
                    "questions the code graph cannot answer.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer", "default": 10},
         "db": DB_ARG}, "required": ["query"]}},
    {"name": "get_doc",
     "description": "The content of one repository doc by path (or a unique path suffix), with its "
                    "last-commit date. Truncated at max_bytes with a note.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "max_bytes": {"type": "integer", "default": 12000},
         "db": DB_ARG}, "required": ["path"]}},
    {"name": "refresh_history",
     "description": "Pull new commits from the history branch and re-sync repo docs. Incremental and "
                    "cheap after the first run; index_status shows when it last ran.",
     "inputSchema": {"type": "object", "properties": {
         "full": {"type": "boolean", "description": "rebuild from the first commit"},
         "db": DB_ARG}}},
    {"name": "build_visualizer",
     "description": "Generate the self-contained HTML graph explorer and return its path.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "description": "module name or path glob"},
         "out": {"type": "string"}, "limit": {"type": "integer"},
         "db": DB_ARG}}},
]

DEFAULTS = {"json": False, "db": None, "exact": False, "no_stale_check": True}


def ns(**kw):
    d = dict(DEFAULTS)
    d.update(kw)
    return argparse.Namespace(**d)


def run(fn, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(args)
    return buf.getvalue() or "(no output)"


def call(name, a):
    db = a.get("db")
    if name == "index_status":
        return run(idxg.cmd_status, ns(db=db))
    if name == "search_graph":
        return run(idxg.cmd_search, ns(db=db, query=a.get("query"), name=a.get("name_pattern"),
                                       kind=a.get("kind"), module=a.get("module"),
                                       file=a.get("file_pattern"), lang=a.get("lang"),
                                       min_degree=a.get("min_degree") or 0, max_degree=a.get("max_degree"),
                                       limit=a.get("limit", 40), offset=a.get("offset", 0),
                                       all=bool(a.get("include_external")), usr=True, detail="default"))
    if name == "trace_path":
        return run(idxg.cmd_trace, ns(db=db, symbol=a["symbol"], direction=a.get("direction", "both"),
                                      depth=a.get("depth", 2), fanout=a.get("fanout", 25),
                                      kind=a.get("edge_kinds", "CALLS"), first=a.get("first", True),
                                      code=a.get("code", True),
                                      max_rows=a.get("max_rows", 120),
                                      max_bytes=a.get("max_bytes", 8000)))
    if name == "find_references":
        return run(idxg.cmd_refs, ns(db=db, symbol=a["symbol"], limit=a.get("limit", 200)))
    if name == "get_code_snippet":
        return run(idxg.cmd_snippet, ns(db=db, symbol=a["symbol"], max_lines=a.get("max_lines", 200),
                                        max_bytes=a.get("max_bytes", 6000)))
    if name == "query_graph":
        return run(idxg.cmd_sql, ns(db=db, query=a["query"], limit=a.get("limit", 200)))
    if name == "check_index_coverage":
        return run(idxg.cmd_coverage, ns(db=db, paths=a["paths"]))
    if name == "get_architecture":
        return run(idxg.cmd_arch, ns(db=db, limit=a.get("limit", 20)))
    if name == "describe_module":
        return run(idxg.cmd_module, ns(db=db, module=a["module"], types=a.get("types", 10), users=a.get("users", 20),
                                       max_bytes=a.get("max_bytes", 12000)))
    if name == "check_usage":
        return run(idxg.cmd_usage, ns(db=db, symbols=a["symbols"]))
    if name == "find_dead_code":
        return run(idxg.cmd_dead, ns(db=db, module=a.get("module"), kind=a.get("kind"),
                                     verify=bool(a.get("verify")), test_only=bool(a.get("test_only")),
                                     include_tests=bool(a.get("include_tests")),
                                     include_vendor=bool(a.get("include_vendor")),
                                     lang=a.get("lang", "Swift"), limit=a.get("limit", 100),
                                     offset=a.get("offset", 0)))
    if name == "refresh_index":
        return run(idxg.cmd_refresh, ns(db=db, all=False, force=bool(a.get("force")),
                                        jobs=None, verbose=False))
    if name == "list_projects":
        return run(idxg.cmd_projects, ns(db=None))
    if name == "get_schema":
        return run(idxg.cmd_schema, ns(db=db))
    if name == "get_history":
        return run(idxg.cmd_history_log, ns(db=db, paths=a.get("paths") or [], symbol=a.get("symbol"),
                                            module=a.get("module"), component=a.get("component"),
                                            author=a.get("author"), since=a.get("since"),
                                            until=a.get("until"), grep=a.get("query"),
                                            files=bool(a.get("with_files")), narrate=bool(a.get("narrate")),
                                            release=a.get("release"),
                                            limit=a.get("limit", 30), max_bytes=a.get("max_bytes", 12000)))
    if name == "get_digest":
        return run(idxg.cmd_history_digest, ns(db=db, week=a.get("week"), release=a.get("release"), since=a.get("since"),
                                               until=a.get("until"), list=False, limit=30, html=None,
                                               open=False, max_bytes=a.get("max_bytes", 16000)))
    if name == "get_commit":
        return run(idxg.cmd_history_show, ns(db=db, sha=a["sha"], max_files=a.get("max_files", 80),
                                             max_body=a.get("max_body", 4000)))
    if name == "triage_crash":
        return run(idxg.cmd_crash, ns(db=db, trace=None, text=a["trace"], since=a.get("since"),
                                      frames=a.get("frames", 6), callers=a.get("callers", 5),
                                      commits=a.get("commits", 5), max_bytes=a.get("max_bytes", 12000)))
    if name == "get_releases":
        return run(idxg.cmd_history_releases, ns(db=db, limit=a.get("limit", 30)))
    if name == "get_churn":
        return run(idxg.cmd_history_churn, ns(db=db, since=a.get("since"), by=a.get("by", "module"),
                                              ext=a.get("ext"), limit=a.get("limit", 25)))
    if name == "get_timeline":
        return run(idxg.cmd_history_timeline, ns(db=db, periods=a.get("periods", 6),
                                                 granularity=a.get("granularity")))
    if name == "list_docs":
        return run(idxg.cmd_docs_list, ns(db=db, module=a.get("module"), kind=a.get("kind"),
                                          path=a.get("path_glob"), limit=a.get("limit", 60)))
    if name == "search_docs":
        return run(idxg.cmd_docs_search, ns(db=db, query=a["query"], limit=a.get("limit", 10)))
    if name == "get_doc":
        return run(idxg.cmd_docs_show, ns(db=db, path=a["path"], max_bytes=a.get("max_bytes", 12000)))
    if name == "refresh_history":
        return run(idxg.cmd_history_build, ns(db=db, full=bool(a.get("full")), branch=None, since=None,
                                              docs=True, prs=True, all_commits=False))
    if name == "build_visualizer":
        return run(idxg.cmd_viz, ns(db=db, scope=a.get("scope"), out=a.get("out"), limit=a.get("limit"),
                                    edge_cap=120000, per_node_cap=14, title=None, open=False))
    raise ValueError(f"unknown tool {name}")


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        try:
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}}, "serverInfo": SERVER,
                    "instructions": INSTRUCTIONS if not BROKEN else
                    "codebase-brain could not start; call index_status for the way back to a working release."}})
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [t for t in TOOLS if t["name"] == "index_status"] if BROKEN else TOOLS}})
            elif method == "tools/call" and BROKEN:
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text":
                      f"codebase-brain failed to start:\n{BROKEN}{recovery_hint()}"}], "isError": True}})
            elif method == "tools/call":
                text = call(params["name"], params.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": text}]}})
            elif method == "ping":
                send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif mid is not None:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32601, "message": f"method not found: {method}"}})
        except SystemExit as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
        except Exception:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": traceback.format_exc(limit=3) + recovery_hint()}],
                "isError": True}})


if __name__ == "__main__":
    main()
