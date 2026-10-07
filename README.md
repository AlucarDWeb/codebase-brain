# codebase-brain

<p align="center"><img src="docs/img/logo.png" alt="codebase-brain logo" width="240"></p>

[![smoke](https://github.com/AlucarDWeb/codebase-brain/actions/workflows/smoke.yml/badge.svg)](https://github.com/AlucarDWeb/codebase-brain/actions/workflows/smoke.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A queryable brain for a Swift or Objective-C codebase, built for coding agents. It holds the compiler's code graph, the main branch's commit history and the repository's own docs.

## Table of contents

- [Background](#background)
- [Install](#install)
- [Usage](#usage)
- [How it works](#how-it-works)
- [API](#api)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)
- [Removing a project](#removing-a-project)
- [Source layout](#source-layout)
- [Contributing](#contributing)
- [License](#license)

## Background

The brain keeps three things about a project in one place:

- the code graph the compiler already resolved: every symbol, call, reference, override
  and conformance, each with its exact `file:line`;
- the main branch's commit history, with the pull request description behind each merge;
- the repository's own markdown docs, searchable by content.

You reach it three ways: as MCP tools for Claude Code, as the `idxg` CLI, and as an HTML
explorer. Because the edges come from the compiler's index store (the same data behind
Xcode's "jump to definition"), "who calls this" returns real resolved calls, with no
parsing and no guessing about generics or dynamic dispatch.

### Why an agent is faster with it

| Task | Tokens processed in total | Tool calls | Tokens read from tools | Correct |
|---|---|---|---|---|
| Crash triage | 34% fewer | 24% fewer | 35% fewer | 3/3 with, 3/3 without |
| Module architecture | 20% fewer | 8% fewer | 41% fewer | 28/30 with, 29/30 without |
| Impact of a protocol signature change | 46% fewer | 41% fewer | 30% fewer | 30/30 with, 30/30 without |
| What shipped in three releases | 44% fewer | 33% fewer | 68% fewer | 28/30 with, 28/30 without |
| Migration scope of a legacy module | 48% fewer | 37% fewer | 40% fewer | 30/30 with, 27/30 without |
| Dead code check of 10 symbols | 41% fewer | 60% fewer | 62% fewer | 30/30 with, 30/30 without |

What makes the difference:

- A stack trace frame resolves to the exact function, even when several files share its
  name, so the agent never opens look-alike files to find the right one.
- Each commit near a crash carries the release it first shipped in, untagged hotfix branches
  included, and the opening of its pull request description, so the agent does not rebuild
  the release history with git and `gh`.
- The commit list covers every frame's file, so a related fix elsewhere turns up without a
  separate search: a root-cause follow-up was reported in 3 of 3 crash triages with
  codebase-brain and 1 of 3 without.
- `describe_module` returns a module's layers, its dependencies (and the imports nothing
  uses), its dependents with the symbols they call, its hotspots, and for each of its types
  the modules that use it with the first file and line, in one call, where plain search reads
  build files, greps imports and opens integration code.
- The graph knows which files the build never compiles, so a file that looks like a user but
  is not built is not counted: in the migration scope, all three answers with codebase-brain
  caught one such file and none of the three without did.
- `check_usage` answers "can these symbols be deleted" for a whole list in one call, including a
  text search of the files the index never saw and the line of code at each use, so the agent
  neither greps each name nor opens each site.
- `impact_of` lists what a signature change breaks: implementations, call sites with their
  code, protocol extension helpers that forward to the member, and the matching lines of files
  the build never compiled.
- `get_history` with several releases prints each release's cut point and every pull request
  that first shipped in it with its what and why, so the agent neither opens each pull request
  nor works out fork points with git.
- Fewer and smaller steps mean the agent re-reads less of its own conversation.

Following a flow through the code still means reading that code, and costs about the same
either way. Total processed counts the conversation re-read at every turn, so it follows the
number of turns. A stale graph reverses the gain: with a graph that still held deleted files, module architecture took
28% more calls with codebase-brain than without it.

## Install

```bash
git clone https://github.com/AlucarDWeb/codebase-brain.git
cd codebase-brain
./install.sh
```

You need macOS with Xcode and Python 3.9 or newer. There are no other dependencies. The
GitHub CLI (`gh`), logged in, is optional and adds pull request descriptions to the
history.

The script:

1. links `idxg`, `idxg-build` and `idxg-history` into `~/.local/bin`;
2. links the `codebase-brain` skill into `~/.claude/skills/`;
3. registers the MCP server with Claude Code, when the `claude` CLI is present;
4. installs a background agent that reindexes your projects after builds
   (`NO_AUTOINDEX=1 ./install.sh` skips it).

Restart any Claude Code session that was already running so it sees the MCP server.

### Set up a project

1. Make sure the project has an index store. The compiler writes one when it builds.
   - Xcode or `xcodebuild`: build once. Nothing else to configure.
   - SwiftPM: build once, or open it in an editor with sourcekit-lsp background indexing.
   - Bazel: build with `--features=swift.index_while_building`, or use
     [sourcekit-bazel-bsp](https://github.com/spotify/sourcekit-bazel-bsp).

2. Index it.

   ```bash
   cd /path/to/your/project
   idxg init
   ```

   This builds the graph, extracts the history and docs, renders the explorer, and writes a
   project skill for agents at `.claude/skills/<project>-brain/SKILL.md`. Seconds on a
   small package; about five to ten minutes the first time on a large monorepo.

3. Check what it covers.

   ```bash
   idxg status
   ```

   Read the coverage line. It counts tracked source files that have index records. A low
   number means much of the project was not compiled in the build that wrote the store:
   build more targets, then run `idxg refresh`.

4. Try it.

   ```bash
   idxg search "something you know exists"
   idxg trace SomeSymbol --direction in --first     # who calls it, with call sites
   idxg history log --symbol SomeSymbol --narrate   # who changed it, when, and why
   idxg open                                        # the explorer, served locally
   ```

### Sharing with a team

`idxg init --claude-md` also writes a short block into the project's CLAUDE.md that points
agents at the graph. Run it once, commit the result, and everyone who clones the repository
gets it. A plain `idxg init` never touches CLAUDE.md, so teammates who run it later leave
the committed block alone.

The project skill ends with a `## Project notes` section. Anything you write there
survives every re-init.

## Usage

### From an agent

Once the MCP server is registered and a project is indexed, Claude Code picks the tools on
its own. The [API](#api) section lists them, and every tool has a CLI equivalent. Large
answers are capped in rows and bytes and say when they were truncated, so a call never
floods the agent's context. The MCP server finds the project from the session's working
directory; pass `db` to query another one.

### The explorer

The explorer links back to the agent: select a module or a symbol and an "ask the agent"
box offers ready-made prompts, filled in from what is on screen, with a copy button.

The graph tab draws the modules and symbols you pick as an architecture diagram: what uses
them on the left, what they use on the right, cards grouped in boxes by module or layer, one
arrow colour per edge kind, with direct neighbours or two hops out.

`idxg open` serves the explorer from a small local server (Ctrl-C stops it), and every search
in it then goes to the whole graph. The symbols tab filters all symbols, and a selected
symbol shows all its callers and callees. The graph tab loads all the edges of whatever you
pick, the 150 heaviest per symbol. The dead code tab lists every candidate, and the docs
filter adds full-text matches with a snippet. `idxg open --static` opens the file instead,
and the file is also what you can send someone. On its own it holds only the most connected symbols (1,500 by
default, `idxg viz --limit` changes it) and each one's strongest edges, so a missing link
there is not proof of absence.

### Triage a crash

```bash
idxg crash crash.txt --since v1.328.0
```

It reads Apple crash reports, lldb backtraces, Sentry frames, or any text containing
`Type.method(labels:)` and `File.swift:line`, and demangles Swift names. For every frame
in your repository it prints the symbol, who calls it, and the commits that touched its
file since the tag you pass (the commits that release does not contain), each with the
release it first shipped in and the opening of its pull request description:

```
#0  reduce(_:_:)  InstanceMethod  SearchFeature
    Modules/Feature/SearchFeature/Sources/UI/SearchReducer.swift:62  (trace line 80)  resolved by file:line
    callers (3 shown):
      sections(afterFavoriteToggle:)  SearchFeature_Tests  SearchRootViewContentInvalidationSpec.swift:180  (x2)
    commits touching SearchReducer.swift since 2026-08-10:
      2026-09-04  6149e25a545  [WPA-116102] Hide the location chip when ...  #21766
```

System frames are skipped, and so are the source lines Sentry quotes under a frame. A
frame is matched only to code it can mean: when several files share the trace's file name,
the one defining the frame's function wins, and a frame that names a type is matched only
to that type's own method. Anything else is reported as unresolved, never guessed. The
output shows what
changed near the crash, not why it crashed: the graph has no runtime data, so a force
unwrap or a race is invisible to it.

### Command reference

Every query command accepts `--json`, and `--db` to target another project, given as its
folder or its graph file (`idxg projects` lists both). Symbols
resolve by bare name, `Module.Name`, `Type.member` (or `Module.Type.member`) or USR; an ambiguous name lists candidates unless you
pass `--first`.

The code graph:

```bash
idxg search "location chip mapper"            # full text over camel-split names
idxg search --name '^SearchRoot' --kind Struct,Class
idxg trace MyReducer --direction in --depth 2 --first
idxg trace MyView --kind CALLS,REFERENCES --direction out --first
idxg refs MyType                              # every occurrence, with roles
idxg snippet MyType                           # the definition, read from disk
idxg arch                                     # layers, modules, hotspots
idxg module MyModule                          # one module's card, with who uses each type
idxg impact MyProtocol.requirement            # what a signature change breaks
idxg usage MyModule.MyType.member OtherType   # can these be deleted
idxg dead --verify                            # symbols nothing in the build reaches
idxg coverage Sources/Feature                 # what the index covers under a path
idxg sql "SELECT kind, COUNT(*) FROM symbols WHERE in_repo=1 GROUP BY kind"
idxg viz --scope MyModule --open              # explorer for one module
```

The history and docs:

```bash
idxg history build                            # incremental; --full starts over
idxg history log Sources/Feature/ --files     # commits touching a path
idxg history log --narrate --since 2026-09-01 # one plain paragraph per commit
idxg history show '#1234'                     # one commit or PR in full
idxg history digest                           # this week, grouped by area
idxg history digest --release 1.329.0         # everything that first shipped in a release
idxg history log Sources/Feature/ --release 1.330.0,1.331.0   # per release: cut point, PRs, what and why
idxg history releases                         # version tags and what shipped in each
idxg history churn --by module                # where change concentrated this year
idxg history timeline --periods 4             # the project's story, one paragraph per period
idxg history vault --out ~/my-vault           # export as knowledge-vault clippings
idxg docs list --module MyModule
idxg docs search "path resolver"
idxg docs show Documentation/Testing.md
```

## How it works

### The history

The history lives in `<project>-history.db`, beside the graph, so rebuilding the graph never
touches it. `idxg init` and every graph build refresh it.

- Commits come from `git log --first-parent` on `main` (or `master`, or whatever
  `idxg config history_branch=...` names). One row per merge, read incrementally.
- Each changed file is attributed to a module using the directory the graph knows for that
  module.
- Pull request descriptions are fetched through `gh` when it is logged in, only for commits
  not seen before. Turn this off with `idxg config history_prs=false`.
- Releases are version tags (`1.329.0`, `v2.3`, or a pattern set with
  `history_release_tags`), plus branches named `release/<version>` that have no tag yet (a
  release in progress, or a hotfix that was never tagged; other prefixes with
  `history_release_branches`). A commit belongs to the first release that branched after it.
  A commit cherry-picked onto an earlier release branch after its cut, such as a hotfix, is
  matched by patch id and reported as shipping first in that release.
- Docs are every tracked markdown file except vendored trees and changelogs, or exactly
  what a JSON manifest named by `idxg config docs_manifest=...` includes.

All narration (per commit, weekly digest, timeline) is computed from fields in the
database. It reads like a factual log, not an interpretation.

### Where the index store comes from

`idxg-build` finds and merges every store it recognises:

| Source | Path |
|---|---|
| Xcode, `xcodebuild` | `~/Library/Developer/Xcode/DerivedData/<Project>-<hash>/Index.noindex/DataStore` |
| sourcekit-lsp background indexing | `.index-build/index`, `~/.sourcekit-lsp/index-build` |
| sourcekit-bazel-bsp | `<output_base>/sourcekit-bazel-bsp/execroot/_main/bazel-out/_global_index_store` |
| Bazel with `swift.index_while_building` | `<output_base>/execroot/_main/bazel-out/_global_index_store` |

Pass `--store <path>` (repeatable) to override detection. Xcode indexes Objective-C too;
Bazel's Swift rules do not, see [docs/objc-index-store.md](docs/objc-index-store.md).

For scale: a monorepo with 790k symbols and 6.9M edges takes about 4 minutes to index and
produces a 2.2 GB graph. Its history (18k commits, 14.7k pull requests, 369 docs) takes
about 7 minutes the first time and 290 MB.

### Keeping it fresh

The background agent from `./install.sh` reindexes a project when its index store changes,
waiting for the build to finish, and checks every 15 minutes as a fallback.

```bash
idxg autoindex --status               # is it running, and what did it do
idxg autoindex --install --every 20   # change the fallback interval
idxg autoindex --uninstall            # stop it; install and update will not put it back
```

Without it, run `idxg refresh` after builds. Queries warn when the graph has fallen behind.

`idxg status` and the explorer tell you once a day when a newer release is out. Run
`idxg update` to install it, then restart Claude Code to load the new MCP server. Graphs and
history databases need no rebuild.

`idxg update` installs release tags only, never unreleased commits on `main`. Before it
switches, it checks that the new release starts on your Python. If it doesn't, the update
stops and you stay on the version you had. If a release you already installed misbehaves, go
back with `idxg update --to 0.6.1` (any earlier version works) and restart Claude Code. When
the MCP server itself cannot start, its one remaining tool prints the `git` command that does
the same thing.

## API

Every MCP tool has a CLI equivalent. `idxg schema` (or `get_schema`) prints every table in both databases.

| Question | MCP tool | CLI |
|---|---|---|
| Is the graph fresh, what does it cover | `index_status` | `idxg status` |
| Find a symbol by words, regex, kind, module or file | `search_graph` | `idxg search` |
| Who calls this, what does it call, override and conformance chains, with the code at each call site | `trace_path` | `idxg trace --code` |
| Every use of a symbol, with read, write and call roles | `find_references` | `idxg refs` |
| Read a definition from disk | `get_code_snippet` | `idxg snippet` |
| Was this file compiled at all | `check_index_coverage` | `idxg coverage` |
| One module: layers, what it uses, who uses it, hotspots, coverage, and which modules use each of its types | `describe_module` | `idxg module` |
| Layers, modules, cross-module hotspots | `get_architecture` | `idxg arch` |
| What a signature change to a method breaks: implementations, call sites, protocol extension helpers, uncompiled files | `impact_of` | `idxg impact` |
| Can these symbols be deleted: production, tests only, or unused, with the code at each use | `check_usage` | `idxg usage` |
| Code nothing reaches, or only tests reach | `find_dead_code` | `idxg dead`, `idxg dead --test-only` |
| Who changed this, when, in which PR, and why | `get_history` | `idxg history log --narrate` |
| One commit or PR in full | `get_commit` | `idxg history show` |
| Where change concentrates | `get_churn` | `idxg history churn` |
| What shipped in one or more releases, with each PR's what and why | `get_history` with `release` | `idxg history log --release` |
| What shipped this week, or in one release, narrated | `get_digest` | `idxg history digest` |
| Release tags and what first shipped in each | `get_releases` | `idxg history releases` |
| How the project evolved, period by period | `get_timeline` | `idxg history timeline` |
| A crash report, frame by frame | `triage_crash` | `idxg crash` |
| The repository's docs | `list_docs`, `search_docs`, `get_doc` | `idxg docs` |
| Any SQL over the graph | `query_graph` | `idxg sql` |
| Rebuild the graph or the history | `refresh_index`, `refresh_history` | `idxg refresh`, `idxg history build` |
| Tables and columns | `get_schema` | `idxg schema` |

## Limits

- The graph is a snapshot of the last compile. Line numbers drift and new code is missing
  until you rebuild. Files deleted from the checkout are skipped, even though the compiler's
  index store still holds their records.
- It only knows what was compiled. Before concluding "nothing calls this", run
  `idxg coverage <path>`: no records means "not compiled", not "not used".
- Module attribution follows the compiled index, so per-module history counts are lower
  bounds. Filter by path when that matters.
- `--symbol` history follows the file that defines the symbol, so unrelated edits to that
  file show up too.
- Each checkout gets its own database, because Bazel output bases differ per worktree.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no index store found` | Nothing has compiled the project yet. Build it, see "Set up a project". |
| `no graph for <path>` | Run `idxg init`, or `idxg projects` to see what is registered. |
| Coverage far below 100% | Build the missing targets, then `idxg refresh`. |
| Several candidates for a symbol | Pass `--first`, or qualify it as `Type.member`, `Module.Name` or the USR. |
| Line numbers are off | The graph predates your edits. Run `idxg refresh`. |
| `.m` files missing under Bazel | See [docs/objc-index-store.md](docs/objc-index-store.md). |
| `no history yet` | Run `idxg history build`. |
| Narration only lists files | PR descriptions were not fetched. Run `gh auth login`, then `idxg history build`. |

## Removing a project

```bash
idxg deinit              # remove the project skill and forget the project
idxg deinit --claude-md  # also strip the CLAUDE.md block (commit that change)
idxg deinit --purge      # also delete the graph, history, explorer and default vault export
```

A project skill with a `## Project notes` section is kept unless you pass `--force`.

## Source layout

| File | Role |
|---|---|
| `src/idxstore.py` | ctypes bindings for `libIndexStore.dylib` |
| `src/build.py` | parallel extractor and SQLite writer |
| `src/project.py` | project registry, store detection, config and staleness |
| `src/history.py` | git log, PR descriptions, docs, narration, digest, timeline, vault export |
| `src/crash.py` | stack trace parsing and frame resolution |
| `src/deadcode.py` | the dead-code candidate query |
| `src/modulecard.py` | the module card behind `idxg module` |
| `src/usage.py` | the deletion check behind `idxg usage` |
| `src/impact.py` | the signature-change check behind `idxg impact` |
| `src/idxg.py` | the CLI and query layer |
| `src/viz.py` | the HTML explorer |
| `src/serve.py` | the local server behind `idxg open` |
| `src/mcp_server.py` | the MCP server |

## Contributing

The project uses the Python standard library only, so there is nothing to install. Before
you open a pull request, run `/usr/bin/python3 tests/smoke.py` on the system Python 3.9. It
checks that every module parses and imports and that the MCP server lists its tools, and CI
runs the same check. Update this README and `skill/codebase-brain/SKILL.md` when a command or
a default changes.

## License

[MIT](LICENSE)
