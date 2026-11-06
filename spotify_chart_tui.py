#!/usr/bin/env python3
"""Terminal chart of the top 100 songs on Spotify: daily, weekly and all time (data from kworb.net)."""

import argparse
import curses
import locale
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

BASE_URL = "https://kworb.net/spotify/"
USER_AGENT = "spotify-chart-tui/1.0 (terminal chart viewer)"
TOP_N = 100

# (key, label, page, column heading over the bars)
CHARTS = [
    ("daily", "Daily", "country/global_daily.html", "per day"),
    ("weekly", "Weekly", "country/global_weekly.html", "per week"),
    ("alltime", "All time", "songs.html", "all time"),
]

MIN_W, MIN_H = 50, 12
CHROME_ROWS = 6                                # title, headings, rule, two detail lines, keys
RANK_W, MOVE_W, VALUE_W = 3, 4, 6
TITLE_MAX, ARTIST_MAX = 46, 24
ARTIST_MIN_LABEL = 40                          # below this the artist only appears in the detail lines

EIGHTHS = " ▏▎▍▌▋▊▉"
TRACK_RE = re.compile(r"/track/(\w+)")
DATE_RE = re.compile(r'class="pagetitle">[^<]*?(\d{4}/\d{2}/\d{2})')


@dataclass
class Song:
    rank: int
    artist: str
    title: str
    streams: int
    track_id: str = ""
    row: dict = field(default_factory=dict)    # the page's cells by column heading


@dataclass
class Chart:
    key: str
    date: str
    songs: list
    sel: int = 0
    top: int = 0


class TableParser(HTMLParser):
    """Headings and rows of the first table on the page, plus each row's track id if it links one."""

    def __init__(self):
        super().__init__()
        self.headers = []
        self.rows = []
        self.cell = None
        self.row = None
        self.track_id = ""
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "tr":
            self.row, self.track_id = [], ""
        elif tag in ("th", "td"):
            self.cell = []
        elif tag == "a" and self.cell is not None:
            match = TRACK_RE.search(dict(attrs).get("href") or "")
            if match:
                self.track_id = match.group(1)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.done:
            return
        if tag in ("th", "td") and self.cell is not None:
            text = "".join(self.cell).strip()
            self.cell = None
            if tag == "th":
                self.headers.append(text)
            elif self.row is not None:
                self.row.append(text)
        elif tag == "tr":
            if self.row:
                self.rows.append((self.row, self.track_id))
            self.row = None
        elif tag == "table":
            self.done = True


def parse_int(text):
    try:
        return int(re.sub(r"[^\d-]", "", text or ""))
    except ValueError:
        return None


def parse_chart(key, html):
    """Page HTML → Chart. Columns are found by heading, so a reordered table still parses."""
    parser = TableParser()
    parser.feed(html)
    if "Artist and Title" not in parser.headers or "Streams" not in parser.headers:
        raise ValueError("the kworb page no longer has the expected table")
    songs = []
    for cells, track_id in parser.rows:
        if len(cells) != len(parser.headers):
            continue
        row = dict(zip(parser.headers, cells))
        streams = parse_int(row["Streams"])
        if streams is None:
            continue
        artist, sep, title = row["Artist and Title"].partition(" - ")
        if not sep:
            artist, title = "", artist
        songs.append(Song(parse_int(row.get("Pos")) or len(songs) + 1, artist, title, streams, track_id, row))
        if len(songs) == TOP_N:
            break
    if not songs:
        raise ValueError("the kworb page has no songs in its table")
    match = DATE_RE.search(html)
    return Chart(key, match.group(1) if match else "", songs)


def fetch_chart(key):
    page = next(c[2] for c in CHARTS if c[0] == key)
    req = urllib.request.Request(BASE_URL + page, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", "replace")
        return parse_chart(key, html), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from kworb.net"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"network error: {getattr(e, 'reason', e)}"
    except ValueError as e:
        return None, str(e)


def song_url(song):
    if song.track_id:
        return f"https://open.spotify.com/track/{song.track_id}"
    return "https://open.spotify.com/search/" + urllib.parse.quote(f"{song.artist} {song.title}")


def fmt_big(value):
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit * 0.9995:       # so 999.6M reads 1.00B and not 1.00e+03M
            return f"{value / limit:#.3g}".rstrip(".") + suffix
    return str(value)


def cwidth(text):
    """Display columns: titles here include CJK, accents and emoji, so len() is wrong."""
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = cwidth(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def bar(value, top, width):
    """A bar from zero in eighths of a cell; anything above zero shows at least a sliver."""
    eighths = round(value / top * width * 8) if top > 0 else 0
    if value > 0:
        eighths = max(eighths, 1)
    full, part = divmod(min(eighths, width * 8), 8)
    return "█" * full + (EIGHTHS[part] if part else "")


def move_cell(text):
    """kworb's P+ column → (cell text, style). NEW and RE fall through as they are."""
    if text == "=":
        return "=", "dim"
    change = parse_int(text)
    if change is None:
        return text, "fresh"
    return (f"▲{change}", "up") if change > 0 else (f"▼{-change}", "down")


def detail(song):
    """The selected song's numbers, most useful first; whatever the chart lacks is left out."""
    row = song.row
    streams = f"{song.streams:,} streams"
    change = parse_int(row.get("Streams+"))
    if change is not None:
        streams += f" ({change:+,})"
    parts = [streams]
    daily = parse_int(row.get("Daily"))
    if daily is not None:
        parts.append(f"{daily:,} a day")
    peak = parse_int(row.get("Pk"))
    if peak is not None:
        times = parse_int(row.get("(x?)"))
        parts.append(f"peak #{peak}" + (f" ×{times}" if times else ""))
    days = parse_int(row.get("Days"))
    if days is not None:
        parts.append(f"{days} day" + ("" if days == 1 else "s"))
    weeks = parse_int(row.get("Wks"))
    if weeks is not None:
        parts.append(f"{weeks} week" + ("" if weeks == 1 else "s"))
    week = parse_int(row.get("7Day"))
    if week is not None:
        parts.append(f"7-day {fmt_big(week)}")
    total = parse_int(row.get("Total"))
    if total is not None:
        parts.append(f"total {fmt_big(total)}")
    return "  ".join(parts)


def columns(width, has_move):
    """Column widths for a window `width` wide. Whatever the labels can't use goes to the bars."""
    avail = width - RANK_W - 1 - (MOVE_W + 1 if has_move else 0) - VALUE_W - 2
    bar_w = max(10, avail // 4)
    label = avail - bar_w - 1
    if label >= ARTIST_MIN_LABEL:
        artist = min(ARTIST_MAX, label * 2 // 5)
        title = min(TITLE_MAX, label - artist - 1)
    else:
        artist, title = 0, label
    used = title + (artist + 1 if artist else 0)
    return {"move": MOVE_W if has_move else 0, "title": title, "artist": artist, "bar": avail - used - 1}


def row_runs(song, top, cols):
    """One chart row as (text, style) runs; the screen and --once each map styles to their colours."""
    runs = [(f"{song.rank:>{RANK_W}} ", "text")]
    if cols["move"]:
        move, style = move_cell(song.row.get("P+", ""))
        runs += [(fit(move, cols["move"]), style), (" ", "text")]
    runs.append((fit(song.title, cols["title"]) + " ", "text"))
    if cols["artist"]:
        runs.append((fit(song.artist, cols["artist"]) + " ", "dim"))
    runs.append((f"{fmt_big(song.streams):>{VALUE_W}} ", "text"))
    runs.append((" " + bar(song.streams, top, cols["bar"]), "bar"))
    return runs


def heading_text(key, cols):
    label = next(c[3] for c in CHARTS if c[0] == key)
    text = f"{'#':>{RANK_W}} "
    if cols["move"]:
        text += fit("move", cols["move"]) + " "
    text += fit("song", cols["title"]) + " "
    if cols["artist"]:
        text += fit("artist", cols["artist"]) + " "
    return text[:-1] + "streams  " + label


def put(stdscr, y, x, text, w, attr=0):
    """addstr clipped to `w` display columns; the bottom-right cell raises, which is harmless."""
    if w <= 0:
        return
    if cwidth(text) > w:
        text = fit(text, w)
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


class App:
    def __init__(self, stdscr, key):
        self.stdscr = stdscr
        self.key = key
        self.charts = {}                       # fetched once per chart; `r` fetches again
        self.message = ""
        self.failed = False

    def chart(self):
        return self.charts.get(self.key)

    def load(self):
        """Fetch the current chart. A failed refresh keeps what is on screen and says so."""
        self.message, self.failed = "Loading from kworb.net…", False
        self.draw()
        chart, err = fetch_chart(self.key)
        self.message, self.failed = err or "", bool(err)
        if chart:
            old = self.charts.get(self.key)
            if old:
                chart.sel = min(old.sel, len(chart.songs) - 1)
                chart.top = old.top
            self.charts[self.key] = chart

    def switch(self, key):
        self.key = key
        if key not in self.charts:
            self.load()

    def move(self, delta):
        chart = self.chart()
        if chart:
            chart.sel = max(0, min(len(chart.songs) - 1, chart.sel + delta))

    def open_selected(self):
        chart = self.chart()
        if not chart:
            return
        song = chart.songs[chart.sel]
        try:
            subprocess.run(["open", song_url(song)], check=True, capture_output=True, timeout=10)
            self.message = f"Opened {song.title}"
        except (OSError, subprocess.SubprocessError) as e:
            self.message, self.failed = f"couldn't open Spotify: {e}", True

    def draw(self):
        stdscr = self.stdscr
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if w < MIN_W or h < MIN_H:
            put(stdscr, 0, 0, f"Window too small, needs {MIN_W}×{MIN_H}", w)
            stdscr.refresh()
            return
        self.draw_title(w)
        chart = self.chart()
        if chart:
            self.draw_chart(chart, h, w)
        elif self.message:
            put(stdscr, 3, 2, self.message, w - 4, curses.color_pair(4 if self.failed else 5))
        self.draw_keys(h, w)
        stdscr.refresh()

    def draw_title(self, w):
        x = 1
        title = "Spotify Top 100" if w >= 70 else "Top 100"
        put(self.stdscr, 0, x, title, w - x, curses.color_pair(1) | curses.A_BOLD)
        x += len(title) + 2
        for n, (key, label, _, _) in enumerate(CHARTS, 1):
            text = f" {n} {label} "
            put(self.stdscr, 0, x, text, w - x, curses.A_REVERSE if key == self.key else 0)
            x += len(text)
        chart = self.chart()
        where = "Global"
        if chart and chart.date:
            where = f"Global {chart.date}" if w >= 70 else chart.date
        if x + 1 + len(where) < w:
            put(self.stdscr, 0, w - len(where) - 1, where, len(where), curses.color_pair(5))

    def draw_chart(self, chart, h, w):
        stdscr = self.stdscr
        visible = h - CHROME_ROWS
        if chart.sel < chart.top:
            chart.top = chart.sel
        elif chart.sel >= chart.top + visible:
            chart.top = chart.sel - visible + 1
        chart.top = max(0, min(chart.top, max(0, len(chart.songs) - visible)))

        cols = columns(w, "P+" in chart.songs[0].row)
        put(stdscr, 1, 0, heading_text(chart.key, cols), w, curses.color_pair(5))
        styles = {
            "text": 0,
            "dim": curses.color_pair(5),
            "up": curses.color_pair(2),
            "down": curses.color_pair(4),
            "fresh": curses.color_pair(3),
            "bar": curses.color_pair(1),
        }
        most = max(s.streams for s in chart.songs)
        for i, song in enumerate(chart.songs[chart.top:chart.top + visible]):
            y = 2 + i
            selected = chart.top + i == chart.sel
            x = 0
            for text, style in row_runs(song, most, cols):
                attr = styles[style]
                if selected:                   # the bar keeps its length readable against the reverse row
                    attr = attr | curses.A_BOLD if style == "bar" else curses.A_REVERSE
                put(stdscr, y, x, text, w - x, attr)
                x += cwidth(text)

        song = chart.songs[chart.sel]
        put(stdscr, h - 4, 0, "─" * w, w, curses.color_pair(5))
        name = f"#{song.rank}  {song.title}" + (f" — {song.artist}" if song.artist else "")
        put(stdscr, h - 3, 1, name, w - 2, curses.A_BOLD)
        put(stdscr, h - 2, 1, detail(song), w - 2)

    def draw_keys(self, h, w):
        keys = "↑↓ move  ←→ chart  o open in Spotify  r refresh  q quit"
        if w < 68:
            keys = "↑↓ ←→  o open  r refresh  q quit"
        chart = self.chart()
        position = f"{chart.sel + 1}/{len(chart.songs)}" if chart else ""
        attr = curses.color_pair(5)
        if self.message and chart:
            keys, attr = self.message, curses.color_pair(4 if self.failed else 5)
        room = w - 2 - len(position) - 1
        put(self.stdscr, h - 1, 1, keys, room, attr)
        put(self.stdscr, h - 1, w - len(position) - 1, position, len(position), curses.color_pair(5))

    def handle(self, ch):
        """Returns False to quit."""
        h = self.stdscr.getmaxyx()[0]
        page = max(1, h - CHROME_ROWS - 1)
        order = [c[0] for c in CHARTS]
        self.message, self.failed = "", False
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.move(page)
        elif ch in (curses.KEY_PPAGE, ord("b")):
            self.move(-page)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.move(-TOP_N)
        elif ch in (curses.KEY_END, ord("e")):
            self.move(TOP_N)
        elif ch in (curses.KEY_RIGHT, ord("\t"), ord("l")):
            self.switch(order[(order.index(self.key) + 1) % len(order)])
        elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord("h")):
            self.switch(order[(order.index(self.key) - 1) % len(order)])
        elif ord("1") <= ch < ord("1") + len(order):
            self.switch(order[ch - ord("1")])
        elif ch in (ord("r"), ord("R")):
            self.load()
        elif ch in (ord("o"), ord("O"), curses.KEY_ENTER, 10, 13):
            self.open_selected()
        return True


def run(stdscr, key):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, bars
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # moved up
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # new or re-entry
    curses.init_pair(4, curses.COLOR_RED, -1)      # moved down, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    app = App(stdscr, key)
    app.load()
    stdscr.timeout(1000)
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch == -1:
            continue
        if not app.handle(ch):
            return


def print_once(key):
    chart, err = fetch_chart(key)
    if err:
        print(err, file=sys.stderr)
        return 1
    colors = {"dim": "\033[90m", "up": "\033[32m", "down": "\033[31m", "fresh": "\033[33m", "bar": "\033[36m"}
    tty = sys.stdout.isatty()
    label = next(c[1] for c in CHARTS if c[0] == key)
    print(f"Spotify Top {len(chart.songs)}, {label}" + (f", Global {chart.date}" if chart.date else ""))
    width = max(MIN_W, shutil.get_terminal_size().columns)
    cols = columns(width, "P+" in chart.songs[0].row)
    most = max(s.streams for s in chart.songs)
    for song in chart.songs:
        line = ""
        for text, style in row_runs(song, most, cols):
            line += f"{colors[style]}{text}\033[0m" if tty and style in colors else text
        print(line.rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(description="The top 100 songs on Spotify as a terminal chart.")
    parser.add_argument("--chart", default=CHARTS[0][0], choices=[c[0] for c in CHARTS],
                        help=f"chart shown at start (default {CHARTS[0][0]})")
    parser.add_argument("--once", action="store_true", help="print the chart once and exit")
    args = parser.parse_args()
    if args.once:
        raise SystemExit(print_once(args.chart))
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args.chart)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
