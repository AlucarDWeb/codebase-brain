#!/usr/bin/env python3
"""Start-up check for a checkout: every module parses and imports on this interpreter, and the
MCP server answers initialize and tools/list. Exit status 0 means a user can run it.

Run it with the oldest Python users have, which on macOS is /usr/bin/python3 (3.9):
    /usr/bin/python3 tests/smoke.py
`idxg update` runs it against a release before switching to it, and CI runs it on every push.
"""
import ast, glob, importlib, json, os, subprocess, sys, warnings

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def main():
    failures = []
    # A bad escape in viz.py's page string is only a warning, and it silently breaks the page.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for path in sorted(glob.glob(os.path.join(SRC, "*.py"))):
            try:
                with open(path) as f:
                    ast.parse(f.read(), path)
            except (SyntaxError, SyntaxWarning, DeprecationWarning) as e:
                failures.append(f"parse {os.path.basename(path)}: {e}")
    sys.path.insert(0, SRC)
    for path in sorted(glob.glob(os.path.join(SRC, "*.py"))):
        name = os.path.basename(path)[:-3]
        try:
            importlib.import_module(name)
        except Exception as e:
            failures.append(f"import {name}: {type(e).__name__}: {e}")
    if not failures:
        failures += _mcp()
    if failures:
        print(f"smoke check failed on Python {sys.version.split()[0]}:", *failures, sep="\n  ")
        return 1
    print(f"smoke check passed on Python {sys.version.split()[0]}")
    return 0


def _mcp():
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    r = subprocess.run([sys.executable, os.path.join(SRC, "mcp_server.py")], capture_output=True, text=True,
                       input="".join(json.dumps(q) + "\n" for q in reqs), timeout=60)
    try:
        replies = {m["id"]: m for m in (json.loads(x) for x in r.stdout.splitlines() if x.strip())}
    except ValueError:
        return [f"mcp server wrote something that is not JSON: {r.stdout[:200]!r}"]
    tools = replies.get(2, {}).get("result", {}).get("tools", [])
    if not tools or replies.get(1, {}).get("result", {}).get("serverInfo", {}).get("degraded"):
        return [f"mcp server did not list its tools: {(r.stderr or r.stdout).strip()[-400:]}"]
    return []


if __name__ == "__main__":
    sys.exit(main())
