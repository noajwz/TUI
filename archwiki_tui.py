#!/usr/bin/env python3
"""Terminal UI for browsing the Arch Wiki: search, follow links, copy commands."""

import argparse
import base64
import curses
import json
import locale
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "archwiki-tui/1.0 (terminal Arch Wiki browser)"
BASE_URL = "https://wiki.archlinux.org"
SITE_NAME = "ArchWiki"
HOME_TITLE = "Main page"

MAX_WIDTH = 96
LEFT_MARGIN = 2
TIMEOUT = 15
SEARCH_LIMIT = 20
MIN_CELL = 6        # a table header is cut no narrower than this

VOID_TAGS = {"br", "img", "link", "meta", "hr", "wbr", "input", "col", "source", "track"}
SKIP_TAGS = {"script", "style", "figure", "audio", "video", "form", "noscript"}
SKIP_CLASSES = {
    "shortdescription", "mw-editsection", "reference", "noprint", "navbox",
    "navbox-styles", "navbar", "metadata", "mw-references-wrap", "references",
    "reflist", "catlinks", "printfooter", "mw-empty-elt", "thumb", "gallery", "toc",
    "mw-jump-link",
}
# Template boxes, checked before SKIP_CLASSES: the "out of date" banners are also noprint.
BOX_CLASSES = {
    "archwiki-template-box-note": "note",
    "archwiki-template-box-tip": "tip",
    "archwiki-template-box-warning": "warning",
    "archwiki-template-message": "message",                 # out of date, needs expansion, ...
    "archwiki-template-meta-related-articles": "related",   # drawn as one line of links
}
# Yes / Partial / No cells in hardware tables carry their meaning only as a class colour.
CELL_STYLES = {"archwiki-template-g": "good", "archwiki-template-y": "meh",
               "archwiki-template-r": "bad"}
INLINE_STYLES = {"code": "code", "kbd": "code", "tt": "code", "samp": "code",
                 "b": "b", "strong": "b", "i": "i", "em": "i", "var": "i"}
# Namespaces that are not readable articles.
BAD_PREFIXES = ("File:", "Image:", "Special:", "Template:", "Template talk:", "Category:",
                "Help:", "ArchWiki:", "Talk:", "Module:", "MediaWiki:", "User:", "User talk:")
# Translations live on the same wiki as "Title (Language)"; random skips them.
TRANSLATED_RE = re.compile(
    r" \((?:العربية|Български|Bosanski|Català|Čeština|Dansk|Deutsch|Ελληνικά|English|"
    r"Esperanto|Español|فارسی|Suomi|Français|עברית|Hrvatski|Magyar|Bahasa Indonesia|"
    r"Italiano|日本語|한국어|Lietuvių|Norsk Bokmål|Nederlands|Polski|Português|Română|"
    r"Русский|Slovenčina|Српски|Svenska|ไทย|Türkçe|Українська|Tiếng Việt|简体中文|"
    r"正體中文|粵語)\)$")

HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = {"p", "li", "dt", "dd", "blockquote", "pre", "figcaption"}
PRE_KINDS = {"pre", "prehead", "prebody"}
PROMPTS = ("$ ", "# ")
# Drawn by layout() rather than taken from the article, so not counted by text_size().
DRAWN_CHARS = str.maketrans("", "", "│┃┌┐└┘├┤┬┴┼─↪")

TAG_RE = re.compile(r"<[^>]+>")
REDIRECT_RE = re.compile(r"#redirect \[\[(.+?)\]\]", re.I)


# ----------------------------------------------------------------- API


def api_get(params):
    params = {**params, "format": "json", "formatversion": 2}
    url = f"{BASE_URL}/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.load(resp)


def fetch_article(title):
    """Return (real_title, html) or None when the page does not exist."""
    data = api_get({
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


def search_titles(query, limit=SEARCH_LIMIT):
    data = api_get({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
    })
    out = []
    for row in data.get("query", {}).get("search", []):
        snippet = TAG_RE.sub("", row.get("snippet", ""))
        snippet = re.sub(r"\s+", " ", snippet).strip()
        redirect = REDIRECT_RE.match(snippet)
        if redirect:
            snippet = "-> " + redirect.group(1)
        out.append((row["title"], snippet))
    # Translations share the wiki and crowd the results; English first, order kept.
    return sorted(out, key=lambda row: bool(TRANSLATED_RE.search(row[0])))


def random_title():
    data = api_get({
        "action": "query",
        "list": "random",
        "rnnamespace": 0,
        "rnfilterredir": "nonredirects",
        "rnlimit": 20,
    })
    titles = [page["title"] for page in data.get("query", {}).get("random", [])]
    english = [title for title in titles if not TRANSLATED_RE.search(title)]
    return (english or titles or [None])[0]


def article_url(title):
    return f"{BASE_URL}/title/" + urllib.parse.quote(title.replace(" ", "_"))


# --------------------------------------------------------------- parsing


class Block:
    """One paragraph, heading, list item, code block or table, with marks into its text."""

    __slots__ = ("kind", "text", "links", "styles", "indent", "bullet", "hid",
                 "box", "box_id", "table")

    def __init__(self, kind, text, links, styles=(), indent=0, bullet="", hid=None,
                 box=None, box_id=0, table=None):
        self.kind = kind
        self.text = text
        self.links = links          # (start, end, kind, target)
        self.styles = list(styles)  # (start, end, style)
        self.indent = indent        # how many lists deep
        self.bullet = bullet
        self.hid = hid
        self.box = box              # note / tip / warning / message / related
        self.box_id = box_id        # tells two boxes of the same kind apart
        self.table = table          # [[Cell]] for kind "table"


class Cell:
    __slots__ = ("text", "links", "styles", "span", "rows", "header", "style")

    def __init__(self, span=1, rows=1, header=False, style=None):
        self.text = ""
        self.links = []
        self.styles = []
        self.span = span
        self.rows = rows
        self.header = header
        self.style = style


def link_target(href):
    """Classify an href into ('page'|'anchor'|'man'|'external', target) or None.

    A page target keeps its '#section', so following it lands on that section."""
    if not href:
        return None
    if href.startswith(BASE_URL + "/"):
        href = href[len(BASE_URL):]
    if href.startswith("/title/"):
        page, _, anchor = urllib.parse.unquote(href[7:]).partition("#")
        title = page.replace("_", " ")
        if not title or title.startswith(BAD_PREFIXES):
            return None
        return "page", f"{title}#{anchor}" if anchor else title
    if href.startswith("#"):
        anchor = urllib.parse.unquote(href[1:])
        return ("anchor", anchor) if anchor else None
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("https://man.archlinux.org/man/"):
        return "man", href
    if href.startswith(("http://", "https://")):
        return "external", href
    return None  # /index.php?...: edit links and links to pages that don't exist


def span_int(value):
    try:
        return max(1, min(50, int(value)))
    except (TypeError, ValueError):
        return 1


class ArticleParser(HTMLParser):
    """Turns the Arch Wiki's article HTML into a flat list of Blocks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.buf = []
        self.pos = 0
        self.links = []
        self.styles = []
        self.kind = "p"
        self.hid = None
        self.bullet = ""
        self.depth = 0
        self.skip_at = None
        self.pre = False
        self.lists = []
        self.a_stack = []
        self.style_stack = []       # [start, style] of open <code>, <b>, <i>, ...
        self.boxes = []             # (depth, kind, id) of open template boxes
        self.box_count = 0
        self.label_pending = False  # the first <strong> in a box is its "Note" / "Tip"
        self.table = None           # rows of Cells while inside a <table>
        self.table_depth = 0
        self.cell = None
        self.caption = None

    # -- text accumulation --

    def _text(self, s):
        if not s:
            return
        if not self.pre:
            if s.startswith(" ") and (not self.buf or self.buf[-1].endswith(" ")):
                s = s.lstrip(" ")
                if not s:
                    return
        if s.strip():
            self.label_pending = False
        self.buf.append(s)
        self.pos += len(s)

    def _reset(self):
        self.buf = []
        self.pos = 0
        self.links = []
        self.styles = []
        self.kind = "p"
        self.hid = None
        self.bullet = ""
        self.a_stack = []

    def _take(self):
        """The text gathered so far, stripped, with its marks moved to match."""
        for entry in self.style_stack:  # a style still open continues in the next block
            if self.pos > entry[0]:
                self.styles.append((entry[0], self.pos, entry[1]))
            entry[0] = 0
        raw = "".join(self.buf)
        text = raw.strip("\n") if self.pre else raw.strip()
        lead = len(raw) - len(raw.lstrip("\n" if self.pre else None))

        def moved(marks):
            out = []
            for start, end, *rest in marks:
                start, end = max(0, start - lead), min(len(text), end - lead)
                if start < end:
                    out.append((start, end, *rest))
            return out

        return text, moved(self.links), moved(self.styles)

    def _flush(self):
        if self.cell is not None:  # a cell collects everything up to its </td>
            return
        text, links, styles = self._take()
        if text:
            _, box, box_id = self.boxes[-1] if self.boxes else (None, None, 0)
            self.blocks.append(Block(self.kind, text, links, styles, len(self.lists),
                                     self.bullet, self.hid, box, box_id))
        self._reset()

    def _break(self):
        """A block boundary: a new Block, or just a space inside a table cell."""
        if self.cell is not None:
            self._text(" ")
        else:
            self._flush()

    # -- tables --

    def _table_tag(self, tag, attr, classes):
        """Handle a table structure tag; returns False for anything else."""
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self._flush()
                self.table = []
                self.caption = None
            return True
        if self.table is None or tag not in ("tr", "td", "th", "caption", "thead",
                                             "tbody", "tfoot"):
            return False
        if self.table_depth > 1:  # a table nested in a cell is flattened into its text
            if tag in ("td", "th", "tr"):
                self._text(" ")
            return True
        if tag == "tr":
            self.table.append([])
        elif tag in ("td", "th", "caption"):
            style = next((CELL_STYLES[c] for c in classes if c in CELL_STYLES), None)
            self.cell = Cell(span_int(attr.get("colspan")), span_int(attr.get("rowspan")),
                             tag != "td", style)
            if tag == "caption":
                self.caption = self.cell
            elif not self.table:
                self.table.append([])
            self._reset()
        return True

    def _end_table_tag(self, tag):
        if tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                rows = [row for row in self.table if row]
                self.table, self.cell = None, None
                if self.caption is not None and self.caption.text:
                    self.blocks.append(Block("dt", self.caption.text, self.caption.links,
                                             self.caption.styles, len(self.lists)))
                if any(cell.text for row in rows for cell in row):
                    self.blocks.append(Block("table", "", [], (), len(self.lists),
                                             table=rows))
                self._reset()
            return True
        if self.table is None or self.table_depth > 1:
            return False
        if tag in ("td", "th", "caption") and self.cell is not None:
            cell = self.cell
            cell.text, cell.links, cell.styles = self._take()
            if cell is not self.caption:
                self.table[-1].append(cell)
            self.cell = None
            self._reset()
            return True
        return tag in ("tr", "thead", "tbody", "tfoot")

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

        box = next((BOX_CLASSES[c] for c in classes if c in BOX_CLASSES), None)
        if box and self.table is None:
            self._flush()
            self.box_count += 1
            self.boxes.append((self.depth, box, self.box_count))
            self.label_pending = box in ("note", "tip", "warning")
            return

        if tag in SKIP_TAGS or classes & SKIP_CLASSES:
            self.skip_at = self.depth
            return

        if self._table_tag(tag, attr, classes):
            return

        style = INLINE_STYLES.get(tag)
        if style:
            if style == "b" and self.label_pending:
                style = "label"
            elif style == "i" and (self.pre or any(s == "code" for _, s in self.style_stack)):
                style = "ph"  # a placeholder in a command: <i>package_name</i>
            self.style_stack.append([self.pos, style])
            return

        if self.boxes and self.boxes[-1][1] == "related" and tag in ("ul", "ol", "li", "p"):
            if tag == "li" and self.buf and not self.buf[-1].endswith(": "):
                self._text(" · ")
            return

        if self.cell is not None and tag in ("li", "ul", "ol", "p", "div", "dt", "dd"):
            self._text(" • " if tag == "li" and self.buf else " ")
            return

        if tag in ("ul", "ol", "dl"):
            self._flush()
            if tag != "dl":
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
            self._break()
            if self.cell is not None:
                return
            self.kind = "quote" if tag == "blockquote" else tag
            if tag == "pre":
                self.pre = True
                # {{hc|file|contents}} is two <pre>s: a header without a bottom margin
                # and a body without a top one.
                css = (attr.get("style") or "").replace(" ", "")
                if "margin-bottom:0" in css:
                    self.kind = "prehead"
                elif "margin-top:0" in css:
                    self.kind = "prebody"
        elif tag == "div":
            if self.kind != "li":
                self._break()
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

        if self.boxes and self.depth == self.boxes[-1][0]:
            self._flush()
            self.boxes.pop()
            self.label_pending = False
            self.depth -= 1
            return

        self.depth -= 1
        if self._end_table_tag(tag):
            return

        if tag in INLINE_STYLES:
            if self.style_stack:
                start, style = self.style_stack.pop()
                if style == "label":
                    self._text(":")  # "Tip:", as the website shows it
                if self.pos > start:
                    self.styles.append((start, self.pos, style))
            return

        if self.boxes and self.boxes[-1][1] == "related" and tag in ("ul", "ol", "li", "p"):
            if tag == "p" and self.buf:  # "Related articles" heads the line of links
                self.styles.append((0, self.pos, "label"))
                self._text(": ")
            return

        if self.cell is not None and tag in ("li", "ul", "ol", "p", "div", "dt", "dd", "pre"):
            self._text(" ")
            return

        if tag in ("ul", "ol", "dl"):
            self._flush()
            if tag != "dl" and self.lists:
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

    def handle_data(self, data):
        if self.skip_at is not None:
            return
        if self.pre:
            self._text(data.replace("\t", "    "))
        else:
            self._text(re.sub(r"[ \t\r\n]+", " ", data))

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
        self.kind = kind        # page / anchor / man / external / code
        self.target = target    # for code: (text, prompts may be stripped)
        self.label = label
        self.line = line
        self.col0 = col0
        self.col1 = col1


class Doc:
    """A laid-out article: display lines plus everything you can navigate to."""

    def __init__(self, lines, links, headings, anchors):
        # (text, kind, [(col0, col1, link index or style name)], text size)
        self.lines = lines
        self.links = links
        self.headings = headings    # (line, level, text)
        self.anchors = anchors      # heading id -> line


def text_size(text):
    """Characters in a line that don't depend on the wrap width: not spaces, and not the
    bars, borders and rules that layout() draws."""
    return len("".join(text.split()).translate(DRAWN_CHARS))


def text_offset(doc, line):
    """Width-independent characters above a line, so a reading position survives re-wrapping."""
    return sum(entry[3] for entry in doc.lines[:line])


def line_at_offset(doc, offset):
    """The line holding the character text_offset() counted up to."""
    seen = 0
    for index, entry in enumerate(doc.lines):
        if seen + entry[3] > offset:
            return index
        seen += entry[3]
    return max(0, len(doc.lines) - 1)


def wrap_block(text, marks, width, first_prefix, cont_prefix):
    """Word-wrap, returning [(line_text, [(col0, col1, tag)])] for marks (start, end, tag).

    Words longer than the line are cut, so a long path or URL can't overflow."""
    room = max(1, width - max(len(first_prefix), len(cont_prefix)))
    words = []
    for m in re.finditer(r"\S+", text):
        for cut in range(0, len(m.group()), room):
            words.append((m.start() + cut, m.group()[cut:cut + room]))
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
        for start, end, tag in marks:
            lo = hi = None
            for src, word, wcol in placed:
                ov0, ov1 = max(start, src), min(end, src + len(word))
                if ov0 < ov1:
                    c0 = wcol + (ov0 - src)
                    c1 = wcol + (ov1 - src)
                    lo = c0 if lo is None else min(lo, c0)
                    hi = c1 if hi is None else max(hi, c1)
            if lo is not None:
                spans.append((lo, hi, tag))
        rows.append((line, spans))
        placed = []
        prefix = cont_prefix
        col = len(prefix)

    for src, word in words:
        need = len(word) + (1 if placed else 0)
        if placed and col + need > width:
            flush()
        col += 1 if placed else 0
        placed.append((src, word, col))
        col += len(word)
    flush()
    return rows


def block_marks(block, key=()):
    """A block's links and styles as wrap_block() marks, plus what each link tag means."""
    marks, info = [], {}
    for index, (start, end, kind, target) in enumerate(block.links):
        tag = ("L", *key, index)
        marks.append((start, end, tag))
        info[tag] = (kind, target, block.text[start:end])
    marks.extend(block.styles)
    return marks, info


def pre_rows(block, width, prefix):
    """A code block cut (not wrapped) to the width; continuation lines start with ↪."""
    room = max(8, width - len(prefix))
    rows = []
    offset = 0
    for raw in block.text.split("\n"):
        pieces = []
        at = 0
        while True:
            lead = "" if at == 0 else "↪ "
            take = room - len(lead)
            pieces.append((lead, at, raw[at:at + take]))
            at += take
            if at >= len(raw):
                break
        for lead, at, piece in pieces:
            line = prefix + lead + (piece or " ")  # a blank line still shows as selected
            x = len(prefix) + len(lead)
            spans = [(len(prefix), x, "dim")] if lead else []
            for start, end, style in block.styles:
                lo = max(start, offset + at)
                hi = min(end, offset + at + len(piece))
                if lo < hi:
                    spans.append((x + lo - offset - at, x + hi - offset - at, style))
            spans.append((len(prefix), len(line), ("C",)))
            rows.append((line, spans))
        offset += len(raw) + 1
    return rows


class Grid:
    """A table's cells placed on columns, honouring colspan and rowspan."""

    def __init__(self, rows):
        self.rows = []   # [(col, Cell or None for space a rowspan covers)]
        taken = set()
        for r, row in enumerate(rows):
            col = 0
            out = []
            for cell in row:
                while (r, col) in taken:
                    out.append((col, None))
                    col += 1
                out.append((col, cell))
                for dr in range(1, cell.rows):
                    for dc in range(cell.span):
                        taken.add((r + dr, col + dc))
                col += cell.span
            while (r, col) in taken:
                out.append((col, None))
                col += 1
            self.rows.append(out)
        self.ncols = max((col + (cell.span if cell else 1)
                          for row in self.rows for col, cell in row), default=0)
        for row in self.rows:
            end = row[-1][0] + (row[-1][1].span if row[-1][1] else 1) if row else 0
            row.extend((col, None) for col in range(end, self.ncols))

    @staticmethod
    def is_header(row):
        cells = [cell for _, cell in row if cell is not None]
        return bool(cells) and all(cell.header for cell in cells)

    def widths(self, width):
        """Column widths that fit, or None to stack the table instead.

        Words in the body are never cut, since a broken command or model name reads
        worse than a stacked record; a header may be cut down to MIN_CELL."""
        avail = width - (3 * self.ncols + 1)
        natural = [1] * self.ncols
        floor = [1] * self.ncols
        for row in self.rows:
            for col, cell in row:
                if cell is None or cell.span != 1:
                    continue
                natural[col] = max(natural[col], len(cell.text))
                longest = max([0] + [len(word) for word in cell.text.split()])
                floor[col] = max(floor[col], min(longest, MIN_CELL) if cell.header else longest)
        if sum(natural) <= avail:
            return natural
        if sum(floor) > avail:
            return None
        # What is left goes first to the columns that need least to stop wrapping, so
        # short commands stay on one line and long prose takes the rest.
        out = list(floor)
        extra = avail - sum(floor)
        order = sorted(range(self.ncols), key=lambda c: natural[c] - floor[c])
        for left, col in enumerate(order):
            give = min(natural[col] - floor[col], extra // (self.ncols - left))
            out[col] += give
            extra -= give
        return out


def cell_attrs(cell):
    styles = ["th"] if cell.header else []
    return styles + ([cell.style] if cell.style else [])


def grid_rows(grid, widths):
    """A table drawn with box characters: [(text, spans)] and the link tags' meanings."""
    xs = [1]
    for w in widths:
        xs.append(xs[-1] + w + 3)   # xs[c]: the border left of column c
    total = xs[-1]
    rows, info = [], {}

    def bounds(row):
        out = {0}
        for col, cell in row:
            out.add(xs[col + (cell.span if cell else 1)] - 1)
        return out

    def rule(above, below):
        up, down = (bounds(above) if above else set()), (bounds(below) if below else set())
        chars = []
        for x in range(total):
            u, d = x in up, x in down
            if x == 0:
                chars.append("├" if u and d else "┌" if d else "└")
            elif x == total - 1:
                chars.append("┤" if u and d else "┐" if d else "┘")
            else:
                chars.append("┼" if u and d else "┴" if u else "┬" if d else "─")
        rows.append(("".join(chars), [(0, total, "border")]))

    rule(None, grid.rows[0])
    for r, row in enumerate(grid.rows):
        if r and (grid.is_header(row) or grid.is_header(grid.rows[r - 1])):
            rule(grid.rows[r - 1], row)
        wrapped = []
        for col, cell in row:
            span = cell.span if cell else 1
            room = xs[col + span] - xs[col] - 3
            if cell is None or not cell.text:
                wrapped.append((col, cell, [("", [])]))
                continue
            marks, cell_info = block_marks(cell, (r, col))
            info.update(cell_info)
            wrapped.append((col, cell, wrap_block(cell.text, marks, room, "", "")))
        for k in range(max(len(lines) for _, _, lines in wrapped)):
            chars = [" "] * total
            spans = []
            for x in bounds(row):
                chars[x] = "│"
                spans.append((x, x + 1, "border"))
            for col, cell, lines in wrapped:
                if k >= len(lines) or not lines[k][0]:
                    continue
                text, cell_spans = lines[k]
                x = xs[col] + 1
                chars[x:x + len(text)] = text
                spans.extend((x, x + len(text), style) for style in cell_attrs(cell))
                spans.extend((x + c0, x + c1, tag) for c0, c1, tag in cell_spans)
            rows.append(("".join(chars), spans))
    rule(grid.rows[-1], None)
    return rows, info


def stacked_rows(grid, width):
    """A table too wide for the window: one record per row, each cell labelled by its header."""
    rows, info = [], {}
    body = grid.rows
    labels = []
    header_size = 0   # the header row becomes labels; counted once so offsets match the grid
    if len(body) > 1 and grid.is_header(body[0]):
        labels = [""] * grid.ncols
        for col, cell in body[0]:
            if cell is not None:
                labels[col] = cell.text
                header_size += text_size(cell.text)
        body = body[1:]
    label_width = min(max([len(label) for label in labels] + [0]), width // 3)
    for r, row in enumerate(body, start=len(grid.rows) - len(body)):
        cells = [(col, cell) for col, cell in row if cell is not None and cell.text]
        if not cells:
            continue
        if rows:
            rows.append(("", [], 0))
        for col, cell in cells:
            label = labels[col] if labels and not grid.is_header(row) else ""
            if len(label) > label_width:
                label = label[:max(1, label_width - 1)] + "…"
            first = label.ljust(label_width) + "  " if labels and label else ""
            marks, cell_info = block_marks(cell, (r, col))
            info.update(cell_info)
            lines = wrap_block(cell.text, marks, width, first, " " * len(first))
            for k, (text, spans) in enumerate(lines):
                spans = [(len(first), len(text), style)
                         for style in cell_attrs(cell)] + spans
                size = text_size(text)
                if k == 0 and first:
                    spans.insert(0, (0, len(label), "th"))
                    size -= text_size(first)
                rows.append((text, spans, size + header_size))
                header_size = 0
    return rows, info


def table_rows(block, width):
    grid = Grid(block.table)
    if not grid.ncols:
        return [], {}
    widths = grid.widths(width)
    if widths is None:
        return stacked_rows(grid, width)
    return grid_rows(grid, widths)


def prune_empty_sections(blocks):
    """Drop headings with nothing under them."""
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

    def add(text, kind="p", spans=(), size=None):
        lines.append((text, kind, list(spans), text_size(text) if size is None else size))

    def emit(rows, kind, info, bar="", box=None):
        """Add wrapped rows, turning link tags into indexes into links."""
        seen = {}
        for row in rows:
            text, spans = row[0], row[1]
            mapped = [(0, 1, box)] if bar else []
            for col0, col1, tag in spans:
                if isinstance(tag, tuple):
                    if tag not in seen:
                        lkind, target, label = info[tag]
                        links.append(Link(lkind, target, label, len(lines), col0, col1))
                        seen[tag] = len(links) - 1
                    tag = seen[tag]
                elif tag == "label":
                    tag = f"{box}-label" if box else "b"
                mapped.append((col0, col1, tag))
            add(text, kind, mapped, row[2] if len(row) > 2 else None)

    for index, block in enumerate(blocks):
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        level = HEADING_TAGS.get(block.kind)
        bar = "┃ " if block.box else ""
        depth = block.indent
        marks, info = block_marks(block)

        if level:
            if lines and lines[-1][0]:
                add("")
            rows = wrap_block(block.text, marks, width, "", "")
            base = len(lines)
            emit(rows, "h2" if level <= 2 else "h3", info)
            if level <= 2:
                add("─" * min(width, max(4, len(rows[-1][0]))), "rule")
            headings.append((base, level, block.text))
            if block.hid:
                anchors[block.hid] = base
        elif block.kind in PRE_KINDS:
            prefix = bar + "  " * depth + "  "
            rows = pre_rows(block, width, prefix)
            command = block.text.startswith(PROMPTS)
            if block.kind == "prehead" and not command:
                rows = [(text, [s for s in spans if s[2] != ("C",)]) for text, spans in rows]
            info[("C",)] = ("code", (block.text, block.kind != "prebody"), "code block")
            emit(rows, "prehead" if block.kind == "prehead" else "pre", info, bar, block.box)
        elif block.kind == "table":
            lead = bar + "  " * depth
            rows, info = table_rows(block, width - len(lead))
            shifted = []
            for row in rows:
                spans = [(c0 + len(lead), c1 + len(lead), tag) for c0, c1, tag in row[1]]
                shifted.append((lead + row[0], spans, *row[2:]))
            emit(shifted, "table", info, bar, block.box)
        else:
            if block.kind == "li":
                indent = "  " * max(0, depth - 1)
                first = bar + indent + block.bullet
                cont = bar + indent + " " * len(block.bullet)
                kind = "p"
            elif block.kind == "dd":
                first = cont = bar + "  " * depth + "    "
                kind = "p"
            elif block.kind == "dt":
                first = cont = bar + "  " * depth + "  "
                kind = "dt"
            elif block.kind == "quote":
                first = cont = bar + "  │ "
                kind = "quote"
            else:
                first = cont = bar + "  " * depth
                kind = "p"
            emit(wrap_block(block.text, marks, width, first, cont), kind, info, bar, block.box)

        # Spacing: list items and a file header with its body stay together, and the
        # blank line between two blocks of one box keeps the box's bar.
        if following is None:
            continue
        if block.kind == "li" and following.kind == "li" and block.box_id == following.box_id:
            continue
        if (block.kind, following.kind) in (("prehead", "prebody"), ("dt", "dd")):
            continue
        if block.box and following.box_id == block.box_id:
            add("┃", "p", [(0, 1, block.box)])
        else:
            add("")

    while lines and not lines[-1][0]:
        lines.pop()
    return Doc(lines, links, headings, anchors)


# ------------------------------------------------------------- clipboard


def copy_to_clipboard(text):
    """Put text on the clipboard; returns what did it, or None."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return cmd[0]
        except (OSError, subprocess.SubprocessError):
            continue
    # No clipboard tool (a bare console, or over SSH): ask the terminal itself with OSC 52.
    try:
        sys.stdout.write("\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")
        sys.stdout.flush()
        return "the terminal (OSC 52)"
    except OSError:
        return None


def code_text(target):
    """A code block as it should be pasted: without the $ / # prompts of a command block."""
    text, may_strip = target
    lines = text.split("\n")
    filled = [line for line in lines if line.strip()]
    if may_strip and filled and all(line.startswith(PROMPTS) for line in filled):
        return "\n".join(line[2:] if line.startswith(PROMPTS) else line for line in lines), True
    return text, False


# ------------------------------------------------------------------ UI


HELP_TEXT = [
    ("Moving", ""),
    ("  j / k, arrows", "scroll one line"),
    ("  space / b", "page down / page up"),
    ("  g / G", "jump to top / bottom"),
    ("Links and code", ""),
    ("  Tab / Shift-Tab", "select next / previous link or code block"),
    ("  c", "select the next code block"),
    ("  Enter", "follow the link, or copy the code block"),
    ("  Backspace / [", "back      ]  forward"),
    ("Finding", ""),
    ("  s", "search the Arch Wiki"),
    ("  /", "find in this article   n / N  next / previous match"),
    ("  t", "table of contents"),
    ("  h", "Main page          r  random article"),
    ("Other", ""),
    ("  o", "open this article in the browser"),
    ("  y", "copy the article URL"),
    ("  w", f"full window width / capped at {MAX_WIDTH} columns"),
    ("  ? ", "this help          q  quit"),
    ("", ""),
    ("Copying a command block drops the $ / # prompts.", ""),
    ("man(8) links open in man on Linux, man.archlinux.org elsewhere.", ""),
]

ERROR_STARTS = ("Could", "No ", "Nothing", "Search failed", "Copy failed", "Select")


class Browser:
    def __init__(self, stdscr):
        self.stdscr = stdscr
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
        self.full_width = False
        self._init_colors()

    # -- colours --

    def _init_colors(self):
        bold, under, dim = curses.A_BOLD, curses.A_UNDERLINE, curses.A_DIM
        italic = getattr(curses, "A_ITALIC", under)
        if not curses.has_colors():
            plain = {"h": bold, "link": under, "sel": curses.A_REVERSE, "ext": under,
                     "dim": dim, "bar": curses.A_REVERSE, "err": bold,
                     "hit": curses.A_REVERSE, "code": bold, "ph": under, "pre": 0,
                     "prehead": bold, "b": bold, "i": italic, "th": bold, "border": dim}
            for box in BOX_CLASSES.values():
                plain[box] = bold
                plain[f"{box}-label"] = bold
            self.color = plain
            return
        curses.start_color()
        curses.use_default_colors()
        pairs = [
            (1, curses.COLOR_CYAN), (2, curses.COLOR_BLUE), (3, curses.COLOR_MAGENTA),
            (4, curses.COLOR_RED), (5, curses.COLOR_YELLOW), (6, curses.COLOR_WHITE),
            (7, curses.COLOR_GREEN),
        ]
        for idx, fg in pairs:
            curses.init_pair(idx, fg, -1)
        cyan, blue, magenta, red, yellow, white, green = (
            curses.color_pair(n) for n in range(1, 8))
        self.color = {
            "h": cyan | bold,
            "link": blue,
            "sel": blue | curses.A_REVERSE | bold,
            "ext": magenta,
            "dim": dim,
            "bar": white | curses.A_REVERSE,
            "err": red | bold,
            "hit": yellow | curses.A_REVERSE,
            "code": green,
            "pre": green,
            "ph": yellow | under,
            "prehead": yellow | bold,
            "b": bold,
            "i": italic,
            "th": bold,
            "border": dim,
            "good": green | bold,
            "meh": yellow | bold,
            "bad": red | bold,
        }
        boxes = {"note": cyan, "tip": green, "warning": red, "message": yellow,
                 "related": dim}
        for box, colour in boxes.items():
            self.color[box] = colour
            self.color[f"{box}-label"] = colour | bold

    # -- geometry --

    @property
    def body_height(self):
        return max(1, self.stdscr.getmaxyx()[0] - 2)

    def text_width(self):
        cols = self.stdscr.getmaxyx()[1]
        cap = cols if self.full_width else MAX_WIDTH
        return max(20, min(cap, cols - 2 * LEFT_MARGIN))

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
                return None, f"{SITE_NAME} is rate-limiting requests - wait a moment"
            return None, f"{SITE_NAME} returned HTTP {exc.code}"
        except (urllib.error.URLError, json.JSONDecodeError,
                TimeoutError, OSError, KeyError, ValueError) as exc:
            return None, str(exc)

    # -- loading articles --

    def load(self, title, push=True, anchor=None):
        if title in self.cache:
            real_title, blocks = self.cache[title]
        else:
            result, error = self.call(f"Loading {title}...", fetch_article, title)
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
            self.cache[real_title] = (real_title, blocks)
            self.cache[title] = (real_title, blocks)

        if push:
            self.remember()
            del self.history[self.hindex + 1:]
            self.history.append([real_title, 0, -1])
            self.hindex = len(self.history) - 1

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

    def toggle_width(self):
        offset = text_offset(self.doc, self.top)
        self.full_width = not self.full_width
        self.relayout()
        top = min(line_at_offset(self.doc, offset), self.max_top())
        if self.matches:
            match_at = self.match_at
            self.find(self.query)  # the hits moved; find() also scrolls, undone below
            self.match_at = min(match_at, len(self.matches) - 1)
        self.top = top
        self.sel = -1
        self.message = ("Full width." if self.full_width
                        else f"Width capped at {MAX_WIDTH} columns.")

    def remember(self):
        if 0 <= self.hindex < len(self.history):
            self.history[self.hindex][1] = self.top
            self.history[self.hindex][2] = self.sel

    def go_history(self, step):
        target = self.hindex + step
        if not (0 <= target < len(self.history)):
            self.message = "No further history." if step > 0 else "No earlier article."
            return
        self.remember()
        self.hindex = target
        title, top, sel = self.history[target]
        if self.load(title, push=False):
            self.top = min(top, self.max_top())
            self.sel = sel if sel < len(self.doc.links) else -1

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
        anchor = anchor.replace(" ", "_")
        line = self.doc.anchors.get(anchor)
        if line is None:
            for hid, pos in self.doc.anchors.items():
                if hid.lower() == anchor.lower():
                    line = pos
                    break
        if line is None:
            self.message = f"No section '{anchor.replace('_', ' ')}' on this page."
            return
        self.top = min(self.max_top(), line)
        self.sel = -1

    def describe(self, link):
        if link.kind == "code":
            return "Code block - Enter copies it."
        if link.kind == "page":
            return f"-> {link.target}"
        if link.kind == "anchor":
            return f"-> section {link.target.replace('_', ' ')}"
        return link.target

    def select_link(self, step, kinds=None):
        links = [i for i, ln in enumerate(self.doc.links) if kinds is None or ln.kind in kinds]
        if not links:
            self.message = "No code blocks on this page." if kinds else "No links on this page."
            return
        visible = [i for i in links if self.doc.links[i].line >= self.top]
        if self.sel < 0 or not (self.top <= self.doc.links[self.sel].line
                                < self.top + self.body_height):
            # Nothing selected on screen: start from the top of what is visible.
            self.sel = (visible[0] if visible else links[0]) if step > 0 else (
                ([i for i in links if self.doc.links[i].line < self.top + self.body_height]
                 or links)[-1])
        else:
            after = [i for i in links if (i > self.sel if step > 0 else i < self.sel)]
            self.sel = (after[0] if step > 0 else after[-1]) if after else (
                links[0] if step > 0 else links[-1])
        self.show_line(self.doc.links[self.sel].line)
        self.message = self.describe(self.doc.links[self.sel])

    def follow(self):
        if not (0 <= self.sel < len(self.doc.links)):
            self.message = "Select a link with Tab first, or a code block with c."
            return
        link = self.doc.links[self.sel]
        if link.kind == "page":
            title, _, anchor = link.target.partition("#")
            if title == self.title:
                self.goto_anchor(anchor) if anchor else self.scroll(-len(self.doc.lines))
            else:
                self.load(title, anchor=anchor or None)
        elif link.kind == "anchor":
            self.goto_anchor(link.target)
        elif link.kind == "code":
            self.copy_code(link)
        elif link.kind == "man":
            self.open_man(link.target)
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

    def open_man(self, url):
        """man(8) links: the local man page on Linux, where it is the same page; elsewhere
        (macOS has BSD pages under the same names) man.archlinux.org."""
        leaf = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
        name, _, section = leaf.rpartition(".")
        if not (sys.platform.startswith("linux") and name and shutil.which("man")):
            self.open_browser(url)
            return
        found = subprocess.run(["man", "-w", section, name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
        if not found:
            self.open_browser(url)
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.run(["man", section, name])
        finally:
            curses.reset_prog_mode()
            self.stdscr.clear()
        self.message = f"Back from {name}({section})."

    def copy_code(self, link):
        text, stripped = code_text(link.target)
        how = copy_to_clipboard(text)
        count = text.count("\n") + 1
        if how is None:
            self.message = "Copy failed: no clipboard tool and the terminal refused."
            return
        lines = "1 line" if count == 1 else f"{count} lines"
        self.message = f"Copied {lines} via {how}" + (" (prompt removed)." if stripped else ".")

    def no_article(self):
        self.message = "No article open - press s to search."

    def copy_url(self):
        how = copy_to_clipboard(article_url(self.title))
        self.message = f"URL copied via {how}." if how else "Copy failed."

    # -- find in page --

    def find(self, query):
        self.query = query
        needle = query.lower()
        self.matches = []
        for idx, entry in enumerate(self.doc.lines):
            start = 0
            low = entry[0].lower()
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

        base = {"h2": self.color["h"], "h3": self.color["h"], "dt": self.color["b"],
                "rule": self.color["dim"], "dim": self.color["dim"],
                "quote": self.color["dim"], "pre": self.color["pre"],
                "prehead": self.color["prehead"]}
        for row in range(body):
            index = self.top + row
            if index >= len(self.doc.lines):
                break
            text, kind, spans, _size = self.doc.lines[index]
            y = row + 1
            self.put(y, LEFT_MARGIN, text, base.get(kind, curses.A_NORMAL), width)

            for col0, col1, ref in spans:
                if isinstance(ref, str):
                    self.highlight(y, LEFT_MARGIN + col0, col1 - col0,
                                   self.color.get(ref, curses.A_NORMAL), width)
            for col0, col1, ref in spans:
                if isinstance(ref, int):
                    link = self.doc.links[ref]
                    if ref == self.sel:
                        style = self.color["sel"]
                    elif link.kind == "code":
                        continue  # code blocks only show up when selected
                    elif link.kind in ("external", "man"):
                        style = self.color["ext"]
                    else:
                        style = self.color["link"]
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
        right = f" wiki.archlinux.org  {pct:>3}% "
        bar = left + " " * max(0, width - 1 - len(left) - len(right)) + right
        self.put(0, 0, bar.ljust(width - 1), self.color["bar"], width)

    def draw_footer(self, height, width):
        if self.message:
            error = self.message.startswith(ERROR_STARTS) or "not found" in self.message
            self.put(height - 1, 0, self.message[:width - 1].ljust(width - 1),
                     self.color["err"] if error else self.color["bar"], width)
        else:
            hint = (" s search  Tab link  c code  Enter follow/copy  Backspace back"
                    "  t contents  / find  ? help  q quit ")
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
        query = self.prompt(f" Search {SITE_NAME}: ", initial)
        if not query:
            return
        results, error = self.call(f"Searching {query}...", search_titles, query)
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
        title, error = self.call("Fetching a random article...", random_title)
        if error or not title:
            self.message = f"Could not fetch a random article: {error or 'no result'}"
            return
        self.load(title)

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
                offset = text_offset(self.doc, self.top)
                self.relayout()
                self.top = min(line_at_offset(self.doc, offset), self.max_top())
                self.sel = -1
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
            elif key == ord("c"):
                self.select_link(1, ("code",))
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
            elif key == ord("h"):
                self.load(HOME_TITLE)
            elif key == ord("r"):
                self.do_random()
            elif key == ord("o"):
                (self.open_browser(article_url(self.title))
                 if self.is_article else self.no_article())
            elif key == ord("y"):
                self.copy_url() if self.is_article else self.no_article()
            elif key == ord("w"):
                self.toggle_width()
            elif key == ord("?"):
                self.show_help()
            else:
                self.message = message_was


def start(stdscr, topic):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    stdscr.keypad(True)
    browser = Browser(stdscr)
    browser.doc = Doc([], [], [], {})
    welcome = [
        f"Press s to search the {SITE_NAME}, h for its Main page.",
        "Press r to open a random article.",
        "Press ? for the full list of keys, q to quit.",
    ]

    if not browser.load(topic or HOME_TITLE):
        failure = browser.message
        if browser.load_failed == "missing":
            browser.show_notice(f"No article '{topic}'", welcome)
            browser.do_search(topic)
        else:
            browser.show_notice(f"Could not reach the {SITE_NAME}", [failure] + welcome)

    browser.run()


def main():
    parser = argparse.ArgumentParser(description=f"Browse the {SITE_NAME} in a terminal UI.")
    parser.add_argument("topic", nargs="*", help="article title to open on start")
    args = parser.parse_args()

    if not sys.stdout.isatty():
        print("archwiki_tui needs a terminal.", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(start, " ".join(args.topic).strip())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
