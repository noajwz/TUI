#!/usr/bin/env python3
"""Diff — two text files side by side, with what changed highlighted down to the character.

Similar lines in a changed block are paired up, so an edited line sits next to its original
rather than next to whatever happened to be at the same position. Unchanged stretches fold
away, n and N jump between changes, and whitespace or case can be ignored. Differences that
don't show — line endings, a missing newline at the end, a byte order mark, the encoding — are
named in the header. The files are read again when they change on disk.
"""

import argparse
import curses
import difflib
import locale
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import namedtuple

TICK_MS = 1000                 # how often the files are checked for changes
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 60, 10
CONTEXT = 3                    # unchanged lines kept around a change when folding
TAB = 4
PAIR_LIMIT = 2500              # a changed block bigger than this (lines × lines) is paired in order
INTRALINE_LIMIT = 1_000_000    # characters × characters: longer pairs are coloured whole, or it stalls
PAIR_CUTOFF = 0.5              # how alike two lines must be to be shown side by side as one change
INTRALINE_CUTOFF = 0.3         # below this a changed pair is coloured whole, not by character
HSCROLL = 8

# kind -> (marker, colour pair) on each side
MARKS = {"equal": " ", "similar": "≈", "change": "~", "delete": "−", "insert": "+"}

# li, ri: line indexes on each side, None for the side that has no line here
Row = namedtuple("Row", "kind li ri lspans rspans")
Fold = namedtuple("Fold", "start count")


class Side:
    """One file as text: its lines, how they were written, and what it looked like on disk."""

    def __init__(self, path):
        self.path = path
        self.error = None
        self.lines, self.shown = [], []
        self.encoding, self.bom, self.ending, self.final_newline = "", False, "", True
        self.stamp = None
        self.read()

    def read(self):
        self.error = None
        try:
            st = os.stat(self.path)
            with open(self.path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            self.error = f"can't read {short(self.path)}: {e.strerror or e}"
            self.lines, self.shown, self.stamp = [], [], None
            return
        self.stamp = (st.st_mtime_ns, st.st_size)
        self.size, self.mtime = st.st_size, st.st_mtime
        if b"\0" in data[:8192]:
            self.error = f"{short(self.path)} looks like a binary file, not text"
            self.lines, self.shown = [], []
            return
        self.bom = data.startswith(b"\xef\xbb\xbf")
        try:
            text, self.encoding = data.decode("utf-8-sig"), "UTF-8"
        except UnicodeDecodeError:
            text, self.encoding = data.decode("cp1252", errors="replace"), "Windows-1252"
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
        cr = text.count("\r") - crlf
        kinds = [name for name, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
        self.ending = kinds[0] if len(kinds) == 1 else "mixed " + "/".join(kinds) if kinds else ""
        lines = re.split(r"\r\n|\r|\n", text)
        self.final_newline = lines[-1] == ""
        if self.final_newline:
            lines.pop()
        self.lines = lines
        self.shown = [display(line) for line in lines]

    def changed(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size) != self.stamp
        except OSError:
            return self.stamp is not None


def display(line):
    """The line as it is drawn: tabs expanded, control characters made visible, one cell each."""
    line = line.expandtabs(TAB)
    if any(ord(c) < 32 or ord(c) == 127 for c in line):
        line = "".join("␣" if c == "\x0c" else "?" if ord(c) < 32 or ord(c) == 127 else c for c in line)
    return line


def key_of(line, ignore_space, ignore_case):
    if ignore_space:
        line = " ".join(line.split())
    if ignore_case:
        line = line.casefold()
    return line


# -- comparing --

def pair_block(ka, kb):
    """[(i, j)] for a changed block: similar lines paired (as difflib's ndiff does it, by the most
    alike pair first and then each side of it), the rest left as deletes (j None) and inserts."""
    if not ka or not kb:
        return [(i, None) for i in range(len(ka))] + [(None, j) for j in range(len(kb))]
    if len(ka) * len(kb) > PAIR_LIMIT:
        return in_order(len(ka), len(kb))
    best, at = PAIR_CUTOFF, None
    sm = difflib.SequenceMatcher(None, autojunk=False)
    for j, y in enumerate(kb):
        sm.set_seq2(y)
        for i, x in enumerate(ka):
            sm.set_seq1(x)
            if sm.real_quick_ratio() > best and sm.quick_ratio() > best:
                r = sm.ratio()
                if r > best:
                    best, at = r, (i, j)
    if at is None:
        return in_order(len(ka), len(kb))
    i, j = at
    head = pair_block(ka[:i], kb[:j])
    tail = [(None if a is None else a + i + 1, None if b is None else b + j + 1)
            for a, b in pair_block(ka[i + 1:], kb[j + 1:])]
    return head + [(i, j)] + tail


def in_order(n, m):
    return [(k if k < n else None, k if k < m else None) for k in range(max(n, m))]


def spans(x, y):
    """The changed character ranges of two paired lines, or None when they hardly match."""
    if len(x) * len(y) > INTRALINE_LIMIT:
        return None, None
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
    if sm.ratio() < INTRALINE_CUTOFF:
        return None, None
    left, right = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            if i2 > i1:
                left.append((i1, i2))
            if j2 > j1:
                right.append((j1, j2))
    return left, right


def compare(a, b, ignore_space=False, ignore_case=False):
    """(rows, ratio) — every line of both files, aligned."""
    ka = [key_of(line, ignore_space, ignore_case) for line in a.lines]
    kb = [key_of(line, ignore_space, ignore_case) for line in b.lines]
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)   # autojunk drops common lines: odd diffs
    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                kind = "equal" if a.lines[i] == b.lines[j] else "similar"
                rows.append(Row(kind, i, j, None, None))
            continue
        for i, j in pair_block(ka[i1:i2], kb[j1:j2]):
            if i is None:
                rows.append(Row("insert", None, j1 + j, None, None))
            elif j is None:
                rows.append(Row("delete", i1 + i, None, None, None))
            else:
                ls, rs = spans(a.shown[i1 + i], b.shown[j1 + j])
                rows.append(Row("change", i1 + i, j1 + j, ls, rs))
    return rows, sm.ratio() if ka or kb else 1.0


def stats(rows):
    deleted = sum(r.kind == "delete" for r in rows)
    inserted = sum(r.kind == "insert" for r in rows)
    changed = sum(r.kind == "change" for r in rows)
    hunks = sum(1 for i, r in enumerate(rows)
                if r.kind not in ("equal", "similar") and (i == 0 or rows[i - 1].kind in ("equal", "similar")))
    return deleted, inserted, changed, hunks


def folded(rows, context, opened):
    """Rows with long unchanged stretches replaced by a Fold, except the ones opened by start."""
    out, i = [], 0
    while i < len(rows):
        if rows[i].kind not in ("equal", "similar"):
            out.append(rows[i])
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j].kind in ("equal", "similar"):
            j += 1
        keep_head = 0 if i == 0 else context
        keep_tail = 0 if j == len(rows) else context
        if j - i > keep_head + keep_tail + 1 and i not in opened:
            out.extend(rows[i:i + keep_head])
            out.append(Fold(i, j - i - keep_head - keep_tail))
            out.extend(rows[j - keep_tail:j])
        else:
            out.extend(rows[i:j])
        i = j
    return out


def notes(a, b):
    """The differences you can't see in the text."""
    out = []
    if a.error or b.error:
        return out
    if a.ending != b.ending and a.ending and b.ending:
        out.append(f"line endings {a.ending} vs {b.ending}")
    elif a.ending.startswith("mixed") or b.ending.startswith("mixed"):
        out.append(f"mixed line endings in the {'left' if a.ending.startswith('mixed') else 'right'} file")
    if a.final_newline != b.final_newline and a.lines and b.lines:
        out.append(f"no newline at the end of the {'left' if not a.final_newline else 'right'} file")
    if a.bom != b.bom:
        out.append(f"byte order mark only in the {'left' if a.bom else 'right'} file")
    if a.encoding != b.encoding:
        out.append(f"read as {a.encoding} vs {b.encoding}")
    elif a.encoding != "UTF-8":
        out.append(f"both read as {a.encoding}")
    return out


# -- drawing helpers --

def chw(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cwidth(text):
    return sum(chw(ch) for ch in text)


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = chw(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def fit_left(text, width):
    """Like fit(), but a path keeps its end."""
    if cwidth(text) <= width:
        return fit(text, width)
    kept, used = [], 1
    for ch in reversed(text):
        w = chw(ch)
        if used + w > width:
            break
        kept.append(ch)
        used += w
    return fit("…" + "".join(reversed(kept)), width)


def segments(text, marked, hx, width):
    """([(text, highlighted)], more) — `text` from column `hx`, at most `width` columns, split
    where the highlighting changes; `more` when it goes on past the edge."""
    flags = bytearray(len(text))
    for a, b in marked or ():
        flags[a:b] = b"\1" * (b - a)
    out, col, used = [], 0, 0
    for i, ch in enumerate(text):
        w = chw(ch)
        if col < hx:
            col += w
            continue
        if used + w > width:
            return [("".join(c), h) for c, h in out], True
        if out and out[-1][1] == flags[i]:
            out[-1][0].append(ch)
        else:
            out.append(([ch], flags[i]))
        used += w
        col += w
    return [("".join(c), h) for c, h in out], False


def fmt_size(n):
    for unit, limit in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def short(path):
    path = os.path.abspath(path)
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


# -- the window --

class App:
    def __init__(self, stdscr, a, b, args):
        self.stdscr, self.a, self.b, self.args = stdscr, a, b, args
        self.ignore_space, self.ignore_case = args.ignore_space, args.ignore_case
        self.unified = args.unified
        self.fold = True
        self.opened = set()                # starts of folds opened with Enter
        self.cur = self.top = self.hx = 0
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.compare()

    def compare(self):
        self.rows, self.ratio = compare(self.a, self.b, self.ignore_space, self.ignore_case)
        self.counts = stats(self.rows)
        self.relayout()

    def relayout(self):
        """The lines on screen: rows folded or not, and in unified mode a change on two lines."""
        rows = folded(self.rows, CONTEXT, self.opened) if self.fold else self.rows
        self.lines = []
        for r in rows:
            if self.unified and isinstance(r, Row) and r.kind == "change":
                self.lines += [(r, "left"), (r, "right")]
            else:
                self.lines.append((r, "both"))
        self.cur = max(0, min(self.cur, len(self.lines) - 1))

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def warn(self, text):
        self.say(text, curses.color_pair(4))

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.draw_header(w)
        if self.a.error or self.b.error:
            for i, err in enumerate(e for e in (self.a.error, self.b.error) if e):
                self.put(4 + i, 2, err, curses.color_pair(4) | curses.A_BOLD)
        else:
            self.draw_lines(3, h - 4, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        a, b = self.a, self.b
        self.put(0, 0, " Diff ", curses.color_pair(1) | curses.A_BOLD)
        deleted, inserted, changed, hunks = self.counts
        if a.error or b.error:
            pass
        elif not (deleted or inserted or changed):
            same = "identical" if a.lines == b.lines and not notes(a, b) else "the same text"
            if any(r.kind == "similar" for r in self.rows):
                same = "the same, ignoring " + " and ".join(
                    x for x, on in (("whitespace", self.ignore_space), ("case", self.ignore_case)) if on)
            self.put(0, 7, same, curses.color_pair(2) | curses.A_BOLD)
        else:
            x = 7
            for text, pair in ((f"−{deleted:,}", 4), (f"+{inserted:,}", 2), (f"~{changed:,}", 3)):
                self.put(0, x, text, curses.color_pair(pair) | curses.A_BOLD, len(text))
                x += len(text) + 1
            self.put(0, x + 1, f"{plural(hunks, 'change')} · {self.ratio:.0%} the same", curses.color_pair(5))
        flags = [name for name, on in (("ignoring whitespace", self.ignore_space), ("ignoring case", self.ignore_case),
                                       ("unified", self.unified), ("all lines", not self.fold)) if on]
        if flags:
            text = " · ".join(flags)
            self.put(0, w - cwidth(text) - 1, text, curses.color_pair(1))
        quiet = notes(a, b)
        if quiet:
            self.put(1, 1, "also: " + " · ".join(quiet), curses.color_pair(3))
        # the two files, over their panes
        if self.unified:
            self.put(2, 1, f"− {short(a.path)}", curses.color_pair(4), w // 2 - 2)
            self.put(2, w // 2, f"+ {short(b.path)}", curses.color_pair(2), w - w // 2 - 1)
            return
        half = (w - 1) // 2
        for x, side, width in ((0, a, half), (half + 1, b, w - half - 1)):     # names over the panes
            # in a narrow pane the name matters more than its size
            info = "" if side.error or width < 45 else f"  {plural(len(side.lines), 'line')} · {fmt_size(side.size)}"
            name_w = max(8, width - 1 - cwidth(info))
            self.put(2, x + 1, fit_left(short(side.path), name_w), curses.A_BOLD, name_w)
            self.put(2, x + 1 + name_w, info, curses.color_pair(5), width - 1 - name_w)

    def draw_lines(self, y0, height, w):
        if self.cur < self.top:
            self.top = self.cur
        elif self.cur >= self.top + height:
            self.top = self.cur - height + 1
        self.top = max(0, min(self.top, len(self.lines) - height))
        nw = max(3, len(str(max(len(self.a.lines), len(self.b.lines)))))
        for n, (r, part) in enumerate(self.lines[self.top:self.top + height]):
            y = y0 + n
            chosen = self.top + n == self.cur
            if isinstance(r, Fold):
                text = f"⋯ {plural(r.count, 'unchanged line')} — ⏎ shows them ⋯"
                attr = curses.color_pair(5) | (curses.A_REVERSE if chosen else 0)
                self.put(y, 0, text.center(w), attr)
                continue
            if self.unified:
                self.draw_unified(y, r, part, nw, w, chosen)
            else:
                self.draw_side_by_side(y, r, nw, w, chosen)
        if len(self.lines) > height:
            pos = f" {self.top + 1}–{min(len(self.lines), self.top + height)} of {len(self.lines):,} "
            self.put(y0 + height, w - len(pos) - 1, pos, curses.color_pair(5))

    def pane(self, y, x, width, num, mark, text, marked, kind, chosen, nw, left):
        """One side of one line: number, marker, and the text from the horizontal scroll on. A
        changed line is red on the old side and green on the new, as a delete and an insert are."""
        pair = {"delete": 4, "insert": 2, "change": 4 if left else 2, "similar": 1, "equal": 0}[kind]
        num_attr = (curses.A_REVERSE if chosen else 0) | (curses.color_pair(5) if pair == 0 else curses.color_pair(pair))
        self.put(y, x, "" if num is None else str(num).rjust(nw), num_attr, nw)
        self.put(y, x + nw, " " + mark + " " if num is not None else "   ",
                 curses.color_pair(pair) | curses.A_BOLD, 3)
        if num is None:
            return
        tx, tw = x + nw + 3, width - nw - 3
        whole = kind in ("delete", "insert") or (kind == "change" and marked is None)
        base = curses.color_pair(pair) if pair and kind != "similar" else 0
        segs, more = segments(text, None if whole else marked, self.hx, tw)
        cx = tx
        for chunk, hl in segs:
            attr = base | (curses.A_REVERSE | curses.A_BOLD if hl else 0)
            self.put(y, cx, chunk, attr, cwidth(chunk))
            cx += cwidth(chunk)
        if more:
            self.put(y, x + width - 1, "›", curses.color_pair(5), 1)
        elif not segs and self.hx and text:
            self.put(y, tx, "‹", curses.color_pair(5), 1)    # all of it is off to the left

    def draw_side_by_side(self, y, r, nw, w, chosen):
        half = (w - 1) // 2
        mark = MARKS[r.kind]
        left = (r.li + 1, self.a.shown[r.li]) if r.li is not None else (None, "")
        right = (r.ri + 1, self.b.shown[r.ri]) if r.ri is not None else (None, "")
        self.pane(y, 0, half, left[0], "−" if r.kind == "change" else mark, left[1], r.lspans, r.kind, chosen, nw, True)
        sep = "≈" if r.kind == "similar" else "│"
        self.put(y, half, sep, curses.color_pair(1 if r.kind == "similar" else 5), 1)
        self.pane(y, half + 2, w - half - 2, right[0], "+" if r.kind == "change" else mark, right[1], r.rspans,
                  r.kind, chosen, nw, False)

    def draw_unified(self, y, r, part, nw, w, chosen):
        if r.kind == "change":
            kind = "delete" if part == "left" else "insert"
            marked = (r.lspans if part == "left" else r.rspans) or []
            text = self.a.shown[r.li] if part == "left" else self.b.shown[r.ri]
            self.put(y, 0, (str(r.li + 1) if part == "left" else "").rjust(nw), curses.color_pair(5), nw)
            self.put(y, nw + 1, (str(r.ri + 1) if part == "right" else "").rjust(nw), curses.color_pair(5), nw)
            self.pane_text(y, 2 * nw + 2, w - 2 * nw - 2, MARKS[kind], text, marked, 4 if part == "left" else 2, chosen)
            return
        ln = "" if r.li is None else str(r.li + 1)
        rn = "" if r.ri is None else str(r.ri + 1)
        self.put(y, 0, ln.rjust(nw), curses.color_pair(5), nw)
        self.put(y, nw + 1, rn.rjust(nw), curses.color_pair(5), nw)
        pair = {"delete": 4, "insert": 2, "similar": 1, "equal": 0}[r.kind]
        text = self.a.shown[r.li] if r.li is not None else self.b.shown[r.ri]
        self.pane_text(y, 2 * nw + 2, w - 2 * nw - 2, MARKS[r.kind], text, None, pair, chosen)

    def pane_text(self, y, x, width, mark, text, marked, pair, chosen):
        self.put(y, x, f"{mark} ", curses.color_pair(pair) | curses.A_BOLD | (curses.A_REVERSE if chosen else 0), 2)
        base = curses.color_pair(pair) if pair and mark != "≈" else 0
        segs, more = segments(text, marked, self.hx, width - 3)
        cx = x + 2
        for chunk, hl in segs:
            self.put(y, cx, chunk, base | (curses.A_REVERSE | curses.A_BOLD if hl else 0), cwidth(chunk))
            cx += cwidth(chunk)
        if more:
            self.put(y, x + width - 1, "›", curses.color_pair(5), 1)

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = ["n/N next change", "←→ scroll", "f fold" if self.fold else "f fold up", "⏎ open fold",
                "w whitespace", "i case", "u unified" if not self.unified else "u side by side",
                "s swap", "c copy patch", "r reload"]
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > w - 2:
            keys.pop()
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))

    # -- actions --

    def change_starts(self):
        out = []
        for i, (r, _) in enumerate(self.lines):
            changed = isinstance(r, Row) and r.kind not in ("equal", "similar")
            before = self.lines[i - 1][0] if i else None
            if changed and not (isinstance(before, Row) and before.kind not in ("equal", "similar")):
                out.append(i)
        return out

    def jump(self, direction):
        starts = self.change_starts()
        if not starts:
            self.say("no changes")
            return
        ahead = [s for s in starts if (s > self.cur if direction > 0 else s < self.cur)]
        if not ahead:
            self.say("that was the last change" if direction > 0 else "that was the first change")
            return
        self.cur = ahead[0] if direction > 0 else ahead[-1]
        h = self.stdscr.getmaxyx()[0] - 4
        self.top = max(0, self.cur - min(CONTEXT + 1, h // 3))   # some context above it
        n = starts.index(self.cur) + 1
        self.say(f"change {n} of {len(starts)}", curses.color_pair(1))

    def toggle_fold_here(self):
        r, _ = self.lines[self.cur] if self.lines else (None, None)
        if isinstance(r, Fold):
            self.opened.add(r.start)
            self.relayout()

    def keep_place(self, fn):
        """Runs `fn`, which rebuilds the lines, and stays on the same line of the left file."""
        r = self.lines[self.cur][0] if self.lines else None
        li = r.li if isinstance(r, Row) else None
        fn()
        if li is not None:
            for i, (x, _) in enumerate(self.lines):
                if isinstance(x, Row) and x.li is not None and x.li >= li:
                    self.cur = i
                    break

    def reload(self, quiet=False):
        changed = [n for n, s in (("left", self.a), ("right", self.b)) if s.changed()]
        for s in (self.a, self.b):
            s.read()
        self.opened.clear()
        self.keep_place(self.compare)
        if not quiet:
            self.say("read again")
        elif changed:
            self.say(f"the {' and '.join(changed)} file changed — compared again", curses.color_pair(1))

    def copy_patch(self):
        a, b = self.a, self.b
        patch = "\n".join(difflib.unified_diff(a.lines, b.lines, a.path, b.path, n=CONTEXT, lineterm=""))
        if not patch:
            self.say("no differences to copy")
            return
        try:
            subprocess.run(["pbcopy"], input=(patch + "\n").encode(), check=True)
            self.say(f"copied the unified diff ({plural(patch.count(chr(10)) + 1, 'line')})")
        except (OSError, subprocess.CalledProcessError):
            self.warn("couldn't copy — pbcopy not found")

    def handle(self, ch):
        if ch in (ord("q"), ord("Q")):
            return False
        h = self.stdscr.getmaxyx()[0]
        page = max(1, h - 6)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page, ord("b"): -page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in steps:
            self.cur = max(0, min(len(self.lines) - 1, self.cur + steps[ch]))
        elif ch in (ord("n"), ord("]"), 9):
            self.jump(1)
        elif ch in (ord("N"), ord("p"), ord("["), curses.KEY_BTAB):
            self.jump(-1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.hx += HSCROLL
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.hx = max(0, self.hx - HSCROLL)
        elif ch in (ord("0"), ord("^")):
            self.hx = 0
        elif ch in (curses.KEY_ENTER, 10, 13):
            self.toggle_fold_here()
        elif ch == ord("f"):
            self.fold = not self.fold
            self.opened.clear()
            self.keep_place(self.relayout)
        elif ch == ord("w"):
            self.ignore_space = not self.ignore_space
            self.keep_place(self.compare)
            self.say("ignoring changes in whitespace" if self.ignore_space else "whitespace counts again")
        elif ch == ord("i"):
            self.ignore_case = not self.ignore_case
            self.keep_place(self.compare)
            self.say("ignoring case" if self.ignore_case else "case counts again")
        elif ch == ord("u"):
            self.unified = not self.unified
            self.keep_place(self.relayout)
        elif ch == ord("s"):
            self.a, self.b = self.b, self.a
            self.opened.clear()
            self.compare()
            self.say("sides swapped")
        elif ch == ord("c"):
            self.copy_patch()
        elif ch == ord("r"):
            self.reload()
        return True


def run(stdscr, a, b, args):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, similar lines
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # added
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # changed, notes, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # removed, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, a, b, args)
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch == -1:
            if a.changed() or b.changed():
                app.reload(quiet=True)
            continue
        if not app.handle(ch):
            return


def print_diff(a, b, args):
    """Side by side (or unified, with -u) as text; exits like diff: 0 same, 1 different."""
    rows, ratio = compare(a, b, args.ignore_space, args.ignore_case)
    deleted, inserted, changed, hunks = stats(rows)
    tty = sys.stdout.isatty()
    width = os.get_terminal_size().columns if tty else args.width
    red, green, dim, rev, reset = ("\033[31m", "\033[32m", "\033[2m", "\033[7m", "\033[0m") if tty else ("",) * 5
    color = {"delete": red, "insert": green, "similar": "", "equal": ""}
    nw = max(3, len(str(max(len(a.lines), len(b.lines)))))

    def side(num, mark, text, marked, kind, width, left):
        kind = ("delete" if left else "insert") if kind == "change" else kind
        tw = width - nw - 3
        head = (str(num).rjust(nw) if num is not None else " " * nw) + f" {mark if num is not None else ' '} "
        if num is None:
            return head + " " * tw
        segs, more = segments(text, marked, 0, tw - (1 if cwidth(text) > tw else 0))
        body = "".join((rev if hl else "") + chunk + (reset + color[kind] if hl else "") for chunk, hl in segs)
        shown = sum(cwidth(c) for c, _ in segs)
        return color[kind] + head + body + ("›" if more else "") + reset + " " * (tw - shown - (1 if more else 0))

    if args.unified:
        out = list(difflib.unified_diff(a.lines, b.lines, a.path, b.path, n=CONTEXT, lineterm=""))
        for line in out:
            c = red if line.startswith("-") and not line.startswith("---") else \
                green if line.startswith("+") and not line.startswith("+++") else dim if line.startswith("@@") else ""
            print(c + line + (reset if c else ""))
    else:
        half = (width - 1) // 2
        for r in (folded(rows, CONTEXT, set()) if not args.all else rows):
            if isinstance(r, Fold):
                print(dim + f"⋯ {plural(r.count, 'unchanged line')} ⋯".center(width) + reset)
                continue
            mark = MARKS[r.kind]
            left = side(None if r.li is None else r.li + 1, "−" if r.kind == "change" else mark,
                        a.shown[r.li] if r.li is not None else "", r.lspans, r.kind, half, True)
            right = side(None if r.ri is None else r.ri + 1, "+" if r.kind == "change" else mark,
                         b.shown[r.ri] if r.ri is not None else "", r.rspans, r.kind, width - half - 1, False)
            print(left + dim + ("≈" if r.kind == "similar" else "│") + reset + right)
    sys.stdout.flush()                     # the summary comes after the diff, even through a pipe
    summary = (f"−{deleted:,} +{inserted:,} ~{changed:,} lines in {plural(hunks, 'change')}, {ratio:.0%} the same"
               if deleted or inserted or changed else "no differences in the text")
    ignored = [x for x, on in (("whitespace", args.ignore_space), ("case", args.ignore_case)) if on]
    if ignored and not (deleted or inserted or changed) and any(r.kind == "similar" for r in rows):
        summary += ", ignoring " + " and ".join(ignored)
    quiet = notes(a, b)
    print(summary + ("; also " + ", ".join(quiet) if quiet else ""), file=sys.stderr)
    return 1 if deleted or inserted or changed or quiet else 0


def main():
    parser = argparse.ArgumentParser(
        description="Compare two text files side by side, with the changed characters highlighted.",
        epilog="If one of the two is a folder, the file of the same name in it is used, as diff does. "
               "Exit status with --print is 0 when the files are the same, 1 when they differ, 2 on trouble.")
    parser.add_argument("left", help="the old file")
    parser.add_argument("right", help="the new file (or a folder holding one of the same name)")
    parser.add_argument("-w", "--ignore-space", action="store_true", help="ignore changes in the amount of whitespace")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="ignore upper and lower case")
    parser.add_argument("-u", "--unified", action="store_true", help="one column, - and + lines, like diff -u")
    parser.add_argument("--print", action="store_true", help="print the diff instead of opening the window")
    parser.add_argument("-a", "--all", action="store_true", help="with --print, show unchanged stretches in full")
    parser.add_argument("--width", type=int, default=160, help="with --print to a pipe, the width (default 160)")
    args = parser.parse_args()
    left, right = args.left, args.right
    if os.path.isdir(left) and not os.path.isdir(right):
        left = os.path.join(left, os.path.basename(right))
    elif os.path.isdir(right) and not os.path.isdir(left):
        right = os.path.join(right, os.path.basename(left))
    for p in (left, right):
        if os.path.isdir(p):
            parser.error(f"{p} is a folder — give two files, or a file and a folder")
        if not os.path.exists(p):
            parser.error(f"no such file: {p}")
    a, b = Side(left), Side(right)
    if args.print:
        for s in (a, b):
            if s.error:
                print(f"error: {s.error}", file=sys.stderr)
                raise SystemExit(2)
        raise SystemExit(print_diff(a, b, args))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, a, b, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
