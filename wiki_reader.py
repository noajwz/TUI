#!/usr/bin/env python3
"""Simple terminal reader for Wikipedia articles."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

USER_AGENT = "wiki-reader-cli/1.0 (terminal Wikipedia reader)"

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
UNDERLINE = "\033[4m"
RESET = "\033[0m"

HEADER_RE = re.compile(r"^(=+)\s*(.+?)\s*=+$")


def api_get(lang, params):
    params = {**params, "format": "json"}
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def fetch_article(lang, title):
    data = api_get(lang, {
        "action": "query",
        "prop": "extracts|info|pageprops",
        "explaintext": 1,
        "redirects": 1,
        "inprop": "url",
        "titles": title,
    })
    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)
    if page is None or "missing" in page:
        return None
    return page


def search_suggestions(lang, query, limit=8):
    data = api_get(lang, {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
    })
    return [r["title"] for r in data.get("query", {}).get("search", [])]


def wrap(text, width):
    words = text.split()
    lines, line, length = [], [], 0
    for word in words:
        if length + len(word) + (1 if line else 0) > width:
            lines.append(" ".join(line))
            line, length = [], 0
        line.append(word)
        length += len(word) + (1 if len(line) > 1 else 0)
    if line:
        lines.append(" ".join(line))
    return lines or [""]


def render(page, width):
    out = []
    title = page.get("title", "")
    url = page.get("fullurl", "")
    is_disambig = "disambiguation" in page.get("pageprops", {})

    out.append(f"{BOLD}{CYAN}{title}{RESET}")
    out.append(f"{DIM}{url}{RESET}")
    out.append("=" * min(width, max(10, len(title))))
    out.append("")

    if is_disambig:
        out.append(f"{YELLOW}⚠ This is a disambiguation page. Results below may list several topics.{RESET}")
        out.append("")

    extract = page.get("extract", "").rstrip()
    for raw_line in extract.split("\n"):
        line = raw_line.rstrip()
        m = HEADER_RE.match(line)
        if m:
            level = len(m.group(1))
            heading = m.group(2)
            out.append("")
            style = BOLD + UNDERLINE if level <= 2 else BOLD
            out.append(f"{style}{heading}{RESET}")
            out.append("")
        elif not line:
            out.append("")
        else:
            out.extend(wrap(line, width))

    return "\n".join(out)


def show(text):
    if not sys.stdout.isatty():
        print(text)
        return
    pager = shutil.which("less")
    if pager:
        subprocess.run([pager, "-R"], input=text.encode(), check=False)
    else:
        print(text)


def main():
    parser = argparse.ArgumentParser(description="Read Wikipedia articles in the terminal.")
    parser.add_argument("topic", nargs="*", help="article title / search terms")
    parser.add_argument("--lang", default="en", help="Wikipedia language code (default: en)")
    args = parser.parse_args()

    topic = " ".join(args.topic).strip()
    if not topic:
        try:
            topic = input("Search Wikipedia for: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not topic:
        return

    width = min(shutil.get_terminal_size((100, 24)).columns - 2, 100)

    try:
        page = fetch_article(args.lang, topic)
    except Exception as e:
        print(f"Error contacting Wikipedia: {e}", file=sys.stderr)
        sys.exit(1)

    if page is None:
        print(f"No article found for '{topic}'.")
        try:
            suggestions = search_suggestions(args.lang, topic)
        except Exception:
            suggestions = []
        if suggestions:
            print("\nDid you mean:")
            for s in suggestions:
                print(f"  - {s}")
        sys.exit(1)

    show(render(page, width))


if __name__ == "__main__":
    main()
