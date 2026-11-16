#!/usr/bin/env python3
"""Day — plan the day in time blocks and track where the time actually went.

Plan: a timeline of the day with your blocks on the left and what you tracked on the right, so
plan and reality sit side by side. Add a block by typing what and when — `Deep work 1h30`,
`Lunch 12:00-13:00`, `Call 14:30 45m` — move it, stretch it, tick it off, or start its timer.

Track: start and stop timers per activity. One runs at a time; starting another stops the first.
A running timer is saved with its start time, so it keeps counting after you quit, and
`--start`, `--stop` and `--status` work from the shell (a status line, a hotkey).

Summary: tracked time per activity against what was planned, for a day, a week or a month.

Everything is kept in one JSON file (~/.local/state/day_tui/day.json, or --file).
"""

import argparse
import curses
import datetime as dt
import json
import locale
import os
import re
import sys
import tempfile
import time
import unicodedata

DATA_PATH = os.path.expanduser("~/.local/state/day_tui/day.json")
SLOTS = (15, 30, 60)          # minutes per timeline row; z cycles
DEFAULT_SLOT = 30
DEFAULT_BLOCK = 60            # minutes a new block lasts when you don't say
MIN_SESSION = 10              # seconds; a timer stopped sooner is dropped as a mistake
TICK_MS = 1000
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 60, 16
UNDO_DEPTH = 50
PALETTE = ("BLUE", "GREEN", "MAGENTA", "YELLOW", "CYAN", "RED")   # activity colours, in order of first use
BARS = " ▏▎▍▌▋▊▉█"


# -- times --

def day_key(d):
    return d.isoformat()


def day_bounds(d):
    """Epoch seconds of the day's midnight and the next — right across DST changes."""
    start = dt.datetime.combine(d, dt.time())
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def epoch_at(d, minute):
    return (dt.datetime.combine(d, dt.time()) + dt.timedelta(minutes=minute)).timestamp()


def minute_of(ts, d):
    """Minutes since the start of day d (may be <0 or >1440 for other days)."""
    return (ts - day_bounds(d)[0]) / 60


def hhmm(minute):
    minute = int(round(minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def clock(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def dur(seconds, secs=False):
    """1:05 (h:mm), or 1:05:09 with seconds."""
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if secs else f"{h}:{m:02d}"


def dur_words(minutes):
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h and m else f"{h}h" if h else f"{m}m"


TIME = r"(\d{1,2})(?:[:.](\d{2}))?"
RANGE = re.compile(rf"(?<![\d:]){TIME}\s*(?:-|–|—|to\b)\s*{TIME}(?![\d:])")
OPEN_RANGE = re.compile(rf"(?<![\d:]){TIME}\s*[-–—]\s*$|(?<![\d:]){TIME}\s*[-–—](?=\s)")
AT = re.compile(r"(?:@\s*|\bat\s+)?(?<![\d:])(\d{1,2})[:.](\d{2})(?![\d:])")
DURATION = re.compile(r"(?<![\d:.])(?:(\d+(?:[.,]\d+)?)\s*h(?:ours?|rs?)?(?:\s*(\d{1,2})\s*(?:m(?:in(?:utes?)?)?)?)?"
                      r"|(\d+)\s*m(?:in(?:utes?)?)?)(?![\w:])", re.I)


class SpecError(Exception):
    pass


def to_minute(h, m):
    h, m = int(h), int(m or 0)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise SpecError(f"{h}:{m:02d} isn't a time of day")
    return h * 60 + m


def parse_spec(text, start=None, length=None):
    """'Deep work 1h30', 'Lunch 12-13', 'Call 14:30 45m' -> (title, start, end) in minutes of the
    day. Missing parts come from `start` and `length`; end None means still open (a running timer)."""
    s = " " + text.strip() + " "
    end = None
    m = RANGE.search(s)
    if m:
        start, end = to_minute(m[1], m[2]), to_minute(m[3], m[4])
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = AT.search(s)
        if m:
            start = to_minute(m[1], m[2])
            s = s[:m.start()] + " " + s[m.end():]
        m = DURATION.search(s)
        if m:
            if m[3]:
                length = int(m[3])
            else:
                length = round(float(m[1].replace(",", ".")) * 60) + int(m[2] or 0)
            s = s[:m.start()] + " " + s[m.end():]
        if start is None:
            raise SpecError("when? add a time like 9:30 or 9-11")
        end = None if length is None else start + length
    title = re.sub(r"\s+", " ", s).strip(" -–—@")
    if end is not None and end <= start:
        raise SpecError("it ends before it starts")
    if end is not None and end > 1440:
        raise SpecError("it runs past midnight")
    return title, start, end


# -- the data --

class Store:
    """The JSON file: plans per day, sessions, the running timer, activities and their colours."""

    def __init__(self, path):
        self.path = path
        self.stamp = None
        self.undo = []
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            raise SystemExit(f"day_tui: can't read {self.path}: {e}")
        data.setdefault("plans", {})
        data.setdefault("sessions", [])
        data.setdefault("running", None)
        data.setdefault("activities", [])
        data.setdefault("colors", {})
        self.data = data
        self.stamp = self.file_stamp()

    def file_stamp(self):
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def changed_on_disk(self):
        return self.file_stamp() != self.stamp

    def save(self):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".day-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)          # never a half-written file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.stamp = self.file_stamp()

    def change(self):
        """Call before changing anything: keeps a copy for undo."""
        self.undo.append(json.dumps(self.data))
        del self.undo[:-UNDO_DEPTH]

    def undo_last(self):
        if not self.undo:
            return False
        self.data = json.loads(self.undo.pop())
        self.save()
        return True

    # plans

    def blocks(self, d):
        return sorted(self.data["plans"].get(day_key(d), []), key=lambda b: b["start"])

    def set_blocks(self, d, blocks):
        if blocks:
            self.data["plans"][day_key(d)] = sorted(blocks, key=lambda b: b["start"])
        else:
            self.data["plans"].pop(day_key(d), None)

    # activities and colours

    def color(self, name):
        key = name.strip().lower()
        colors = self.data["colors"]
        if key not in colors:
            used = list(colors.values())
            colors[key] = min(range(len(PALETTE)), key=lambda i: (used.count(i), i))
        return colors[key]

    def touch(self, name):
        """Put an activity first in the list."""
        acts = [a for a in self.data["activities"] if a.lower() != name.lower()]
        self.data["activities"] = [name] + acts
        self.color(name)

    # sessions

    def running(self):
        return self.data["running"]

    def start(self, name, at=None):
        now = at or time.time()
        stopped = self.stop(now)
        self.data["running"] = {"activity": name, "start": now}
        self.touch(name)
        return stopped

    def stop(self, at=None):
        """Stop the running timer; returns (activity, seconds) or None. Very short ones are dropped."""
        run = self.data["running"]
        if not run:
            return None
        now = at or time.time()
        self.data["running"] = None
        secs = now - run["start"]
        if secs >= MIN_SESSION:
            self.data["sessions"].append({"activity": run["activity"], "start": run["start"], "end": now})
        return run["activity"], secs

    def sessions(self, lo, hi, now=None):
        """Sessions overlapping [lo, hi), the running one included, as dicts with an index
        (-1 for the running one), clipped start/end and the real ones."""
        out = []
        for i, s in enumerate(self.data["sessions"]):
            if s["end"] > lo and s["start"] < hi:
                out.append({"i": i, "activity": s["activity"], "start": max(s["start"], lo),
                            "end": min(s["end"], hi), "real_start": s["start"], "real_end": s["end"]})
        run = self.data["running"]
        if run:
            now = now or time.time()
            if now > lo and run["start"] < hi:
                out.append({"i": -1, "activity": run["activity"], "start": max(run["start"], lo),
                            "end": min(now, hi), "real_start": run["start"], "real_end": None})
        return sorted(out, key=lambda s: s["start"])

    def totals(self, lo, hi):
        out = {}
        for s in self.sessions(lo, hi):
            out[s["activity"]] = out.get(s["activity"], 0) + s["end"] - s["start"]
        return out


def overlaps(blocks, start, end, skip=None):
    for b in blocks:
        if b is not skip and b["start"] < end and b["end"] > start:
            return b
    return None


# -- the app --

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


def bar(fraction, width):
    cells = max(0.0, min(1.0, fraction)) * width
    full = int(cells)
    part = BARS[int((cells - full) * 8)] if full < width else ""
    return ("█" * full + part.strip()).ljust(width)


VIEWS = ("plan", "track", "summary")
PERIODS = ("day", "week", "month")

HELP = [
    ("Everywhere", None),
    ("1 2 3  Tab", "plan, track, summary"),
    ("[ ]  T", "previous and next day, today"),
    ("s", "stop the running timer"),
    ("u", "undo"),
    ("q", "quit — a running timer keeps running"),
    ("Plan", None),
    ("↑↓ jk  PgUp PgDn", "move through the day"),
    ("⏎ / a", "add a block here, or edit the one under the cursor"),
    ("", "write what and when: Deep work 1h30 · Lunch 12-13 · Call 14:30 45m"),
    ("J K", "move the block down or up"),
    ("L H", "make it longer or shorter"),
    ("x", "tick the block off (or not)"),
    ("Space", "start a timer for the block (again: stop it)"),
    ("d", "delete the block"),
    ("y", "copy the blocks of the last planned day into this one"),
    ("z", "zoom: 15, 30 or 60 minutes a line"),
    ("n", "jump to now"),
    ("Track", None),
    ("↑↓  ← →", "move in a list, switch between activities and the log"),
    ("Space ⏎", "start the selected activity (again: stop it)"),
    ("n", "a new activity, started right away"),
    ("a", "add a session by hand: Reading 20:00-21:15"),
    ("e", "edit the selected session's times, or rename the activity"),
    ("d", "delete the selected session, or remove the activity from the list"),
    ("Summary", None),
    ("p", "day, week or month"),
]


class App:
    def __init__(self, stdscr, store):
        self.stdscr, self.store = stdscr, store
        self.view = "plan"
        self.day = dt.date.today()
        self.slot = DEFAULT_SLOT
        self.row = None                    # cursor in the plan, as a row of the timeline
        self.top = 0
        self.pane = 0                      # track view: 0 activities, 1 log
        self.act_sel = 0
        self.log_sel = 0
        self.period = 0
        self.help = False
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.go_now()

    # -- helpers --

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

    def act_attr(self, name, reverse=True):
        idx = self.store.color(name)
        return curses.color_pair(10 + idx) if reverse else curses.color_pair(20 + idx)

    def save(self):
        try:
            self.store.save()
        except OSError as e:
            self.say(f"couldn't save: {e.strerror}", curses.color_pair(4))

    def today(self):
        return self.day == dt.date.today()

    def go_now(self):
        if self.today():
            now = dt.datetime.now()
            self.row = (now.hour * 60 + now.minute) // self.slot
        else:
            blocks = self.store.blocks(self.day)
            self.row = (blocks[0]["start"] if blocks else 8 * 60) // self.slot
        self.top = max(0, self.row - 3)

    def set_day(self, d):
        self.day = d
        self.log_sel = 0
        self.go_now()

    # -- plan --

    def row_minutes(self, r):
        return r * self.slot, (r + 1) * self.slot

    def block_at_row(self, r=None):
        lo, hi = self.row_minutes(self.row if r is None else r)
        for b in self.store.blocks(self.day):
            if b["start"] < hi and b["end"] > lo:
                return b
        return None

    def plan_edit(self):
        blocks = self.store.blocks(self.day)
        b = self.block_at_row()
        lo, _ = self.row_minutes(self.row)
        if b:
            initial = f"{b['title']} {hhmm(b['start'])}-{hhmm(b['end'])}"
            text = self.prompt(" block: ", initial)
        else:
            nxt = [x["start"] for x in blocks if x["start"] >= lo]
            text = self.prompt(f" new block at {hhmm(lo)} (what, and 1h30 / 9-11 / 14:30): ")
        if not text or not text.strip():
            return
        try:
            if b:
                title, start, end = parse_spec(text, b["start"], b["end"] - b["start"])
            else:
                title, start, end = parse_spec(text, lo, None)
                if end is None:
                    later = [x["start"] for x in blocks if x["start"] > start]
                    end = min([start + DEFAULT_BLOCK, 1440] + later)
                    if end <= start:
                        raise SpecError("there's a block right there")
        except SpecError as e:
            self.say(str(e), curses.color_pair(4))
            return
        if not title:
            self.say("what is it? add a title", curses.color_pair(4))
            return
        clash = overlaps(blocks, start, end, skip=b)
        if clash:
            self.say(f"overlaps “{clash['title']}” {hhmm(clash['start'])}–{hhmm(clash['end'])}", curses.color_pair(4))
            return
        self.store.change()
        if b:
            b.update(title=title, start=start, end=end)
            self.store.set_blocks(self.day, blocks)
        else:
            blocks.append({"title": title, "start": start, "end": end, "done": False})
            self.store.set_blocks(self.day, blocks)
        self.store.color(title)
        self.save()
        self.row = start // self.slot
        self.say(f"{title}  {hhmm(start)}–{hhmm(end)} ({dur_words(end - start)})")

    def plan_move(self, delta_start, delta_end):
        blocks = self.store.blocks(self.day)
        b = self.block_at_row()
        if not b:
            return
        target = next(x for x in blocks if x == b)
        start, end = b["start"] + delta_start, b["end"] + delta_end
        if start < 0 or end > 1440 or end - start < 5:
            return
        clash = overlaps(blocks, start, end, skip=target)
        if clash:
            self.say(f"“{clash['title']}” is in the way", curses.color_pair(4))
            return
        self.store.change()
        target.update(start=start, end=end)
        self.store.set_blocks(self.day, blocks)
        self.save()
        if delta_start:
            self.row = max(0, min(1440 // self.slot - 1, self.row + delta_start // self.slot))

    def plan_toggle(self):
        blocks = self.store.blocks(self.day)
        b = self.block_at_row()
        if not b:
            return
        self.store.change()
        target = next(x for x in blocks if x == b)
        target["done"] = not target.get("done")
        self.store.set_blocks(self.day, blocks)
        self.save()

    def plan_delete(self):
        blocks = self.store.blocks(self.day)
        b = self.block_at_row()
        if not b:
            return
        self.store.change()
        self.store.set_blocks(self.day, [x for x in blocks if x != b])
        self.save()
        self.say(f"deleted “{b['title']}” — u brings it back")

    def plan_copy(self):
        """The blocks of the most recent earlier day with a plan, where they fit."""
        earlier = sorted(k for k in self.store.data["plans"] if k < day_key(self.day))
        if not earlier:
            self.say("no earlier plan to copy")
            return
        source = earlier[-1]
        blocks = self.store.blocks(self.day)
        added = 0
        self.store.change()
        for b in sorted(self.store.data["plans"][source], key=lambda b: b["start"]):
            if not overlaps(blocks, b["start"], b["end"]):
                blocks.append({"title": b["title"], "start": b["start"], "end": b["end"], "done": False})
                added += 1
        self.store.set_blocks(self.day, blocks)
        self.save()
        self.say(f"copied {added} block{'s' if added != 1 else ''} from {source}" if added else "nothing fitted")

    # -- timers --

    def toggle_timer(self, name):
        run = self.store.running()
        self.store.change()
        if run and run["activity"].lower() == name.lower():
            stopped = self.store.stop()
            self.say(f"stopped {stopped[0]} after {dur(stopped[1], True)}")
        else:
            stopped = self.store.start(name)
            self.say(f"started {name}" + (f" — stopped {stopped[0]} ({dur(stopped[1])})" if stopped else ""))
        self.save()

    def stop_timer(self):
        if not self.store.running():
            self.say("no timer running")
            return
        self.store.change()
        name, secs = self.store.stop()
        self.save()
        self.say(f"stopped {name} after {dur(secs, True)}" + ("" if secs >= MIN_SESSION else " — too short to keep"))

    def activities(self):
        """Activities for the track list: the stored order, plus any tracked on this day."""
        names = list(self.store.data["activities"])
        lo, hi = day_bounds(self.day)
        for s in self.store.sessions(lo, hi):
            if s["activity"].lower() not in (n.lower() for n in names):
                names.append(s["activity"])
        return names

    def day_log(self):
        lo, hi = day_bounds(self.day)
        return self.store.sessions(lo, hi)

    def track_new(self):
        text = self.prompt(" new activity: ")
        if text and text.strip():
            self.toggle_timer(text.strip())
            self.act_sel = 0

    def track_add(self):
        text = self.prompt(" add a session (what and when: Reading 20:00-21:15): ")
        if not text or not text.strip():
            return
        try:
            title, start, end = parse_spec(text)
            if end is None:
                raise SpecError("give the end too: 20:00-21:15, or 20:00 45m")
        except SpecError as e:
            self.say(str(e), curses.color_pair(4))
            return
        if not title:
            sel = self.activities()
            title = sel[self.act_sel] if sel else ""
        if not title:
            self.say("which activity?", curses.color_pair(4))
            return
        self.store.change()
        self.store.data["sessions"].append({"activity": title, "start": epoch_at(self.day, start),
                                            "end": epoch_at(self.day, end)})
        self.store.data["sessions"].sort(key=lambda s: s["start"])
        self.store.touch(title)
        self.save()
        self.say(f"added {title} {hhmm(start)}–{hhmm(end)}")

    def track_edit(self):
        if self.pane == 0:
            names = self.activities()
            if not names:
                return
            old = names[min(self.act_sel, len(names) - 1)]
            text = self.prompt(" rename to: ", old)
            if not text or not text.strip() or text.strip() == old:
                return
            new = text.strip()
            self.store.change()
            data = self.store.data
            for s in data["sessions"]:
                if s["activity"] == old:
                    s["activity"] = new
            if data["running"] and data["running"]["activity"] == old:
                data["running"]["activity"] = new
            data["activities"] = [new if a == old else a for a in data["activities"]]
            data["colors"].setdefault(new.lower(), data["colors"].get(old.lower(), 0))
            self.save()
            self.say(f"renamed {old} to {new}, everywhere")
            return
        log = self.day_log()
        if not log:
            return
        s = log[min(self.log_sel, len(log) - 1)]
        start_min = minute_of(s["real_start"], self.day)
        if s["i"] == -1:
            text = self.prompt(" started at: ", hhmm(start_min) if 0 <= start_min < 1440 else "")
            if not text or not text.strip():
                return
            m = AT.search(" " + text + " ") or re.search(r"(\d{1,2})()", text)
            try:
                minute = to_minute(m[1], m[2]) if m else None
            except SpecError as e:
                self.say(str(e), curses.color_pair(4))
                return
            if minute is None or epoch_at(self.day, minute) > time.time():
                self.say("a time before now, please", curses.color_pair(4))
                return
            self.store.change()
            self.store.data["running"]["start"] = epoch_at(self.day, minute)
            self.save()
            return
        end_min = minute_of(s["real_end"], self.day)
        initial = f"{s['activity']} {hhmm(start_min)}-{hhmm(end_min)}" if 0 <= start_min and end_min <= 1440 \
            else s["activity"]
        text = self.prompt(" session: ", initial)
        if not text or not text.strip():
            return
        try:
            title, start, end = parse_spec(text, max(0, start_min), end_min - start_min)
        except SpecError as e:
            self.say(str(e), curses.color_pair(4))
            return
        self.store.change()
        rec = self.store.data["sessions"][s["i"]]
        rec.update(activity=title or s["activity"], start=epoch_at(self.day, start), end=epoch_at(self.day, end))
        self.store.data["sessions"].sort(key=lambda x: x["start"])
        self.save()

    def track_delete(self):
        if self.pane == 0:
            names = self.activities()
            if not names:
                return
            name = names[min(self.act_sel, len(names) - 1)]
            self.store.change()
            self.store.data["activities"] = [a for a in self.store.data["activities"] if a != name]
            self.save()
            self.say(f"removed {name} from the list — its sessions stay")
            return
        log = self.day_log()
        if not log:
            return
        s = log[min(self.log_sel, len(log) - 1)]
        self.store.change()
        if s["i"] == -1:
            self.store.data["running"] = None
        else:
            del self.store.data["sessions"][s["i"]]
        self.save()
        self.say(f"deleted {s['activity']} {clock(s['real_start'])} — u brings it back")

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
        if self.help:
            self.draw_help(h, w)
        else:
            getattr(self, "draw_" + self.view)(h, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_title(self, w):
        self.put(0, 0, " Day ", curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE)
        label = self.day.strftime("%a %d %b %Y")
        delta = (self.day - dt.date.today()).days
        rel = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(delta, f"{delta:+d} days")
        self.put(0, 6, f"{label}  ({rel})", curses.A_BOLD)
        x = 6 + cwidth(label) + cwidth(rel) + 6
        for i, v in enumerate(VIEWS, 1):
            text = f" {i} {v.capitalize()} "
            on = v == self.view and not self.help
            self.put(0, x, text, (curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD) if on else curses.color_pair(5),
                     len(text))
            x += len(text) + 1
        run = self.store.running()
        if run:
            text = f"● {run['activity']}  {dur(time.time() - run['start'], True)} "
            tw = min(cwidth(text), max(0, w - x - 2))
            self.put(0, w - tw - 1, text, self.act_attr(run["activity"], False) | curses.A_BOLD, tw)

    def draw_plan(self, h, w):
        rows_total = 1440 // self.slot
        y0, height = 2, h - 4
        self.row = max(0, min(self.row, rows_total - 1))
        if self.row < self.top:
            self.top = self.row
        elif self.row >= self.top + height:
            self.top = self.row - height + 1
        self.top = max(0, min(self.top, rows_total - height))
        label_w = 7
        plan_w = (w - label_w - 2) * 11 // 20
        act_x = label_w + plan_w + 1
        act_w = w - act_x - 1
        blocks = self.store.blocks(self.day)
        lo, hi = day_bounds(self.day)
        sessions = self.store.sessions(lo, hi)
        planned = sum(b["end"] - b["start"] for b in blocks)
        done = sum(b["end"] - b["start"] for b in blocks if b.get("done"))
        tracked = sum(s["end"] - s["start"] for s in sessions) / 60
        self.put(1, label_w, f"Plan  {dur_words(planned)}" + (f" · {dur_words(done)} done" if done else ""),
                 curses.color_pair(1) | curses.A_BOLD, plan_w)
        self.put(1, act_x, f"Tracked  {dur_words(tracked)}", curses.color_pair(1) | curses.A_BOLD, act_w)
        now_min = None
        if self.today():
            now = dt.datetime.now()
            now_min = now.hour * 60 + now.minute + now.second / 60
        sel_block = self.block_at_row()
        shown_before = None                # the session labelled on the line above
        for k in range(height):
            r = self.top + k
            if r >= rows_total:
                break
            y = y0 + k
            t0, t1 = self.row_minutes(r)
            here = r == self.row
            # the time
            is_now = now_min is not None and t0 <= now_min < t1
            if is_now:
                self.put(y, 0, f"{hhmm(now_min)}▶", curses.color_pair(4) | curses.A_BOLD, label_w)
            elif t0 % 60 == 0:
                self.put(y, 0, hhmm(t0), curses.A_BOLD | (curses.A_REVERSE if here else 0), 5)
            else:
                self.put(y, 0, f"  :{t0 % 60:02d}", curses.color_pair(5) | (curses.A_REVERSE if here else 0), 5)
            if here and not is_now:
                self.put(y, 5, "›", curses.color_pair(1) | curses.A_BOLD, 1)
            # the plan
            b = next((b for b in blocks if b["start"] < t1 and b["end"] > t0), None)
            x = label_w
            if b:
                first = b["start"] >= t0 or r == self.top
                second = not first and (b["start"] >= t0 - self.slot or r == self.top + 1) and b["end"] - b["start"] > self.slot
                attr = self.act_attr(b["title"])
                if b.get("done"):
                    attr = curses.color_pair(5) | curses.A_REVERSE
                if b is sel_block:
                    attr |= curses.A_BOLD
                text = ""
                if first:
                    text = ("✓ " if b.get("done") else "") + b["title"]
                    if b["end"] - b["start"] <= self.slot:
                        text += f"   {hhmm(b['start'])}–{hhmm(b['end'])}"
                elif second:
                    text = f"{hhmm(b['start'])}–{hhmm(b['end'])}  {dur_words(b['end'] - b['start'])}"
                edge = "▌" if b is sel_block else " "
                self.put(y, x, edge + text, attr, plan_w)
            elif here:
                self.put(y, x, " ⏎ add a block here", curses.color_pair(5), plan_w)
            elif is_now:
                self.put(y, x, "┈" * plan_w, curses.color_pair(4), plan_w)
            # what was tracked
            best, best_overlap = None, 0
            for s in sessions:
                ov = min(s["end"], epoch_at(self.day, t1)) - max(s["start"], epoch_at(self.day, t0))
                if ov > best_overlap:
                    best, best_overlap = s, ov
            self.put(y, act_x - 1, "│", curses.color_pair(5), 1)
            if best:
                text = ""
                if best is not shown_before:       # the first line it fills on screen gets its name
                    text = ("● " if best["i"] == -1 else "") + best["activity"] + f"  {dur(best['end'] - best['start'])}"
                frac = best_overlap / (self.slot * 60)
                shown_before = best
                attr = self.act_attr(best["activity"]) if frac >= 0.5 else self.act_attr(best["activity"], False)
                fill = text if frac >= 0.5 else ("▄ " + text if text else "▄" * max(1, int(act_w * frac)))
                self.put(y, act_x, " " + fill if frac >= 0.5 else fill, attr, act_w)
            else:
                shown_before = None
                if is_now:
                    self.put(y, act_x, "┈" * act_w, curses.color_pair(4), act_w)

    def draw_track(self, h, w):
        run = self.store.running()
        if run:
            secs = time.time() - run["start"]
            self.put(2, 2, "●", self.act_attr(run["activity"], False) | curses.A_BOLD, 1)
            self.put(2, 4, run["activity"], curses.A_BOLD, w // 2)
            self.put(2, w // 2 + 2, dur(secs, True), curses.color_pair(2) | curses.A_BOLD, 12)
            self.put(3, 4, f"since {clock(run['start'])}" + ("" if time.localtime(run["start"]).tm_yday ==
                     time.localtime().tm_yday else time.strftime(" on %a %d %b", time.localtime(run["start"]))),
                     curses.color_pair(5))
        else:
            self.put(2, 2, "○ no timer running", curses.color_pair(5) | curses.A_BOLD)
            self.put(3, 4, "Space starts the selected activity, n a new one", curses.color_pair(5))
        y0 = 5
        height = h - 1 - y0 - 1
        left_w = max(28, (w - 3) // 2)
        right_x = left_w + 2
        lo, hi = day_bounds(self.day)
        totals = self.store.totals(lo, hi)
        names = self.activities()
        self.act_sel = max(0, min(self.act_sel, len(names) - 1))
        log = self.day_log()
        self.log_sel = max(0, min(self.log_sel, len(log) - 1))
        head = curses.color_pair(1) | curses.A_BOLD
        self.put(y0, 1, "Activities" + ("" if self.today() else f" — {self.day:%a %d %b}"),
                 head | (curses.A_UNDERLINE if self.pane == 0 else 0), left_w - 1)
        self.put(y0, right_x, f"Log  {dur(sum(totals.values()))} tracked",
                 head | (curses.A_UNDERLINE if self.pane == 1 else 0), w - right_x - 1)
        peak = max(totals.values(), default=0) or 1
        top = max(0, self.act_sel - height + 1)
        for k, name in enumerate(names[top:top + height]):
            i = top + k
            y = y0 + 1 + k
            here = self.pane == 0 and i == self.act_sel
            base = curses.A_REVERSE if here else 0
            running = run and run["activity"].lower() == name.lower()
            self.put(y, 1, " " * (left_w - 1), base)
            self.put(y, 1, "●" if running else "■", self.act_attr(name, False) | base, 1)
            total = totals.get(name, 0)
            name_w = left_w - 19
            self.put(y, 3, name, base | (curses.A_BOLD if running else 0), name_w)
            self.put(y, 3 + name_w, dur(total).rjust(6) if total else "", base | curses.color_pair(5), 6)
            if total:
                self.put(y, 10 + name_w, bar(total / peak, 8), self.act_attr(name, False) | base, 8)
        if not names:
            self.put(y0 + 2, 3, "none yet — n adds one", curses.color_pair(5))
        top = max(0, self.log_sel - height + 1)
        for k, s in enumerate(log[top:top + height]):
            i = top + k
            y = y0 + 1 + k
            here = self.pane == 1 and i == self.log_sel
            base = curses.A_REVERSE if here else 0
            end = "now  " if s["i"] == -1 else clock(s["real_end"])
            span = f"{clock(s['real_start'])}–{end}"
            self.put(y, right_x, " " * (w - right_x - 1), base)
            self.put(y, right_x, span, base | curses.color_pair(5), 12)
            self.put(y, right_x + 12, dur(s["end"] - s["start"]).rjust(5), base, 6)
            self.put(y, right_x + 19, "■", self.act_attr(s["activity"], False) | base, 1)
            self.put(y, right_x + 21, s["activity"], base | (curses.A_BOLD if s["i"] == -1 else 0), w - right_x - 22)
        if not log:
            self.put(y0 + 2, right_x + 2, "nothing tracked" + (" yet" if self.today() else ""), curses.color_pair(5))

    def period_range(self):
        d = self.day
        kind = PERIODS[self.period]
        if kind == "day":
            first, last = d, d
        elif kind == "week":
            first = d - dt.timedelta(days=d.weekday())
            last = first + dt.timedelta(days=6)
        else:
            first = d.replace(day=1)
            last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        return first, last

    def draw_summary(self, h, w):
        first, last = self.period_range()
        kind = PERIODS[self.period]
        title = {"day": f"{first:%a %d %b}", "week": f"week {first:%V}: {first:%d %b} – {last:%d %b}",
                 "month": f"{first:%B %Y}"}[kind]
        self.put(1, 1, title, curses.color_pair(1) | curses.A_BOLD)
        lo, hi = day_bounds(first)[0], day_bounds(last)[1]
        tracked = self.store.totals(lo, hi)
        planned = {}
        names = {}
        d = first
        days = []
        while d <= last:
            day_plan = 0
            for b in self.store.blocks(d):
                key = b["title"].lower()
                planned[key] = planned.get(key, 0) + (b["end"] - b["start"]) * 60
                names.setdefault(key, b["title"])
                day_plan += b["end"] - b["start"]
            dlo, dhi = day_bounds(d)
            days.append((d, sum(self.store.totals(dlo, dhi).values()), day_plan * 60))
            d += dt.timedelta(days=1)
        for name in tracked:
            names.setdefault(name.lower(), name)
        rows = sorted(names, key=lambda k: (-sum(v for n, v in tracked.items() if n.lower() == k),
                                            -planned.get(k, 0)))
        y = 3
        self.put(y, 3, "Activity", curses.color_pair(5), 22)
        self.put(y, 26, "Tracked".rjust(8), curses.color_pair(5), 8)
        self.put(y, 35, "Planned".rjust(8), curses.color_pair(5), 8)
        peak = max([sum(v for n, v in tracked.items() if n.lower() == k) for k in rows] + list(planned.values()) + [1])
        bar_w = max(0, w - 56)
        room = h - 1 - 5 - (min(len(days), 8) + 2 if kind != "day" else 0)
        for k in rows[:max(0, room)]:
            y += 1
            t = sum(v for n, v in tracked.items() if n.lower() == k)
            p = planned.get(k, 0)
            self.put(y, 1, "■", self.act_attr(names[k], False), 1)
            self.put(y, 3, names[k], curses.A_BOLD if t else 0, 22)
            self.put(y, 26, dur(t).rjust(8) if t else "—".rjust(8), 0, 8)
            self.put(y, 35, dur(p).rjust(8) if p else "".rjust(8), curses.color_pair(5), 8)
            if p and t:
                pct = f"{t / p:.0%}"
                self.put(y, 44, pct.rjust(5), curses.color_pair(2 if 0.8 <= t / p <= 1.2 else 3), 5)
            if bar_w > 4:
                self.put(y, 51, bar(t / peak, bar_w), self.act_attr(names[k], False), bar_w)
                if p:
                    mark = 51 + min(bar_w - 1, int(p / peak * bar_w))
                    self.put(y, mark, "┃", curses.color_pair(5) | curses.A_BOLD, 1)
        if not rows:
            self.put(y + 2, 3, "nothing planned or tracked in this " + kind, curses.color_pair(5))
        y += 1
        total_t = sum(tracked.values())
        total_p = sum(planned.values())
        self.put(y, 3, "Total", curses.A_BOLD | curses.color_pair(1), 22)
        self.put(y, 26, dur(total_t).rjust(8), curses.A_BOLD, 8)
        self.put(y, 35, dur(total_p).rjust(8) if total_p else "", curses.color_pair(5), 8)
        if bar_w > 4 and rows:
            self.put(y + 1, 51, "█ tracked  ┃ planned", curses.color_pair(5))
        if kind != "day":
            y += 3
            self.put(y, 1, "Per day", curses.color_pair(1) | curses.A_BOLD)
            peak_day = max([t for _, t, _ in days] + [p for _, _, p in days] + [1])
            space = h - 2 - y
            show = days if len(days) <= space else [x for x in days if x[1] or x[2]][-space:]
            for d, t, p in show[:space]:
                y += 1
                here = d == dt.date.today()
                self.put(y, 3, f"{d:%a %d}", curses.A_BOLD if here else curses.color_pair(5), 7)
                self.put(y, 26, dur(t).rjust(8) if t else "", 0, 8)
                self.put(y, 35, dur(p).rjust(8) if p else "", curses.color_pair(5), 8)
                if bar_w > 4:
                    self.put(y, 51, bar(t / peak_day, bar_w), curses.color_pair(2), bar_w)
                    if p:
                        self.put(y, 51 + min(bar_w - 1, int(p / peak_day * bar_w)), "┃",
                                 curses.color_pair(5) | curses.A_BOLD, 1)

    def draw_help(self, h, w):
        for k, (keys, what) in enumerate(HELP[:h - 2]):
            y = 1 + k
            if what is None:
                self.put(y, 1, keys, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.put(y, 3, keys, curses.A_BOLD, 18)
                self.put(y, 22, what, curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        if self.help:
            keys = ["? back"]
        elif self.view == "plan":
            b = self.block_at_row()
            keys = (["⏎ edit", "Space timer", "x done", "J K move", "L H length", "d delete"] if b else
                    ["⏎ add block", "y copy last plan"]) + ["z zoom", "n now", "[ ] day", "u undo", "? help", "q quit"]
        elif self.view == "track":
            keys = ["Space start/stop", "n new", "a add", "e edit", "d delete", "← → panes", "s stop", "[ ] day",
                    "? help", "q quit"]
        else:
            keys = [f"p {PERIODS[self.period]}", "[ ] day", "? help", "q quit"]
        while len(keys) > 2 and cwidth("  ".join(keys)) > w - 2:
            keys.pop(-3)
        self.put(y, 0, " " + "  ".join(keys), curses.color_pair(5))

    # -- input --

    def prompt(self, label, initial=""):
        curses.curs_set(1)
        buf = list(initial)
        self.stdscr.timeout(-1)
        try:
            while True:
                h, w = self.stdscr.getmaxyx()
                room = w - cwidth(label) - 2
                text = label + "".join(buf)[-room:]
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
                    elif key == "\x15":
                        buf.clear()
                    elif key == "\x17":
                        while buf and buf[-1] == " ":
                            buf.pop()
                        while buf and buf[-1] != " ":
                            buf.pop()
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf)
        finally:
            curses.curs_set(0)
            self.stdscr.timeout(TICK_MS)

    def handle(self, ch):
        if self.help:
            if ch in (ord("?"), ord("q"), 27, 10, 13):
                self.help = False
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("?"):
            self.help = True
        elif ch in (ord("1"), ord("2"), ord("3")):
            self.view = VIEWS[ch - ord("1")]
        elif ch == ord("\t"):
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        elif ch == curses.KEY_BTAB:
            self.view = VIEWS[(VIEWS.index(self.view) - 1) % len(VIEWS)]
        elif ch == ord("["):
            self.set_day(self.day - dt.timedelta(days=1))
        elif ch == ord("]"):
            self.set_day(self.day + dt.timedelta(days=1))
        elif ch == ord("T"):
            self.set_day(dt.date.today())
        elif ch == ord("s"):
            self.stop_timer()
        elif ch == ord("u"):
            self.say("undone" if self.store.undo_last() else "nothing to undo")
        elif self.view == "plan":
            self.handle_plan(ch)
        elif self.view == "track":
            self.handle_track(ch)
        elif ch == ord("p"):
            self.period = (self.period + 1) % len(PERIODS)
        return True

    def handle_plan(self, ch):
        h = self.stdscr.getmaxyx()[0]
        rows_total = 1440 // self.slot
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -(h - 5), curses.KEY_NPAGE: h - 5, ord("g"): -10**6, ord("G"): 10**6,
                 curses.KEY_HOME: -10**6, curses.KEY_END: 10**6}
        if ch in steps:
            self.row = max(0, min(rows_total - 1, self.row + steps[ch]))
        elif ch in (10, 13, curses.KEY_ENTER, ord("a"), ord("e")):
            self.plan_edit()
        elif ch == ord("J"):
            self.plan_move(self.slot, self.slot)
        elif ch == ord("K"):
            self.plan_move(-self.slot, -self.slot)
        elif ch == ord("L"):
            self.plan_move(0, self.slot)
        elif ch == ord("H"):
            self.plan_move(0, -self.slot)
        elif ch == ord("x"):
            self.plan_toggle()
        elif ch == ord("d"):
            self.plan_delete()
        elif ch == ord(" "):
            b = self.block_at_row()
            if b:
                self.toggle_timer(b["title"])
        elif ch == ord("y"):
            self.plan_copy()
        elif ch == ord("z"):
            minute = self.row * self.slot
            self.slot = SLOTS[(SLOTS.index(self.slot) + 1) % len(SLOTS)]
            self.row = minute // self.slot
            self.top = max(0, self.row - 3)
            self.say(f"{self.slot} minutes a line")
        elif ch == ord("n"):
            self.go_now()

    def handle_track(self, ch):
        names = self.activities()
        log = self.day_log()
        if ch in (curses.KEY_UP, ord("k")):
            if self.pane == 0:
                self.act_sel = max(0, self.act_sel - 1)
            else:
                self.log_sel = max(0, self.log_sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if self.pane == 0:
                self.act_sel = min(len(names) - 1, self.act_sel + 1)
            else:
                self.log_sel = min(len(log) - 1, self.log_sel + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.pane = 0
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.pane = 1
            self.log_sel = len(log) - 1 if self.log_sel >= len(log) else self.log_sel
        elif ch in (ord(" "), 10, 13, curses.KEY_ENTER):
            if self.pane == 0 and names:
                self.toggle_timer(names[min(self.act_sel, len(names) - 1)])
                self.act_sel = 0
            elif self.pane == 1 and log:
                self.toggle_timer(log[min(self.log_sel, len(log) - 1)]["activity"])
        elif ch == ord("n"):
            self.track_new()
        elif ch == ord("a"):
            self.track_add()
        elif ch == ord("e"):
            self.track_edit()
        elif ch in (ord("d"), curses.KEY_DC):
            self.track_delete()


def run(stdscr, store):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, headings
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # the running clock
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors, the now line
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    for i, name in enumerate(PALETTE):              # activity colours: filled (10+) and as text (20+)
        color = getattr(curses, "COLOR_" + name)
        curses.init_pair(10 + i, curses.COLOR_BLACK, color)
        curses.init_pair(20 + i, color, -1)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, store)
    while True:
        if store.changed_on_disk():                 # another window or --start/--stop changed it
            store.load()
        app.draw()
        ch = stdscr.getch()
        if ch in (-1, curses.KEY_RESIZE):
            continue
        if not app.handle(ch):
            return


def print_today(store):
    d = dt.date.today()
    tty = sys.stdout.isatty()
    bold, dim, reset = ("\033[1m", "\033[2m", "\033[0m") if tty else ("", "", "")
    print(f"{bold}{d:%A %d %B %Y}{reset}")
    blocks = store.blocks(d)
    print(f"\n{bold}Plan{reset}")
    for b in blocks:
        print(f"  {hhmm(b['start'])}–{hhmm(b['end'])}  {'✓' if b.get('done') else ' '} {b['title']}"
              f"  {dim}{dur_words(b['end'] - b['start'])}{reset}")
    if not blocks:
        print(f"  {dim}nothing planned{reset}")
    lo, hi = day_bounds(d)
    log = store.sessions(lo, hi)
    print(f"\n{bold}Tracked{reset}")
    for s in log:
        end = "now" if s["i"] == -1 else clock(s["real_end"])
        print(f"  {clock(s['real_start'])}–{end:<5}  {dur(s['end'] - s['start']):>5}  {s['activity']}")
    if not log:
        print(f"  {dim}nothing tracked{reset}")
    totals = store.totals(lo, hi)
    if totals:
        print(f"\n{bold}Totals{reset}")
        for name, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"  {dur(secs):>5}  {name}")
        print(f"  {dur(sum(totals.values())):>5}  {dim}in all{reset}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Plan the day in time blocks and track time with timers.")
    parser.add_argument("--file", default=os.environ.get("DAY_TUI_FILE", DATA_PATH),
                        help=f"the data file (default: {DATA_PATH.replace(os.path.expanduser('~'), '~')})")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", metavar="ACTIVITY", help="start a timer (stops the running one) and exit")
    group.add_argument("--stop", action="store_true", help="stop the running timer and exit")
    group.add_argument("--status", action="store_true",
                       help="print the running timer, e.g. for a status line; exit 1 when none runs")
    group.add_argument("--once", action="store_true", help="print today's plan, sessions and totals")
    args = parser.parse_args()
    store = Store(os.path.expanduser(args.file))
    if args.start:
        stopped = store.start(args.start.strip())
        store.save()
        print(f"started {args.start.strip()}" + (f" (stopped {stopped[0]} after {dur(stopped[1])})" if stopped else ""))
        return
    if args.stop:
        stopped = store.stop()
        store.save()
        print(f"stopped {stopped[0]} after {dur(stopped[1], True)}" if stopped else "no timer running")
        return
    if args.status:
        current = store.running()
        if not current:
            raise SystemExit(1)
        print(f"{current['activity']} {dur(time.time() - current['start'], True)}")
        return
    if args.once:
        raise SystemExit(print_today(store))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, store)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
