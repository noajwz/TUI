#!/usr/bin/env python3
"""Folder watch — what changes in a folder while you look: files created, deleted, modified and
renamed anywhere below it, as a live log with an activity strip (macOS).

The standard library has no FSEvents, so the tree is polled: a worker thread lstat()s every entry
each interval and compares the result with the previous scan. Renames are matched by inode, an
editor's save-by-rename shows as one modification, a folder created or deleted with everything in
it is one line, and a folder that turns unreadable keeps its old entries instead of reporting them
all deleted. Whatever comes and goes between two scans is not seen.
"""

import argparse
import curses
import locale
import os
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque, namedtuple

POLL_SECONDS = 1.0
POLL_STEPS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)   # what + and - step through
MAX_ENTRIES = 200_000          # a bigger tree is refused: every scan would take seconds
MAX_EVENTS = 5000              # the log keeps this many, oldest dropped
BUCKET_SECONDS = 10            # one column of the activity strip
TICK_MS = 250
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 10
# skipped unless --all: Finder and Spotlight litter, and the inside of git and Python caches
IGNORED = {".DS_Store", ".localized", ".Spotlight-V100", ".fseventsd", ".Trashes", ".git", "__pycache__"}

BARS = " ▁▂▃▄▅▆▇█"
# kind -> (mark, colour pair)
KINDS = {"created": ("+", 2), "deleted": ("-", 4), "modified": ("~", 3), "renamed": ("→", 6)}
FILTERS = [None, "created", "deleted", "modified", "renamed"]

# size is the file's new size (old size when deleted, the total for a folder with `count` items)
Event = namedtuple("Event", "when kind path dest is_dir size delta count")
# one scanned entry; dev and ino recognise a file under a new name
Entry = namedtuple("Entry", "is_dir size mtime dev ino")


class TooBig(Exception):
    pass


def snapshot(root, depth=None, everything=False, old=None):
    """{relative path: Entry} for everything below `root`, symlinks not followed. A folder that
    can't be read keeps what `old` had below it, so losing access isn't reported as deletions."""
    entries, unreadable = {}, []
    stack = [("", 0)]
    while stack:
        rel, level = stack.pop()
        try:
            with os.scandir(os.path.join(root, rel)) as it:
                for e in it:
                    if not everything and e.name in IGNORED:
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue           # gone between the listing and the stat
                    path = f"{rel}/{e.name}" if rel else e.name
                    is_dir = stat.S_ISDIR(st.st_mode)
                    entries[path] = Entry(is_dir, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
                    if len(entries) > MAX_ENTRIES:
                        raise TooBig
                    if is_dir and (depth is None or level < depth):
                        stack.append((path, level + 1))
        except OSError:
            if not rel:
                raise                      # the watched folder itself
            unreadable.append(rel + "/")
    if unreadable and old:
        prefixes = tuple(unreadable)
        for path, entry in old.items():
            if path.startswith(prefixes):
                entries.setdefault(path, entry)
    return entries


def parent(path):
    return path.rpartition("/")[0]


def diff(old, new, when):
    """The changes from snapshot `old` to `new`, as Events sorted by path."""
    gone = {p: e for p, e in old.items() if p not in new}
    born = {p: e for p, e in new.items() if p not in old}
    events = []

    replaced = {}                          # path -> its old Entry, for files with a new inode
    for p in old.keys() & new.keys():
        o, n = old[p], new[p]
        if o.is_dir != n.is_dir:           # a file became a folder or the other way round
            gone[p], born[p] = o, n
        elif not n.is_dir and (o.size, o.mtime, o.ino) != (n.size, n.mtime, n.ino):
            events.append(Event(when, "modified", p, None, False, n.size, n.size - o.size, 0))
            if o.ino != n.ino:
                replaced[(n.dev, n.ino)] = p

    by_inode = {}
    for p, e in gone.items():
        by_inode.setdefault((e.dev, e.ino), []).append(p)
    renames = {}
    for p, e in sorted(born.items()):
        src = by_inode.get((e.dev, e.ino))
        if src and gone[src[0]].is_dir == e.is_dir:
            renames[src.pop(0)] = p
    # a temp file renamed over its target is the save, already reported as the target's change
    for key, target in replaced.items():
        for src in by_inode.get(key, ()):
            if not gone[src].is_dir:
                gone.pop(src)
    for src, dst in sorted(renames.items()):
        up = parent(src)
        if up in renames and renames[up] + src[len(up):] == dst:
            continue                       # moved along with its folder
        e = new[dst]
        events.append(Event(when, "renamed", src, dst, e.is_dir, e.size if not e.is_dir else 0, 0, 0))

    moved_to = set(renames.values())
    for kind, side in (("deleted", gone), ("created", born)):
        tops = {}                          # outermost folder created or deleted -> [items, bytes]
        loose = []
        for p, e in side.items():
            if p in renames or p in moved_to:
                continue
            top, up = p, parent(p)
            while up and up in side and up not in renames and up not in moved_to:
                top, up = up, parent(up)
            if top == p:
                loose.append(p)
                if e.is_dir:
                    tops.setdefault(p, [0, 0])
            else:
                t = tops.setdefault(top, [0, 0])
                t[0] += 1
                t[1] += 0 if e.is_dir else e.size
        for p in loose:
            e = side[p]
            if e.is_dir:
                count, size = tops[p]
                events.append(Event(when, kind, p, None, True, size, 0, count))
            else:
                events.append(Event(when, kind, p, None, False, e.size, 0, 0))
    events.sort(key=lambda ev: ev.path)
    return events


class Watcher:
    """Scans the tree every `interval` seconds on a thread of its own and keeps the log."""

    def __init__(self, root, depth=None, everything=False, interval=POLL_SECONDS):
        self.root, self.depth, self.everything, self.interval = root, depth, everything, interval
        self.lock = threading.Lock()
        self.events = deque(maxlen=MAX_EVENTS)
        self.snap = None
        self.error = None
        self.stopped = False               # a tree over MAX_ENTRIES is not watched at all
        self.paused = False
        self.scan_ms = 0
        self.scanned_at = None
        self.files = self.dirs = self.bytes = 0
        self.total = 0                     # changes since the start, including any dropped from the log
        self.wake = threading.Event()

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stopped:
            if not self.paused:
                self.poll()
            self.wake.wait(self.interval)
            self.wake.clear()

    def poll(self):
        """One scan; returns the changes it found. A pause doesn't lose changes: the first scan
        after it compares against the last one before it."""
        started = time.monotonic()
        try:
            new = snapshot(self.root, self.depth, self.everything, self.snap)
        except TooBig:
            self.error = (f"more than {MAX_ENTRIES:,} files and folders — too many to poll;"
                          " watch a smaller folder or use --depth")
            self.stopped = True
            return []
        except OSError as e:
            self.error = f"can't read the folder: {e.strerror or e}"
            return []
        changes = diff(self.snap, new, time.time()) if self.snap is not None else []
        files = sum(not e.is_dir for e in new.values())
        with self.lock:
            self.snap = new
            self.error = None
            self.scan_ms = (time.monotonic() - started) * 1000
            self.scanned_at = time.time()
            self.files, self.dirs = files, len(new) - files
            self.bytes = sum(e.size for e in new.values() if not e.is_dir)
            self.events.extend(changes)
            self.total += len(changes)
        return changes

    def clear(self):
        with self.lock:
            self.events.clear()

    def log(self):
        with self.lock:
            return list(self.events)


def fmt_size(n):
    """Decimal units, as Finder shows them."""
    for unit, limit in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


def fmt_delta(n):
    if n == 0:
        return "touched"
    return ("+" if n > 0 else "−") + fmt_size(abs(n))


def fmt_ago(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {s % 3600 // 60}m"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def fmt_interval(s):
    return f"{s:g}s"


def clean(name):
    """Filenames come NFD-normalised from APFS and may hold control characters."""
    name = unicodedata.normalize("NFC", name)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        name = "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in name)
    return name.encode("utf-8", "replace").decode("utf-8")


def cwidth(text):
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


def fit_left(text, width):
    """Like fit(), but a path keeps its end: the file name matters more than the top folders."""
    if cwidth(text) <= width:
        return fit(text, width)
    kept, used = [], 1
    for ch in reversed(text):
        w = cwidth(ch)
        if used + w > width:
            break
        kept.append(ch)
        used += w
    return fit("…" + "".join(reversed(kept)), width)


def describe(ev):
    """The path column of a log line."""
    slash = "/" if ev.is_dir else ""
    text = clean(ev.path) + slash
    if ev.kind == "renamed":
        text += " → " + clean(ev.dest) + slash
    if ev.count:
        text += f"  ({ev.count:,} item{'s' if ev.count != 1 else ''})"
    return text


def amount(ev):
    """The size column of a log line."""
    if ev.kind == "modified":
        return fmt_delta(ev.delta)
    if ev.is_dir:
        return fmt_size(ev.size) if ev.count else ""
    return fmt_size(ev.size)


def activity(events, buckets, now):
    """Changes per BUCKET_SECONDS for the last `buckets` buckets, oldest first."""
    counts = [0] * buckets
    for ev in events:
        i = int((now - ev.when) // BUCKET_SECONDS)
        if 0 <= i < buckets:
            counts[buckets - 1 - i] += 1
    return counts


def spark(counts):
    top = max(counts, default=0)
    if not top:
        return "·" * len(counts)
    return "".join("·" if c == 0 else BARS[max(1, round(c / top * (len(BARS) - 1)))] for c in counts)


class App:
    def __init__(self, stdscr, watcher, label):
        self.stdscr, self.w, self.label = stdscr, watcher, label
        self.filter = None                 # one of FILTERS
        self.search = ""
        self.sel = None                    # the selected Event; None follows the newest
        self.top = 0
        self.bell = False
        self.heard = 0                     # w.total when the bell last rang
        self.message, self.message_at = "", 0.0

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def rows(self):
        """The log newest first, with the kind filter and search applied."""
        needle = self.search.lower()
        return [ev for ev in reversed(self.w.log())
                if (self.filter is None or ev.kind == self.filter)
                and (not needle or needle in ev.path.lower() or needle in (ev.dest or "").lower())]

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
        if self.bell and self.w.total > self.heard:
            curses.beep()
        self.heard = self.w.total
        self.draw_header(w)
        rows = self.rows()
        self.draw_log(4, h - 5, rows, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        wt = self.w
        state = ("■ stopped", 4) if wt.stopped else ("‖ paused", 3) if wt.paused else ("● watching", 2)
        self.put(0, 0, " Folder watch ", curses.color_pair(1) | curses.A_BOLD)
        self.put(0, w - cwidth(state[0]) - 1, state[0], curses.color_pair(state[1]) | curses.A_BOLD)
        self.put(0, 15, fit_left(self.label, w - 17 - cwidth(state[0])), curses.A_BOLD,
                 w - 17 - cwidth(state[0]))

        if wt.error:
            self.put(1, 1, wt.error, curses.color_pair(4))
        elif wt.scanned_at is None:
            self.put(1, 1, "first scan…", curses.color_pair(3))
        else:
            depth = "" if wt.depth is None else f" · depth {wt.depth}"
            self.put(1, 1, f"{plural(wt.files, 'file')} · {plural(wt.dirs, 'folder')} · {fmt_size(wt.bytes)}   "
                     f"scan {wt.scan_ms:.0f} ms every {fmt_interval(wt.interval)}{depth}", curses.color_pair(5))

        now = time.time()
        events = self.w.log()
        lead = "activity "
        last = (f"  last change {fmt_ago(now - events[-1].when)} ago" if events
                else f"  {plural(wt.total, 'change')}" if wt.total else "  no changes yet")
        span = max(0, w - 2 - len(lead) - len(last) - 1)
        counts = activity(events, span, now)
        self.put(2, 1, lead, curses.color_pair(5))
        self.put(2, 1 + len(lead), spark(counts), curses.color_pair(1), span)
        self.put(2, 1 + len(lead) + span, last, curses.color_pair(5))

        tally = {k: 0 for k in KINDS}
        for ev in events:
            tally[ev.kind] += 1
        x = 1
        for kind, (mark, pair) in KINDS.items():
            text = f"{mark} {tally[kind]:,} {kind}"
            on = self.filter in (None, kind)
            attr = curses.color_pair(pair) | (curses.A_BOLD if self.filter == kind else 0) if on else curses.color_pair(5)
            self.put(3, x, text, attr, len(text))
            x += len(text) + 3
        if self.search:
            self.put(3, x, f"/ {self.search}", curses.color_pair(1))

    def draw_log(self, y0, height, rows, w):
        self.shown = ""                    # "21–39 of 120" in the footer when the log overflows
        if not rows:
            if self.w.scanned_at is None:
                return
            what = ("nothing matches" if self.filter or self.search
                    else "no changes yet — this updates on its own")
            self.put(y0 + 1, 2, what, curses.color_pair(5))
            return
        index = self.index(rows)
        if index < self.top:
            self.top = index
        elif index >= self.top + height:
            self.top = index - height + 1
        self.top = max(0, min(self.top, len(rows) - height))
        size_w = 10
        path_w = w - 1 - 9 - 2 - 2 - size_w - 1
        for i, ev in enumerate(rows[self.top:self.top + height]):
            y = y0 + i
            mark, pair = KINDS[ev.kind]
            chosen = self.top + i == index
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 1, time.strftime("%H:%M:%S", time.localtime(ev.when)), base | curses.color_pair(5), 8)
            self.put(y, 11, mark, base | curses.color_pair(pair) | curses.A_BOLD, 1)
            self.put(y, 13, fit_left(describe(ev), path_w), base | (curses.A_BOLD if ev.is_dir else 0), path_w)
            self.put(y, w - size_w - 1, amount(ev).rjust(size_w), base | curses.color_pair(pair), size_w)
        if len(rows) > height:
            self.shown = f"{self.top + 1}–{min(len(rows), self.top + height)} of {len(rows):,}"

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = ["↑↓ move", "⏎ Finder", f"f {self.filter or 'all'}", "/ find", "␣ pause",
                f"+- {fmt_interval(self.w.interval)}", "x clear", "c copy", f"b bell{' on' if self.bell else ''}"]
        room = w - 2 - (len(self.shown) + 2 if self.shown else 0)
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > room:
            keys.pop()                     # the least needed go first in a narrow window
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))
        if self.shown:
            self.put(y, w - len(self.shown) - 1, self.shown, curses.color_pair(1))

    # -- selection --

    def index(self, rows):
        if self.sel is not None:
            for i, ev in enumerate(rows):
                if ev is self.sel:
                    return i
            self.sel = None                # filtered away or dropped from the log
        return 0

    def move(self, delta):
        rows = self.rows()
        if not rows:
            return
        i = max(0, min(len(rows) - 1, self.index(rows) + delta))
        self.sel = None if i == 0 else rows[i]   # back at the top it follows the newest again

    def selected(self):
        rows = self.rows()
        return rows[self.index(rows)] if rows else None

    # -- actions --

    def reveal(self, ev):
        path = os.path.join(self.w.root, ev.dest if ev.kind == "renamed" else ev.path)
        if not os.path.lexists(path):
            self.say(f"{describe(ev)} is no longer there", curses.color_pair(4))
            return
        try:
            subprocess.run(["open", "-R", path], check=True, capture_output=True)
            self.say(f"shown in Finder: {clean(path)}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't open Finder", curses.color_pair(4))

    def copy(self, ev):
        path = os.path.join(self.w.root, ev.dest if ev.kind == "renamed" else ev.path)
        try:
            subprocess.run(["pbcopy"], input=path.encode(), check=True)
            self.say(f"copied {clean(path)}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))

    def step_interval(self, direction):
        wt = self.w
        later = [s for s in POLL_STEPS if (s > wt.interval if direction > 0 else s < wt.interval)]
        if not later:
            return
        wt.interval = min(later) if direction > 0 else max(later)
        wt.wake.set()
        self.say(f"scanning every {fmt_interval(wt.interval)}")

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
                    self.draw()
        finally:
            curses.curs_set(0)

    def handle(self, ch):
        page = max(1, self.stdscr.getmaxyx()[0] - 5)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in steps:
            self.move(steps[ch])
        elif ch in (curses.KEY_ENTER, 10, 13, ord("o")):
            ev = self.selected()
            if ev:
                self.reveal(ev)
        elif ch == ord("c"):
            ev = self.selected()
            if ev:
                self.copy(ev)
        elif ch == ord("f"):
            self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
            self.top = 0
        elif ch == ord("/"):
            text = self.prompt(" search: ", self.search)
            if text is not None:
                self.search, self.top = text, 0
        elif ch == 27 and (self.search or self.filter):
            self.search, self.filter, self.top = "", None, 0
        elif ch == ord(" "):
            self.w.paused = not self.w.paused
            if not self.w.paused:
                self.w.wake.set()
            self.say("paused — changes made meanwhile show up on resume" if self.w.paused else "watching again")
        elif ch == ord("x"):
            self.w.clear()
            self.sel, self.top = None, 0
        elif ch == ord("r"):
            self.w.wake.set()
        elif ch in (ord("+"), ord("=")):
            self.step_interval(1)
        elif ch in (ord("-"), ord("_")):
            self.step_interval(-1)
        elif ch == ord("b"):
            self.bell = not self.bell
            self.say("bell on: it beeps when something changes" if self.bell else "bell off")
        return True


def run(stdscr, watcher, label):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, activity
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # created
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # modified, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # deleted, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # renamed
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    watcher.start()
    app = App(stdscr, watcher, label)
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            return


def print_changes(watcher):
    """Streams the changes as lines until Ctrl-C, like a headless log."""
    tty = sys.stdout.isatty()
    colors = {"created": "\033[32m", "deleted": "\033[31m", "modified": "\033[33m", "renamed": "\033[34m"}
    dim, reset = ("\033[2m", "\033[0m") if tty else ("", "")
    watcher.poll()
    if watcher.error:
        print(f"error: {watcher.error}", file=sys.stderr)
        if watcher.stopped:
            return 1
    else:
        print(f"{dim}watching {watcher.root}: {plural(watcher.files, 'file')}, {plural(watcher.dirs, 'folder')}, "
              f"every {fmt_interval(watcher.interval)} — Ctrl-C stops{reset}", file=sys.stderr)
    try:
        while True:
            time.sleep(watcher.interval)
            error = watcher.error
            for ev in watcher.poll():
                mark = KINDS[ev.kind][0]
                color = colors[ev.kind] if tty else ""
                size = amount(ev)
                print(f"{dim}{time.strftime('%H:%M:%S', time.localtime(ev.when))}{reset}  "
                      f"{color}{mark}{reset} {describe(ev)}" + (f"  {dim}{size}{reset}" if size else ""),
                      flush=True)
            if watcher.error and watcher.error != error:
                print(f"error: {watcher.error}", file=sys.stderr)
            if watcher.stopped:
                return 1
    except KeyboardInterrupt:
        pass
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Watch a folder for changes: files created, deleted, modified and renamed below it.",
        epilog="The folder is polled, so a file that comes and goes between two scans is not seen.")
    parser.add_argument("path", nargs="?", default=".", help="folder to watch (default: the current one)")
    parser.add_argument("-n", "--interval", type=float, default=POLL_SECONDS, metavar="SECONDS",
                        help=f"seconds between scans (default {POLL_SECONDS:g})")
    parser.add_argument("-d", "--depth", type=int, metavar="N",
                        help="only look N folders deep (0: the folder's own entries); default unlimited")
    parser.add_argument("-a", "--all", action="store_true",
                        help="include " + ", ".join(sorted(IGNORED)) + ", which are skipped by default")
    parser.add_argument("--print", action="store_true",
                        help="print changes as lines instead of the full-screen view, until Ctrl-C")
    args = parser.parse_args()
    path = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        parser.error(f"not a folder: {args.path}")
    if args.interval <= 0:
        parser.error("--interval must be more than 0")
    if args.depth is not None and args.depth < 0:
        parser.error("--depth can't be negative")
    watcher = Watcher(path, args.depth, args.all, args.interval)
    if args.print:
        raise SystemExit(print_changes(watcher))
    home = os.path.expanduser("~")
    label = "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, watcher, label)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
