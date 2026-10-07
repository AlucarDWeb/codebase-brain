"""Draws the README roadmap cards from the repository's issues.

Writes status_<name>.svg (a count per status label) and issue_<n>.svg (one card per open
issue, doing first and Android last) into docs/roadmap. Run it after changing an issue's status label, then
commit the result. It also points the README's card links at the right issues. Standard library only.
"""
import html
import json
import os
import re
import sys
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "AlucarDWeb/codebase-brain")
STATUSES = ["doing", "next", "planned", "done"]
COLORS = {"doing": "#bf8700", "next": "#2da44e", "planned": "#0969da", "done": "#8250df"}
MAX_CARDS = 10

STYLE = """<style>
  :root { --bg: #ffffff; --border: #d1d9e0; --fg: #1f2328; --muted: #59636e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0d1117; --border: #3d444d; --fg: #f0f6fc; --muted: #9198a1; }
  }
  text { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; fill: var(--fg); }
  .card { fill: var(--bg); stroke: var(--border); stroke-width: 1; }
  .title { font-size: 15px; font-weight: 600; }
  .muted { font-size: 12px; fill: var(--muted); }
  .count { font-size: 34px; font-weight: 600; text-anchor: middle; }
  .pill { font-size: 12px; font-weight: 600; text-anchor: middle; fill: #ffffff; }
</style>"""


def api(path):
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (REPO, path))
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def status_of(issue):
    for label in issue["labels"]:
        if label["name"].startswith("status:"):
            return label["name"].split(":", 1)[1]
    return None


def issues():
    found = []
    page = 1
    while True:
        batch = api("issues?state=all&per_page=100&page=%d" % page)
        found += [i for i in batch if "pull_request" not in i]
        if len(batch) < 100:
            return found
        page += 1


def status_card(name, count):
    return (
        '<svg width="130" height="100" xmlns="http://www.w3.org/2000/svg">%s'
        '<rect class="card" x="0.5" y="0.5" width="129" height="99" rx="6"/>'
        '<rect x="25" y="14" width="80" height="24" rx="12" fill="%s"/>'
        '<text class="pill" x="65" y="30">%s</text>'
        '<text class="count" x="65" y="80">%d</text></svg>'
    ) % (STYLE, COLORS[name], name, count)


def issue_card(issue, status):
    title = issue["title"]
    if len(title) > 62:
        title = title[:59] + "..."
    hearts = issue.get("reactions", {}).get("heart", 0)
    meta = "#%d  ·  opened %s" % (issue["number"], issue["created_at"][:10])
    if issue["comments"]:
        meta += "  ·  %d comments" % issue["comments"]
    if hearts:
        meta += "  ·  %d hearts" % hearts
    return (
        '<svg width="670" height="56" xmlns="http://www.w3.org/2000/svg">%s'
        '<rect class="card" x="0.5" y="0.5" width="669" height="55" rx="6"/>'
        '<rect x="0.5" y="0.5" width="6" height="55" rx="3" fill="%s"/>'
        '<text class="title" x="20" y="24">%s</text>'
        '<text class="muted" x="20" y="43">%s</text>'
        '<rect x="584" y="14" width="72" height="22" rx="11" fill="%s"/>'
        '<text class="pill" x="620" y="29">%s</text></svg>'
    ) % (STYLE, COLORS[status], html.escape(title), html.escape(meta), COLORS[status], status)


def relink_readme(numbers):
    """Points each card's link at the issue it shows, since the order changes with the labels."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "README.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for n, number in enumerate(numbers):
        pattern = r'(<a href=")[^"]*/issues/\d+("><img src="docs/roadmap/issue_%d\.svg")' % n
        text = re.sub(pattern, lambda m: m.group(1) + "https://github.com/%s/issues/%d" % (REPO, number) + m.group(2), text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(out):
    os.makedirs(out, exist_ok=True)
    found = [(status_of(i), i) for i in issues()]
    found = [(s, i) for s, i in found if s in STATUSES]
    for name in STATUSES:
        count = sum(1 for s, i in found if s == name and (name == "done" or i["state"] == "open"))
        with open(os.path.join(out, "status_%s.svg" % name), "w", encoding="utf-8") as f:
            f.write(status_card(name, count))
    open_cards = sorted(
        [(s, i) for s, i in found if i["state"] == "open"],
        key=lambda p: (p[1]["title"].startswith("Android"), STATUSES.index(p[0]), p[1]["number"]),
    )[:MAX_CARDS]
    for n, (s, i) in enumerate(open_cards):
        with open(os.path.join(out, "issue_%d.svg" % n), "w", encoding="utf-8") as f:
            f.write(issue_card(i, s))
    relink_readme([i["number"] for s, i in open_cards])
    for n in range(len(open_cards), MAX_CARDS):
        path = os.path.join(out, "issue_%d.svg" % n)
        if os.path.exists(path):
            os.remove(path)
    print("wrote %d status cards and %d issue cards to %s" % (len(STATUSES), len(open_cards), out))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/roadmap")
