---
name: dev-loop
description: How to continue developing codebase-brain in this repository: where things are, how to change and verify each layer (graph, history, explorer, MCP, docs), the release ritual, the writing rules the maintainer wants, and the current backlog. Use at the start of any session in ~/Projects/codebase-brain, and whenever asked to release, verify the explorer, or pick up where the last session stopped.
---

# codebase-brain dev loop

Read `CLAUDE.md` in this repo first for the invariants and the traps. This skill is the
working routine around them. Current released version is in `src/idxg.py` (`VERSION`); the
GitHub releases page is the changelog.

## Where things are

| What | Where |
|---|---|
| this repo | `~/Projects/codebase-brain`, GitHub `AlucarDWeb/codebase-brain`, branch `main` |
| test project | `~/Projects/Wallapop-iOS` (registered; graph `~/.cache/codebase-brain/Wallapop-iOS-b7e1b0.db`, history db beside it, explorer `...-explorer.html`) |
| stale worktree | `~/Projects/Wallapop-iOS-feature` is no longer registered; ignore it |
| global skill | `skill/codebase-brain/SKILL.md`, linked into `~/.claude/skills/codebase-brain` |
| generated per-project files | templates in `src/idxg.py` (`install_project_skill`, `install_claude_md`); regenerate with `idxg init --no-build` in the test project |
| MCP server | `src/mcp_server.py`, registered as `codebase-brain` in Claude Code; restart Claude Code to load code changes |
| scratch files | the session scratchpad; never `/tmp` |
| memory | `~/.claude/projects/-Users-ferdinando-furci-Projects-ios-codebase-indexer/memory/` (still keyed to the old repo name; keep using it) |

## The loop for a change

1. Edit under `src/`. Standard library only. Comments only for the non-obvious.
2. Syntax check everything, with warnings as errors (the explorer is a Python string, so
   a bad escape is a silent page break):
   ```bash
   python3 -W error -c "import ast,pathlib; [ast.parse(pathlib.Path(f).read_text()) for f in __import__('glob').glob('src/*.py')]"
   ```
3. Verify the layer you touched, from the test project:
   - graph or build: `idxg-build --jobs 8 && idxg status` (about 4 minutes, plus history and explorer)
   - history: `idxg history build` (incremental, seconds; `--full` to start over), then `idxg history timeline --periods 2`, `idxg history digest`, `idxg history releases`
   - explorer: `idxg viz`, then render headless and probe the DOM; the interactive parts fail silently. Copy the explorer to the scratchpad, append a probe script before `</script>` that drives the tab and writes JSON into a `<pre id="probe">`, then:
     ```bash
     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --virtual-time-budget=20000 --dump-dom file:///path/probe.html | rg -o '<pre id="probe">[^<]*</pre>'
     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --window-size=1500,1500 --virtual-time-budget=20000 --screenshot=/path/shot.png file:///path/probe.html
     ```
     Look at the screenshot once. Do not loop on screenshots.
   - MCP or CLI output paths: `python3 bench/bench_mcp.py` from the test project; payloads stay capped and say when they truncate
   - agent files: `idxg init --no-build` in the test project, then read the regenerated `CLAUDE.md` block and `.claude/skills/project-brain/SKILL.md`
4. Update the docs that name the thing you changed: `README.md` (user), `skill/codebase-brain/SKILL.md` (agent), `CLAUDE.md` (traps, invariants, line counts), the MCP tool descriptions, the templates in `idxg.py`, `bench/bench_mcp.py` for new tools.
5. Commit with the `[main] ` prefix the hook expects, one change per commit, and push. The maintainer asks for commits and pushes explicitly; releases are separate.

## The release ritual

Only when the maintainer says "release X" or "the word".

```bash
cd ~/Projects/codebase-brain && git status --short          # must be clean
sed -i '' 's/^VERSION = "OLD"/VERSION = "NEW"/' src/idxg.py && idxg --version
git add -A && git commit -q -m "[main] Release NEW" && git push origin main
git tag -a vNEW -m "codebase-brain NEW" && git push origin vNEW
gh release create vNEW --title "codebase-brain NEW" --notes-file - <<'NOTES'
## Changes since OLD
- plain sentences, grouped by area, saying what a user can now do and what to run
NOTES
idxg update --check                                          # must say up to date
```

`idxg status` on other machines announces the new version within a day. Patch versions
for fixes and text, minor for new commands or tabs.

## Writing rules the maintainer wants

- Everything a human reads, including this file, README, release notes, chat, code
  comments and commit messages: plain language, a narrative thread, no jargon; explain an
  unavoidable term once. No em or en dashes, no emoji, no bolded inline labels, no
  rule-of-three padding, no upbeat closers.
- Chat replies: lead with the action or the answer, number multi-step things, state what
  now works, end with one concrete next step. The maintainer has ADHD; short beats complete.
- Prefer the `codebase-brain` MCP tools over shell `idxg` for questions about a codebase.
  Shell `idxg` is for build, viz, init, deinit, vault, and for testing the CLI itself.
- Explorer prose is computed, never interpreted: every sentence maps to a field or count.
- Never write into `~/Desktop/brains/*` (the maintainer's knowledge vaults) unasked.

## Backlog, in the order they came up

- Hotspot rows on the overview and crash frames should get "ask the agent" prompt boxes like modules and symbols have.
- Vault export on build when `history_vault` is configured (about ten lines in `build.py`); the maintainer asked whether the vault updates automatically, the answer today is no.
- `idxg history build` alone does not re-render the explorer; consider rendering when `viz_on_build` is on.
- Android: a history-only mode (`idxg init` without an index store, module attribution from Gradle directories) first, then a SCIP extractor (`scip-java` covers Kotlin) writing the same tables. Java and Kotlin stack trace lines in `crash.py`.
- A logged run of the same Sentry issue without the MCP tools, to replace the estimated "by hand" column in the Brain Dividend artifact (https://claude.ai/code/artifact/d06c928c-fbef-49aa-927b-3819814870b2).
- Releases: a fix synced to a release branch by a second commit shows on `main` as "not in any tagged release yet"; consider reading release branches (`release/<version>`) as releases in progress.

## Where the last session stopped

0.2.9 released on 2026-09-15. Everything above the backlog is shipped and verified on
Wallapop-iOS. The two regenerated agent files in `~/Projects/Wallapop-iOS` (CLAUDE.md block
and `.claude/skills/project-brain/`) are uncommitted there and are the maintainer's to commit.
