#!/usr/bin/env python3
"""Terminal UI for reading 4chan: boards, catalogs and threads. Read-only."""

import argparse
import curses
import html
import json
import locale
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

API = "https://a.4cdn.org"
SITE = "https://boards.4chan.org"
MEDIA = "https://i.4cdn.org"
USER_AGENT = "chan-tui/1.0 (terminal 4chan reader)"

TIMEOUT = 15
MIN_INTERVAL = 1.0      # the API rules: no more than one request a second
THREAD_REFRESH = 30     # seconds between refetches while watching a thread
MAX_WIDTH = 100
LEFT_MARGIN = 2
BODY_INDENT = 2
EXCERPT_LINES = 2
SORTS = ["bump", "replies", "images", "new"]

# termimage.py is used only if it sits beside this script, and only as a subprocess.
TERMIMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "termimage.py")
TERMIMAGE_TYPES = {".jpg", ".png", ".gif"}

TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
HOST = r"(?:https?:)?(?://boards\.4chan(?:nel)?\.org)?"
THREAD_HREF_RE = re.compile(HOST + r"/(\w+)/thread/(\d+)(?:/[^#]*)?(?:#[pq](\d+))?$")
BOARD_HREF_RE = re.compile(HOST + r"/(\w+)/(?:catalog)?(?:#s=(.*))?$")


# ----------------------------------------------------------------- API


_last_request = 0.0


def api_get(path, since=None):
    """GET one JSON endpoint. Returns (data, last_modified); data is None on a 304."""
    global _last_request
    wait = _last_request + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    headers = {"User-Agent": USER_AGENT}
    if since:
        headers["If-Modified-Since"] = since
    req = urllib.request.Request(API + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, since
        raise
    finally:
        _last_request = time.monotonic()


def fetch_boards():
    data, _ = api_get("/boards.json")
    return data["boards"]


def fetch_catalog(board):
    data, _ = api_get(f"/{board}/catalog.json")
    threads = []
    for page in data:
        for thread in page.get("threads", []):
            thread["page"] = page.get("page", 0)
            threads.append(thread)
    return threads


def fetch_thread(board, no, since=None):
    """Return (posts, last_modified); posts is None when nothing changed since `since`."""
    data, modified = api_get(f"/{board}/thread/{no}.json", since)
    return (data["posts"] if data else None), modified


def thread_url(board, no, post=None):
    return f"{SITE}/{board}/thread/{no}" + (f"#p{post}" if post and post != no else "")


def media_url(board, post, thumb=False):
    if thumb:
        return f"{MEDIA}/{board}/{post['tim']}s.jpg"
    return f"{MEDIA}/{board}/{post['tim']}{post['ext']}"


def parse_target(text):
    """A start argument -> ("board", (b, "")) or ("thread", (b, no, post)) or None.

    Takes `g`, `/g/`, `g/123`, `/g/thread/123` or a full boards.4chan.org URL.
    """
    text = text.strip()
    m = re.fullmatch(r"/?(\w+)/?", text)
    if m:
        return "board", (m.group(1), "")
    m = re.fullmatch(r"/?(\w+)/(\d+)", text)
    if m:
        return "thread", (m.group(1), int(m.group(2)), None)
    found = link_target(text)
    if found and found[0] in ("board", "thread"):
        return found
    return None


# --------------------------------------------------------------- parsing


def clean(text):
    """Plain text from an HTML-escaped field such as `sub` or `name`."""
    return html.unescape(TAG_RE.sub("", text or ""))


def link_target(href):
    """Classify an href into (kind, target): post, thread, board or url. None if unusable."""
    if not href:
        return None
    if href.startswith("#p") and href[2:].isdigit():
        return "post", int(href[2:])
    m = THREAD_HREF_RE.match(href)
    if m:
        return "thread", (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    m = BOARD_HREF_RE.match(href)
    if m:
        return "board", (m.group(1), urllib.parse.unquote(m.group(2) or ""))
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith(("http://", "https://")):
        return "url", href
    return None


class CommentParser(HTMLParser):
    """Turns a post's comment HTML into runs of (text, style, link)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.runs = []
        self.stack = []     # (tag, style or None); the innermost style wins
        self.link = None

    def style(self):
        for _, style in reversed(self.stack):
            if style:
                return style
        return "text"

    def newline(self):
        self.runs.append(("\n", "text", None))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "br":
            self.newline()
            return
        if tag in ("wbr", "img", "hr"):
            return
        style = None
        if tag == "a":
            self.link = link_target(attrs.get("href"))
            style = "link" if self.link else None
        elif tag == "s":
            style = "spoiler"
        elif tag == "pre":
            if self.runs and not self.runs[-1][0].endswith("\n"):
                self.newline()
            style = "code"
        elif tag in ("b", "strong"):
            style = "red" if "red" in (attrs.get("style") or "") else "bold"
        elif tag == "u":
            style = "under"
        elif tag == "span":
            if "quote" in classes:
                style = "quote"
            elif "deadlink" in classes:
                style = "dead"
            elif "fortune" in classes:
                style = "red"
        self.stack.append((tag, style))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == "a":
            self.link = None
        elif tag == "pre":
            self.newline()

    def handle_data(self, data):
        if data:
            self.runs.append((data, self.style(), self.link))


def merge_runs(runs):
    out = []
    for text, style, link in runs:
        if out and out[-1][1] == style and out[-1][2] == link and "\n" not in (text, out[-1][0]):
            out[-1] = (out[-1][0] + text, style, link)
        else:
            out.append((text, style, link))
    return out


def linkify(runs):
    """Plain-text URLs become url links. Runs must be merged first: <wbr> splits URLs."""
    out = []
    for text, style, link in runs:
        if link or "http" not in text:
            out.append((text, style, link))
            continue
        pos = 0
        for m in URL_RE.finditer(text):
            url = m.group(0).rstrip(".,;:!?)'\"")
            if m.start() > pos:
                out.append((text[pos:m.start()], style, None))
            out.append((url, "link", ("url", url)))
            pos = m.start() + len(url)
        if pos < len(text):
            out.append((text[pos:], style, None))
    return out


def parse_comment(com):
    parser = CommentParser()
    parser.feed(com or "")
    parser.close()
    runs = linkify(merge_runs(parser.runs))
    while runs and runs[-1][0] == "\n":
        runs.pop()
    return runs


def plain_text(runs):
    return "".join(text for text, _, _ in runs)


def one_line(text):
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- layout


def char_width(ch):
    if unicodedata.combining(ch) or ch in "​‌‍︎️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def cwidth(text):
    return sum(char_width(ch) for ch in text)


def clip(text, cols):
    """The longest prefix of text that fits in cols terminal columns."""
    used = 0
    for index, ch in enumerate(text):
        used += char_width(ch)
        if used > cols:
            return text[:index]
    return text


def wrap_runs(runs, width):
    """Word-wrap styled runs to width columns. Returns lines of [(text, style, lid)]."""
    lines = []
    line, col = [], 0

    def flush():
        nonlocal line, col
        while line and line[-1][0].isspace():
            line.pop()
        merged = []
        for text, style, lid in line:
            if merged and merged[-1][1] == style and merged[-1][2] == lid:
                merged[-1] = (merged[-1][0] + text, style, lid)
            else:
                merged.append((text, style, lid))
        lines.append(merged)
        line, col = [], 0

    for text, style, lid in runs:
        for token in re.split(r"(\n|[ \t]+)", text.replace("\t", "    ")):
            if not token:
                continue
            if token == "\n":
                flush()
                continue
            width_of = cwidth(token)
            if token[0] == " ":
                if col == 0 and style != "code":
                    continue
                if col + width_of > width:
                    flush()
                    continue
                line.append((token, style, lid))
                col += width_of
                continue
            if col and col + width_of > width:
                flush()
            while width_of > width:
                head = clip(token, width) or token[0]
                line.append((head, style, lid))
                flush()
                token = token[len(head):]
                width_of = cwidth(token)
            if token:
                line.append((token, style, lid))
                col += width_of
    if line:
        flush()
    return lines


def wrap_plain(text, width):
    return ["".join(t for t, _, _ in line) for line in wrap_runs([(text, "text", None)], width)]


def ago(ts, now=None):
    seconds = max(0, int((now or time.time()) - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") + f", {ago(ts)} ago"


def file_size(size):
    if size >= 1 << 20:
        return f"{size / (1 << 20):.1f} MB"
    return f"{max(1, size >> 10)} KB"


class Doc:
    """A laid-out thread: display lines plus where every link and post landed."""

    def __init__(self):
        self.lines = []       # [(text, style, lid)] per row
        self.row_post = []    # post index per row
        self.post_rows = {}   # post number -> first row
        self.post_starts = []  # first row per post index
        self.links = []       # [kind, target, row]

    def add(self, runs, width, indent, post):
        start = len(self.lines)
        out = []
        for text, style, link in runs:
            lid = None
            if link:
                lid = len(self.links)
                self.links.append([link[0], link[1], start])
            out.append((text, style, lid))
        seen = set()
        for line in wrap_runs(out, max(10, width - indent)):
            row = len(self.lines)
            for _, _, lid in line:
                if lid is not None and lid not in seen:
                    seen.add(lid)
                    self.links[lid][2] = row
            if indent and line:
                line = [(" " * indent, "text", None)] + line
            self.lines.append(line)
            self.row_post.append(post)

    def blank(self, post):
        self.lines.append([])
        self.row_post.append(post)

    def plain(self, row):
        return "".join(text for text, _, _ in self.lines[row])


def backlinks(posts, parsed):
    """post number -> numbers of the later posts that quote it, in thread order."""
    replies = {}
    for post, runs in zip(posts, parsed):
        for _, _, link in runs:
            if link and link[0] == "post" and link[1] != post["no"]:
                quoting = replies.setdefault(link[1], [])
                if post["no"] not in quoting:
                    quoting.append(post["no"])
    return replies


def header_runs(post, index, new_after):
    runs = []

    def add(text, style):
        if runs:
            runs.append(("  ", "text", None))
        runs.append((text, style, None))

    if post.get("sub"):
        add(clean(post["sub"]), "subject")
    add(clean(post.get("name") or "Anonymous") + (post.get("trip") or ""), "name")
    if post.get("capcode"):
        capcode = post["capcode"].replace("_highlight", "").title()
        runs.append((f" ## {capcode}", "red", None))
    if post.get("id"):
        add(f"ID:{post['id']}", "meta")
    flag = post.get("country_name") or post.get("flag_name")
    if flag:
        add(f"[{clean(flag)}]", "meta")
    add(when(post["time"]), "meta")
    add(f"No.{post['no']}" + (f"  #{index}" if index else "  OP"), "meta")
    for key in ("sticky", "closed", "archived"):
        if post.get(key):
            add(f"[{key}]", "new")
    if new_after and post["no"] > new_after:
        add("new", "new")
    return runs


def file_runs(post, index):
    if post.get("filedeleted"):
        return [("File deleted.", "meta", None)]
    if "tim" not in post:
        return []
    name = clean(post.get("filename", "")) + post.get("ext", "")
    details = f"  {file_size(post.get('fsize', 0))}, {post.get('w', 0)}×{post.get('h', 0)}"
    if post.get("spoiler"):
        details += "  [spoiler]"
    return [("File: ", "meta", None), (name, "file", ("image", index)), (details, "meta", None)]


def layout_thread(posts, parsed, replies, width, new_after=None):
    doc = Doc()
    op = posts[0]["no"]
    for index, (post, runs) in enumerate(zip(posts, parsed)):
        if index:
            doc.blank(index)
        doc.post_rows[post["no"]] = len(doc.lines)
        doc.post_starts.append(len(doc.lines))
        doc.add(header_runs(post, index, new_after), width, 0, index)
        files = file_runs(post, index)
        if files:
            doc.add(files, width, BODY_INDENT, index)
        body = [(text + " (OP)", style, link) if link == ("post", op) else (text, style, link)
                for text, style, link in runs]
        if body:
            doc.add(body, width, BODY_INDENT, index)
        quoting = replies.get(post["no"])
        if quoting:
            back = [("Replies: ", "meta", None)]
            for no in quoting:
                back += [(f">>{no}", "link", ("post", no)), (" ", "text", None)]
            doc.add(back, width, BODY_INDENT, index)
    return doc


def catalog_entry(thread, width):
    """A catalog row: the title line's text, meta text, and excerpt lines."""
    meta = f"{thread.get('replies', 0)}R {thread.get('images', 0)}I  {ago(thread['time'])}  p{thread.get('page', 0)}"
    title_width = max(10, width - cwidth(meta) - 4)
    excerpt = one_line(plain_text(parse_comment(thread.get("com", ""))))
    sub = one_line(clean(thread.get("sub", "")))
    if sub:
        title = sub
    else:
        first = wrap_plain(excerpt, title_width)
        title = first[0] if first else f"No.{thread['no']}"
        excerpt = excerpt[len(title):].strip() if first else ""
    lines = wrap_plain(excerpt, max(10, width - 2))
    if len(lines) > EXCERPT_LINES:
        lines = lines[:EXCERPT_LINES]
        lines[-1] = clip(lines[-1], max(10, width - 2) - 1) + "…"
    return title, meta, lines


# ------------------------------------------------------------------ UI


HELP_TEXT = [
    ("Everywhere", ""),
    ("  j / k, arrows", "move / scroll one line"),
    ("  space / b", "page down / page up"),
    ("  g / G", "top / bottom"),
    ("  Enter", "open the board, thread or selected link"),
    ("  Backspace / [ / ←", "back"),
    ("  r", "refresh"),
    ("  o", "open in the browser      y  copy the URL"),
    ("  ?", "this help                q  quit"),
    ("Boards and catalog", ""),
    ("  /", "filter (Enter on an empty filter clears it)"),
    ("  s", "sort the catalog: bump order, replies, images, newest"),
    ("Thread", ""),
    ("  Tab / Shift-Tab", "select next / previous link; quotes preview below"),
    ("  > / <", "next / previous post"),
    ("  /", "find in thread           n / N  next / previous match"),
    ("  i", "open the post's file in the browser"),
    ("  v", "show the post's image in the terminal (needs termimage.py)"),
    ("  s", "show / hide spoilers"),
    ("  a", f"watch: refetch every {THREAD_REFRESH} s and mark new posts"),
]

BACK_KEYS = (curses.KEY_BACKSPACE, 127, 8, ord("["), curses.KEY_LEFT)
ENTER_KEYS = (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT)


class App:
    def __init__(self, stdscr, sfw):
        self.stdscr = stdscr
        self.sfw = sfw
        self.screens = []
        self.message = ""
        self.boards = []
        self.reveal = False
        self._init_colors()

    # -- colours --

    def _init_colors(self):
        names = ("text", "quote", "link", "sel", "dead", "spoiler", "hidden", "bold", "under",
                 "code", "red", "subject", "name", "meta", "file", "new", "nsfw", "board",
                 "bar", "err", "hit", "pick")
        if not curses.has_colors():
            self.color = {k: curses.A_NORMAL for k in names}
            self.color.update(link=curses.A_UNDERLINE, sel=curses.A_REVERSE, bold=curses.A_BOLD,
                              under=curses.A_UNDERLINE, subject=curses.A_BOLD, name=curses.A_BOLD,
                              meta=curses.A_DIM, hidden=curses.A_DIM, spoiler=curses.A_REVERSE,
                              file=curses.A_UNDERLINE, new=curses.A_BOLD, board=curses.A_BOLD,
                              bar=curses.A_REVERSE, err=curses.A_BOLD, hit=curses.A_REVERSE,
                              pick=curses.A_REVERSE, red=curses.A_BOLD)
            return
        curses.start_color()
        curses.use_default_colors()
        for idx, fg in [(1, curses.COLOR_CYAN), (2, curses.COLOR_BLUE),
                        (3, curses.COLOR_MAGENTA), (4, curses.COLOR_RED),
                        (5, curses.COLOR_YELLOW), (6, curses.COLOR_WHITE),
                        (7, curses.COLOR_GREEN)]:
            curses.init_pair(idx, fg, -1)
        pair = curses.color_pair
        self.color = {
            "text": curses.A_NORMAL,
            "quote": pair(7),
            "link": pair(1),
            "sel": pair(1) | curses.A_REVERSE | curses.A_BOLD,
            "dead": pair(4) | curses.A_DIM,
            "spoiler": curses.A_REVERSE,
            "hidden": curses.A_DIM,
            "bold": curses.A_BOLD,
            "under": curses.A_UNDERLINE,
            "code": pair(5),
            "red": pair(4) | curses.A_BOLD,
            "subject": pair(2) | curses.A_BOLD,
            "name": pair(7) | curses.A_BOLD,
            "meta": curses.A_DIM,
            "file": pair(3),
            "new": pair(5) | curses.A_BOLD,
            "nsfw": pair(4),
            "board": pair(1) | curses.A_BOLD,
            "bar": pair(6) | curses.A_REVERSE,
            "err": pair(4) | curses.A_BOLD,
            "hit": pair(5) | curses.A_REVERSE,
            "pick": curses.A_REVERSE | curses.A_BOLD,
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
        self.put(height - 1, 0, f" {text} ".ljust(width), self.color["bar"], width)
        self.stdscr.refresh()

    def call(self, what, fn, *args):
        self.busy(what)
        try:
            return fn(*args), None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, "not found (404) - pruned, deleted or never existed"
            if exc.code == 429:
                return None, "4chan is rate-limiting requests - wait a moment"
            return None, f"4chan returned HTTP {exc.code}"
        except (urllib.error.URLError, json.JSONDecodeError,
                TimeoutError, OSError, KeyError, ValueError, TypeError) as exc:
            return None, str(getattr(exc, "reason", exc))

    def load_boards(self):
        boards, error = self.call("Loading boards...", fetch_boards)
        if error:
            self.message = f"Could not load the board list: {error}"
            return False
        self.boards = boards
        return True

    def board_title(self, board):
        for row in self.boards:
            if row["board"] == board:
                return row["title"]
        return ""

    # -- screen stack --

    def open_board(self, board, query=""):
        screen = CatalogScreen(self, board, query)
        if screen.load():
            self.screens.append(screen)

    def open_thread(self, board, no, post=None):
        screen = ThreadScreen(self, board, no)
        if screen.load(post):
            self.screens.append(screen)

    # -- outside the terminal --

    def open_external(self, url):
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, url], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.message = f"Opened {url}"
        except (OSError, subprocess.CalledProcessError):
            self.message = f"Could not open a browser ({url})"

    def copy(self, text):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.message = f"Copied {text}"
        except (OSError, subprocess.CalledProcessError):
            self.message = "Copy failed (pbcopy unavailable)."

    # -- drawing --

    def put(self, y, x, text, attr, width):
        space = width - x - 1
        if space <= 0 or not text:
            return 0
        text = clip(text, space)
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return cwidth(text)

    def put_spans(self, y, x, spans, width, sel=-1, hits=(), fill=None):
        """Draw one line of (text, style, lid) spans; returns the column after it."""
        start = x
        for text, style, lid in spans:
            if style == "spoiler" and not self.reveal:
                text = "".join("░" * char_width(ch) for ch in text)
                style = "hidden"
            attr = self.color["sel"] if lid is not None and lid == sel else self.color[style]
            if fill is not None:
                attr |= fill
            x += self.put(y, x, text, attr, width)
        for col, length in hits:
            length = min(length, width - 1 - (start + col))
            if length > 0:
                try:
                    self.stdscr.chgat(y, start + col, length, self.color["hit"])
                except curses.error:
                    pass
        return x

    def draw(self):
        screen = self.screens[-1]
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        left, right = screen.status()
        left, right = f" {left} ", f" {right} "
        left = clip(left, max(0, width - 1 - cwidth(right)))
        bar = left + " " * max(0, width - 1 - cwidth(left) - cwidth(right)) + right
        self.put(0, 0, bar, self.color["bar"], width)
        screen.draw_body(1, max(1, height - 2), width)
        text = self.message or screen.hint()
        attr = self.color["bar"]
        if self.message.startswith(("Could not", "No ", "Not ")) or "404" in self.message:
            attr = self.color["err"]
        self.put(height - 1, 0, f" {text}".ljust(width), attr, width)
        self.stdscr.refresh()

    # -- modal widgets --

    def prompt(self, label, initial=""):
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                height, width = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(height - 1, 0, text.ljust(width), self.color["bar"], width)
                self.stdscr.move(height - 1, min(cwidth(text), width - 2))
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
                    self.screens[-1].relayout()
                    self.draw()
        finally:
            curses.curs_set(0)

    def show_help(self):
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        self.put(0, 0, " chan_tui keys ".ljust(width), self.color["bar"], width)
        for row, (label, detail) in enumerate(HELP_TEXT, start=2):
            if row >= height - 1:
                break
            if not detail:
                self.put(row, LEFT_MARGIN, label, self.color["subject"], width)
            else:
                self.put(row, LEFT_MARGIN, label, self.color["link"], width)
                self.put(row, LEFT_MARGIN + 24, detail, curses.A_NORMAL, width)
        self.put(height - 1, 0, " any key to close ".ljust(width), self.color["bar"], width)
        self.stdscr.refresh()
        while self.stdscr.getch() == -1:
            pass

    def view_in_terminal(self, url, label):
        """Hand the terminal to termimage.py for one image, then come back."""
        if not os.path.exists(TERMIMAGE):
            self.message = "Not available: termimage.py is not next to chan_tui.py - try i instead."
            return
        curses.endwin()
        print(f"\n  {label}\n  {url}\n", flush=True)
        try:
            subprocess.run([sys.executable, TERMIMAGE, url], check=False)
            input("\n  Enter to return ")
        except (OSError, EOFError, KeyboardInterrupt):
            pass
        self.stdscr.clear()
        self.stdscr.refresh()

    # -- main loop --

    def run(self):
        self.stdscr.timeout(1000)
        while self.screens:
            screen = self.screens[-1]
            screen.tick()
            self.draw()
            key = self.stdscr.getch()
            if key == -1:
                continue
            message_was = self.message
            self.message = ""
            if key in (ord("q"), ord("Q")):
                return
            elif key == curses.KEY_RESIZE:
                for each in self.screens:
                    each.relayout()
            elif key == ord("?"):
                self.show_help()
            elif key in BACK_KEYS:
                if not screen.back():
                    if len(self.screens) > 1:
                        self.screens.pop()
                    else:
                        self.message = "This is the board list - q quits."
            elif not screen.key(key):
                self.message = message_was


class ListScreen:
    """A scrolling list of entries, each one or more lines; one entry is selected."""

    def __init__(self, app):
        self.app = app
        self.items = []
        self.sel = 0
        self.top = 0
        self.query = ""
        self._lines = {}
        self._width = None

    def entry_lines(self, item, width):
        raise NotImplementedError

    def empty_text(self):
        return "Nothing here."

    def lines(self, index, width):
        if width != self._width:
            self._lines = {}
            self._width = width
        if index not in self._lines:
            self._lines[index] = self.entry_lines(self.items[index], width)
        return self._lines[index]

    def changed(self):
        self._lines = {}
        self.sel = min(self.sel, max(0, len(self.items) - 1))
        self.top = min(self.top, self.sel)

    def relayout(self):
        self._lines = {}

    def tick(self):
        pass

    def back(self):
        if self.query:
            self.set_query("")
            return True
        return False

    def set_query(self, query):
        self.query = query
        self.refilter()
        self.sel = self.top = 0

    def fix_top(self, rows, width):
        if self.sel < self.top:
            self.top = self.sel
        while self.top < self.sel and sum(
                len(self.lines(i, width)) for i in range(self.top, self.sel + 1)) > rows:
            self.top += 1

    def draw_body(self, y0, rows, width):
        text_width = self.app.text_width()
        if not self.items:
            self.app.put(y0 + 1, LEFT_MARGIN, self.empty_text(), self.app.color["meta"], width)
            return
        self.fix_top(rows, text_width)
        y = y0
        for index in range(self.top, len(self.items)):
            for lineno, spans in enumerate(self.lines(index, text_width)):
                if y >= y0 + rows:
                    return
                if index == self.sel and lineno == 0:
                    x = self.app.put_spans(y, LEFT_MARGIN, spans, width,
                                           fill=self.app.color["pick"])
                    self.app.put(y, x, " " * max(0, LEFT_MARGIN + text_width - x),
                                 self.app.color["pick"], width)
                else:
                    self.app.put_spans(y, LEFT_MARGIN, spans, width)
                y += 1

    def move(self, step):
        if self.items:
            self.sel = max(0, min(len(self.items) - 1, self.sel + step))

    def page(self):
        height = len(self.lines(self.sel, self.app.text_width())) if self.items else 1
        return max(1, self.app.body_height // height - 1)

    def key(self, key):
        if key in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif key in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif key in (curses.KEY_NPAGE, ord(" "), 4):
            self.move(self.page())
        elif key in (curses.KEY_PPAGE, ord("b"), 21):
            self.move(-self.page())
        elif key in (ord("g"), curses.KEY_HOME):
            self.sel = 0
        elif key in (ord("G"), curses.KEY_END):
            self.sel = max(0, len(self.items) - 1)
        elif key == ord("/"):
            query = self.app.prompt(" Filter: ", self.query)
            if query is not None:
                self.set_query(query)
        elif key in ENTER_KEYS:
            if self.items:
                self.open(self.items[self.sel])
        else:
            return False
        return True

    def open(self, item):
        raise NotImplementedError


class BoardsScreen(ListScreen):
    def __init__(self, app):
        super().__init__(app)
        self.refilter()

    def refilter(self):
        needle = self.query.lower().strip("/")
        self.items = [b for b in self.app.boards
                      if not (self.app.sfw and not b.get("ws_board"))
                      and (needle in b["board"].lower() or needle in b["title"].lower())]
        self.changed()

    def entry_lines(self, board, width):
        description = clean(board.get("meta_description", ""))
        description = re.sub(r'^"[^"]*"\s*is\s*', "", description)
        description = re.sub(r"^4chan's (?:\w+ )?(?:image)?board (?:dedicated to |for )?(?:the )?",
                             "", description)
        description = description[:1].upper() + description[1:]
        spans = [(f"/{board['board']}/".ljust(7), "board", None),
                 (board["title"], "bold", None)]
        if not board.get("ws_board"):
            spans.append(("  nsfw", "nsfw", None))
        spans.append(("  " + description, "meta", None))
        return [spans]

    def empty_text(self):
        if not self.app.boards:
            return "No board list - r to try again, or Enter a board with /"
        return f"No board matches '{self.query}'."

    def status(self):
        left = "4chan" + (f"   filter: {self.query}" if self.query else "")
        count = f"{len(self.items)} board" + ("" if len(self.items) == 1 else "s")
        return left, count + ("  sfw only" if self.app.sfw else "")

    def hint(self):
        return "Enter open board  / filter  r reload  ? help  q quit"

    def open(self, board):
        self.app.open_board(board["board"])

    def key(self, key):
        if key == ord("r"):
            if self.app.load_boards():
                self.refilter()
            return True
        if key == ord("/") and not self.app.boards:
            # With no board list to filter, the prompt opens a board by name instead.
            query = self.app.prompt(" Board: ")
            if query:
                self.app.open_board(query.strip("/"))
            return True
        return super().key(key)


class CatalogScreen(ListScreen):
    def __init__(self, app, board, query=""):
        super().__init__(app)
        self.board = board
        self.threads = []
        self.sort = "bump"
        self.query = query
        self.fetched = 0

    def load(self):
        threads, error = self.app.call(f"Loading /{self.board}/...", fetch_catalog, self.board)
        if error:
            self.app.message = f"Could not load /{self.board}/: {error}"
            return False
        for thread in threads:
            thread["_text"] = (clean(thread.get("sub", "")) + " "
                               + plain_text(parse_comment(thread.get("com", "")))).lower()
        self.threads = threads
        self.fetched = time.time()
        self.refilter()
        return True

    def refilter(self):
        needle = self.query.lower()
        items = [t for t in self.threads if needle in t["_text"]]
        if self.sort == "replies":
            items.sort(key=lambda t: -t.get("replies", 0))
        elif self.sort == "images":
            items.sort(key=lambda t: -t.get("images", 0))
        elif self.sort == "new":
            items.sort(key=lambda t: -t["no"])
        self.items = items
        self.changed()

    def entry_lines(self, thread, width):
        title, meta, excerpt = catalog_entry(thread, width)
        marks = "".join(f"[{key}] " for key in ("sticky", "closed") if thread.get(key))
        head = clip(marks + title, max(10, width - cwidth(meta) - 2))
        gap = " " * max(2, width - cwidth(head) - cwidth(meta))
        style = "subject" if thread.get("sub") else "bold"
        lines = [[(head, style, None), (gap, "text", None), (meta, "meta", None)]]
        lines += [[("  " + line, "text", None)] for line in excerpt]
        lines.append([])
        return lines

    def empty_text(self):
        return f"No thread matches '{self.query}'." if self.query else "This board is empty."

    def status(self):
        title = self.app.board_title(self.board)
        left = f"/{self.board}/" + (f" - {title}" if title else "")
        if self.query:
            left += f"   filter: {self.query}"
        count = f"{len(self.items)} thread" + ("" if len(self.items) == 1 else "s")
        return left, f"{count}  sort: {self.sort}  {ago(self.fetched)} old"

    def hint(self):
        return "Enter open  / filter  s sort  r refresh  o browser  Backspace boards  ? help"

    def open(self, thread):
        self.app.open_thread(self.board, thread["no"])

    def key(self, key):
        if key == ord("s"):
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
            self.refilter()
            self.sel = self.top = 0
            self.app.message = f"Sorted by {self.sort}."
        elif key == ord("r"):
            keep = self.items[self.sel]["no"] if self.items else None
            if self.load():
                self.sel = next((i for i, t in enumerate(self.items) if t["no"] == keep), 0)
                self.app.message = f"/{self.board}/ refreshed."
        elif key == ord("o"):
            self.app.open_external(f"{SITE}/{self.board}/catalog")
        elif key == ord("y"):
            target = (thread_url(self.board, self.items[self.sel]["no"]) if self.items
                      else f"{SITE}/{self.board}/catalog")
            self.app.copy(target)
        else:
            return super().key(key)
        return True


class ThreadScreen:
    def __init__(self, app, board, no):
        self.app = app
        self.board = board
        self.no = no
        self.posts = []
        self.parsed = []
        self.replies = {}
        self.modified = None
        self.fetched = 0
        self.doc = Doc()
        self.top = 0
        self.sel = -1
        self.jumps = []
        self.watch = False
        self.new_after = None
        self.query = ""
        self.matches = []
        self.match_at = -1

    # -- loading --

    def load(self, post=None):
        result, error = self.app.call(f"Loading /{self.board}/{self.no}...",
                                      fetch_thread, self.board, self.no)
        if error:
            self.app.message = f"Could not load /{self.board}/{self.no}: {error}"
            return False
        self.set_posts(*result)
        self.relayout()
        if post and post in self.doc.post_rows:
            self.top = min(self.doc.post_rows[post], self.max_top())
        return True

    def set_posts(self, posts, modified):
        self.posts = posts
        self.parsed = [parse_comment(p.get("com", "")) for p in posts]
        self.replies = backlinks(posts, self.parsed)
        self.modified = modified
        self.fetched = time.time()

    def refresh(self, quiet=False):
        result, error = self.app.call("Checking for new posts...", fetch_thread,
                                      self.board, self.no, self.modified)
        self.fetched = time.time()
        if error:
            if "404" in error:
                self.watch = False
                self.app.message = "Thread 404'd - it is gone from 4chan. Watching stopped."
            else:
                self.app.message = f"Could not refresh: {error}"
            return
        posts, modified = result
        if posts is None:
            if not quiet:
                self.app.message = "No new posts."
            return
        last = self.posts[-1]["no"]
        fresh = sum(1 for p in posts if p["no"] > last)
        if fresh:
            self.new_after = last
        anchor = self.anchor()
        self.set_posts(posts, modified)
        self.sel = -1
        self.relayout(anchor)
        if posts[0].get("archived") or posts[0].get("closed"):
            self.watch = False
        if fresh or not quiet or not self.watch:
            self.app.message = (f"+{fresh} new post{'s' if fresh != 1 else ''}" if fresh
                                else "No new posts.") + ("" if self.watch or not quiet
                                                         else " - thread closed, watching stopped")

    def tick(self):
        if self.watch and time.time() - self.fetched >= THREAD_REFRESH:
            self.refresh(quiet=True)

    # -- layout --

    def anchor(self):
        if not self.doc.lines:
            return None
        index = self.doc.row_post[min(self.top, len(self.doc.lines) - 1)]
        return index, self.top - self.doc.post_starts[index]

    def relayout(self, anchor=None):
        if anchor is None:
            anchor = self.anchor()
        self.doc = layout_thread(self.posts, self.parsed, self.replies,
                                 self.app.text_width(), self.new_after)
        if anchor:
            index, offset = anchor
            start = self.doc.post_starts[min(index, len(self.doc.post_starts) - 1)]
            self.top = start + max(0, offset)
        self.top = max(0, min(self.top, self.max_top()))
        if self.sel >= len(self.doc.links):
            self.sel = -1
        if self.query:
            self.find(self.query, jump=False)

    def max_top(self):
        return max(0, len(self.doc.lines) - self.app.body_height)

    def scroll(self, step):
        self.top = max(0, min(self.max_top(), self.top + step))

    def current_post(self):
        if self.sel >= 0 and self.link_visible(self.sel):
            return self.doc.row_post[self.doc.links[self.sel][2]]
        return self.doc.row_post[min(self.top, len(self.doc.lines) - 1)]

    # -- drawing --

    def status(self):
        op = self.posts[0]
        title = one_line(clean(op.get("sub", ""))) or one_line(plain_text(self.parsed[0]))[:60]
        left = f"/{self.board}/ › {title or self.no}"
        total = max(1, len(self.doc.lines))
        pct = int(min(total, self.top + self.app.body_height) * 100 / total)
        state = f"watching, checked {ago(self.fetched)} ago  " if self.watch else ""
        if op.get("archived"):
            state = "archived  "
        elif op.get("closed"):
            state = "closed  "
        return left, (f"{len(self.posts) - 1} replies  {op.get('images', 0)} images  "
                      f"{state}{pct:>3}%")

    def hint(self):
        if self.sel >= 0 and self.link_visible(self.sel):
            kind, target, _ = self.doc.links[self.sel]
            return self.preview(kind, target)
        return "Tab link  Enter follow  > < posts  i file  a watch  r refresh  Backspace back  ? help"

    def preview(self, kind, target):
        if kind == "post":
            index = next((i for i, p in enumerate(self.posts) if p["no"] == target), None)
            if index is None:
                return f">>{target} is not in this thread (deleted?)"
            post = self.posts[index]
            name = clean(post.get("name") or "Anonymous")
            text = one_line(plain_text(self.parsed[index])) or "(no text)"
            return f">>{target}  {name}: {text}"
        if kind == "thread":
            board, no, post = target
            return f"Enter opens /{board}/ thread {no}" + (f" at >>{post}" if post else "")
        if kind == "board":
            board, query = target
            return f"Enter opens /{board}/" + (f" filtered on '{query}'" if query else "")
        if kind == "image":
            post = self.posts[target]
            return f"Enter opens {clean(post.get('filename', ''))}{post['ext']} in the browser  (v: in the terminal)"
        return f"Enter opens {target}"

    def draw_body(self, y0, rows, width):
        hits = {}
        for row, col, length in self.matches:
            hits.setdefault(row, []).append((col, length))
        for offset in range(rows):
            row = self.top + offset
            if row >= len(self.doc.lines):
                break
            self.app.put_spans(y0 + offset, LEFT_MARGIN, self.doc.lines[row], width,
                               sel=self.sel, hits=hits.get(row, ()))

    # -- links --

    def link_visible(self, lid):
        return self.top <= self.doc.links[lid][2] < self.top + self.app.body_height

    def select_link(self, step):
        links = self.doc.links
        if not links:
            self.app.message = "No links in this thread."
            return
        if self.sel >= 0 and self.link_visible(self.sel):
            target = self.sel + step
        else:
            visible = [i for i in range(len(links)) if self.link_visible(i)]
            if visible:
                target = visible[0] if step > 0 else visible[-1]
            elif step > 0:
                target = next((i for i, l in enumerate(links) if l[2] >= self.top), len(links))
            else:
                target = next((i for i in range(len(links) - 1, -1, -1)
                               if links[i][2] < self.top), -1)
        if not 0 <= target < len(links):
            self.app.message = "No more links."
            return
        self.sel = target
        row = links[target][2]
        body = self.app.body_height
        if row < self.top:
            self.top = row
        elif row >= self.top + body:
            self.top = min(self.max_top(), row - body + 1)

    def jump_to(self, row):
        self.jumps.append((self.top, self.sel))
        self.top = min(row, self.max_top())
        self.sel = -1

    def follow(self):
        if self.sel < 0 or not self.link_visible(self.sel):
            self.app.message = "Select a link with Tab first."
            return
        kind, target, _ = self.doc.links[self.sel]
        if kind == "post":
            if target in self.doc.post_rows:
                self.jump_to(self.doc.post_rows[target])
                self.app.message = f">>{target} - Backspace goes back."
            else:
                self.app.message = f"No post >>{target} in this thread - it may be deleted."
        elif kind == "thread":
            board, no, post = target
            if (board, no) == (self.board, self.no):
                self.jump_to(self.doc.post_rows.get(post, 0))
            else:
                self.app.open_thread(board, no, post)
        elif kind == "board":
            self.app.open_board(*target)
        elif kind == "image":
            self.app.open_external(media_url(self.board, self.posts[target]))
        else:
            self.app.open_external(target)

    def back(self):
        if self.jumps:
            self.top, self.sel = self.jumps.pop()
            self.top = min(self.top, self.max_top())
            return True
        return False

    def step_post(self, step):
        starts = self.doc.post_starts
        if step > 0:
            target = next((r for r in starts if r > self.top), None)
        else:
            target = next((r for r in reversed(starts) if r < self.top), None)
        if target is None or (step > 0 and self.top >= self.max_top()):
            self.app.message = "No more posts."
            return
        self.top = min(target, self.max_top())
        self.sel = -1

    # -- find in thread --

    def find(self, query, jump=True):
        self.query = query
        needle = query.lower()
        self.matches = []
        for row in range(len(self.doc.lines)):
            plain = self.doc.plain(row)
            lowered = plain.lower()
            at = lowered.find(needle)
            while at >= 0:
                self.matches.append((row, cwidth(plain[:at]), cwidth(plain[at:at + len(needle)])))
                at = lowered.find(needle, at + len(needle))
        if not jump:
            return
        if not self.matches:
            self.match_at = -1
            self.app.message = f"No match for '{query}'."
            return
        self.match_at = next((i for i, m in enumerate(self.matches) if m[0] >= self.top), 0)
        self.show_match()

    def next_match(self, step):
        if not self.matches:
            self.app.message = "Nothing to find - press / first."
            return
        self.match_at = (self.match_at + step) % len(self.matches)
        self.show_match()

    def show_match(self):
        row = self.matches[self.match_at][0]
        body = self.app.body_height
        if not self.top <= row < self.top + body:
            self.top = min(self.max_top(), max(0, row - body // 3))
        self.app.message = f"Match {self.match_at + 1} of {len(self.matches)}"

    # -- keys --

    def key(self, key):
        body = self.app.body_height
        if key in (curses.KEY_DOWN, ord("j")):
            self.scroll(1)
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll(-1)
        elif key in (curses.KEY_NPAGE, ord(" "), 4):
            self.scroll(body - 2)
        elif key in (curses.KEY_PPAGE, ord("b"), 21):
            self.scroll(-(body - 2))
        elif key in (ord("g"), curses.KEY_HOME):
            self.top = 0
        elif key in (ord("G"), curses.KEY_END):
            self.top = self.max_top()
        elif key == ord("\t"):
            self.select_link(1)
        elif key == curses.KEY_BTAB:
            self.select_link(-1)
        elif key in ENTER_KEYS:
            self.follow()
        elif key == ord(">"):
            self.step_post(1)
        elif key == ord("<"):
            self.step_post(-1)
        elif key == ord("/"):
            query = self.app.prompt(" Find in thread: ", self.query)
            if query:
                self.find(query)
            elif query == "":
                self.query, self.matches = "", []
        elif key == ord("n"):
            self.next_match(1)
        elif key == ord("N"):
            self.next_match(-1)
        elif key == ord("r"):
            self.refresh()
        elif key == ord("a"):
            op = self.posts[0]
            if op.get("archived") or op.get("closed"):
                self.app.message = "Not watching: this thread is closed to new posts."
            else:
                self.watch = not self.watch
                self.app.message = (f"Watching - refetching every {THREAD_REFRESH} s."
                                    if self.watch else "Stopped watching.")
        elif key == ord("s"):
            self.app.reveal = not self.app.reveal
            self.app.message = "Spoilers shown." if self.app.reveal else "Spoilers hidden."
        elif key == ord("o"):
            if self.sel >= 0 and self.link_visible(self.sel) and self.doc.links[self.sel][0] == "url":
                self.app.open_external(self.doc.links[self.sel][1])
            else:
                post = self.posts[self.current_post()]
                self.app.open_external(thread_url(self.board, self.no, post["no"]))
        elif key == ord("y"):
            post = self.posts[self.current_post()]
            self.app.copy(thread_url(self.board, self.no, post["no"]))
        elif key in (ord("i"), ord("v")):
            post = self.posts[self.current_post()]
            if "tim" not in post:
                self.app.message = f"No file on No.{post['no']}."
            elif key == ord("i"):
                self.app.open_external(media_url(self.board, post))
            else:
                thumb = post["ext"] not in TERMIMAGE_TYPES
                label = f"No.{post['no']}  {clean(post.get('filename', ''))}{post['ext']}"
                self.app.view_in_terminal(media_url(self.board, post, thumb),
                                          label + ("  (thumbnail)" if thumb else ""))
        else:
            return False
        return True


def start(stdscr, target, sfw):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    stdscr.keypad(True)
    app = App(stdscr, sfw)
    app.load_boards()
    failure = app.message
    app.screens.append(BoardsScreen(app))
    if target:
        kind, value = target
        if kind == "board":
            app.open_board(*value)
        else:
            board, no, post = value
            app.open_thread(board, no, post)
    if failure and not app.message:
        app.message = failure
    app.run()


def main():
    parser = argparse.ArgumentParser(
        description="Read 4chan in a terminal UI: boards, catalogs and threads (read-only).")
    parser.add_argument("target", nargs="?",
                        help="board or thread to open: g, /g/, g/123456 or a thread URL")
    parser.add_argument("--sfw", action="store_true", help="list only work-safe boards")
    args = parser.parse_args()

    target = None
    if args.target:
        target = parse_target(args.target)
        if not target:
            parser.error(f"not a board or thread: {args.target}")

    if not sys.stdout.isatty():
        print("chan_tui needs a terminal.", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(start, target, args.sfw)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
