#!/usr/bin/env python3
"""Terminal reader for BBC News world headlines: the RSS feeds, plus the full text of each story."""

import argparse
import bisect
import curses
import html
import http.client
import locale
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import namedtuple
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

FEED_URL = "https://feeds.bbci.co.uk/news/{}/rss.xml"
USER_AGENT = "bbc-tui/1.0 (terminal news reader)"

# (tab label, feed path); the number keys pick these in order
SECTIONS = [
    ("World", "world"),
    ("Africa", "world/africa"),
    ("Asia", "world/asia"),
    ("Europe", "world/europe"),
    ("Latin America", "world/latin_america"),
    ("Middle East", "world/middle_east"),
    ("US & Canada", "world/us_and_canada"),
]

REFRESH_SECONDS = 900                          # the feeds advertise <ttl>15</ttl>
RETRY_SECONDS = 60                             # after a failed fetch, before trying again by itself
FETCH_TIMEOUT = 15
MAX_WIDTH = 88                                 # `w` toggles between this and the full window
MIN_W, MIN_H = 40, 10
LEFT_MARGIN = 2
META_W = 9                                     # right-hand column of the list: "live 12h"
TITLE_LINES = 2                                # per story in the list; the rest gets an ellipsis
SUMMARY_LINES = 2
THIN_WORDS = 30                                # a page with less text also gets the feed's summary

KEY_BACK = (curses.KEY_BACKSPACE, curses.KEY_LEFT, 8, 127)

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "svg"}
# Which elements of which data-block hold text, and the block kind they become. Everything else on
# the page (topicList, promoList, links, ...) is navigation and is dropped.
BLOCK_TAGS = {
    "text": {"p": "p", "li": "li"},
    "subheadline": {"h2": "h", "h3": "h"},
    "image": {"figcaption": "caption"},
    "media": {"figcaption": "media"},
}
CAPTION_PREFIX = re.compile(r"^(Image|Figure|Media|Video) caption,\s*")
PLAYER_MESSAGES = ("To play this video", "This video can not be played")
# The sign-offs that end a story, whatever it is about; the Africa desk's are on nearly all of theirs.
BOILERPLATE = re.compile(r"^(Go to BBCAfrica\.com|Follow us on Twitter|Get our flagship newsletter"
                         r"|Sign up for our|Listen to Newsbeat|You may also be interested in)")
# (kind, first line prefix, continuation prefix)
PREFIXES = {"li": ("• ", "  "), "media": ("▶ ", "  "), "caption": ("  ", "  ")}
TAG_MARKS = {"video": "▶ ", "live": "live ", "audio": "♪ "}
TAG_NOTES = {
    "video": "This is a video report. Press o to watch it in a browser.",
    "live": "This is a live page. Press o to follow it in a browser.",
    "audio": "This is an audio programme. Press o to listen in a browser.",
}

Story = namedtuple("Story", "title summary link published")
Line = namedtuple("Line", "text kind block offset")


# ---------------------------------------------------------------- network


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.read(), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        return None, f"network error: {getattr(e, 'reason', e)}"


def fetch_feed(path):
    data, err = fetch(FEED_URL.format(path))
    if err:
        return None, err
    try:
        return parse_feed(data), None
    except ET.ParseError:
        return None, "the feed is not valid XML"


def fetch_article(link):
    data, err = fetch(link)
    if err:
        return None, err
    return parse_article(data.decode("utf-8", "replace")), None


# ---------------------------------------------------------------- parsing


def clean(text):
    """Entities decoded, whitespace runs collapsed, control characters gone."""
    text = html.unescape(text or "")
    return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())


def parse_pubdate(text):
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return None


def parse_iso(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def parse_feed(data):
    stories, seen = [], set()
    for item in ET.fromstring(data).iter("item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        key = link.split("?")[0]                # a story can be listed twice under one feed
        if not title or not link or key in seen:
            continue
        seen.add(key)
        stories.append(Story(title, clean(item.findtext("description")), link,
                             parse_pubdate(item.findtext("pubDate"))))
    return stories


class Article:
    def __init__(self, blocks=(), byline="", published=None, updated=None, error=None):
        self.blocks = list(blocks)              # [(kind, text)]
        self.byline = byline
        self.published = published
        self.updated = updated
        self.error = error
        self.fetched = time.time()


class ArticleParser(HTMLParser):
    """Pulls the story out of the page's <article>, which is a flat run of data-block sections."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.block = None
        self.block_at = 0
        self.kind = None                        # the element being captured
        self.kind_at = 0
        self.buf = []
        self.bold = 0
        self.unbold = 0                         # characters seen outside <b> in this capture
        self.linked = 0
        self.unlinked = 0                       # characters seen outside <a> in this capture
        self.blocks = []
        self.loose = []                         # <p> outside any block: all a video page has
        self.byline = []
        self.times = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        self.depth += 1
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        attrs = dict(attrs)
        if self.block is None and "data-block" in attrs:
            self.block, self.block_at = attrs["data-block"], self.depth
            return
        if tag in ("b", "strong"):
            self.bold += 1
        if tag == "a":
            self.linked += 1
        if self.block == "metadata" and tag == "time":
            self.times.append(parse_iso(attrs.get("datetime")))
        if self.kind is None:
            kind = "loose" if self.block is None and tag == "p" else BLOCK_TAGS.get(self.block, {}).get(tag)
            if kind:
                self.kind, self.kind_at = kind, self.depth
                self.buf, self.unbold, self.unlinked = [], 0, 0

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        if tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        if tag == "a":
            self.linked = max(0, self.linked - 1)
        if self.kind and self.depth <= self.kind_at:
            self._flush()
        if self.block and self.depth <= self.block_at:
            self.block = None
        self.depth -= 1

    def handle_data(self, data):
        if self.skip:
            return
        if self.block == "byline" and data.strip():
            self.byline.append(clean(data))
        if self.kind:
            if self.linked and data == "external" and self.buf[-1:] == [", "]:
                self.buf.pop()                  # hidden screen-reader text at the end of a link
                return
            self.buf.append(data)
            if not self.bold:
                self.unbold += len(data.strip())
            if not self.linked:
                self.unlinked += len(data.strip())

    def _flush(self):
        kind, self.kind = self.kind, None
        if kind == "li" and not self.unlinked:
            return                              # a bullet that is one link: a "more on this" promo
        text = clean("".join(self.buf))
        if kind in ("caption", "media"):
            text = CAPTION_PREFIX.sub("", text)
        if BOILERPLATE.match(text):
            return
        if kind == "p" and not self.unbold:
            kind = "lede"                       # the paragraph is bold from end to end
        if text:
            (self.loose if kind == "loose" else self.blocks).append((kind, text))


def format_byline(parts):
    """["By", "A Name", "Role", ", Place"] → "By A Name · Role, Place"."""
    if parts[:1] != ["By"] or len(parts) < 2:
        return " ".join(parts)
    rest = re.sub(r"\s+,", ",", " ".join(parts[2:]))
    if not rest:
        return f"By {parts[1]}"
    if rest.startswith(","):                    # "By A Name, Reporting from Place"
        return f"By {parts[1]}{rest}"
    if rest.startswith(("and ", "& ")):         # "By A Name and Another Name"
        return f"By {parts[1]} {rest}"
    return f"By {parts[1]} · {rest}"


def parse_article(page):
    start, end = page.find("<article"), page.rfind("</article>")
    parser = ArticleParser()
    if start >= 0:
        parser.feed(page[start:end if end > start else len(page)])
    blocks = parser.blocks
    if not any(kind in ("p", "lede", "li") for kind, _ in blocks):
        blocks = blocks + [("p", text) for _, text in parser.loose
                           if not text.startswith(PLAYER_MESSAGES)]
    times = [t for t in parser.times if t]
    return Article(blocks, format_byline(parser.byline),
                   times[0] if times else None, times[1] if len(times) > 1 else None)


def story_tag(link):
    """What the feed item links to, when it isn't a written story."""
    if "/videos/" in link or "/av/" in link:
        return "video"
    if "/live/" in link:
        return "live"
    if "/sounds/" in link:
        return "audio"
    return ""


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


def wrap_text(text, width):
    lines, line, col = [], "", 0
    for word in text.split():
        size = cwidth(word)
        if line and col + 1 + size > width:
            lines.append(line)
            line, col = "", 0
        while size > width:
            head = clip(word, width) or word[0]
            lines.append(head)
            word = word[len(head):]
            size = cwidth(word)
        line = f"{line} {word}" if line else word
        col = cwidth(line)
    if line:
        lines.append(line)
    return lines


def wrap_capped(text, width, limit):
    """At most limit lines of wrapped text, with an ellipsis if some of it did not fit."""
    lines = wrap_text(text, width)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = clip(lines[-1], width - 1).rstrip() + "…"
    return lines


def age(ts, now=None):
    if ts is None:
        return ""
    seconds = max(0, int((now or time.time()) - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return "now"


def when(ts):
    """A timestamp in the local zone; the year only appears if it isn't this one."""
    moment = datetime.fromtimestamp(ts)
    year = f" {moment.year}" if moment.year != datetime.now().year else ""
    return f"{moment:%a} {moment.day} {moment:%b}{year}, {moment:%H:%M}"


def build_rows(stories, width):
    """The story list as rows of (kind, text, story index), and the first row of each story."""
    rows, first = [], []
    for index, story in enumerate(stories):
        first.append(len(rows))
        for text in wrap_capped(story.title, width, TITLE_LINES):
            rows.append(("title", text, index))
        for text in wrap_capped(story.summary, width, SUMMARY_LINES):
            rows.append(("summary", text, index))
        rows.append(("blank", "", index))
    return rows, first


def article_blocks(story, article):
    """Everything the reader shows, in order: the feed's title and the page's body."""
    blocks = [("title", story.title)]
    dateline = []
    if article.byline:
        dateline.append(article.byline)
    published = article.published or story.published
    if published:
        stamp = when(published)
        if article.updated and article.updated - published >= 60:
            stamp += f" · updated {when(article.updated)}"
        dateline.append(stamp)
    blocks += [("dim", text) for text in dateline]
    blocks.append(("rule", ""))

    tag = story_tag(story.link)
    words = sum(len(text.split()) for kind, text in article.blocks if kind in ("p", "lede", "li"))
    if article.error:
        blocks.append(("error", f"Could not fetch the full story ({article.error}). "
                                "This is the summary from the feed; r tries again, o opens it in a browser."))
    elif tag:
        blocks.append(("note", TAG_NOTES[tag]))
    elif not words:
        blocks.append(("note", "There is no article text on this page. "
                               "This is the summary from the feed; o opens it in a browser."))
    if words < THIN_WORDS and story.summary:    # a video page can have only "Produced by ..."
        blocks.append(("p", story.summary))
    return blocks + article.blocks


def layout(blocks, width):
    """Display lines. Each remembers its block and how far into it (non-space characters) it starts,
    which is what keeps the reader on the same sentence when the wrapping width changes."""
    lines = []
    for block, (kind, text) in enumerate(blocks):
        if kind == "rule":
            lines.append(Line("─" * width, kind, block, 0))
            lines.append(Line("", "blank", block, 1))
            continue
        if kind == "h":
            lines.append(Line("", "blank", block, -2))
        if lines and kind not in ("dim", "h") and lines[-1].kind not in ("blank", "title", "dim", "h"):
            lines.append(Line("", "blank", block, -1))
        lead, rest = PREFIXES.get(kind, ("", ""))
        offset = 0
        for index, part in enumerate(wrap_text(text, max(1, width - cwidth(lead)))):
            lines.append(Line((rest if index else lead) + part, kind, block, offset))
            offset += len(part) - part.count(" ")
    return lines


def line_at(lines, block, offset):
    """The line holding that position, after a re-layout."""
    found = 0
    for index, line in enumerate(lines):
        if line.block == block and line.offset <= offset:
            found = index
        elif line.block > block:
            break
    return found


def tab_start(labels, active, width):
    """The first tab to draw so that the active one still fits in width columns."""
    start = 0
    while start < active and sum(cwidth(label) + 2 for label in labels[start:active + 1]) > width:
        start += 1
    return start


# ---------------------------------------------------------------- the app


class Feed:
    def __init__(self):
        self.stories = []
        self.fetched = 0.0
        self.tried = 0.0
        self.error = None
        self.sel = 0
        self.top = 0


class App:
    def __init__(self, stdscr, section):
        self.stdscr = stdscr
        self.section = section
        self.feeds = {}
        self.articles = {}                      # link → Article, refetched after REFRESH_SECONDS
        self.read = set()                       # links opened in this session
        self.story = None                       # the story on screen; None means the list
        self.article = None
        self.blocks = []
        self.lines = []
        self.lines_width = 0
        self.atop = 0
        self.full_width = False
        self.message = ""
        self._init_colors()

    def _init_colors(self):
        self.color = {"bar": curses.A_REVERSE, "foot": curses.A_REVERSE, "h": curses.A_BOLD,
                      "mark": curses.A_BOLD, "err": curses.A_BOLD, "tab": curses.A_NORMAL}
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        self.color.update({
            "bar": curses.color_pair(1) | curses.A_BOLD,
            "h": curses.color_pair(3) | curses.A_BOLD,
            "mark": curses.color_pair(2) | curses.A_BOLD,
            "err": curses.color_pair(2) | curses.A_BOLD,
        })

    # -- geometry --

    @property
    def feed(self):
        return self.feeds.setdefault(self.section, Feed())

    def text_width(self):
        cols = self.stdscr.getmaxyx()[1]
        room = cols - 2 * LEFT_MARGIN
        return max(20, room if self.full_width else min(MAX_WIDTH, room))

    def list_width(self):
        return max(10, self.text_width() - META_W)

    def list_height(self):
        return max(1, self.stdscr.getmaxyx()[0] - 3)    # header, tabs, footer

    def page_height(self):
        return max(1, self.stdscr.getmaxyx()[0] - 2)    # header, footer

    # -- loading --

    def busy(self, text):
        height, width = self.stdscr.getmaxyx()
        self.put(height - 1, 0, f" {text} ".ljust(width), self.color["foot"])
        self.stdscr.refresh()

    def load_feed(self, force=False):
        feed = self.feed
        now = time.time()
        if not force and feed.stories and now - feed.fetched < REFRESH_SECONDS:
            return
        self.busy(f"Fetching {SECTIONS[self.section][0]}…")
        feed.tried = now
        stories, err = fetch_feed(SECTIONS[self.section][1])
        feed.error = err
        if err:
            if feed.stories:                    # keep what is on screen and say so
                self.message = f"Refresh failed: {err}"
            return
        selected = feed.stories[feed.sel].link if feed.sel < len(feed.stories) else None
        feed.stories, feed.fetched = stories, now
        links = [story.link for story in stories]
        feed.sel = links.index(selected) if selected in links else 0

    def tick(self):
        """Refresh the list by itself once it is stale; never while a story is being read."""
        now = time.time()
        feed = self.feed
        if (self.story is None and now - feed.fetched >= REFRESH_SECONDS
                and now - feed.tried >= RETRY_SECONDS):
            self.load_feed(force=True)

    def open_story(self, force=False):
        feed = self.feed
        if not feed.stories:
            return
        story = feed.stories[feed.sel]
        hit = self.articles.get(story.link)
        article = hit
        if force or not hit or time.time() - hit.fetched >= REFRESH_SECONDS:
            self.busy("Loading story…")
            fresh, err = fetch_article(story.link)
            if err:
                if hit:
                    self.message = f"Reload failed: {err}"
                else:
                    article = Article(error=err)
            else:
                article = self.articles[story.link] = fresh
        anchor = self.anchor() if force and self.story == story else None
        self.story, self.article = story, article
        self.read.add(story.link)
        self.blocks = article_blocks(story, article)
        self.lines, self.lines_width = [], 0
        self.ensure_layout()
        self.atop = line_at(self.lines, *anchor) if anchor else 0

    def anchor(self):
        if not self.lines:
            return None
        line = self.lines[min(self.atop, len(self.lines) - 1)]
        return line.block, line.offset

    def ensure_layout(self):
        """Re-wraps when the window or the `w` toggle changed the width, staying on the same text."""
        width = self.text_width()
        if width == self.lines_width:
            return
        anchor = self.anchor()
        self.lines = layout(self.blocks, width)
        self.lines_width = width
        if anchor:
            self.atop = line_at(self.lines, *anchor)

    def open_browser(self):
        story = self.story
        if story is None and self.feed.stories:
            story = self.feed.stories[self.feed.sel]
        if story is None:
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, story.link], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.message = "Opened in the browser."
        except (OSError, subprocess.CalledProcessError):
            self.message = f"Could not open a browser ({story.link})"

    # -- drawing --

    def put(self, y, x, text, attr=0):
        height, width = self.stdscr.getmaxyx()
        room = width - x - (1 if y == height - 1 else 0)  # the last cell of the last row can't be written
        if y < 0 or y >= height or room <= 0:
            return
        try:
            self.stdscr.addstr(y, x, clip(text, room), attr)
        except curses.error:
            pass

    def draw_bar(self, y, left, right, attr):
        width = self.stdscr.getmaxyx()[1]
        left = clip(left, max(0, width - cwidth(right) - 1))
        self.put(y, 0, left + " " * max(0, width - cwidth(left) - cwidth(right)) + right, attr)

    def draw_footer(self, hint):
        height, width = self.stdscr.getmaxyx()
        attr = self.color["foot"]
        if "failed" in self.message or "Could not" in self.message:
            attr = self.color["bar"]
        self.put(height - 1, 0, f" {self.message or hint} ".ljust(width), attr)

    def draw_tabs(self, y):
        labels = [label for label, _ in SECTIONS]
        width = self.stdscr.getmaxyx()[1]
        x = 0
        for index in range(tab_start(labels, self.section, width), len(labels)):
            text = f" {labels[index]} "
            if x + cwidth(text) > width:
                break
            self.put(y, x, text, self.color["bar"] if index == self.section else curses.A_DIM)
            x += cwidth(text)

    def draw_list(self):
        height = self.stdscr.getmaxyx()[0]
        feed = self.feed
        now = time.time()
        right = f"{len(feed.stories)} stories · {datetime.fromtimestamp(feed.fetched):%H:%M} " if feed.fetched else ""
        self.draw_bar(0, f" BBC News · {SECTIONS[self.section][0]}", right, self.color["bar"])
        self.draw_tabs(1)

        body = self.list_height()
        if not feed.stories:
            lines = ["Fetching…"]
            if feed.error:
                lines = ["Could not fetch the headlines:", feed.error, "", "Press r to try again."]
            for index, text in enumerate(lines):
                self.put(3 + index, LEFT_MARGIN, text, self.color["err"] if feed.error and index < 2 else 0)
            self.draw_footer("r retry  ←→ section  q quit")
            return

        rows, first = build_rows(feed.stories, self.list_width())
        feed.sel = max(0, min(feed.sel, len(feed.stories) - 1))
        sel_top = first[feed.sel]
        sel_end = first[feed.sel + 1] - 1 if feed.sel + 1 < len(first) else len(rows) - 1
        feed.top = min(feed.top, sel_top)       # keep the selected story wholly on screen
        if sel_end > feed.top + body:
            feed.top = first[bisect.bisect_left(first, sel_end - body)]
        if feed.top > len(rows) - body:         # the window grew; always start on a story's first row
            feed.top = first[bisect.bisect_left(first, max(0, len(rows) - body))]

        meta_x = LEFT_MARGIN + self.list_width() + 1
        for row in range(body):
            at = feed.top + row
            if at >= len(rows):
                break
            kind, text, index = rows[at]
            y = 2 + row
            story = feed.stories[index]
            selected = index == feed.sel
            if selected and kind != "blank":
                self.put(y, 0, "▌", self.color["mark"])
            if kind == "title":
                unread = story.link not in self.read
                attr = curses.A_DIM if not unread else curses.A_BOLD
                if selected:
                    attr = curses.A_REVERSE | (curses.A_BOLD if unread else 0)
                self.put(y, LEFT_MARGIN, text, attr)
                if at == first[index]:
                    meta = TAG_MARKS.get(story_tag(story.link), "") + age(story.published, now)
                    self.put(y, meta_x + META_W - 1 - cwidth(meta), meta, curses.A_DIM)
            elif kind == "summary":
                self.put(y, LEFT_MARGIN, text, curses.A_DIM if story.link in self.read else 0)
        self.draw_footer("↑↓ move  Enter read  ←→ section  r refresh  o browser  w width  q quit")

    def draw_article(self):
        self.ensure_layout()
        body = self.page_height()
        self.atop = max(0, min(self.atop, max(0, len(self.lines) - body)))
        total = max(1, len(self.lines))
        pct = min(100, (self.atop + body) * 100 // total)
        feed = self.feed
        position = f"{feed.sel + 1}/{len(feed.stories)}" if feed.stories else ""
        self.draw_bar(0, f" BBC News · {SECTIONS[self.section][0]}", f"{position}  {pct:>3}% ", self.color["bar"])
        attrs = {"title": curses.A_BOLD, "h": self.color["h"], "lede": curses.A_BOLD, "dim": curses.A_DIM,
                 "rule": curses.A_DIM, "caption": curses.A_DIM, "media": curses.A_DIM, "note": self.color["h"], "error": self.color["err"]}
        for row in range(body):
            at = self.atop + row
            if at >= len(self.lines):
                break
            line = self.lines[at]
            self.put(1 + row, LEFT_MARGIN, line.text, attrs.get(line.kind, 0))
        self.draw_footer("↑↓ Space scroll  ← back  n/p story  o browser  r reload  w width  q quit")

    def draw(self):
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if height < MIN_H or width < MIN_W:
            self.put(0, 0, f"Too small: needs {MIN_W}×{MIN_H}")
        elif self.story is None:
            self.draw_list()
        else:
            self.draw_article()
        self.stdscr.refresh()

    # -- keys --

    def set_section(self, section):
        self.section = section % len(SECTIONS)
        self.load_feed()

    def move_page(self, step):
        """Moves the selection by about one screenful of rows, whatever the stories' heights."""
        feed = self.feed
        _, first = build_rows(feed.stories, self.list_width())
        if not first:
            return
        target = first[feed.sel] + step * self.list_height()
        if step > 0:
            feed.sel = next((i for i, row in enumerate(first) if row >= target), len(first) - 1)
        else:
            feed.sel = next((i for i in range(len(first) - 1, -1, -1) if first[i] <= target), 0)

    def key_list(self, key):
        feed = self.feed
        if key in (curses.KEY_DOWN, ord("j")):
            feed.sel = min(feed.sel + 1, max(0, len(feed.stories) - 1))
        elif key in (curses.KEY_UP, ord("k")):
            feed.sel = max(feed.sel - 1, 0)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.move_page(1)
        elif key in (curses.KEY_PPAGE, ord("b")):
            self.move_page(-1)
        elif key in (curses.KEY_HOME, ord("g")):
            feed.sel = 0
        elif key in (curses.KEY_END, ord("e")):
            feed.sel = max(0, len(feed.stories) - 1)
        elif key in (10, 13, curses.KEY_ENTER, ord("l")):
            self.open_story()
        elif key in (curses.KEY_RIGHT, ord("\t")):
            self.set_section(self.section + 1)
        elif key in (curses.KEY_LEFT, curses.KEY_BTAB):
            self.set_section(self.section - 1)
        elif ord("1") <= key < ord("1") + len(SECTIONS):
            self.set_section(key - ord("1"))
        elif key == ord("r"):
            self.load_feed(force=True)

    def key_article(self, key):
        page = max(1, self.page_height() - 2)
        if key in (curses.KEY_DOWN, ord("j")):
            self.atop += 1
        elif key in (curses.KEY_UP, ord("k")):
            self.atop -= 1
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.atop += page
        elif key in (curses.KEY_PPAGE, ord("b")):
            self.atop -= page
        elif key in (curses.KEY_HOME, ord("g")):
            self.atop = 0
        elif key in (curses.KEY_END, ord("e")):
            self.atop = len(self.lines)
        elif key in KEY_BACK:
            self.story = None
        elif key == ord("n") and self.feed.sel + 1 < len(self.feed.stories):
            self.feed.sel += 1
            self.open_story()
        elif key == ord("p") and self.feed.sel > 0:
            self.feed.sel -= 1
            self.open_story()
        elif key == ord("r"):
            self.open_story(force=True)

    def run(self):
        self.stdscr.timeout(1000)               # ages tick over and a stale list refreshes itself
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.draw()
        self.load_feed()
        while True:
            self.draw()
            key = self.stdscr.getch()
            if key == -1:
                self.tick()
                continue
            self.message = ""
            if key == ord("q"):
                return
            if key == curses.KEY_RESIZE:
                continue
            if key == ord("o"):
                self.open_browser()
            elif key == ord("w"):
                self.full_width = not self.full_width
            elif self.story is None:
                self.key_list(key)
            else:
                self.key_article(key)


def find_section(text):
    """A section by any prefix of its label or feed name: `eu`, `latin`, `us`, `middle_east`."""
    needle = text.lower().replace(" ", "_")
    for index, (label, path) in enumerate(SECTIONS):
        if label.lower().replace(" ", "_").startswith(needle) or path.split("/")[-1].startswith(needle):
            return index
    return None


def print_once(section):
    label, path = SECTIONS[section]
    stories, err = fetch_feed(path)
    if err:
        print(f"Could not fetch {label}: {err}", file=sys.stderr)
        return 1
    print(f"BBC News · {label}\n")
    for story in stories:
        print(f"{age(story.published):>4}  {story.title}")
        for line in wrap_text(story.summary, 74):
            print(f"      {line}")
    return 0


def run(stdscr, section):
    App(stdscr, section).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", nargs="?", default="world",
                        help="world, africa, asia, europe, latin, middle or us (default: world)")
    parser.add_argument("--once", action="store_true", help="print the headlines once and exit")
    args = parser.parse_args()
    section = find_section(args.section)
    if section is None:
        parser.error(f"no section called {args.section!r}")
    if args.once:
        raise SystemExit(print_once(section))
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, section)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
