#!/usr/bin/env python3
"""Terminal UI for browsing Wikipedia: search, follow links, jump between sections."""

import argparse
import curses
import json
import locale
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "wiki-tui/1.0 (terminal Wikipedia browser)"

MAX_WIDTH = 96
LEFT_MARGIN = 2
TIMEOUT = 15
SEARCH_LIMIT = 20

VOID_TAGS = {"br", "img", "link", "meta", "hr", "wbr", "input", "col", "source", "track"}
SKIP_TAGS = {"script", "style", "table", "figure", "audio", "video", "form", "noscript"}
SKIP_CLASSES = {
    "shortdescription", "mw-editsection", "reference", "noprint", "navbox",
    "navbox-styles", "navbar", "metadata", "side-box", "mw-references-wrap",
    "references", "reflist", "portal-bar", "sister-bar", "catlinks",
    "printfooter", "mw-empty-elt", "thumb", "gallery", "toc", "mw-jump-link",
}
# Namespaces that are not readable articles.
BAD_PREFIXES = ("File:", "Image:", "Special:", "Template:", "Template talk:", "Category:",
                "Help:", "Portal:", "Wikipedia:", "Talk:", "Module:", "MediaWiki:")

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = {"p", "li", "dt", "dd", "blockquote", "pre", "figcaption"}

TAG_RE = re.compile(r"<[^>]+>")


# ----------------------------------------------------------------- API


def api_get(lang, params):
    params = {**params, "format": "json", "formatversion": 2}
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_article(lang, title):
    """Return (real_title, html) or None when the page does not exist."""
    data = api_get(lang, {
        "action": "parse",
        "page": title,
        "prop": "text",
        "redirects": 1,
    })
    if "error" in data:
        return None
    parse = data.get("parse", {})
    if "text" not in parse:
        return None
    return parse.get("title", title), parse["text"]


def search_titles(lang, query, limit=SEARCH_LIMIT):
    data = api_get(lang, {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
    })
    out = []
    for row in data.get("query", {}).get("search", []):
        snippet = TAG_RE.sub("", row.get("snippet", ""))
        snippet = re.sub(r"\s+", " ", snippet).strip()
        out.append((row["title"], snippet))
    return out


def random_title(lang):
    data = api_get(lang, {
        "action": "query",
        "list": "random",
        "rnnamespace": 0,
        "rnlimit": 1,
    })
    pages = data.get("query", {}).get("random", [])
    return pages[0]["title"] if pages else None


def translated_title(lang, title, target_lang):
    """The same article's title in another language, or None."""
    data = api_get(lang, {
        "action": "query",
        "titles": title,
        "prop": "langlinks",
        "lllang": target_lang,
        "redirects": 1,
    })
    for page in data.get("query", {}).get("pages", []):
        for row in page.get("langlinks", []):
            return row.get("title")
    return None


def article_url(lang, title):
    return f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# --------------------------------------------------------------- parsing


class Block:
    """One paragraph, heading or list item, with link offsets into its text."""

    __slots__ = ("kind", "text", "links", "indent", "bullet", "hid")

    def __init__(self, kind, text, links, indent=0, bullet="", hid=None):
        self.kind = kind
        self.text = text
        self.links = links
        self.indent = indent
        self.bullet = bullet
        self.hid = hid


def link_target(href):
    """Classify an href into ('page'|'anchor'|'external', target) or None."""
    if not href:
        return None
    if href.startswith("/wiki/"):
        title = urllib.parse.unquote(href[6:]).replace("_", " ")
        if not title or title.startswith(BAD_PREFIXES):
            return None
        return "page", title.split("#")[0]
    if href.startswith("#"):
        anchor = urllib.parse.unquote(href[1:])
        return ("anchor", anchor) if anchor else None
    if href.startswith(("http://", "https://")):
        return "external", href
    if href.startswith("//"):
        return "external", "https:" + href
    return None


class ArticleParser(HTMLParser):
    """Turns MediaWiki's article HTML into a flat list of Blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.buf = []
        self.pos = 0
        self.links = []
        self.kind = "p"
        self.hid = None
        self.bullet = ""
        self.depth = 0
        self.skip_at = None
        self.pre = False
        self.lists = []
        self.a_stack = []

    # -- text accumulation --

    def _text(self, s):
        if not s:
            return
        if not self.pre:
            if s.startswith(" ") and (not self.buf or self.buf[-1].endswith(" ")):
                s = s.lstrip(" ")
                if not s:
                    return
        self.buf.append(s)
        self.pos += len(s)

    def _flush(self):
        text = "".join(self.buf)
        if not self.pre:
            text = text.strip()
        else:
            text = text.strip("\n")
        if text:
            indent = max(0, len(self.lists) - 1)
            self.blocks.append(Block(self.kind, text, self.links, indent, self.bullet, self.hid))
        self.buf = []
        self.pos = 0
        self.links = []
        self.kind = "p"
        self.hid = None
        self.bullet = ""
        self.a_stack = []

    def _marker(self, text):
        self._flush()
        self.blocks.append(Block("marker", text, []))

    # -- HTMLParser hooks --

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            if self.skip_at is None and tag == "br":
                self._text("\n" if self.pre else " ")
            return

        self.depth += 1
        if self.skip_at is not None:
            return

        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())

        if tag in SKIP_TAGS or classes & SKIP_CLASSES:
            if tag == "table":
                self._marker("[ infobox omitted ]" if "infobox" in classes else "[ table omitted ]")
            self.skip_at = self.depth
            return

        if tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            self.kind = "li"
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.bullet = f"{self.lists[-1][1]}. "
            else:
                self.bullet = "• "
        elif tag in HEADING_TAGS:
            self._flush()
            self.kind = tag
            self.hid = attr.get("id")
        elif tag in BLOCK_TAGS:
            self._flush()
            self.kind = "quote" if tag == "blockquote" else tag
            if tag == "pre":
                self.pre = True
        elif tag == "div":
            if self.kind != "li":
                self._flush()
        elif tag == "a":
            self.a_stack.append((self.pos, link_target(attr.get("href"))))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return

        if self.skip_at is not None:
            if self.depth <= self.skip_at:
                self.skip_at = None
            self.depth -= 1
            return

        if tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag == "a":
            if self.a_stack:
                start, target = self.a_stack.pop()
                if target and self.pos > start:
                    self.links.append((start, self.pos, target[0], target[1]))
        elif tag in HEADING_TAGS or tag in BLOCK_TAGS:
            self._flush()
            if tag == "pre":
                self.pre = False
        elif tag == "div" and self.kind != "li":
            self._flush()

        self.depth -= 1

    def handle_data(self, data):
        if self.skip_at is not None:
            return
        self._text(data if self.pre else re.sub(r"[ \t\r\n]+", " ", data))

    def close(self):
        super().close()
        self._flush()


def parse_article(html_text):
    parser = ArticleParser()
    parser.feed(html_text)
    parser.close()
    return parser.blocks


# ---------------------------------------------------------------- layout


class Link:
    __slots__ = ("kind", "target", "label", "line", "col0", "col1")

    def __init__(self, kind, target, label, line, col0, col1):
        self.kind = kind
        self.target = target
        self.label = label
        self.line = line
        self.col0 = col0
        self.col1 = col1


class Doc:
    """A laid-out article: display lines plus everything you can navigate to."""

    def __init__(self, lines, links, headings, anchors):
        self.lines = lines          # (text, kind, [(col0, col1, link_index)])
        self.links = links
        self.headings = headings    # (line, level, text)
        self.anchors = anchors      # heading id -> line


def wrap_block(text, links, width, first_prefix, cont_prefix):
    """Word-wrap, returning [(line_text, [(col0, col1, link_local_index)])]."""
    words = [(m.start(), m.group()) for m in re.finditer(r"\S+", text)]
    if not words:
        return [(first_prefix.rstrip(), [])]

    rows = []
    placed = []
    prefix = first_prefix
    col = len(prefix)

    def flush():
        nonlocal placed, prefix, col
        if not placed:
            return
        line = prefix + " ".join(word for _, word, _ in placed)
        spans = []
        for idx, (start, end, _kind, _target) in enumerate(links):
            lo = hi = None
            for src, word, wcol in placed:
                ov0, ov1 = max(start, src), min(end, src + len(word))
                if ov0 < ov1:
                    c0 = wcol + (ov0 - src)
                    c1 = wcol + (ov1 - src)
                    lo = c0 if lo is None else min(lo, c0)
                    hi = c1 if hi is None else max(hi, c1)
            if lo is not None:
                spans.append((lo, hi, idx))
        rows.append((line, spans))
        placed = []
        prefix = cont_prefix
        col = len(prefix)

    avail = max(20, width)
    for src, word in words:
        need = len(word) + (1 if placed else 0)
        if placed and col + need > avail:
            flush()
            need = len(word)
        col += 1 if placed else 0
        placed.append((src, word, col))
        col += len(word)
    flush()
    return rows


def prune_empty_sections(blocks):
    """Drop headings with nothing under them (References, Notes: their lists are skipped)."""
    kept = []
    for block in reversed(blocks):
        level = HEADING_TAGS.get(block.kind)
        if level:
            following = kept[-1] if kept else None
            next_level = HEADING_TAGS.get(following.kind) if following else None
            if following is None or (next_level and next_level <= level):
                continue
        kept.append(block)
    kept.reverse()
    return kept


def layout(blocks, width):
    blocks = prune_empty_sections(blocks)
    lines = []
    links = []
    headings = []
    anchors = {}

    def add(text, kind="p", spans=None):
        lines.append((text, kind, spans or []))

    for block in blocks:
        level = HEADING_TAGS.get(block.kind)

        if block.kind == "marker":
            if lines and lines[-1][0]:
                add("")
            add(block.text, "dim")
            continue

        if level:
            if lines:
                add("")
            first = cont = ""
            kind = "h2" if level <= 2 else "h3"
        elif block.kind == "li":
            first = "  " * block.indent + block.bullet
            cont = "  " * block.indent + " " * len(block.bullet)
            kind = "p"
        elif block.kind == "dd":
            first = cont = "    "
            kind = "p"
        elif block.kind == "dt":
            first = cont = "  "
            kind = "h3"
        elif block.kind == "quote":
            first = cont = "  │ "
            kind = "quote"
        elif block.kind == "pre":
            for raw in block.text.split("\n"):
                add("  " + raw, "pre")
            add("")
            continue
        else:
            first = cont = ""
            kind = "p"

        rows = wrap_block(block.text, block.links, width, first, cont)
        base = len(lines)
        seen = {}
        for row_no, (text, spans) in enumerate(rows):
            mapped = []
            for col0, col1, local in spans:
                if local not in seen:
                    start, end, lkind, target = block.links[local]
                    links.append(Link(lkind, target, block.text[start:end],
                                      base + row_no, col0, col1))
                    seen[local] = len(links) - 1
                mapped.append((col0, col1, seen[local]))
            add(text, kind, mapped)

        if level:
            if level <= 2 and rows:
                add("─" * min(width, max(4, len(rows[-1][0]))), "rule")
            headings.append((base, level, block.text))
            if block.hid:
                anchors[block.hid] = base
        add("")

    while lines and not lines[-1][0]:
        lines.pop()
    return Doc(lines, links, headings, anchors)


# ------------------------------------------------------------------ UI


HELP_TEXT = [
    ("Moving", ""),
    ("  j / k, arrows", "scroll one line"),
    ("  space / b", "page down / page up"),
    ("  g / G", "jump to top / bottom"),
    ("Links", ""),
    ("  Tab / Shift-Tab", "select next / previous link"),
    ("  Enter", "follow the selected link"),
    ("  Backspace / [", "back      ]  forward"),
    ("Finding", ""),
    ("  s", "search Wikipedia"),
    ("  /", "find in this article   n / N  next / previous match"),
    ("  t", "table of contents"),
    ("  r", "random article"),
    ("Other", ""),
    ("  L", "switch language edition"),
    ("  o", "open this article in the browser"),
    ("  y", "copy the article URL"),
    ("  ? ", "this help          q  quit"),
]


class Browser:
    def __init__(self, stdscr, lang):
        self.stdscr = stdscr
        self.lang = lang
        self.doc = None
        self.title = ""
        self.blocks = []
        self.is_article = False
        self.load_failed = None
        self.top = 0
        self.sel = -1
        self.message = ""
        self.cache = {}
        self.history = []
        self.hindex = -1
        self.matches = []
        self.match_at = -1
        self.query = ""
        self.width = 80
        self._init_colors()

    # -- colours --

    def _init_colors(self):
        self.color = {}
        if not curses.has_colors():
            self.color = {k: 0 for k in ("h", "link", "sel", "ext", "dim", "bar", "err", "hit")}
            self.color["h"] = curses.A_BOLD
            self.color["link"] = curses.A_UNDERLINE
            self.color["sel"] = curses.A_REVERSE
            self.color["ext"] = curses.A_UNDERLINE
            self.color["dim"] = curses.A_DIM
            self.color["bar"] = curses.A_REVERSE
            self.color["err"] = curses.A_BOLD
            self.color["hit"] = curses.A_REVERSE
            return
        curses.start_color()
        curses.use_default_colors()
        pairs = [
            (1, curses.COLOR_CYAN), (2, curses.COLOR_BLUE), (3, curses.COLOR_MAGENTA),
            (4, curses.COLOR_RED), (5, curses.COLOR_YELLOW), (6, curses.COLOR_WHITE),
        ]
        for idx, fg in pairs:
            curses.init_pair(idx, fg, -1)
        self.color = {
            "h": curses.color_pair(1) | curses.A_BOLD,
            "link": curses.color_pair(2),
            "sel": curses.color_pair(2) | curses.A_REVERSE | curses.A_BOLD,
            "ext": curses.color_pair(3),
            "dim": curses.A_DIM,
            "bar": curses.color_pair(6) | curses.A_REVERSE,
            "err": curses.color_pair(4) | curses.A_BOLD,
            "hit": curses.color_pair(5) | curses.A_REVERSE,
        }

    # -- geometry --

    @property
    def body_height(self):
        return max(1, self.stdscr.getmaxyx()[0] - 2)

    def text_width(self):
        cols = self.stdscr.getmaxyx()[1]
        return max(20, min(MAX_WIDTH, cols - 2 * LEFT_MARGIN))

    # -- network with on-screen feedback --

    def busy(self, text):
        height, width = self.stdscr.getmaxyx()
        note = f" {text} "[: max(0, width - 1)]
        try:
            self.stdscr.addstr(height - 1, 0, note.ljust(width - 1), self.color["bar"])
        except curses.error:
            pass
        self.stdscr.refresh()

    def call(self, what, fn, *args):
        self.busy(what)
        try:
            return fn(*args), None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return None, "Wikipedia is rate-limiting requests - wait a moment"
            return None, f"Wikipedia returned HTTP {exc.code}"
        except (urllib.error.URLError, json.JSONDecodeError,
                TimeoutError, OSError, KeyError, ValueError) as exc:
            return None, str(exc)

    # -- loading articles --

    def load(self, title, lang=None, push=True, anchor=None):
        lang = lang or self.lang
        key = (lang, title)
        if key in self.cache:
            real_title, blocks = self.cache[key]
        else:
            result, error = self.call(f"Loading {title}...", fetch_article, lang, title)
            if error:
                self.message = f"Could not load '{title}': {error}"
                self.load_failed = "error"
                return False
            if result is None:
                self.message = f"No article '{title}'."
                self.load_failed = "missing"
                return False
            real_title, html_text = result
            blocks = parse_article(html_text)
            self.cache[(lang, real_title)] = (real_title, blocks)
            self.cache[key] = (real_title, blocks)

        if push:
            self.remember()
            del self.history[self.hindex + 1:]
            self.history.append([lang, real_title, 0, -1])
            self.hindex = len(self.history) - 1

        self.lang = lang
        self.title = real_title
        self.blocks = blocks
        self.is_article = True
        self.load_failed = None
        self.relayout()
        self.top = 0
        self.sel = -1
        self.matches = []
        self.match_at = -1
        self.message = ""
        if anchor:
            self.goto_anchor(anchor)
        return True

    def show_notice(self, heading, lines):
        """Render a plain message as the current page, so it stays on screen."""
        self.blocks = [Block("h2", heading, [])] + [Block("p", line, []) for line in lines]
        self.title = heading
        self.is_article = False
        self.relayout()
        self.top = 0
        self.sel = -1
        self.matches = []
        self.match_at = -1

    def relayout(self):
        self.width = self.text_width()
        self.doc = layout(self.blocks, self.width)

    def remember(self):
        if 0 <= self.hindex < len(self.history):
            self.history[self.hindex][2] = self.top
            self.history[self.hindex][3] = self.sel

    def go_history(self, step):
        target = self.hindex + step
        if not (0 <= target < len(self.history)):
            self.message = "No further history." if step > 0 else "At the first article."
            return
        self.remember()
        self.hindex = target
        lang, title, top, sel = self.history[target]
        if self.load(title, lang=lang, push=False):
            self.top = min(top, self.max_top())
            self.sel = sel

    # -- movement --

    def max_top(self):
        return max(0, len(self.doc.lines) - self.body_height)

    def scroll(self, delta):
        self.top = max(0, min(self.max_top(), self.top + delta))

    def show_line(self, line):
        height = self.body_height
        if not (self.top <= line < self.top + height):
            self.top = max(0, min(self.max_top(), line - height // 3))

    def goto_anchor(self, anchor):
        line = self.doc.anchors.get(anchor)
        if line is None:
            for hid, pos in self.doc.anchors.items():
                if hid.lower() == anchor.lower():
                    line = pos
                    break
        if line is None:
            self.message = f"No section '{anchor}' on this page."
            return
        self.top = min(self.max_top(), line)
        self.sel = -1

    def select_link(self, step):
        links = self.doc.links
        if not links:
            self.message = "No links on this page."
            return
        if self.sel < 0:
            visible = [i for i, ln in enumerate(links) if ln.line >= self.top]
            self.sel = (visible[0] if visible else 0) if step > 0 else len(links) - 1
        else:
            self.sel = (self.sel + step) % len(links)
        self.show_line(links[self.sel].line)

    def follow(self):
        if not (0 <= self.sel < len(self.doc.links)):
            self.message = "Select a link with Tab first."
            return
        link = self.doc.links[self.sel]
        if link.kind == "page":
            self.load(link.target)
        elif link.kind == "anchor":
            self.goto_anchor(link.target)
        else:
            self.open_browser(link.target)

    # -- external helpers --

    def open_browser(self, url):
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, url], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.message = f"Opened {url}"
        except (OSError, subprocess.CalledProcessError):
            self.message = f"Could not open a browser ({url})"

    def no_article(self):
        self.message = "No article open - press s to search."

    def copy_url(self):
        url = article_url(self.lang, self.title)
        try:
            subprocess.run(["pbcopy"], input=url.encode(), check=True)
            self.message = "URL copied."
        except (OSError, subprocess.CalledProcessError):
            self.message = "Copy failed (pbcopy unavailable)."

    # -- find in page --

    def find(self, query):
        self.query = query
        needle = query.lower()
        self.matches = []
        for idx, (text, _kind, _spans) in enumerate(self.doc.lines):
            start = 0
            low = text.lower()
            while True:
                at = low.find(needle, start)
                if at < 0:
                    break
                self.matches.append((idx, at, len(query)))
                start = at + max(1, len(query))
        if not self.matches:
            self.message = f"'{query}' not found."
            self.match_at = -1
            return
        self.match_at = -1
        self.next_match(1)

    def next_match(self, step):
        if not self.matches:
            self.message = "Nothing to find yet - press / first."
            return
        self.match_at = (self.match_at + step) % len(self.matches)
        self.show_line(self.matches[self.match_at][0])
        self.message = f"Match {self.match_at + 1} of {len(self.matches)}"

    # -- drawing --

    def draw(self):
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        body = max(1, height - 2)

        self.draw_status(width)

        visible_matches = {}
        for line, col, length in self.matches:
            visible_matches.setdefault(line, []).append((col, length))

        for row in range(body):
            index = self.top + row
            if index >= len(self.doc.lines):
                break
            text, kind, spans = self.doc.lines[index]
            y = row + 1
            attr = {"h2": self.color["h"], "h3": self.color["h"], "rule": self.color["dim"],
                    "dim": self.color["dim"], "quote": self.color["dim"],
                    "pre": self.color["dim"]}.get(kind, curses.A_NORMAL)
            self.put(y, LEFT_MARGIN, text, attr, width)

            for col0, col1, link_index in spans:
                link = self.doc.links[link_index]
                style = self.color["sel"] if link_index == self.sel else (
                    self.color["ext"] if link.kind == "external" else self.color["link"])
                self.highlight(y, LEFT_MARGIN + col0, col1 - col0, style, width)

            for col, length in visible_matches.get(index, []):
                self.highlight(y, LEFT_MARGIN + col, length, self.color["hit"], width)

        self.draw_footer(height, width)
        self.stdscr.refresh()

    def put(self, y, x, text, attr, width):
        space = width - x - 1
        if space <= 0:
            return
        try:
            self.stdscr.addstr(y, x, text[:space], attr)
        except curses.error:
            pass

    def highlight(self, y, x, length, attr, width):
        length = min(length, width - x - 1)
        if length <= 0 or x >= width - 1:
            return
        try:
            self.stdscr.chgat(y, x, length, attr)
        except curses.error:
            pass

    def draw_status(self, width):
        total = max(1, len(self.doc.lines))
        seen = min(total, self.top + self.body_height)
        pct = int(seen * 100 / total)
        left = f" {self.title} "
        right = f" {self.lang}.wikipedia  {pct:>3}% "
        bar = left + " " * max(0, width - 1 - len(left) - len(right)) + right
        self.put(0, 0, bar.ljust(width - 1), self.color["bar"], width)

    def draw_footer(self, height, width):
        if self.message:
            self.put(height - 1, 0, self.message[:width - 1].ljust(width - 1),
                     self.color["err"] if "not" in self.message.lower()
                     or "could" in self.message.lower() else self.color["bar"], width)
        else:
            hint = " s search  Tab link  Enter follow  Backspace back  t contents  / find  ? help  q quit "
            self.put(height - 1, 0, hint[:width - 1].ljust(width - 1), self.color["bar"], width)

    # -- modal widgets --

    def prompt(self, label, initial=""):
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                height, width = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(height - 1, 0, text[:width - 1].ljust(width - 1), self.color["bar"], width)
                self.stdscr.move(height - 1, min(len(text), width - 2))
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf).strip()
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":
                        buf = []
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf).strip()
                elif key == curses.KEY_RESIZE:
                    self.relayout()
                    self.draw()
        finally:
            curses.curs_set(0)

    def pick(self, title, rows, start=0):
        """Modal list. rows is [(main, detail)]; returns the chosen index or None."""
        if not rows:
            return None
        sel = start
        top = 0
        while True:
            height, width = self.stdscr.getmaxyx()
            self.stdscr.erase()
            self.put(0, 0, f" {title} ".ljust(width - 1), self.color["bar"], width)
            body = max(1, height - 2)
            if sel < top:
                top = sel
            elif sel >= top + body:
                top = sel - body + 1
            for row in range(body):
                index = top + row
                if index >= len(rows):
                    break
                main, detail = rows[index]
                marker = "> " if index == sel else "  "
                line = f"{marker}{main}"
                if detail:
                    line = f"{line}  -  {detail}"
                attr = curses.A_REVERSE if index == sel else curses.A_NORMAL
                self.put(row + 1, 0, line[:width - 1].ljust(width - 1), attr, width)
            self.put(height - 1, 0,
                     " Enter open   Esc cancel   arrows / j k to move ".ljust(width - 1),
                     self.color["bar"], width)
            self.stdscr.refresh()

            key = self.stdscr.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                sel = min(len(rows) - 1, sel + 1)
            elif key in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                sel = min(len(rows) - 1, sel + body)
            elif key == curses.KEY_PPAGE:
                sel = max(0, sel - body)
            elif key == curses.KEY_HOME or key == ord("g"):
                sel = 0
            elif key == curses.KEY_END or key == ord("G"):
                sel = len(rows) - 1
            elif key in (curses.KEY_ENTER, 10, 13):
                return sel
            elif key in (27, ord("q")):
                return None
            elif key == curses.KEY_RESIZE:
                self.relayout()

    def show_help(self):
        rows = [(label, detail) for label, detail in HELP_TEXT]
        self.pick("Keys  (Esc to close)", rows)

    # -- commands --

    def do_search(self, initial=""):
        query = self.prompt(" Search Wikipedia: ", initial)
        if not query:
            return
        results, error = self.call(f"Searching {query}...", search_titles, self.lang, query)
        if error:
            self.message = f"Search failed: {error}"
            return
        if not results:
            self.message = f"Nothing found for '{query}'."
            return
        choice = self.pick(f"Results for '{query}'", results)
        if choice is not None:
            self.load(results[choice][0])

    def do_contents(self):
        if not self.doc.headings:
            self.message = "This article has no sections."
            return
        rows = [("  " * max(0, level - 2) + text, "") for _line, level, text in self.doc.headings]
        here = 0
        for index, (line, _level, _text) in enumerate(self.doc.headings):
            if line <= self.top:
                here = index
        choice = self.pick(f"Contents of {self.title}", rows, here)
        if choice is not None:
            self.top = min(self.max_top(), self.doc.headings[choice][0])
            self.sel = -1

    def do_random(self):
        title, error = self.call("Fetching a random article...", random_title, self.lang)
        if error or not title:
            self.message = f"Could not fetch a random article: {error or 'no result'}"
            return
        self.load(title)

    def do_language(self):
        code = self.prompt(" Language code (en, nl, de, fr, ...): ")
        if not code:
            return
        code = code.lower()
        if code == self.lang:
            return
        other, error = self.call(f"Looking for the {code} version...",
                                 translated_title, self.lang, self.title, code)
        if error:
            self.message = f"Could not switch language: {error}"
            return
        if other:
            self.load(other, lang=code)
        else:
            self.message = f"No {code} version of '{self.title}'."
            self.lang = code
            self.do_search(self.title)

    # -- main loop --

    def run(self):
        while True:
            self.draw()
            key = self.stdscr.getch()
            message_was = self.message
            self.message = ""

            if key in (ord("q"), ord("Q")):
                return
            elif key == curses.KEY_RESIZE:
                self.relayout()
                self.top = min(self.top, self.max_top())
            elif key in (curses.KEY_DOWN, ord("j")):
                self.scroll(1)
            elif key in (curses.KEY_UP, ord("k")):
                self.scroll(-1)
            elif key in (curses.KEY_NPAGE, ord(" "), 4):
                self.scroll(self.body_height - 2)
            elif key in (curses.KEY_PPAGE, ord("b"), 21):
                self.scroll(-(self.body_height - 2))
            elif key in (ord("g"), curses.KEY_HOME):
                self.top = 0
            elif key in (ord("G"), curses.KEY_END):
                self.top = self.max_top()
            elif key == ord("\t"):
                self.select_link(1)
            elif key == curses.KEY_BTAB:
                self.select_link(-1)
            elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                self.follow()
            elif key in (curses.KEY_BACKSPACE, 127, 8, ord("["), curses.KEY_LEFT):
                self.go_history(-1)
            elif key == ord("]"):
                self.go_history(1)
            elif key == ord("s"):
                self.do_search()
            elif key == ord("/"):
                query = self.prompt(" Find in article: ", self.query)
                if query:
                    self.find(query)
            elif key == ord("n"):
                self.next_match(1)
            elif key == ord("N"):
                self.next_match(-1)
            elif key == ord("t"):
                self.do_contents()
            elif key == ord("r"):
                self.do_random()
            elif key == ord("L"):
                self.do_language() if self.is_article else self.no_article()
            elif key == ord("o"):
                (self.open_browser(article_url(self.lang, self.title))
                 if self.is_article else self.no_article())
            elif key == ord("y"):
                self.copy_url() if self.is_article else self.no_article()
            elif key == ord("?"):
                self.show_help()
            else:
                self.message = message_was


def start(stdscr, lang, topic):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    stdscr.keypad(True)
    browser = Browser(stdscr, lang)
    browser.doc = Doc([], [], [], {})
    welcome = [
        "Press s to search Wikipedia.",
        "Press r to open a random article.",
        "Press ? for the full list of keys, q to quit.",
    ]

    if topic:
        if not browser.load(topic):
            failure = browser.message
            if browser.load_failed == "missing":
                browser.show_notice(f"No article '{topic}'", welcome)
                browser.do_search(topic)
            else:
                browser.show_notice("Could not reach Wikipedia", [failure] + welcome)
    else:
        browser.show_notice("wiki_tui", welcome)
        browser.do_search()

    browser.run()


def main():
    parser = argparse.ArgumentParser(description="Browse Wikipedia in a terminal UI.")
    parser.add_argument("topic", nargs="*", help="article title to open on start")
    parser.add_argument("--lang", default="en", help="Wikipedia language code (default: en)")
    args = parser.parse_args()

    if not sys.stdout.isatty():
        print("wiki_tui needs a terminal; use wiki_reader.py for piped output.", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(start, args.lang, " ".join(args.topic).strip())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
