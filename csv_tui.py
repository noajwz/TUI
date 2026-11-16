#!/usr/bin/env python3
"""CSV reader — a spreadsheet-style view of a .csv or .tsv file in the terminal: a grid with a fixed
header row, a cell cursor, sorting, search, filters, one record at a time, and statistics per column.

The delimiter, header row and encoding are guessed: the delimiter is whichever of , ; tab | splits the
first lines into the most consistent number of fields, so the semicolon files Excel writes in Dutch
and other European locales open as they should, and their decimal commas (1.234,56) count as numbers.
Encodings tried are UTF-8 (with or without a BOM), UTF-16 by its BOM, Windows-1252 and Latin-1.
`-` reads standard input. Nothing is ever written back to the file.
"""

import argparse
import csv
import curses
import io
import locale
import math
import os
import re
import statistics
import subprocess
import sys
import textwrap
import time
import unicodedata
from collections import Counter

SAMPLE_ROWS = 2000         # rows looked at to guess column types and widths
SNIFF_LINES = 100          # lines looked at to guess the delimiter
MAX_COL_W = 30             # a column is never drawn wider than this unless widened by hand
TOP_VALUES = 15
HIST_BINS = 10
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 10
DELIMITERS = ",;\t|"
NULLS = {"n/a", "na", "null", "none", "nan", "-", "—", "?", "#n/a"}   # don't count against a column's type
DELIM_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
BARS = " ▏▎▍▌▋▊▉█"

csv.field_size_limit(2**31 - 1)    # the default of 128 K characters raises on big cells

NUM = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
NUM_THOUSANDS = re.compile(r"[-+]?\d{1,3}(,\d{3})+(\.\d+)?")             # 1,234.56
NUM_COMMA = re.compile(r"[-+]?(\d{1,3}(\.\d{3})+(,\d+)?|\d+,\d+)")       # 1.234,56 and 3,5
DOT_DECIMAL = re.compile(r"[-+]?\d+\.\d+")
DATE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})(.*)")
DATE_DMY = re.compile(r"(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})(.*)")
DIGITS = re.compile(r"(\d+)")


def number(text, comma):
    """The value of a number cell, or None. `comma` means a decimal comma (1.234,56)."""
    s = text.strip().strip("€$£").rstrip("%").strip()
    if not s or len(s) > 40:
        return None
    if comma and NUM_COMMA.fullmatch(s):
        return float(s.replace(".", "").replace(",", "."))
    if NUM.fullmatch(s):
        return float(s)
    if not comma and NUM_THOUSANDS.fullmatch(s):
        return float(s.replace(",", ""))
    return None


def date(text, day_first):
    """(year, month, day, rest) of a date cell, or None. Two-digit years below 70 are 20xx."""
    s = text.strip()
    m = DATE_ISO.fullmatch(s)
    if m:
        y, mo, d, rest = int(m[1]), int(m[2]), int(m[3]), m[4]
    else:
        m = DATE_DMY.fullmatch(s)
        if not m:
            return None
        a, b, y, rest = int(m[1]), int(m[3]), int(m[4]), m[5]
        d, mo = (a, b) if day_first else (b, a)
        if y < 100:
            y += 2000 if y < 70 else 1900
    if not (1 <= mo <= 12 and 1 <= d <= 31) or (rest and rest[0] not in " T"):
        return None
    return (y, mo, d, rest.strip())


def natural(text):
    """Sort key that puts item2 before item10."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in DIGITS.split(text.casefold()) if p)


def col_letter(i):
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def shown(text):
    """A cell on one line: newlines and tabs become visible marks."""
    if "\n" in text or "\r" in text or "\t" in text:
        return text.replace("\r\n", "↵").replace("\n", "↵").replace("\r", "↵").replace("\t", "→")
    return text


# -- reading --

def decode(data, encoding=None):
    if encoding:
        return data.decode(encoding, errors="replace"), encoding
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "UTF-16"
    for enc, label in (("utf-8-sig", "UTF-8"), ("cp1252", "Windows-1252")):
        try:
            text = data.decode(enc)
            return text, label + (" BOM" if enc == "utf-8-sig" and data.startswith(b"\xef\xbb\xbf") else "")
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1"), "Latin-1"


def sniff(text, path):
    """The delimiter that gives the most consistent number of fields over the first lines."""
    if path.lower().endswith((".tsv", ".tab")):
        return "\t"
    sample = "\n".join(text.splitlines()[:SNIFF_LINES])
    best, best_score = ",", (0, 0)
    for d in DELIMITERS:
        try:
            counts = [len(r) for r in csv.reader(io.StringIO(sample, newline=""), delimiter=d) if r]
        except csv.Error:
            continue
        if not counts:
            continue
        width, n = Counter(counts).most_common(1)[0]
        if width < 2:
            continue
        score = (round(n / len(counts), 2), width)
        if score > best_score:
            best, best_score = d, score
    return best


class Column:
    def __init__(self, name, kind, width, comma, day_first):
        self.name, self.kind, self.width = name, kind, width
        self.comma, self.day_first = comma, day_first

    @property
    def numeric(self):
        return self.kind in ("integer", "number")

    def key(self, text):
        """Sort key of a cell: None when blank, (0, value) when it reads as this column's type,
        (1, natural text) otherwise — so a stray word in a number column sorts after the numbers."""
        if not text.strip():
            return None
        if self.numeric:
            v = number(text, self.comma)
            if v is not None:
                return (0, v)
        elif self.kind == "date":
            v = date(text, self.day_first)
            if v is not None:
                return (0, v)
        return (1, natural(text))


class Table:
    def __init__(self, path, data=None, delimiter=None, encoding=None, header=None):
        self.path, self.data = path, data
        self.forced = delimiter, encoding, header
        self.load()

    @property
    def name(self):
        return "stdin" if self.path == "-" else os.path.basename(self.path)

    def load(self):
        delimiter, encoding, header = self.forced
        if self.data is not None:
            data = self.data
        else:
            with open(self.path, "rb") as f:
                data = f.read()
        self.bytes = len(data)
        text, self.encoding = decode(data, encoding)
        self.delimiter = delimiter or sniff(text, self.path)
        rows = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=self.delimiter) if r]
        lengths = Counter(len(r) for r in rows)
        self.ragged = len(rows) - lengths.most_common(1)[0][1] if rows else 0
        self.ncols = max(lengths, default=0)
        for r in rows:
            if len(r) < self.ncols:
                r.extend([""] * (self.ncols - len(r)))
        self.all = rows
        self.set_header(self.guess_header() if header is None else header)

    def set_header(self, header):
        self.header = header
        self.rows = self.all[1:] if header else self.all
        self.analyse()

    def guess_header(self):
        """A header unless most of the first row reads as numbers or dates — headers rarely do."""
        if len(self.all) < 2:
            return bool(self.all)
        cells = [c for c in self.all[0] if c.strip()]
        valued = sum(number(c, False) is not None or number(c, True) is not None or date(c, True) is not None
                     for c in cells)
        return bool(cells) and valued * 2 <= len(cells) and len(cells) * 2 >= self.ncols

    def analyse(self):
        """Names, types and natural widths of the columns, from a sample of the rows."""
        names = self.all[0] if self.header and self.all else [""] * self.ncols
        sample = self.rows[:SAMPLE_ROWS]
        self.columns = []
        for c in range(self.ncols):
            values = [r[c] for r in sample if r[c].strip() and r[c].strip().casefold() not in NULLS]
            name = names[c].strip() or col_letter(c)
            # a decimal comma when commas between digits outnumber dot decimals that can't be thousands (1.5)
            commas = sum(1 for v in values if re.search(r"\d,\d", v))
            dots = sum(1 for v in values if DOT_DECIMAL.fullmatch(v.strip()) and not NUM_COMMA.fullmatch(v.strip()))
            comma = self.delimiter != "," and commas > 0 and commas >= dots
            nums = [number(v, comma) for v in values]
            ok = [v for v in nums if v is not None]
            firsts = [m for m in (DATE_DMY.fullmatch(v.strip()) for v in values) if m]
            day_first = not any(int(m[3]) > 12 for m in firsts) or any(int(m[1]) > 12 for m in firsts)
            dates = sum(1 for v in values if date(v, day_first) is not None)
            if not values:
                kind = "empty"
            elif len(ok) >= 0.9 * len(values):
                kind = "integer" if all(v == int(v) and "." not in s and "," not in s
                                        for v, s in zip(nums, values) if v is not None) else "number"
            elif dates >= 0.9 * len(values):
                kind = "date"
            else:
                kind = "text"
            width = max([cwidth(name)] + [len(s) if s.isascii() else cwidth(s) for s in map(shown, (r[c] for r in sample))])
            self.columns.append(Column(name, kind, max(1, min(MAX_COL_W, width)), comma, day_first))


# -- text width --

def cwidth(text):
    if text.isascii():
        return len(text)
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


def rfit(text, width):
    """Like fit(), but right-aligned when it fits — for numbers."""
    pad = width - cwidth(text)
    return " " * pad + text if pad >= 0 else fit(text, width)


def bar(fraction, width):
    cells = max(0.0, min(1.0, fraction)) * width
    full = int(cells)
    part = BARS[int((cells - full) * 8)] if full < width else ""
    return ("█" * full + part.strip()).ljust(width)


def wrap(text, width):
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines += textwrap.wrap(line.replace("\t", "    "), width, drop_whitespace=False) or [""]
    return lines


def fmt_num(x):
    if math.isfinite(x) and x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}"
    if abs(x) >= 0.01:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.4g}"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def fmt_size(n):
    for unit, limit in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= limit:
            return f"{n / limit:.1f} {unit}"
    return f"{n} B"


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


# -- statistics --

def column_stats(table, c, view):
    """Lines of the statistics page: (kind, ...) with kind section, pair or bar."""
    col = table.columns[c]
    rows = table.rows
    values = [rows[i][c] for i in view]
    filled = [v for v in values if v.strip()]
    counts = Counter(v.strip() for v in filled)
    n = len(values)
    out = [("section", f"{col.name}  ({col.kind})"),
           ("pair", "Rows", f"{n:,}" + (f" of {len(rows):,} (filtered)" if n != len(rows) else "")),
           ("pair", "Filled", f"{len(filled):,}" + (f"  ({len(filled) / n:.1%})" if n else "")),
           ("pair", "Empty", f"{n - len(filled):,}"),
           ("pair", "Distinct", f"{len(counts):,}"),
           ("pair", "Seen once", f"{sum(1 for k in counts.values() if k == 1):,}")]
    nums = [x for x in (number(v, col.comma) for v in filled) if x is not None] if col.numeric else []
    if nums:
        nums.sort()
        mean = statistics.fmean(nums)
        out += [("", ""), ("section", "Numbers"),
                ("pair", "Count", f"{len(nums):,}" + (f"  ({len(filled) - len(nums):,} not numbers)" if len(nums) < len(filled) else "")),
                ("pair", "Sum", fmt_num(math.fsum(nums))),
                ("pair", "Min", fmt_num(nums[0])),
                ("pair", "Lower quartile", fmt_num(nums[len(nums) // 4])),
                ("pair", "Median", fmt_num(statistics.median(nums))),
                ("pair", "Mean", fmt_num(mean)),
                ("pair", "Upper quartile", fmt_num(nums[(3 * len(nums)) // 4])),
                ("pair", "Max", fmt_num(nums[-1])),
                ("pair", "Std deviation", fmt_num(statistics.pstdev(nums, mean)) if len(nums) > 1 else "—")]
    if col.kind == "date":
        dated = sorted((k, v) for k, v in ((date(v, col.day_first), v) for v in filled) if k)
        if dated:
            out += [("", ""), ("section", "Dates"),
                    ("pair", "Earliest", dated[0][1].strip()), ("pair", "Latest", dated[-1][1].strip())]
            years = Counter(k[0] for k, _ in dated)
            if len(years) > 1:
                out += [("", ""), ("section", "Per year")]
                peak = max(years.values())
                out += [("bar", str(y), years[y], years[y] / len(dated), years[y] / peak) for y in sorted(years)]
    if filled and not nums and col.kind != "date":
        lengths = [cwidth(v) for v in filled]
        out += [("", ""), ("section", "Text length"),
                ("pair", "Shortest", f"{min(lengths):,}"), ("pair", "Average", f"{sum(lengths) / len(lengths):,.1f}"),
                ("pair", "Longest", f"{max(lengths):,}")]
    if counts:
        top = counts.most_common(TOP_VALUES)
        peak = top[0][1]
        out += [("", ""), ("section", "Most common" + (f" (top {TOP_VALUES})" if len(counts) > TOP_VALUES else ""))]
        out += [("bar", shown(v), k, k / n, k / peak) for v, k in top]
        if len(filled) < n:
            out.append(("bar", "(empty)", n - len(filled), (n - len(filled)) / n, (n - len(filled)) / max(peak, n - len(filled))))
    if len(nums) > 1 and nums[0] < nums[-1]:
        lo, hi = nums[0], nums[-1]
        step = (hi - lo) / HIST_BINS
        bins = [0] * HIST_BINS
        for x in nums:
            bins[min(HIST_BINS - 1, int((x - lo) / step))] += 1
        peak = max(bins)
        out += [("", ""), ("section", "Distribution")]
        out += [("bar", f"{fmt_num(lo + i * step)} – {fmt_num(lo + (i + 1) * step)}", k, k / len(nums), k / peak)
                for i, k in enumerate(bins)]
    return out


# -- filters --

OPS = re.compile(r"(>=|<=|!=|>|<|=|!|~)\s*(.*)", re.S)


def make_filter(text, col):
    """A predicate on one cell from what was typed: text (contains), =x, !x, !=x, ~regex, >n, <=n …
    `=` alone keeps empty cells and `!=` alone the filled ones. Raises ValueError on a bad regex."""
    m = OPS.fullmatch(text)
    op, arg = m.groups() if m else ("", text)
    low = arg.strip().casefold()
    if op == "":
        return lambda v: low in v.casefold()
    if op == "!":
        return lambda v: low not in v.casefold()
    if op == "=":
        return lambda v: v.strip().casefold() == low
    if op == "!=":
        return lambda v: v.strip().casefold() != low
    if op == "~":
        try:
            rx = re.compile(arg, re.I)
        except re.error as e:
            raise ValueError(f"bad pattern: {e}") from None
        return lambda v: rx.search(v) is not None
    target = col.key(arg) if col else None
    if target is None:
        raise ValueError(f"nothing to compare with after {op}")
    compare = {">": lambda a: a > target, "<": lambda a: a < target,
               ">=": lambda a: a >= target, "<=": lambda a: a <= target}[op]

    def keep(v):
        k = col.key(v)
        return k is not None and k[0] == target[0] and compare(k)
    return keep


# -- the app --

HELP = [
    ("Moving", None),
    ("↑↓←→ hjkl", "move the cursor"),
    ("PgUp PgDn  g G", "page up and down, first and last row"),
    ("0 $  Tab ⇧Tab", "first and last column, next and previous column"),
    ("click  wheel", "pick a cell, scroll"),
    ("Viewing", None),
    ("⏎", "the whole row as a record (←→ to the previous and next row)"),
    ("c", "statistics of the column: numbers, dates, most common values, distribution"),
    ("s", "sort by the column: ascending, descending, back to file order"),
    ("< >  w", "narrow or widen the column, fit it to its longest value"),
    ("p", "pin columns up to the cursor so they stay put when scrolling sideways"),
    ("x  X", "hide the column, show every column again"),
    ("H", "first row is the header, or data"),
    ("Finding", None),
    ("/  n N", "search every cell, next and previous match"),
    ("f", "filter on the column: text, =exact, !not, ~regex, >n, <=n, = (empty)"),
    ("F", "filter on text anywhere in the row"),
    ("=  !", "keep only rows with this cell's value, or drop them"),
    ("u  Esc", "undo the last filter, clear filters and search"),
    ("Other", None),
    ("y  Y", "copy the cell, copy the row (as the file's delimiter)"),
    ("e", "export the rows on screen — filtered, sorted, shown columns — to a new file"),
    ("r", "read the file again"),
    ("q", "quit, or back from a page"),
]


class App:
    def __init__(self, stdscr, table):
        self.stdscr, self.table = stdscr, table
        self.mode = "grid"                 # grid, record, stats, help
        self.filters = []                  # (column or None for the whole row, label, predicate)
        self.sort = None                   # (column, descending)
        self.search = ""
        self.widths = {}                   # column -> width set by hand
        self.pinned = 0                    # leading entries of self.cols that don't scroll
        self.row = self.cur = 0            # cursor: index in self.view, index in self.cols
        self.top = 0
        self.left = 0                      # first scrolling entry of self.cols on screen
        self.page_top = 0                  # scroll of the record, stats and help pages
        self.stats = []
        self.layout = []                   # (x, width, cols index) of columns on screen, for clicks
        self.body_y = 3
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.page = 1
        self.reset_columns()
        self.rebuild()

    def reset_columns(self):
        self.cols = list(range(self.table.ncols))
        self.pinned = min(self.pinned, len(self.cols))
        self.cur = max(0, min(self.cur, len(self.cols) - 1))

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h or x < 0:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    # -- the rows on screen --

    @property
    def col(self):
        """Index into table.columns of the cursor's column, or None when there are no columns."""
        return self.cols[self.cur] if self.cols else None

    def current_row(self):
        return self.view[self.row] if self.view else None

    def rebuild(self):
        """Filter, then sort, keeping the cursor on the same row of the file when it's still there."""
        keep = self.current_row() if hasattr(self, "view") else None
        rows = self.table.rows
        view = range(len(rows))
        for c, _, pred in self.filters:
            view = [i for i in view if pred(rows[i][c])] if c is not None else \
                [i for i in view if any(pred(v) for v in rows[i])]
        view = list(view)
        if self.sort and self.sort[0] < self.table.ncols:
            c, desc = self.sort
            key = self.table.columns[c].key
            keyed, blank = [], []
            for i in view:
                k = key(rows[i][c])
                if k is None:
                    blank.append(i)
                else:
                    keyed.append((k, i))
            keyed.sort(key=lambda p: p[0], reverse=desc)
            view = [i for _, i in keyed] + blank    # blanks last both ways
        self.view = view
        if keep is not None:
            try:
                self.row = view.index(keep)
            except ValueError:
                self.row = 0
        self.row = max(0, min(self.row, len(view) - 1))

    def width(self, c):
        return self.widths.get(c, self.table.columns[c].width)

    def fix_left(self, room):
        """Scroll sideways so the cursor's column is on screen."""
        self.left = max(self.pinned, min(self.left, len(self.cols) - 1))
        if self.cur < self.pinned:
            return
        if self.cur < self.left:
            self.left = self.cur
        pinned_w = sum(self.width(c) + 1 for c in self.cols[:self.pinned])
        while self.left < self.cur and pinned_w + sum(self.width(c) + 1 for c in self.cols[self.left:self.cur + 1]) > room:
            self.left += 1

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.draw_title(w)
        getattr(self, "draw_" + self.mode)(h, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_title(self, w):
        t = self.table
        self.put(0, 0, " CSV ", curses.color_pair(1) | curses.A_BOLD)
        shown_rows = f"{len(self.view):,} of {len(t.rows):,} rows" if len(self.view) != len(t.rows) else f"{len(t.rows):,} rows"
        info = (f"{shown_rows} × {t.ncols} cols · {DELIM_NAMES.get(t.delimiter, repr(t.delimiter))} · {t.encoding}"
                + (f" · {t.ragged:,} ragged" if t.ragged else "") + f" · {fmt_size(t.bytes)}")
        if cwidth(info) > w - 20:
            info = shown_rows
        self.put(0, w - cwidth(info) - 1, info, curses.color_pair(5))
        self.put(0, 6, t.name, curses.A_BOLD, w - 8 - cwidth(info))

    def status(self):
        parts = []
        if self.filters:
            parts.append("filter: " + " · ".join(label for _, label, _ in self.filters))
        if self.sort:
            parts.append(f"sort: {self.table.columns[self.sort[0]].name} {'▼' if self.sort[1] else '▲'}")
        if self.search:
            parts.append(f"search: “{self.search}”")
        if len(self.cols) < self.table.ncols:
            parts.append(f"{self.table.ncols - len(self.cols)} hidden")
        return "   ".join(parts)

    def draw_grid(self, h, w):
        t = self.table
        rows = t.rows
        c = self.col
        # the line with the whole value of the cell under the cursor
        if c is not None:
            name = f" {t.columns[c].name} "
            self.put(1, 0, name, curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD, min(len(name), w // 3))
            value = shown(rows[self.view[self.row]][c]) if self.view else ""
            x = min(cwidth(name), w // 3) + 1
            self.put(1, x, value, 0, w - x)
        status = self.status()
        bottom = h - 2 if status else h - 1
        if status:
            self.put(h - 2, 0, " " + status, curses.color_pair(3))
        if not t.ncols:
            self.put(3, 2, "the file is empty", curses.color_pair(5))
            return
        gutter = len(str(len(rows))) + 1
        x0 = gutter + 1
        self.fix_left(w - x0)
        order = list(range(self.pinned)) + list(range(self.left, len(self.cols)))
        self.layout, x = [], x0
        for ci in order:
            if x >= w:
                break
            cw = min(self.width(self.cols[ci]), w - x)
            self.layout.append((x, cw, ci))
            x += cw + 1
        # header
        y = 2
        self.put(y, 0, " " * w, curses.A_UNDERLINE)
        for x, cw, ci in self.layout:
            col = t.columns[self.cols[ci]]
            label = col.name
            if self.sort and self.sort[0] == self.cols[ci]:
                label = fit(label, cw - 1).rstrip() + ("▼" if self.sort[1] else "▲")
            attr = curses.A_BOLD | curses.A_UNDERLINE | curses.color_pair(1)
            if ci == self.cur:
                attr = curses.A_BOLD | curses.A_REVERSE | curses.color_pair(1)
            self.put(y, x, rfit(label, cw) if col.numeric else label, attr, cw)
            self.put(y, x + cw, "│", curses.color_pair(1 if ci == self.pinned - 1 else 5) | curses.A_UNDERLINE, 1)
        # body
        self.body_y = y0 = 3
        height = bottom - y0
        self.page = max(1, height - 1)
        if not self.view:
            self.put(y0 + 1, 2, "no rows match the filters  (u undo, Esc clear)" if self.filters else "no rows",
                     curses.color_pair(5))
            return
        if self.row < self.top:
            self.top = self.row
        elif self.row >= self.top + height:
            self.top = self.row - height + 1
        self.top = max(0, min(self.top, len(self.view) - height))
        needle = self.search.casefold()
        for k, i in enumerate(self.view[self.top:self.top + height]):
            y = y0 + k
            here = self.top + k == self.row
            self.put(y, 0, str(i + 1).rjust(gutter), (curses.color_pair(1) | curses.A_BOLD) if here else curses.color_pair(5))
            for x, cw, ci in self.layout:
                c = self.cols[ci]
                text = shown(rows[i][c])
                attr = curses.A_BOLD if here else 0
                if needle and needle in text.casefold():
                    attr |= curses.color_pair(3)
                if here and ci == self.cur:
                    attr = curses.A_REVERSE | curses.A_BOLD | (attr & curses.A_COLOR)
                self.put(y, x, rfit(text, cw) if t.columns[c].numeric else text, attr, cw)
                self.put(y, x + cw, "│", curses.color_pair(1 if ci == self.pinned - 1 else 5), 1)

    def page_lines(self, h, lines):
        """Scroll the record, stats or help page; returns the slice on screen and its first line."""
        height = h - 3
        self.page = max(1, height - 1)
        self.page_top = max(0, min(self.page_top, len(lines) - height))
        return lines[self.page_top:self.page_top + height]

    def draw_record(self, h, w):
        t = self.table
        if not self.view:
            self.mode = "grid"
            return
        i = self.view[self.row]
        sort = f" · sorted by {t.columns[self.sort[0]].name}" if self.sort else ""
        self.put(1, 1, f"row {i + 1:,} of the file · {self.row + 1:,} of {len(self.view):,} shown{sort}", curses.color_pair(5))
        name_w = min(max((cwidth(c.name) for c in t.columns), default=4) + 2, max(10, w // 3))
        lines = []
        for c, col in enumerate(t.columns):
            value = t.rows[i][c]
            for k, part in enumerate(wrap(value, w - name_w - 3)):
                lines.append((c, col.name if k == 0 else "", part))
        for k, (c, name, part) in enumerate(self.page_lines(h, lines)):
            y = 2 + k
            here = c == self.col
            self.put(y, 1, name, (curses.color_pair(1) | curses.A_BOLD) | (curses.A_REVERSE if here and name else 0), name_w - 1)
            self.put(y, name_w + 1, part, curses.A_BOLD if here else 0)

    def draw_stats(self, h, w):
        bar_w = max(0, min(30, w - 60))
        label_w = min(max((cwidth(l[1]) for l in self.stats if l[0] == "bar"), default=10) + 2, w - 26 - bar_w)
        for k, line in enumerate(self.page_lines(h, self.stats)):
            y = 2 + k
            if line[0] == "section":
                self.put(y, 1, line[1], curses.color_pair(1) | curses.A_BOLD)
            elif line[0] == "pair":
                self.put(y, 3, line[1], curses.color_pair(5), 18)
                self.put(y, 21, line[2])
            elif line[0] == "bar":
                _, label, count, share, size = line
                self.put(y, 3, label, curses.A_BOLD, label_w)
                x = 3 + label_w
                self.put(y, x, f"{count:,}".rjust(9), 0, 9)
                self.put(y, x + 10, f"{share:.1%}".rjust(6), curses.color_pair(5), 6)
                if bar_w > 3:
                    self.put(y, x + 18, bar(size, bar_w), curses.color_pair(1), bar_w)

    def draw_help(self, h, w):
        for k, (keys, what) in enumerate(self.page_lines(h, [("", None)] + HELP)):
            y = 2 + k
            if what is None:
                self.put(y, 1, keys, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.put(y, 3, keys, curses.A_BOLD, 16)
                self.put(y, 20, what, curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = {"grid": ["⏎ record", "s sort", "/ search", "f filter", "= keep", "c stats", "y copy", "? help", "q quit"],
                "record": ["↑↓ scroll", "←→ row", "Tab field", "y copy", "q back"],
                "stats": ["↑↓ scroll", "←→ column", "q back"],
                "help": ["↑↓ scroll", "q back"]}[self.mode]
        where = ""
        if self.mode == "grid" and self.view and self.cols:
            where = f"{self.row + 1:,}/{len(self.view):,} · {self.cur + 1}/{len(self.cols)}"
        room = w - 2 - (len(where) + 2 if where else 0)
        while len(keys) > 2 and cwidth("  ".join(keys)) > room:
            keys.pop(-2)                   # the least needed go first, quit stays
        self.put(y, 0, " " + "  ".join(keys), curses.color_pair(5))
        if where:
            self.put(y, w - len(where) - 1, where, curses.color_pair(1))

    # -- actions --

    def prompt(self, label, initial=""):
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                h, w = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(1) | curses.A_REVERSE)
                self.stdscr.move(h - 1, min(cwidth(text), w - 2))
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf)
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":        # ^U clears
                        buf.clear()
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf)
        finally:
            curses.curs_set(0)

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {what}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))

    def add_filter(self, c, label, pred):
        self.filters.append((c, label, pred))
        self.rebuild()
        self.say(f"{len(self.view):,} rows match" if self.view else "no rows match — u undoes it",
                 None if self.view else curses.color_pair(4))

    def filter_column(self):
        c = self.col
        if c is None:
            return
        col = self.table.columns[c]
        hint = " (text =exact !not ~regex >n <n)" if self.stdscr.getmaxyx()[1] >= 90 else ""
        text = self.prompt(f" filter {col.name}{hint}: ")
        if text is None or (not text.strip() and text not in ("=", "!=")):
            return
        try:
            pred = make_filter(text, col)
        except ValueError as e:
            self.say(str(e), curses.color_pair(4))
            return
        label = text.strip() if OPS.fullmatch(text.strip()) else f"“{text.strip()}”"
        self.add_filter(c, f"{col.name} {label}", pred)

    def filter_row(self):
        text = self.prompt(" rows containing: ")
        if text and text.strip():
            self.add_filter(None, f"any “{text.strip()}”", make_filter(text.strip(), None))

    def filter_value(self, keep):
        if not self.view or self.col is None:
            return
        c = self.col
        value = self.table.rows[self.view[self.row]][c].strip()
        col = self.table.columns[c]
        low = value.casefold()
        if keep:
            self.add_filter(c, f"{col.name} = “{value}”", lambda v: v.strip().casefold() == low)
        else:
            self.add_filter(c, f"{col.name} ≠ “{value}”", lambda v: v.strip().casefold() != low)

    def find(self, step):
        """Move to the next cell, in reading order of the shown rows and columns, holding the search."""
        if not self.search or not self.view or not self.cols:
            return
        needle = self.search.casefold()
        rows, nr = self.table.rows, len(self.view)
        for k in range(nr + 1):
            r = (self.row + step * k) % nr
            row = rows[self.view[r]]
            hits = [ci for ci, c in enumerate(self.cols) if needle in row[c].casefold()]
            if k == 0:                 # the rest of the cursor's own row
                hits = [ci for ci in hits if (ci > self.cur if step > 0 else ci < self.cur)]
            elif k == nr:              # all the way round, back to where it started
                hits = [ci for ci in hits if (ci <= self.cur if step > 0 else ci >= self.cur)]
            if hits:
                if k and (r <= self.row if step > 0 else r >= self.row):
                    self.say("search wrapped around")
                self.row, self.cur = r, hits[0] if step > 0 else hits[-1]
                return
        self.say(f"“{self.search}” isn't in the shown rows", curses.color_pair(4))

    def start_search(self):
        text = self.prompt(" search: ")
        if text is None:
            return
        self.search = text.strip()
        if self.search:
            total = sum(1 for i in self.view for c in self.cols if self.search.casefold() in self.table.rows[i][c].casefold())
            # look from the cell under the cursor itself first
            if self.view and self.cols and self.search.casefold() in self.table.rows[self.view[self.row]][self.col].casefold():
                self.say(f"{total:,} matching cells")
                return
            self.find(1)
            if total:
                self.say(f"{total:,} matching cells")

    def cycle_sort(self):
        c = self.col
        if c is None:
            return
        if not self.sort or self.sort[0] != c:
            self.sort = (c, False)
        elif not self.sort[1]:
            self.sort = (c, True)
        else:
            self.sort = None
        self.rebuild()
        self.row = 0
        col = self.table.columns[c].name
        self.say(f"sorted by {col}, {'descending' if self.sort[1] else 'ascending'}" if self.sort else "file order")

    def fit_width(self):
        c = self.col
        if c is None:
            return
        if c in self.widths:
            del self.widths[c]
            return
        h, w = self.stdscr.getmaxyx()
        rows = self.table.rows
        widest = max([cwidth(self.table.columns[c].name)] + [cwidth(shown(rows[i][c])) for i in self.view[:50_000]])
        self.widths[c] = max(1, min(widest, w - len(str(len(rows))) - 4))

    def export(self):
        if not self.cols:
            return
        base, ext = os.path.splitext(self.table.name if self.table.path != "-" else "stdin.csv")
        folder = os.path.dirname(os.path.abspath(self.table.path)) if self.table.path != "-" else os.getcwd()
        name = self.prompt(" export to: ", os.path.join(short_path(folder), f"{base}-view{ext or '.csv'}"))
        if not name or not name.strip():
            return
        path = os.path.expanduser(name.strip())
        if os.path.exists(path):
            self.say(f"{short_path(path)} exists — not overwritten", curses.color_pair(4))
            return
        t = self.table
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                out = csv.writer(f, delimiter=t.delimiter)
                if t.header:
                    out.writerow([t.all[0][c] for c in self.cols])
                for i in self.view:
                    out.writerow([t.rows[i][c] for c in self.cols])
        except OSError as e:
            self.say(f"couldn't write: {e.strerror}", curses.color_pair(4))
            return
        self.say(f"wrote {len(self.view):,} rows to {short_path(path)}")

    def reload(self):
        if self.table.path == "-":
            self.say("standard input can't be read again", curses.color_pair(4))
            return
        try:
            self.table.load()
        except (OSError, csv.Error) as e:
            self.say(f"couldn't read: {e}", curses.color_pair(4))
            return
        self.after_reshape()
        self.say(f"read again: {len(self.table.rows):,} rows")

    def toggle_header(self):
        t = self.table
        t.forced = (t.forced[0], t.forced[1], not t.header)
        t.set_header(not t.header)
        self.after_reshape()
        self.say("first row is the header" if t.header else "first row is data")

    def after_reshape(self):
        """The rows or columns changed under us: drop what may no longer apply."""
        ncols = self.table.ncols
        self.widths = {c: v for c, v in self.widths.items() if c < ncols}
        self.filters = [f for f in self.filters if f[0] is None or f[0] < ncols]
        if self.sort and self.sort[0] >= ncols:
            self.sort = None
        hidden = set(range(max(self.cols, default=-1) + 1)) - set(self.cols)
        self.cols = [c for c in range(ncols) if c not in hidden] or list(range(ncols))
        self.pinned = min(self.pinned, len(self.cols))
        self.cur = max(0, min(self.cur, len(self.cols) - 1))
        self.view = []
        self.rebuild()

    def click(self, mx, my):
        if self.mode != "grid":
            return
        for x, cw, ci in self.layout:
            if x <= mx < x + cw + 1:
                self.cur = ci
                break
        if my >= self.body_y:
            self.row = max(0, min(len(self.view) - 1, self.top + my - self.body_y))

    def handle(self, ch):
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                return True
            if bstate & curses.BUTTON4_PRESSED:
                ch = curses.KEY_UP
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000):
                ch = curses.KEY_DOWN
            elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                self.click(mx, my)
                return True
            else:
                return True
        if self.mode == "grid":
            return self.handle_grid(ch)
        return self.handle_page(ch)

    def handle_page(self, ch):
        page = self.page
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in (ord("q"), 27, curses.KEY_BACKSPACE, 127, 8, curses.KEY_ENTER, 10, 13) or \
                (self.mode == "help" and ch == ord("?")) or (self.mode == "stats" and ch == ord("c")):
            self.mode = "grid"
        elif ch in steps:
            self.page_top = max(0, self.page_top + steps[ch])
        elif self.mode == "record" and ch in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l")):
            self.row = max(0, min(len(self.view) - 1, self.row + (1 if ch in (curses.KEY_RIGHT, ord("l")) else -1)))
            self.page_top = 0
        elif self.mode == "record" and ch in (ord("\t"), curses.KEY_BTAB) and self.cols:
            self.cur = (self.cur + (1 if ch == ord("\t") else -1)) % len(self.cols)
        elif self.mode == "record" and ch == ord("y") and self.col is not None:
            self.copy(self.table.rows[self.view[self.row]][self.col], "the field")
        elif self.mode == "stats" and ch in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l")) and self.cols:
            self.cur = (self.cur + (1 if ch in (curses.KEY_RIGHT, ord("l")) else -1)) % len(self.cols)
            self.stats = column_stats(self.table, self.col, self.view)
            self.page_top = 0
        return True

    def handle_grid(self, ch):
        page = self.page
        moves = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page,
                 ord("g"): -10**9, ord("G"): 10**9}
        sideways = {curses.KEY_LEFT: -1, ord("h"): -1, curses.KEY_RIGHT: 1, ord("l"): 1,
                    ord("\t"): 1, curses.KEY_BTAB: -1, ord("0"): -10**9, curses.KEY_HOME: -10**9,
                    ord("$"): 10**9, curses.KEY_END: 10**9}
        t = self.table
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in moves:
            self.row = max(0, min(len(self.view) - 1, self.row + moves[ch]))
        elif ch in sideways and self.cols:
            self.cur = max(0, min(len(self.cols) - 1, self.cur + sideways[ch]))
        elif ch in (curses.KEY_ENTER, 10, 13) and self.view and self.cols:
            self.mode, self.page_top = "record", 0
        elif ch == ord("c") and self.cols:
            self.stats = column_stats(t, self.col, self.view)
            self.mode, self.page_top = "stats", 0
        elif ch == ord("?"):
            self.mode, self.page_top = "help", 0
        elif ch == ord("s"):
            self.cycle_sort()
        elif ch == ord("/"):
            self.start_search()
        elif ch == ord("n"):
            self.find(1)
        elif ch == ord("N"):
            self.find(-1)
        elif ch == ord("f"):
            self.filter_column()
        elif ch == ord("F"):
            self.filter_row()
        elif ch == ord("="):
            self.filter_value(True)
        elif ch == ord("!"):
            self.filter_value(False)
        elif ch == ord("u"):
            if self.filters:
                self.filters.pop()
                self.rebuild()
                self.say(f"{plural(len(self.filters), 'filter')} left" if self.filters else "no filters")
        elif ch == 27:
            if self.filters or self.search:
                self.filters, self.search = [], ""
                self.rebuild()
                self.say("filters and search cleared")
        elif ch in (ord("<"), ord(",")) and self.cols:
            self.widths[self.col] = max(1, self.width(self.col) - 2)
        elif ch in (ord(">"), ord(".")) and self.cols:
            self.widths[self.col] = self.width(self.col) + 2
        elif ch == ord("w"):
            self.fit_width()
        elif ch == ord("p") and self.cols:
            self.pinned = 0 if self.pinned == self.cur + 1 else self.cur + 1
            self.say(f"{plural(self.pinned, 'column')} pinned" if self.pinned else "nothing pinned")
        elif ch == ord("x") and len(self.cols) > 1:
            name = t.columns[self.col].name
            del self.cols[self.cur]
            if self.cur < self.pinned:
                self.pinned -= 1
            self.cur = min(self.cur, len(self.cols) - 1)
            self.say(f"hid {name} — X shows every column")
        elif ch == ord("X"):
            c = self.col
            self.cols = list(range(t.ncols))
            self.cur = self.cols.index(c) if c is not None else 0
        elif ch == ord("y") and self.view and self.cols:
            self.copy(t.rows[self.view[self.row]][self.col], "the cell")
        elif ch == ord("Y") and self.view:
            buf = io.StringIO()
            csv.writer(buf, delimiter=t.delimiter, lineterminator="").writerow(t.rows[self.view[self.row]])
            self.copy(buf.getvalue(), "the row")
        elif ch == ord("e"):
            self.export()
        elif ch == ord("r"):
            self.reload()
        elif ch == ord("H"):
            self.toggle_header()
        return True


def run(stdscr, table):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, header, pinned edge
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # search matches, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    app = App(stdscr, table)
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch != curses.KEY_RESIZE and not app.handle(ch):
            return


def print_once(table):
    tty = sys.stdout.isatty()
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if tty else ("", "", "")
    t = table
    print(f"{bold}{t.name}{reset}  {dim}{plural(len(t.rows), 'row')} × {plural(t.ncols, 'column')} · "
          f"{DELIM_NAMES.get(t.delimiter, repr(t.delimiter))} · {t.encoding} · "
          f"{'header row' if t.header else 'no header row'}"
          + (f" · {t.ragged:,} ragged rows" if t.ragged else "") + f" · {fmt_size(t.bytes)}{reset}")
    if not t.ncols:
        return 0
    view = range(len(t.rows))
    name_w = min(24, max(len("Column"), max(cwidth(c.name) for c in t.columns)))
    print(f"\n{bold}{'#':>3}  {'Column':<{name_w}}  {'Type':<8}{'Filled':>9}{'Distinct':>10}  {'Min':<16}  {'Max'}{reset}")
    for c, col in enumerate(t.columns):
        values = [v for v in (t.rows[i][c] for i in view) if v.strip()]
        keyed = sorted((k, v) for k, v in ((col.key(v), v) for v in values) if k and k[0] == 0)
        if keyed and col.numeric:
            lo, hi = fmt_num(keyed[0][0][1]), fmt_num(keyed[-1][0][1])
        elif keyed:
            lo, hi = keyed[0][1].strip(), keyed[-1][1].strip()
        else:
            lo = hi = ""
        print(f"{c + 1:>3}  {fit(col.name, name_w)}  {col.kind:<8}{len(values):>9,}{len(set(values)):>10,}  "
              f"{fit(lo, 16)}  {fit(hi, 16)}".rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Browse a CSV or TSV file in the terminal: sort, search, filter, column statistics.")
    parser.add_argument("path", help="the file, or - for standard input")
    parser.add_argument("-d", "--delimiter", help="field delimiter (default: guessed); 'tab' for a tab")
    parser.add_argument("--encoding", help="text encoding (default: guessed — UTF-8, UTF-16, Windows-1252, Latin-1)")
    head = parser.add_mutually_exclusive_group()
    head.add_argument("--header", dest="header", action="store_true", default=None, help="the first row is a header")
    head.add_argument("--no-header", dest="header", action="store_false", help="the first row is data")
    parser.add_argument("--once", action="store_true", help="print the columns with their types and ranges, and exit")
    args = parser.parse_args()
    delimiter = {"tab": "\t", "\\t": "\t"}.get(args.delimiter, args.delimiter)
    if delimiter is not None and len(delimiter) != 1:
        parser.error("the delimiter must be one character")
    data = None
    if args.path == "-":
        data = sys.stdin.buffer.read()
    elif not os.path.isfile(os.path.expanduser(args.path)):
        parser.error(f"no such file: {args.path}")
    path = args.path if args.path == "-" else os.path.expanduser(args.path)
    if data is None and os.path.getsize(path) > 20_000_000 and sys.stderr.isatty():
        print(f"reading {fmt_size(os.path.getsize(path))}…", file=sys.stderr)
    try:
        table = Table(path, data, delimiter, args.encoding, args.header)
    except (OSError, csv.Error, LookupError) as e:
        parser.error(f"couldn't read {args.path}: {e}")
    if args.once:
        raise SystemExit(print_once(table))
    if args.path == "-":
        os.dup2(os.open("/dev/tty", os.O_RDONLY), 0)     # keys come from the terminal, not the pipe
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, table)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
