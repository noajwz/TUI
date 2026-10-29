#!/usr/bin/env python3
"""Terminal UI for Apple Reminders.

Reads and writes the same store the Reminders app uses, so whatever is done
here shows up in the app and on the phone, and the other way round.

Reminders' AppleScript dictionary is far too slow to drive a UI: reading 236
reminders took 30 s, and a `whose completed is false` filter over 70 s.  So
the talking is done by a small Swift program against EventKit, which reads the
same in 0.3 s.  Its source is embedded below and compiled once with `swiftc`
into ~/Library/Caches/reminders_tui/ (about 30 s, first run only).  The file
name carries a hash of the source, so editing it triggers a rebuild.
"""

import argparse
import colorsys
import curses
import glob
import hashlib
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

REFRESH_SECONDS = 60
HELPER_TIMEOUT = 120        # generous: the very first call waits on the permission dialog
MESSAGE_SECONDS = 5
CACHE_DIR = os.path.expanduser("~/Library/Caches/reminders_tui")

LIST_PANE_WIDTH = 24
NARROW = 64                 # below this the lists pane is hidden; use [ and ]
DETAIL_MIN_HEIGHT = 16

# EventKit priorities: 0 none, 1-4 high, 5 medium, 6-9 low.  `p` cycles these.
PRIORITY_CYCLE = (0, 9, 5, 1)
SMART_VIEWS = (("today", "Today"), ("scheduled", "Scheduled"), ("all", "All"))

RELATIVE_DAYS = {
    "today": 0, "tod": 0, "vandaag": 0,
    "tomorrow": 1, "tom": 1, "tmrw": 1, "morgen": 1,
    "overmorgen": 2,
}
WEEKDAYS = {
    "mon": 0, "monday": 0, "ma": 0, "maandag": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "di": 1, "dinsdag": 1,
    "wed": 2, "wednesday": 2, "wo": 2, "woensdag": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "do": 3, "donderdag": 3,
    "fri": 4, "friday": 4, "vr": 4, "vrijdag": 4,
    "sat": 5, "saturday": 5, "za": 5, "zaterdag": 5,
    "sun": 6, "sunday": 6, "zo": 6, "zondag": 6,
}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?(am|pm)?$")
OFFSET_RE = re.compile(r"^\+(\d+)([dw]?)$")
ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
DMY_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2}|\d{4}))?$")

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

HELP = """\
Moving             Changing the selected reminder
  j k  ↑ ↓   move    space x   done / not done
  g G        ends    e  ⏎      rename
  h l  Tab   panes   d         due date
  [ ]        lists   p         priority (none, !, !!, !!!)
  / Esc      filter  n         notes, in $EDITOR
                     m         move to another list
Other                D  Del    delete
  a o   add (repeats until an empty line)
  c     show completed       r   reload
  ?     this help            q   quit

Due dates: today  tom  fri  +3  +2w  5-11  5/11/27  2026-11-05
           with an optional time: tom 9:00, fri 14.30, 9am
           Dutch works too: morgen, vr, overmorgen.  Blank clears."""

SWIFT_SOURCE = r"""
import CoreGraphics
import EventKit
import Foundation

let store = EKEventStore()
let cal = Calendar.current
let sem = DispatchSemaphore(value: 0)

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

func emit(_ obj: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj) else { die("cannot encode output") }
    FileHandle.standardOutput.write(data)
}

let stamp: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return f
}()

func hex(_ color: CGColor?) -> Any {
    guard let srgb = CGColorSpace(name: CGColorSpace.sRGB),
          let c = color?.converted(to: srgb, intent: .defaultIntent, options: nil),
          let p = c.components, p.count >= 3 else { return NSNull() }
    let b = p.prefix(3).map { Int((min(max($0, 0), 1) * 255).rounded()) }
    return String(format: "#%02x%02x%02x", b[0], b[1], b[2])
}

// Due dates go over the wire as [y, m, d] or [y, m, d, H, M] in local time.
func due(_ r: EKReminder) -> Any {
    guard let c = r.dueDateComponents, let y = c.year, let m = c.month, let d = c.day else { return NSNull() }
    if c.hour != nil, let when = (c.calendar ?? cal).date(from: c) {
        let l = cal.dateComponents([.year, .month, .day, .hour, .minute], from: when)
        return [l.year!, l.month!, l.day!, l.hour!, l.minute!]
    }
    return [y, m, d]
}

// The Reminders app ties the alert to the due time, so this does too: a timed
// due date gets one alarm at that moment, a date-only one gets none.
func setDue(_ r: EKReminder, _ value: Any?) {
    for alarm in r.alarms ?? [] { r.removeAlarm(alarm) }
    guard let a = value as? [Int], a.count >= 3 else { r.dueDateComponents = nil; return }
    var c = DateComponents(year: a[0], month: a[1], day: a[2])
    if a.count >= 5 {
        c.hour = a[3]
        c.minute = a[4]
        c.timeZone = TimeZone.current
        if let when = cal.date(from: c) { r.addAlarm(EKAlarm(absoluteDate: when)) }
    }
    r.dueDateComponents = c
}

func encode(_ r: EKReminder) -> [String: Any] {
    return [
        "id": r.calendarItemIdentifier,
        "list": r.calendar?.calendarIdentifier ?? "",
        "title": r.title ?? "",
        "notes": r.notes ?? "",
        "done": r.isCompleted,
        "priority": r.priority,
        "due": due(r),
        "created": r.creationDate.map { stamp.string(from: $0) } ?? "",
        "completed_at": r.completionDate.map { stamp.string(from: $0) } ?? "",
    ]
}

func calendar(_ id: Any?) -> EKCalendar {
    if let id = id as? String, let c = store.calendar(withIdentifier: id) { return c }
    if let c = store.defaultCalendarForNewReminders() { return c }
    die("there is no list to put the reminder in")
}

func reminder(_ req: [String: Any]) -> EKReminder {
    guard let id = req["id"] as? String,
          let r = store.calendarItem(withIdentifier: id) as? EKReminder
    else { die("that reminder is gone; it was probably deleted elsewhere") }
    return r
}

func apply(_ r: EKReminder, _ req: [String: Any]) {
    if let t = req["title"] as? String { r.title = t }
    if let n = req["notes"] as? String { r.notes = n.isEmpty ? nil : n }
    if let p = req["priority"] as? Int { r.priority = p }
    if let d = req["done"] as? Bool { r.isCompleted = d }
    if req.keys.contains("due") { setDue(r, req["due"]) }
    if req.keys.contains("list") { r.calendar = calendar(req["list"]) }
}

func save(_ r: EKReminder) {
    do { try store.save(r, commit: true) } catch { die("could not save: \(error.localizedDescription)") }
}

var granted = false
store.requestFullAccessToReminders { ok, _ in granted = ok; sem.signal() }
sem.wait()
if !granted {
    die("no access to Reminders. Allow your terminal app under System Settings > Privacy & Security > Reminders.")
}

let input = FileHandle.standardInput.readDataToEndOfFile()
let req = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any] ?? [:]

switch CommandLine.arguments.dropFirst().first ?? "dump" {
case "dump":
    let lists = store.calendars(for: .reminder).map { c -> [String: Any] in
        ["id": c.calendarIdentifier, "title": c.title, "color": hex(c.cgColor),
         "writable": c.allowsContentModifications]
    }
    var items: [EKReminder] = []
    store.fetchReminders(matching: store.predicateForReminders(in: nil)) { rs in
        items = rs ?? []
        sem.signal()
    }
    sem.wait()
    emit(["lists": lists, "reminders": items.map(encode),
          "default": store.defaultCalendarForNewReminders()?.calendarIdentifier ?? ""])
case "add":
    let r = EKReminder(eventStore: store)
    r.calendar = calendar(req["list"])
    apply(r, req)
    save(r)
    emit(encode(r))
case "update":
    let r = reminder(req)
    apply(r, req)
    save(r)
    emit(encode(r))
case "delete":
    let r = reminder(req)
    do { try store.remove(r, commit: true) } catch { die("could not delete: \(error.localizedDescription)") }
    emit(["ok": true])
case let op:
    die("unknown operation \(op)")
}
"""


# ---------------------------------------------------------------- helper


def helper_path():
    digest = hashlib.sha256(SWIFT_SOURCE.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, f"ekhelper-{digest}")


def build_helper(force=False):
    """Return (path, error), compiling the EventKit helper if it is missing."""
    path = helper_path()
    if os.access(path, os.X_OK) and not force:
        return path, None
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None, "swiftc not found. Install the Command Line Tools: xcode-select --install"
    os.makedirs(CACHE_DIR, exist_ok=True)
    print("Building the EventKit helper (first run only, about 30 s)…", file=sys.stderr, flush=True)
    src = f"{path}.{os.getpid()}.swift"
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(SWIFT_SOURCE)
        proc = subprocess.run([swiftc, src, "-o", tmp], capture_output=True, text=True)
        if proc.returncode:
            return None, "swiftc failed:\n" + proc.stderr.strip()
        os.replace(tmp, path)
    finally:
        for leftover in (src, tmp):
            if os.path.exists(leftover):
                os.unlink(leftover)
    for old in glob.glob(os.path.join(CACHE_DIR, "ekhelper-*")):
        if old != path:
            os.unlink(old)
    return path, None


def call_helper(path, op, payload=None):
    """Run one helper operation. Returns (data, error)."""
    try:
        proc = subprocess.run([path, op], input=json.dumps(payload or {}),
                              capture_output=True, text=True, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "Reminders did not answer in time"
    except OSError as exc:
        return None, f"could not run the helper: {exc}"
    if proc.returncode:
        lines = proc.stderr.strip().splitlines()
        return None, lines[-1] if lines else f"helper exited with {proc.returncode}"
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return None, "the helper's answer was not JSON"


# ---------------------------------------------------------------- model


@dataclass
class Reminder:
    id: str
    list: str
    title: str
    notes: str
    done: bool
    priority: int
    due: date | None
    due_time: tuple | None      # (hour, minute), or None for a date-only due
    created: str
    completed_at: str


def to_reminder(raw):
    due = raw.get("due") or None
    notes = (raw.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n")
    return Reminder(
        id=raw["id"],
        list=raw.get("list", ""),
        title=raw.get("title") or "",
        notes=notes.strip("\n"),
        done=bool(raw.get("done")),
        priority=int(raw.get("priority") or 0),
        due=date(*due[:3]) if due else None,
        due_time=tuple(due[3:5]) if due and len(due) >= 5 else None,
        created=raw.get("created") or "",
        completed_at=raw.get("completed_at") or "",
    )


def due_wire(r):
    """A reminder's due date in the helper's [y, m, d(, H, M)] shape, or None."""
    if r.due is None:
        return None
    parts = [r.due.year, r.due.month, r.due.day]
    return parts + list(r.due_time) if r.due_time else parts


def parse_due(text, now):
    """Parse what was typed at the due-date prompt.

    Returns (value, error).  `value` is None to clear the due date, otherwise
    the helper's [y, m, d] or [y, m, d, H, M].
    """
    words = [w for w in text.lower().replace(",", " ").split() if w not in ("at", "om")]
    if not words or words in (["-"], ["none"], ["geen"]):
        return None, None
    today = now.date()

    clock = None
    m = TIME_RE.match(words[-1])
    if m and (len(words) > 1 or m.group(2) or m.group(3)):
        hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if half:
            if not 1 <= hour <= 12:
                return None, f"not a time: {words[-1]}"
            hour = hour % 12 + (12 if half == "pm" else 0)
        if hour > 23 or minute > 59:
            return None, f"not a time: {words[-1]}"
        clock = (hour, minute)
        words = words[:-1]

    if not words:
        # A bare time means the next time the clock shows it.
        day = today if (now.hour, now.minute) < clock else today + timedelta(days=1)
    elif len(words) > 1:
        return None, f"did not understand: {text.strip()}"
    else:
        day, error = parse_day(words[0], today)
        if error:
            return None, error
    parts = [day.year, day.month, day.day]
    return (parts + list(clock) if clock else parts), None


def parse_day(word, today):
    """Return (date, error) for a single date word."""
    if word in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[word]), None
    if word in WEEKDAYS:
        ahead = (WEEKDAYS[word] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), None
    m = OFFSET_RE.match(word)
    if m:
        n = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
        return today + timedelta(days=n), None
    try:
        m = ISO_RE.match(word)
        if m:
            return date(*map(int, m.groups())), None
        m = DMY_RE.match(word)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if year:
                return date(int(year) + (2000 if len(year) == 2 else 0), month, day), None
            found = date(today.year, month, day)
            # Day-month without a year is the next one to come round.
            return (found if found >= today else date(today.year + 1, month, day)), None
    except ValueError:
        return None, f"no such date: {word}"
    return None, f"did not understand: {word}"


def format_due(r, now):
    """Return (text, state) where state is 'overdue', 'today' or ''."""
    if r.due is None:
        return "", ""
    today = now.date()
    delta = (r.due - today).days
    if delta == 0:
        text = "Today"
    elif delta == 1:
        text = "Tomorrow"
    elif delta == -1:
        text = "Yesterday"
    elif 1 < delta < 7:
        text = DAY_NAMES[r.due.weekday()]
    elif r.due.year == today.year:
        text = f"{DAY_NAMES[r.due.weekday()]} {r.due.day} {MONTH_NAMES[r.due.month - 1]}"
    else:
        text = f"{r.due.day} {MONTH_NAMES[r.due.month - 1]} {r.due.year}"
    if r.due_time:
        text += " %02d:%02d" % r.due_time
    if r.done:
        return text, ""
    if delta < 0 or (delta == 0 and r.due_time and r.due_time <= (now.hour, now.minute)):
        return text, "overdue"
    return text, "today" if delta == 0 else ""


def priority_mark(p):
    if 1 <= p <= 4:
        return "!!!"
    if p == 5:
        return "!!"
    if 6 <= p <= 9:
        return "!"
    return ""


def priority_name(p):
    return {"!!!": "high", "!!": "medium", "!": "low"}.get(priority_mark(p), "none")


def next_priority(p):
    mark = priority_mark(p)
    current = next(q for q in PRIORITY_CYCLE if priority_mark(q) == mark)
    return PRIORITY_CYCLE[(PRIORITY_CYCLE.index(current) + 1) % len(PRIORITY_CYCLE)]


def in_view(r, view, today):
    if view == "today":
        return r.due is not None and r.due <= today
    if view == "scheduled":
        return r.due is not None
    if view == "all":
        return True
    return r.list == view


def visible(reminders, view, today, show_done=False, keep=(), query=""):
    """The reminders a view shows, in display order.

    `keep` holds ids ticked off this session: they stay put, struck through,
    until the view changes, so a mis-tick can be undone where it happened.
    """
    query = query.casefold()
    shown = [
        r for r in reminders
        if in_view(r, view, today)
        and (show_done or not r.done or r.id in keep)
        and (not query or query in r.title.casefold() or query in r.notes.casefold())
    ]
    pending = [r for r in shown if not r.done or r.id in keep]
    finished = [r for r in shown if r.done and r.id not in keep]
    pending.sort(key=lambda r: (
        r.due is None,
        r.due or date.max,
        r.due_time or (-1, -1),
        r.priority or 10,
        r.created,
    ))
    finished.sort(key=lambda r: r.completed_at, reverse=True)
    return pending + finished


# ---------------------------------------------------------------- drawing


def char_width(c):
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def clip(s, width):
    """Cut `s` to `width` terminal cells, ending in … when something was cut."""
    if width <= 0:
        return ""
    if text_width(s) <= width:
        return s
    out, used = [], 0
    for c in s:
        w = char_width(c)
        if used + w > width - 1:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"


def one_line(s):
    return " ".join(s.split())


def put(win, y, x, text, attr=0, width=None):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return 0
    room = w - x if width is None else min(width, w - x)
    text = clip(text, room)
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass  # writing the bottom-right cell raises even though it succeeds
    return text_width(text)


def curses_color(hex_color):
    """Nearest terminal colour to a list's #rrggbb, or -1 for the default."""
    if not hex_color or not curses.has_colors():
        return -1
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if curses.COLORS >= 256:
        levels = (0, 95, 135, 175, 215, 255)
        idx = [min(range(6), key=lambda i: abs(levels[i] - v)) for v in (r, g, b)]
        return 16 + 36 * idx[0] + 6 * idx[1] + idx[2]
    hue, sat, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if sat < 0.25:
        return -1
    hue *= 360
    for limit, color in ((20, curses.COLOR_RED), (70, curses.COLOR_YELLOW),
                         (170, curses.COLOR_GREEN), (200, curses.COLOR_CYAN),
                         (260, curses.COLOR_BLUE), (330, curses.COLOR_MAGENTA)):
        if hue < limit:
            return color
    return curses.COLOR_RED


class App:
    def __init__(self, stdscr, helper, data):
        self.stdscr = stdscr
        self.helper = helper
        self.lists = []
        self.reminders = {}
        self.default_list = ""
        self.view = 0               # index into self.views()
        self.cursor = 0
        self.top = 0
        self.focus = "items"
        self.show_done = False
        self.keep = set()
        self.query = ""
        self.message = ("", False, 0.0)
        self.loaded_at = 0.0
        self.pairs = {}
        self.load(data)
        if not self.open_count("today"):
            keys = [key for key, _, _ in self.views()]
            self.view = keys.index(self.default_list) if self.default_list in keys else 0

    # ---------------------------------------------------------- data

    def load(self, data):
        self.lists = sorted(data["lists"], key=lambda li: li["title"].casefold())
        self.reminders = {r.id: r for r in map(to_reminder, data["reminders"])}
        self.default_list = data.get("default") or (self.lists[0]["id"] if self.lists else "")
        self.keep &= self.reminders.keys()
        self.loaded_at = time.monotonic()
        if self.view >= len(self.views()):
            self.view = 0

    def reload(self, quiet=False):
        selected = self.selected()
        if not quiet:
            self.status("Loading…")
        data, error = call_helper(self.helper, "dump")
        if error:
            self.loaded_at = time.monotonic()   # don't retry every second
            self.say(error, error=True)
            return
        self.load(data)
        self.follow(selected.id if selected else None)
        if not quiet:
            self.say("Reloaded")

    def mutate(self, op, payload, follow=True):
        """Send one change to the helper and apply its answer locally."""
        self.status("Saving…")
        data, error = call_helper(self.helper, op, payload)
        if error:
            self.say(error, error=True)
            return None
        if op == "delete":
            self.reminders.pop(payload["id"], None)
            return True
        r = to_reminder(data)
        self.reminders[r.id] = r
        if follow:
            self.follow(r.id)
        return r

    # ---------------------------------------------------------- views

    def views(self):
        """[(key, label, hex colour)] — the smart views, then every list."""
        return ([(key, label, None) for key, label in SMART_VIEWS]
                + [(li["id"], li["title"], li["color"]) for li in self.lists])

    def view_key(self):
        return self.views()[self.view][0]

    def list_title(self, list_id):
        return next((li["title"] for li in self.lists if li["id"] == list_id), "?")

    def list_color(self, list_id):
        return next((li["color"] for li in self.lists if li["id"] == list_id), None)

    def rows(self, view=None):
        return visible(self.reminders.values(), view or self.view_key(), date.today(),
                       self.show_done, self.keep, self.query)

    def open_count(self, view):
        today = date.today()
        return sum(1 for r in self.reminders.values() if not r.done and in_view(r, view, today))

    def selected(self):
        rows = self.rows()
        return rows[self.cursor] if 0 <= self.cursor < len(rows) else None

    def follow(self, reminder_id):
        rows = self.rows()
        ids = [r.id for r in rows]
        if reminder_id in ids:
            self.cursor = ids.index(reminder_id)
        self.cursor = max(0, min(self.cursor, len(rows) - 1))

    def switch_view(self, index):
        index %= len(self.views())
        if index != self.view:
            self.view, self.cursor, self.top = index, 0, 0
            self.keep.clear()

    # ---------------------------------------------------------- feedback

    def say(self, text, error=False):
        self.message = (text, error, time.monotonic())

    def status(self, text):
        """Paint `text` in the footer right now, for work that blocks."""
        h, w = self.stdscr.getmaxyx()
        self.stdscr.move(h - 1, 0)
        self.stdscr.clrtoeol()
        put(self.stdscr, h - 1, 1, text, curses.A_DIM)
        self.stdscr.refresh()

    def pair(self, fg):
        if not curses.has_colors():
            return 0
        if fg not in self.pairs:
            n = len(self.pairs) + 1
            if n >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(n, fg, -1)
            self.pairs[fg] = n
        return curses.color_pair(self.pairs[fg])

    # ---------------------------------------------------------- draw

    def list_pane_width(self, w):
        return min(LIST_PANE_WIDTH, w // 3) if w >= NARROW else 0

    def body(self):
        """(first row, row after the last) of the scrolling area."""
        h, _ = self.stdscr.getmaxyx()
        detail = 4 if h >= DETAIL_MIN_HEIGHT else 0
        return 2, h - 1 - detail

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if h < 8 or w < 32:
            put(s, 0, 0, "Window too small")
            s.refresh()
            return
        rows = self.rows()
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        top, bottom = self.body()
        left = self.list_pane_width(w)

        self.draw_header(w)
        if left:
            self.draw_lists(top, bottom, left)
            for y in range(1, bottom):
                put(s, y, left, "│", curses.A_DIM)
        self.draw_items(rows, top, bottom, left + 2 if left else 1, w)
        if bottom < h - 1:
            self.draw_detail(bottom, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        s = self.stdscr
        s.attron(curses.A_REVERSE)
        put(s, 0, 0, " " * w)
        key, label, color = self.views()[self.view]
        title = f" ✓ Reminders · {label} ({self.open_count(key)})"
        if self.query:
            title += f"  /{self.query}"
        if self.show_done:
            title += "  +completed"
        clock = time.strftime("%H:%M ")
        put(s, 0, 0, title, curses.A_BOLD | curses.A_REVERSE, w - len(clock) - 1)
        put(s, 0, w - len(clock), clock, curses.A_REVERSE)
        s.attroff(curses.A_REVERSE)

    def draw_lists(self, top, bottom, width):
        s = self.stdscr
        views = self.views()
        lines = []                  # (view index or None for the gap)
        for i in range(len(views)):
            if i == len(SMART_VIEWS):
                lines.append(None)
            lines.append(i)
        pos = lines.index(self.view)
        room = bottom - top
        first = max(0, min(pos - room // 2, len(lines) - room))
        for y, i in enumerate(lines[first:first + room], start=top):
            if i is None:
                put(s, y, 1, "─" * (width - 2), curses.A_DIM)
                continue
            key, label, color = views[i]
            count = str(self.open_count(key) or "")
            chosen = i == self.view
            attr = curses.A_REVERSE if chosen and self.focus == "lists" else (curses.A_BOLD if chosen else 0)
            if attr & curses.A_REVERSE:
                put(s, y, 0, " " * width, attr)
            put(s, y, 1, "●" if color else "◆", attr | self.pair(curses_color(color)) if color else attr | curses.A_DIM)
            put(s, y, 3, label, attr, width - 5 - len(count))
            put(s, y, width - 1 - len(count), count, attr | (0 if chosen else curses.A_DIM))

    def draw_items(self, rows, top, bottom, x, w):
        s = self.stdscr
        room = bottom - top
        if not rows:
            text = "No matches." if self.query else "Nothing to do here."
            put(s, top, x + 1, text, curses.A_DIM)
            put(s, top + 1, x + 1, "a adds a reminder.", curses.A_DIM)
            return
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + room:
            self.top = self.cursor - room + 1
        self.top = max(0, min(self.top, len(rows) - room))
        now = datetime.now()
        shown = rows[self.top:self.top + room]
        width = w - x

        # Right-hand columns, sized to what is on screen: list, priority, due.
        dues = [format_due(r, now) for r in shown]
        columns = []
        if self.view < len(SMART_VIEWS):
            columns.append(("list", max(text_width(clip(self.list_title(r.list), 14)) for r in shown)))
        if any(priority_mark(r.priority) for r in shown):
            columns.append(("priority", 3))
        if any(due for due, _ in dues):
            columns.append(("due", max(text_width(due) for due, _ in dues)))
        while columns and width - sum(cw + 2 for _, cw in columns) < max(16, width // 2):
            columns.pop(0)                               # a narrow window keeps the due date longest
        right = sum(cw + 2 for _, cw in columns)

        for y, (r, (due, state)) in enumerate(zip(shown, dues), start=top):
            chosen = self.top + y - top == self.cursor
            base = curses.A_REVERSE if chosen and self.focus == "items" else 0
            if base:
                put(s, y, x - 1, " " * (width + 1), base)
            elif chosen:
                put(s, y, x - 1, "›", curses.A_BOLD)

            list_attr = self.pair(curses_color(self.list_color(r.list)))
            put(s, y, x, "●" if r.done else "○", base | list_attr)
            title = one_line(r.title) + ("  ≡" if r.notes else "")
            put(s, y, x + 2, title, base | (curses.A_DIM if r.done else 0), width - 3 - right)

            cx = w - 1 - right
            for name, cw in columns:
                cx += 2
                if name == "list":
                    put(s, y, cx, clip(self.list_title(r.list), cw), base | list_attr | curses.A_DIM)
                elif name == "priority":
                    mark = priority_mark(r.priority)
                    put(s, y, cx + cw - len(mark), mark, base | self.pair(curses.COLOR_YELLOW) | curses.A_BOLD)
                elif due:
                    attr = {"overdue": self.pair(curses.COLOR_RED) | curses.A_BOLD,
                            "today": self.pair(curses.COLOR_CYAN)}.get(state, curses.A_DIM)
                    put(s, y, cx, due, base | attr)
                cx += cw

    def draw_detail(self, y, w):
        s = self.stdscr
        put(s, y, 0, "─" * w, curses.A_DIM)
        r = self.selected()
        if r is None:
            return
        put(s, y + 1, 1, one_line(r.title), curses.A_BOLD, w - 2)
        due, state = format_due(r, datetime.now())
        bits = [f"Due {due}" if due else "No due date",
                f"Priority {priority_name(r.priority)}",
                f"List {self.list_title(r.list)}"]
        if r.done and r.completed_at:
            bits.append("Completed " + r.completed_at[:16].replace("T", " "))
        put(s, y + 2, 1, "  ·  ".join(bits), curses.A_DIM, w - 2)
        notes = " ⏎ ".join(line.strip() for line in r.notes.splitlines() if line.strip())
        put(s, y + 3, 1, notes or "No notes (n to add some)", 0 if notes else curses.A_DIM, w - 2)

    def draw_footer(self, y, w):
        text, error, at = self.message
        if text and time.monotonic() - at < MESSAGE_SECONDS:
            attr = self.pair(curses.COLOR_RED) | curses.A_BOLD if error else curses.A_DIM
            put(self.stdscr, y, 1, text, attr, w - 2)
            return
        hints = ("space done  a add  e edit  d due  p prio  n notes  m move  "
                 "D delete  c completed  / filter  ? help  q quit")
        put(self.stdscr, y, 1, hints, curses.A_DIM, w - 2)

    # ---------------------------------------------------------- input

    def prompt(self, label, initial="", on_change=None):
        """Edit one line in the footer.  Returns the text, or None on Esc."""
        buf, pos = list(initial), len(initial)
        s = self.stdscr
        s.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                if on_change:
                    on_change("".join(buf))
                self.draw()
                h, w = s.getmaxyx()
                s.move(h - 1, 0)
                s.clrtoeol()
                lab = clip(label, max(8, w // 2))
                lw = put(s, h - 1, 1, lab, curses.A_BOLD) + 2
                room = max(4, w - lw - 2)
                before = "".join(buf[:pos])
                # Scroll the line so the cursor stays in view.
                start = 0
                while text_width("".join(buf[start:pos])) > room - 1:
                    start += 1
                shown = "".join(buf[start:])
                put(s, h - 1, lw, shown, 0, room)
                s.move(h - 1, min(w - 1, lw + text_width(before) - text_width("".join(buf[:start]))))
                s.refresh()
                try:
                    ch = s.get_wch()
                except curses.error:
                    continue
                if ch in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if ch == "\x1b":
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos:
                        pos -= 1
                        del buf[pos]
                elif ch == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif ch == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif ch == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif ch in (curses.KEY_HOME, "\x01"):
                    pos = 0
                elif ch in (curses.KEY_END, "\x05"):
                    pos = len(buf)
                elif ch == "\x15":                       # ^U
                    del buf[:pos]
                    pos = 0
                elif ch == "\x17":                       # ^W
                    cut = pos
                    while cut and buf[cut - 1] == " ":
                        cut -= 1
                    while cut and buf[cut - 1] != " ":
                        cut -= 1
                    del buf[cut:pos]
                    pos = cut
                elif isinstance(ch, str) and ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1
        finally:
            curses.curs_set(0)
            s.timeout(1000)

    def confirm(self, question):
        answer = self.prompt(f"{question} [y/N]")
        return answer is not None and answer.strip().lower() in ("y", "yes", "j", "ja")

    def pick(self, title, options):
        """Choose from [(value, label)] in a centred box.  Returns a value or None."""
        if not options:
            return None
        s = self.stdscr
        index = 0
        s.timeout(-1)
        try:
            while True:
                self.draw()
                h, w = s.getmaxyx()
                bw = min(w - 4, max(text_width(title), *(text_width(label) for _, label in options)) + 6)
                bh = min(h - 2, len(options) + 2)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                first = max(0, min(index - (bh - 2) // 2, len(options) - (bh - 2)))
                put(s, y0, x0, "┌" + "─" * (bw - 2) + "┐")
                put(s, y0, x0 + 2, f" {title} ", curses.A_BOLD, bw - 4)
                for row in range(bh - 2):
                    i = first + row
                    put(s, y0 + 1 + row, x0, "│" + " " * (bw - 2) + "│")
                    if i < len(options):
                        attr = curses.A_REVERSE if i == index else 0
                        put(s, y0 + 1 + row, x0 + 1, " " + options[i][1].ljust(bw - 3), attr, bw - 2)
                put(s, y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘")
                s.refresh()
                try:
                    ch = s.get_wch()
                except curses.error:
                    continue
                if ch in ("j", curses.KEY_DOWN):
                    index = min(len(options) - 1, index + 1)
                elif ch in ("k", curses.KEY_UP):
                    index = max(0, index - 1)
                elif ch in ("\n", "\r", curses.KEY_ENTER, " "):
                    return options[index][0]
                elif ch in ("\x1b", "q"):
                    return None
        finally:
            s.timeout(1000)

    def show_help(self):
        s = self.stdscr
        lines = HELP.splitlines()
        self.draw()
        h, w = s.getmaxyx()
        bw = min(w - 2, max(map(text_width, lines)) + 4)
        bh = min(h - 2, len(lines) + 2)
        y0, x0 = (h - bh) // 2, (w - bw) // 2
        for row in range(bh):
            put(s, y0 + row, x0, " " * bw, curses.A_REVERSE)
        for row, line in enumerate(lines[:bh - 2], start=1):
            put(s, y0 + row, x0 + 2, line, curses.A_REVERSE, bw - 4)
        s.refresh()
        s.timeout(-1)
        try:
            s.get_wch()
        except curses.error:
            pass
        s.timeout(1000)

    def edit_notes(self, text):
        """Open `text` in $VISUAL / $EDITOR. Returns the edited text, or None."""
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write(text + "\n" if text else "")
            name = fh.name
        curses.def_prog_mode()
        curses.endwin()
        try:
            code = subprocess.call(editor + [name])
            with open(name, encoding="utf-8") as fh:
                edited = fh.read().rstrip("\n")
        except OSError as exc:
            self.say(f"could not start {editor[0]}: {exc}", error=True)
            return None
        finally:
            os.unlink(name)
            curses.reset_prog_mode()
            self.stdscr.refresh()
        return edited if code == 0 else None

    # ---------------------------------------------------------- actions

    def add(self):
        key = self.view_key()
        smart = self.view < len(SMART_VIEWS)
        list_id = self.default_list if smart else key
        payload = {"list": list_id}
        if key == "today":
            today = date.today()
            payload["due"] = [today.year, today.month, today.day]
        while True:
            title = self.prompt(f"New in {self.list_title(list_id)}:")
            if not title or not title.strip():
                return
            r = self.mutate("add", {**payload, "title": title.strip()})
            if r is None:
                return
            self.say(f"Added “{r.title}”")

    def toggle(self, r):
        done = not r.done
        if done:
            self.keep.add(r.id)
        r = self.mutate("update", {"id": r.id, "done": done})
        if r is not None:
            self.say(("Completed “%s”" if done else "Reopened “%s”") % one_line(r.title))

    def rename(self, r):
        title = self.prompt("Title:", one_line(r.title))
        if title and title.strip() and title.strip() != r.title:
            self.mutate("update", {"id": r.id, "title": title.strip()})

    def set_due(self, r):
        current = ""
        if r.due:
            current = r.due.isoformat() + (" %02d:%02d" % r.due_time if r.due_time else "")
        error = None
        while True:
            label = f"Due ({error}):" if error else "Due (tom 9:00, fri, +3, 5-11; blank clears):"
            text = self.prompt(label, current)
            if text is None:
                return
            value, error = parse_due(text, datetime.now())
            if not error:
                break
            current = text
        if value != due_wire(r):
            self.mutate("update", {"id": r.id, "due": value})

    def cycle_priority(self, r):
        self.mutate("update", {"id": r.id, "priority": next_priority(r.priority)})

    def notes(self, r):
        edited = self.edit_notes(r.notes)
        if edited is not None and edited != r.notes:
            self.mutate("update", {"id": r.id, "notes": edited})

    def move(self, r):
        options = [(li["id"], li["title"]) for li in self.lists
                   if li["writable"] and li["id"] != r.list]
        target = self.pick("Move to", options)
        if target:
            moved = self.mutate("update", {"id": r.id, "list": target}, follow=False)
            if moved:
                self.say(f"Moved to {self.list_title(target)}")

    def delete(self, r):
        if self.confirm(f"Delete “{clip(one_line(r.title), 40)}”?"):
            if self.mutate("delete", {"id": r.id}):
                self.say(f"Deleted “{one_line(r.title)}”")

    def filter(self):
        """Filter as you type; Enter keeps the filter, Esc drops it."""
        def live(text):
            self.query = text.strip()
            self.cursor = 0

        if self.prompt("/", self.query, on_change=live) is None:
            self.query = ""

    # ---------------------------------------------------------- loop

    def handle(self, ch):
        """Act on one key. Returns False to quit."""
        rows = self.rows()
        _, bottom = self.body()
        page = max(1, bottom - 2)
        r = self.selected()
        if ch in ("q", "Q"):
            return False
        if ch == curses.KEY_RESIZE:
            return True

        moving_lists = self.focus == "lists" and self.list_pane_width(self.stdscr.getmaxyx()[1])
        if ch in ("j", curses.KEY_DOWN):
            if moving_lists:
                self.switch_view(self.view + 1)
            else:
                self.cursor = min(len(rows) - 1, self.cursor + 1)
        elif ch in ("k", curses.KEY_UP):
            if moving_lists:
                self.switch_view(self.view - 1)
            else:
                self.cursor = max(0, self.cursor - 1)
        elif ch == curses.KEY_NPAGE:
            self.cursor = min(len(rows) - 1, self.cursor + page)
        elif ch == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - page)
        elif ch in ("g", curses.KEY_HOME):
            self.cursor = 0
        elif ch in ("G", curses.KEY_END):
            self.cursor = len(rows) - 1
        elif ch in ("h", curses.KEY_LEFT):
            self.focus = "lists"
        elif ch in ("l", curses.KEY_RIGHT):
            self.focus = "items"
        elif ch == "\t":
            self.focus = "items" if self.focus == "lists" else "lists"
        elif ch == "]":
            self.switch_view(self.view + 1)
        elif ch == "[":
            self.switch_view(self.view - 1)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            if self.focus == "lists":
                self.focus = "items"
            elif r:
                self.rename(r)
        elif ch in ("a", "o"):
            self.add()
        elif ch == "c":
            self.show_done = not self.show_done
            self.follow(r.id if r else None)
        elif ch == "/":
            self.filter()
        elif ch == "\x1b":
            self.query = ""
        elif ch == "r":
            self.keep.clear()
            self.reload()
        elif ch == "?":
            self.show_help()
        elif r is None:
            pass
        elif ch in (" ", "x"):
            self.toggle(r)
        elif ch == "e":
            self.rename(r)
        elif ch == "d":
            self.set_due(r)
        elif ch == "p":
            self.cycle_priority(r)
        elif ch == "n":
            self.notes(r)
        elif ch == "m":
            self.move(r)
        elif ch in ("D", curses.KEY_DC):
            self.delete(r)
        return True

    def run(self):
        self.stdscr.timeout(1000)
        while True:
            self.draw()
            try:
                ch = self.stdscr.get_wch()
            except curses.error:
                ch = None
            if ch is None:
                if time.monotonic() - self.loaded_at >= REFRESH_SECONDS:
                    self.reload(quiet=True)
                continue
            if not self.handle(ch):
                return


def run_curses(stdscr, helper, data):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
    App(stdscr, helper, data).run()


# ---------------------------------------------------------------- --once


def print_once(data):
    now = datetime.now()
    reminders = [to_reminder(raw) for raw in data["reminders"]]
    for li in sorted(data["lists"], key=lambda li: li["title"].casefold()):
        rows = visible(reminders, li["id"], now.date())
        if not rows:
            continue
        print(f"{BOLD}{li['title']}{RESET} {DIM}({len(rows)}){RESET}")
        for r in rows:
            due, state = format_due(r, now)
            color = {"overdue": RED, "today": CYAN}.get(state, DIM)
            mark = priority_mark(r.priority)
            line = f"  ○ {one_line(r.title)}"
            if mark:
                line += f"  {YELLOW}{mark}{RESET}"
            if due:
                line += f"  {color}{due}{RESET}"
            print(line)
        print()


def main():
    parser = argparse.ArgumentParser(description="Apple Reminders in the terminal.")
    parser.add_argument("--once", action="store_true", help="print open reminders and exit")
    parser.add_argument("--rebuild", action="store_true", help="recompile the EventKit helper")
    args = parser.parse_args()

    if sys.platform != "darwin":
        sys.exit("Apple Reminders lives on macOS only.")
    helper, error = build_helper(force=args.rebuild)
    if error:
        sys.exit(error)
    data, error = call_helper(helper, "dump")
    if error:
        sys.exit(f"Reminders: {error}")

    if args.once:
        print_once(data)
        return
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run_curses, helper, data)


if __name__ == "__main__":
    main()
