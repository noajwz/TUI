#!/usr/bin/env python3
"""Terminal UI for Apple Calendar.

Reads the same store the Calendar app uses, so every account it shows (iCloud,
Google, Exchange, subscriptions, birthdays) shows up here too, as a month
grid, a week, or an agenda.  Read-only: nothing here changes a calendar.

As in reminders_tui.py, the talking is done by a small Swift program against
EventKit rather than through AppleScript, which is far too slow to drive a UI.
EventKit also expands recurring events into their occurrences, which is the
part that would be miserable to do by hand.  The source is embedded below and
compiled once with `swiftc` into ~/Library/Caches/calendar_tui/ (about 30 s,
first run only).  The file name carries a hash of the source, so editing it
triggers a rebuild.
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
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

REFRESH_SECONDS = 60
HELPER_TIMEOUT = 120        # generous: the very first call waits on the permission dialog
MESSAGE_SECONDS = 5
CACHE_DIR = os.path.expanduser("~/Library/Caches/calendar_tui")

LOAD_MONTHS_BEFORE = 1      # what one fetch covers around the month on screen
LOAD_MONTHS_AFTER = 3
AGENDA_DAYS_BEFORE = 14     # the agenda's span around the day it was opened on
AGENDA_DAYS_AFTER = 120
AGENDA_TITLE_MAX = 56

DAY_PANE_WIDTH = 36
DAY_PANE_MIN = 100          # window width from which the day pane sits beside the grid
WEEK_NUMBERS_MIN = 60
TIMES_MIN = 16               # cell width from which month cells show start times
DETAIL_MIN_HEIGHT = 18
DETAIL_HEIGHT = 7

VIEWS = ("month", "week", "agenda")

RELATIVE_DAYS = {
    "today": 0, "tod": 0, "vandaag": 0,
    "tomorrow": 1, "tom": 1, "tmrw": 1, "morgen": 1,
    "yesterday": -1, "gisteren": -1,
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
MONTHS = {
    "jan": 1, "january": 1, "januari": 1,
    "feb": 2, "february": 2, "februari": 2,
    "mar": 3, "march": 3, "mrt": 3, "maart": 3,
    "apr": 4, "april": 4,
    "may": 5, "mei": 5,
    "jun": 6, "june": 6, "juni": 6,
    "jul": 7, "july": 7, "juli": 7,
    "aug": 8, "august": 8, "augustus": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LONG_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LONG_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July",
                    "August", "September", "October", "November", "December")

OFFSET_RE = re.compile(r"^([+-]\d+)([dwm]?)$")
ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
DMY_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2}|\d{4}))?$")
YEAR_RE = re.compile(r"^\d{4}$")

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
RESET = "\033[0m"

HELP = """\
Moving                       Views
  h l  ← →    day              m       month
  j k  ↑ ↓    week             w       week
  [ ]         month / week     a  ⏎    agenda (from the selected day)
  t           today            Tab     next view
  g           go to a date     Esc     back from the agenda

In the agenda               Other
  j k  ↑ ↓    event            c       show / hide calendars
  PgUp PgDn   page             /       search titles, places, notes
  Home End    ends             r       reload
                               ?       this help     q  quit

Go to: today  tom  fri  +3  -2w  +1m  5-11  25/12/27  2026-12-25  dec  march 2027
       Dutch works too: morgen, vr, overmorgen, mrt."""

SWIFT_SOURCE = r"""
import CoreGraphics
import EventKit
import Foundation

// Reads {"from": [y, m, d], "to": [y, m, d]} on stdin and answers every
// calendar plus each event occurrence that overlaps that range, as JSON.
// Times go over the wire as [y, m, d, H, M] in local time.

let store = EKEventStore()
let cal = Calendar.current
let sem = DispatchSemaphore(value: 0)

func die(_ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

func hex(_ color: CGColor?) -> Any {
    guard let srgb = CGColorSpace(name: CGColorSpace.sRGB),
          let c = color?.converted(to: srgb, intent: .defaultIntent, options: nil),
          let p = c.components, p.count >= 3 else { return NSNull() }
    let b = p.prefix(3).map { Int((min(max($0, 0), 1) * 255).rounded()) }
    return String(format: "#%02x%02x%02x", b[0], b[1], b[2])
}

func wire(_ d: Date) -> [Int] {
    let c = cal.dateComponents([.year, .month, .day, .hour, .minute], from: d)
    return [c.year!, c.month!, c.day!, c.hour!, c.minute!]
}

func day(_ value: Any?) -> Date {
    guard let a = value as? [Int], a.count >= 3,
          let d = cal.date(from: DateComponents(year: a[0], month: a[1], day: a[2]))
    else { die("the request needs from and to as [y, m, d]") }
    return d
}

func encode(_ e: EKEvent) -> [String: Any] {
    let attendees = e.attendees ?? []
    let me = attendees.first { $0.isCurrentUser }
    return [
        "id": e.eventIdentifier ?? "",
        "calendar": e.calendar?.calendarIdentifier ?? "",
        "title": e.title ?? "",
        "location": e.location ?? "",
        "notes": e.notes ?? "",
        "url": e.url?.absoluteString ?? "",
        "all_day": e.isAllDay,
        "start": wire(e.startDate),
        "end": wire(e.endDate),
        "recurring": e.hasRecurrenceRules,
        "canceled": e.status == .canceled,
        "declined": me?.participantStatus == .declined,
        "organizer": e.organizer?.name ?? "",
        "attendees": attendees.compactMap { $0.name },
    ]
}

var granted = false
store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
sem.wait()
if !granted {
    die("no access to Calendar. Allow your terminal app under System Settings > Privacy & Security > Calendars.")
}

let input = FileHandle.standardInput.readDataToEndOfFile()
let req = (try? JSONSerialization.jsonObject(with: input)) as? [String: Any] ?? [:]
let start = day(req["from"]), end = day(req["to"])
let calendars = store.calendars(for: .event).map { c -> [String: Any] in
    ["id": c.calendarIdentifier, "title": c.title, "color": hex(c.cgColor),
     "source": c.source?.title ?? ""]
}
let events = store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil))
guard let data = try? JSONSerialization.data(withJSONObject: ["calendars": calendars, "events": events.map(encode)])
else { die("cannot encode output") }
FileHandle.standardOutput.write(data)
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


def call_helper(path, start, end):
    """Fetch every event overlapping [start, end). Returns (data, error)."""
    payload = {"from": [start.year, start.month, start.day], "to": [end.year, end.month, end.day]}
    try:
        proc = subprocess.run([path], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "Calendar did not answer in time"
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
class Event:
    id: str
    calendar: str
    title: str
    location: str
    notes: str
    url: str
    all_day: bool
    start: datetime
    end: datetime
    recurring: bool
    canceled: bool
    declined: bool
    organizer: str
    attendees: list

    @property
    def first_day(self):
        return self.start.date()

    @property
    def last_day(self):
        """The last day the event covers: one ending at midnight doesn't reach the next day."""
        if self.end <= self.start:
            return self.start.date()
        return (self.end - timedelta(minutes=1)).date()


def to_event(raw):
    notes = (raw.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n")
    return Event(
        id=raw.get("id") or "",
        calendar=raw.get("calendar") or "",
        title=raw.get("title") or "(no title)",
        location=raw.get("location") or "",
        notes=notes.strip("\n"),
        url=raw.get("url") or "",
        all_day=bool(raw.get("all_day")),
        start=datetime(*raw["start"][:5]),
        end=datetime(*raw["end"][:5]),
        recurring=bool(raw.get("recurring")),
        canceled=bool(raw.get("canceled")),
        declined=bool(raw.get("declined")),
        organizer=raw.get("organizer") or "",
        attendees=list(raw.get("attendees") or []),
    )


def matches(e, query):
    query = query.casefold()
    return not query or any(query in s.casefold() for s in (e.title, e.location, e.notes))


def index_by_day(events):
    """{date: [events that day]}, all-day events first, then by start time."""
    days = {}
    for e in events:
        d = e.first_day
        while d <= e.last_day:
            days.setdefault(d, []).append(e)
            d += timedelta(days=1)
    for evs in days.values():
        evs.sort(key=lambda e: (not e.all_day, e.start, e.end, e.title.casefold()))
    return days


def add_months(d, n):
    """`d` moved by `n` months, the day clamped to the length of the month it lands in."""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def week_start(d):
    return d - timedelta(days=d.weekday())


def month_weeks(year, month):
    """The weeks a month grid shows, Monday first: [[7 dates], ...]."""
    start = week_start(date(year, month, 1))
    weeks = []
    while not weeks or (start.year, start.month) == (year, month):
        weeks.append([start + timedelta(days=i) for i in range(7)])
        start += timedelta(days=7)
    return weeks


def hm(t):
    return f"{t.hour:02d}:{t.minute:02d}"


def short_day(d, today):
    text = f"{DAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]}"
    return text if d.year == today.year else f"{text} {d.year}"


def long_day(d, today):
    text = f"{LONG_DAY_NAMES[d.weekday()]} {d.day} {LONG_MONTH_NAMES[d.month - 1]}"
    return text if d.year == today.year else f"{text} {d.year}"


def time_text(e, day):
    """What goes before the title on `day`: a start time, an arrow for a carried-over
    event (with its end time on its last day), or nothing for an all-day event."""
    if e.all_day:
        return ""
    if e.first_day == day:
        return hm(e.start)
    if e.last_day == day:
        return "→" + hm(e.end)
    return "→"


def duration_text(e):
    if e.all_day:
        days = (e.last_day - e.first_day).days + 1
        return f"{days} days" if days > 1 else ""
    minutes = int((e.end - e.start).total_seconds() // 60)
    if minutes <= 0:
        return ""
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} d"] * bool(days) + [f"{hours} h"] * bool(hours) + [f"{mins} min"] * bool(mins)
    return " ".join(parts)


def format_when(e, today):
    first, last = e.first_day, e.last_day
    if e.all_day:
        if first == last:
            return f"{short_day(first, today)}, all day"
        return f"{short_day(first, today)} – {short_day(last, today)}, all day"
    if first == last:
        return f"{short_day(first, today)}, {hm(e.start)}–{hm(e.end)}"
    return f"{short_day(first, today)} {hm(e.start)} – {short_day(last, today)} {hm(e.end)}"


def span_text(e, day):
    """The time range shown in a day list: `09:00–10:30`, `all day`, `→ 02:00`."""
    if e.all_day:
        return "all day"
    start = hm(e.start) if e.first_day == day else "…"
    end = hm(e.end) if e.last_day == day else "…"
    return f"{start}–{end}"


def agenda_rows(by_day, start, end, keep=()):
    """Rows for the agenda over [start, end): ("day", d) headers, then ("event", d, e).

    Days without events are left out, except those in `keep` (today, and the day
    the agenda was opened on), which get an ("empty", d) row instead.
    """
    rows = []
    d = start
    while d < end:
        evs = by_day.get(d, [])
        if evs or d in keep:
            rows.append(("day", d))
            rows.extend(("event", d, e) for e in evs)
            if not evs:
                rows.append(("empty", d))
        d += timedelta(days=1)
    return rows


def parse_goto(text, today):
    """Parse what was typed at the go-to prompt. Returns (date, error)."""
    words = text.lower().replace(",", " ").split()
    if not words:
        return None, "type a date"
    if words[0] in MONTHS and len(words) <= 2:
        month = MONTHS[words[0]]
        if len(words) == 2:
            if not YEAR_RE.match(words[1]):
                return None, f"not a year: {words[1]}"
            return date(int(words[1]), month, 1), None
        # A month without a year is the next one to come round, or this one.
        year = today.year + (month < today.month)
        return date(year, month, 1), None
    if len(words) > 1:
        return None, f"did not understand: {text.strip()}"
    return parse_day(words[0], today)


def parse_day(word, today):
    """Return (date, error) for a single date word."""
    if word in RELATIVE_DAYS:
        return today + timedelta(days=RELATIVE_DAYS[word]), None
    if word in WEEKDAYS:
        ahead = (WEEKDAYS[word] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), None
    m = OFFSET_RE.match(word)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "m":
            return add_months(today, n), None
        return today + timedelta(days=n * (7 if unit == "w" else 1)), None
    try:
        m = ISO_RE.match(word)
        if m:
            return date(*map(int, m.groups())), None
        m = DMY_RE.match(word)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if year:
                return date(int(year) + (2000 if len(year) == 2 else 0), month, day), None
            return date(today.year, month, day), None
    except ValueError:
        return None, f"no such date: {word}"
    return None, f"did not understand: {word}"


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


def pad(s, width):
    s = clip(s, width)
    return s + " " * (width - text_width(s))


def one_line(s):
    return " ".join(s.split())


def wrap(text, width):
    """Word-wrap `text` to `width` cells; words too long for a line are cut."""
    lines = []
    for para in text.splitlines():
        line = ""
        for word in para.split():
            if line and text_width(line) + 1 + text_width(word) > width:
                lines.append(line)
                line = ""
            line = f"{line} {word}" if line else word
        lines.append(clip(line, width))
    return lines


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
    """Nearest terminal colour to a calendar's #rrggbb, or -1 for the default."""
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
    def __init__(self, stdscr, helper, data, loaded):
        self.stdscr = stdscr
        self.helper = helper
        self.calendars = []
        self.events = []
        self.by_day = {}
        self.hidden = set()
        self.query = ""
        self.loaded = None          # (first day, day after the last) of the fetched range
        self.loaded_at = 0.0
        self.view = "month"
        self.back = "month"         # where Esc goes from the agenda
        self.day = date.today()
        self.anchor = self.day      # the day the agenda was opened on
        self.cursor = 0             # agenda row
        self.top = 0
        self.message = ("", False, 0.0)
        self.pairs = {}
        self.load(data, loaded)

    # ---------------------------------------------------------- data

    def load(self, data, loaded):
        self.calendars = sorted(data["calendars"],
                                key=lambda c: (c["source"].casefold(), c["title"].casefold()))
        self.events = [to_event(raw) for raw in data["events"]]
        self.loaded = loaded
        self.loaded_at = time.monotonic()
        self.reindex()

    def reindex(self):
        self.by_day = index_by_day(e for e in self.events
                                   if e.calendar not in self.hidden and matches(e, self.query))

    def fetch(self, start, end, quiet=False):
        if not quiet:
            self.status("Loading…")
        data, error = call_helper(self.helper, start, end)
        if error:
            # Count the range as loaded anyway, so the next draw doesn't retry
            # at once; the timed refresh tries again.
            self.loaded, self.loaded_at = (start, end), time.monotonic()
            self.say(error, error=True)
            return False
        self.load(data, (start, end))
        return True

    def ensure(self, lo, hi):
        """Make sure [lo, hi) is loaded, fetching a few months around it if not."""
        if self.loaded and self.loaded[0] <= lo and hi <= self.loaded[1]:
            return
        self.fetch(*load_range(lo, hi))

    def needed(self):
        """The range of days the current view draws."""
        if self.view == "month":
            weeks = month_weeks(self.day.year, self.day.month)
            return weeks[0][0], weeks[-1][-1] + timedelta(days=1)
        if self.view == "week":
            start = week_start(self.day)
            return start, start + timedelta(days=7)
        return self.agenda_span()

    def agenda_span(self):
        return (self.anchor - timedelta(days=AGENDA_DAYS_BEFORE),
                self.anchor + timedelta(days=AGENDA_DAYS_AFTER))

    def calendar(self, cal_id):
        return next((c for c in self.calendars if c["id"] == cal_id), None)

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

    def cal_attr(self, cal_id):
        c = self.calendar(cal_id)
        return self.pair(curses_color(c["color"] if c else None))

    # ---------------------------------------------------------- draw

    def draw(self):
        s = self.stdscr
        self.ensure(*self.needed())
        s.erase()
        h, w = s.getmaxyx()
        if h < 14 or w < 56:
            put(s, 0, 0, "Window too small")
            s.refresh()
            return
        self.draw_header(w)
        if self.view == "agenda":
            self.draw_agenda(1, h - 1, w)
        else:
            pane = DAY_PANE_WIDTH if w >= DAY_PANE_MIN else 0
            gw = w - pane - 1 if pane else w
            if self.view == "month":
                weeks, month = month_weeks(self.day.year, self.day.month), self.day.month
            else:
                weeks, month = [[week_start(self.day) + timedelta(days=i) for i in range(7)]], None
            self.draw_grid(weeks, month, 1, h - 1, gw)
            if pane:
                for y in range(1, h - 1):
                    put(s, y, gw, "│", curses.A_DIM)
                self.draw_day_pane(gw + 2, 2, h - 1, pane - 2)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        s = self.stdscr
        today = date.today()
        if self.view == "month":
            title = f"{LONG_MONTH_NAMES[self.day.month - 1]} {self.day.year}"
        elif self.view == "week":
            start = week_start(self.day)
            end = start + timedelta(days=6)
            title = f"Week {start.isocalendar()[1]} · {short_day(start, today)} – {short_day(end, today)}"
        else:
            title = f"Agenda · {long_day(self.day, today)}"
        title = f" ▦ Calendar · {title}"
        if self.query:
            title += f"  /{self.query}"
        if self.hidden:
            title += f"  ({len(self.hidden)} hidden)"
        clock = f"{short_day(today, today)}  {time.strftime('%H:%M')} "
        put(s, 0, 0, " " * w, curses.A_REVERSE)
        put(s, 0, 0, title, curses.A_BOLD | curses.A_REVERSE, w - len(clock) - 1)
        put(s, 0, w - len(clock), clock, curses.A_REVERSE)

    def draw_grid(self, weeks, month, top, bottom, width):
        s = self.stdscr
        wk = 3 if width >= WEEK_NUMBERS_MIN else 0
        xs = [wk + i * (width - wk) // 7 for i in range(8)]
        names = LONG_DAY_NAMES if xs[1] - xs[0] >= 12 else DAY_NAMES
        for i, name in enumerate(names):
            put(s, top, xs[i] + 1, name, curses.A_BOLD if i < 5 else curses.A_DIM, xs[i + 1] - xs[i] - 1)
        top += 1
        ys = [top + j * (bottom - top) // len(weeks) for j in range(len(weeks) + 1)]
        # Times for every cell or none, decided by the narrowest column.
        times = min(b - a for a, b in zip(xs, xs[1:])) - 1 >= TIMES_MIN
        for j, week in enumerate(weeks):
            if wk:
                put(s, ys[j], 0, f"{week[0].isocalendar()[1]:>2}", curses.A_DIM)
            for i, d in enumerate(week):
                outside = month is not None and d.month != month
                self.draw_cell(d, ys[j], ys[j + 1], xs[i], xs[i + 1], i > 0, j > 0, outside, times)

    def draw_cell(self, d, y0, y1, x0, x1, left_rule, top_rule, outside, times):
        s = self.stdscr
        today = date.today()
        chosen = d == self.day
        cw = x1 - x0
        put(s, y0, x0, ("━" if chosen else "─") * cw, curses.A_BOLD if chosen else curses.A_DIM)
        if left_rule:
            put(s, y0, x0, "┼" if top_rule else "┬", curses.A_DIM)
            for y in range(y0 + 1, y1):
                put(s, y, x0, "│", curses.A_DIM)

        evs = self.by_day.get(d, [])
        room = y1 - y0 - 1
        label = f" {d.day} {MONTH_NAMES[d.month - 1]} " if d.day == 1 else f" {d.day} "
        if room <= 0 and evs:
            label += f"·{len(evs)} "
        if d == today:
            attr = self.pair(curses.COLOR_RED) | curses.A_REVERSE | curses.A_BOLD
        elif chosen:
            attr = curses.A_REVERSE | curses.A_BOLD
        else:
            attr = curses.A_DIM if outside else curses.A_BOLD
        put(s, y0, x0 + 1, label, attr, cw - 1)

        if room <= 0:
            return
        if self.view == "week":
            self.draw_tall_cell(evs, d, y0 + 1, y1, x0 + 1, cw - 1)
            return
        # A one-line cell keeps its first event rather than a bare "+2 more".
        shown = evs if len(evs) <= room else evs[:max(1, room - 1)]
        for k, e in enumerate(shown):
            self.draw_event_line(e, d, y0 + 1 + k, x0 + 1, cw - 1, outside, times)
        if len(evs) > len(shown) and room > 1:
            put(s, y0 + room, x0 + 1, f" +{len(evs) - len(shown)} more", curses.A_DIM, cw - 1)

    def draw_event_line(self, e, d, y, x, width, faint, times):
        s = self.stdscr
        color = self.cal_attr(e.calendar)
        faint = faint or e.declined or e.canceled
        title = one_line(e.title)
        if e.all_day:
            # A coloured bar, the way the Calendar app draws all-day events.
            attr = color | (curses.A_DIM if faint else curses.A_REVERSE)
            put(s, y, x, pad(" " + title, width), attr)
            return
        put(s, y, x, "•", color)
        used = 2
        when = time_text(e, d)
        if when and times:
            put(s, y, x + used, when, curses.A_DIM)
            used += len(when) + 1
        put(s, y, x + used, title, curses.A_DIM if faint else 0, width - used)

    def draw_tall_cell(self, evs, d, y, bottom, x, width):
        """A week-view day: each event gets its time range, title and place on lines of their own."""
        s = self.stdscr
        for n, e in enumerate(evs):
            faint = curses.A_DIM if e.declined or e.canceled else 0
            color = self.cal_attr(e.calendar)
            title = wrap(one_line(e.title), width - 1)[:2]
            block = (1 if e.all_day else 1 + len(title)) + (bool(e.location) and not e.all_day)
            if y + block > bottom - (n < len(evs) - 1):
                put(s, bottom - 1, x, f" +{len(evs) - n} more", curses.A_DIM, width)
                return
            if e.all_day:
                put(s, y, x, pad(" " + one_line(e.title), width), color | (faint or curses.A_REVERSE))
                y += 1
                continue
            put(s, y, x, "•", color)
            span = span_text(e, d)
            put(s, y, x + 2, span if text_width(span) <= width - 2 else time_text(e, d), curses.A_DIM, width - 2)
            for k, line in enumerate(title, start=1):
                put(s, y + k, x + 1, line, curses.A_BOLD | faint, width - 1)
            if e.location:
                put(s, y + len(title) + 1, x + 1, one_line(e.location), curses.A_DIM, width - 1)
            y += block + (y + block < bottom)

    def draw_day_pane(self, x, top, bottom, width):
        s = self.stdscr
        today = date.today()
        put(s, top, x, long_day(self.day, today), curses.A_BOLD, width)
        if self.day == today:
            put(s, top + 1, x, "Today", self.pair(curses.COLOR_RED) | curses.A_BOLD)
        y = top + 3
        evs = self.by_day.get(self.day, [])
        if not evs:
            put(s, y, x, "No events." if not self.query else "No matches.", curses.A_DIM, width)
            return
        for n, e in enumerate(evs):
            title = wrap(one_line(e.title), width - 2)[:2]
            block = 1 + len(title) + bool(e.location)
            if y + block > bottom:
                put(s, bottom - 1, x, f"+{len(evs) - n} more", curses.A_DIM, width)
                return
            faint = curses.A_DIM if e.declined or e.canceled else 0
            put(s, y, x, "●", self.cal_attr(e.calendar))
            when = span_text(e, self.day)
            if not e.all_day and e.first_day != e.last_day:
                when = (f"{DAY_NAMES[e.start.weekday()]} {e.start.day} {hm(e.start)} → "
                        f"{DAY_NAMES[e.end.weekday()]} {e.end.day} {hm(e.end)}")
            put(s, y, x + 2, when, curses.A_DIM, width - 2)
            for k, line in enumerate(title, start=1):
                put(s, y + k, x + 2, line, curses.A_BOLD | faint, width - 2)
            if e.location:
                put(s, y + len(title) + 1, x + 2, one_line(e.location), curses.A_DIM, width - 2)
            y += block + 1

    def agenda(self):
        lo, hi = self.agenda_span()
        return agenda_rows(self.by_day, lo, hi, keep={date.today(), self.anchor})

    def selectable(self, rows):
        return [i for i, row in enumerate(rows) if row[0] == "event"]

    def draw_agenda(self, top, bottom, w):
        s = self.stdscr
        today = date.today()
        rows = self.agenda()
        h, _ = s.getmaxyx()
        detail = DETAIL_HEIGHT if h >= DETAIL_MIN_HEIGHT else 0
        list_bottom = bottom - detail
        room = list_bottom - top
        picks = self.selectable(rows)
        if picks and self.cursor not in picks:
            self.cursor = min(picks, key=lambda i: abs(i - self.cursor))
        if not rows:
            put(s, top + 1, 2, "Nothing in this stretch." if not self.query else "No matches.", curses.A_DIM)
            return
        # Keep the cursor in view, with its day header above it when there is room.
        if self.cursor - 1 < self.top:
            self.top = max(0, self.cursor - 1)
        elif self.cursor >= self.top + room:
            self.top = self.cursor - room + 1
        self.top = max(0, min(self.top, len(rows) - room))

        # Columns: time, title, calendar, then the place in whatever a wide window has left.
        cal_width = min(18, max((text_width(c["title"]) for c in self.calendars), default=0))
        show_cal = w >= 70
        title_width = min(AGENDA_TITLE_MAX, w - 18 - (cal_width + 2 if show_cal else 0))
        cal_x = 17 + title_width + 2
        place_x = cal_x + cal_width + 2
        for y, (i, row) in enumerate(zip(range(self.top, len(rows)), rows[self.top:self.top + room]), start=top):
            kind, d = row[0], row[1]
            if kind == "day":
                label = long_day(d, today)
                attr = curses.A_BOLD
                if d == today:
                    label = f"Today · {label}"
                    attr |= self.pair(curses.COLOR_RED)
                put(s, y, 1, label, attr)
                continue
            if kind == "empty":
                put(s, y, 4, "No events", curses.A_DIM)
                continue
            e = row[2]
            chosen = i == self.cursor
            base = curses.A_REVERSE if chosen else 0
            if chosen:
                put(s, y, 0, " " * w, base)
            faint = curses.A_DIM if e.declined or e.canceled else 0
            put(s, y, 3, "●", base | self.cal_attr(e.calendar))
            put(s, y, 5, f"{span_text(e, d):<11}", base | curses.A_DIM)
            put(s, y, 17, one_line(e.title), base | faint, title_width)
            if show_cal:
                c = self.calendar(e.calendar)
                put(s, y, cal_x, clip(c["title"] if c else "", cal_width),
                    base | self.cal_attr(e.calendar) | curses.A_DIM)
                if e.location and w - place_x >= 12:
                    put(s, y, place_x, one_line(e.location), base | curses.A_DIM, w - place_x - 1)
        if detail:
            self.draw_detail(rows, list_bottom, w)

    def draw_detail(self, rows, y, w):
        s = self.stdscr
        put(s, y, 0, "─" * w, curses.A_DIM)
        if not (0 <= self.cursor < len(rows)) or rows[self.cursor][0] != "event":
            return
        e = rows[self.cursor][2]
        today = date.today()
        title = one_line(e.title)
        if e.canceled:
            title += "  (canceled)"
        elif e.declined:
            title += "  (declined)"
        put(s, y + 1, 1, title, curses.A_BOLD, w - 2)
        c = self.calendar(e.calendar)
        bits = [format_when(e, today)]
        if duration_text(e):
            bits.append(duration_text(e))
        if e.recurring:
            bits.append("repeats")
        x = put(s, y + 2, 1, "  ·  ".join(bits) + "  ·  ", curses.A_DIM, w - 2) + 1
        put(s, y + 2, x, c["title"] if c else "", self.cal_attr(e.calendar), w - x - 1)
        lines = []
        if e.location:
            lines.append(("Where", one_line(e.location)))
        people = ", ".join(e.attendees)
        if e.organizer and e.organizer not in e.attendees:
            people = f"{e.organizer} (organizer)" + (f", {people}" if people else "")
        if people:
            lines.append(("Who", people))
        if e.url:
            lines.append(("Link", e.url))
        notes = " ⏎ ".join(line.strip() for line in e.notes.splitlines() if line.strip())
        if notes:
            lines.append(("Notes", notes))
        for k, (label, text) in enumerate(lines[:DETAIL_HEIGHT - 3]):
            put(s, y + 3 + k, 1, f"{label:<6}", curses.A_DIM)
            put(s, y + 3 + k, 8, text, 0, w - 9)

    def draw_footer(self, y, w):
        text, error, at = self.message
        if text and time.monotonic() - at < MESSAGE_SECONDS:
            attr = self.pair(curses.COLOR_RED) | curses.A_BOLD if error else curses.A_DIM
            put(self.stdscr, y, 1, text, attr, w - 2)
            return
        if self.view == "agenda":
            hints = "↑↓ event  [ ] month  t today  m w month/week  Esc back  g go to  c calendars  / search  ? help  q quit"
        else:
            hints = "←→ day  ↑↓ week  [ ] move  t today  m w a views  ⏎ agenda  g go to  c calendars  / search  ? help  q quit"
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
                lw = put(s, h - 1, 1, clip(label, max(8, w // 2)), curses.A_BOLD) + 2
                room = max(4, w - lw - 2)
                start = 0
                while text_width("".join(buf[start:pos])) > room - 1:
                    start += 1
                put(s, h - 1, lw, "".join(buf[start:]), 0, room)
                s.move(h - 1, min(w - 1, lw + text_width("".join(buf[start:pos]))))
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

    def pick_calendars(self):
        """Tick calendars on and off in a centred box; Esc or q closes it."""
        options = []                # (calendar or None for a source header, label)
        source = None
        for c in self.calendars:            # sorted by source, so each heads one run
            if c["source"] != source:
                source = c["source"]
                options.append((None, source or "Other"))
            options.append((c, c["title"]))
        picks = [i for i, (c, _) in enumerate(options) if c]
        if not picks:
            return
        s = self.stdscr
        index = picks[0]
        s.timeout(-1)
        try:
            while True:
                self.draw()
                h, w = s.getmaxyx()
                bw = min(w - 4, max(text_width(label) for _, label in options) + 10)
                bh = min(h - 2, len(options) + 4)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                inner = bh - 4
                first = max(0, min(index - inner // 2, len(options) - inner))
                put(s, y0, x0, "┌" + "─" * (bw - 2) + "┐")
                put(s, y0, x0 + 2, " Calendars ", curses.A_BOLD)
                for row in range(bh - 2):
                    put(s, y0 + 1 + row, x0, "│" + " " * (bw - 2) + "│")
                for row in range(inner):
                    i = first + row
                    if i >= len(options):
                        break
                    c, label = options[i]
                    y = y0 + 1 + row
                    if c is None:
                        put(s, y, x0 + 2, label, curses.A_DIM | curses.A_BOLD, bw - 4)
                        continue
                    attr = curses.A_REVERSE if i == index else 0
                    on = c["id"] not in self.hidden
                    put(s, y, x0 + 2, pad(f"   {'✓' if on else ' '} {label}", bw - 4), attr)
                    put(s, y, x0 + 3, "●" if on else "○", attr | self.pair(curses_color(c["color"])))
                put(s, y0 + bh - 2, x0 + 2, "space toggles · Esc closes", curses.A_DIM, bw - 4)
                put(s, y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘")
                s.refresh()
                try:
                    ch = s.get_wch()
                except curses.error:
                    continue
                at = picks.index(index)
                if ch in ("j", curses.KEY_DOWN):
                    index = picks[min(len(picks) - 1, at + 1)]
                elif ch in ("k", curses.KEY_UP):
                    index = picks[max(0, at - 1)]
                elif ch in (" ", "x", "\n", "\r", curses.KEY_ENTER):
                    self.hidden ^= {options[index][0]["id"]}
                    self.reindex()
                elif ch in ("\x1b", "q", "c"):
                    return
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

    # ---------------------------------------------------------- actions

    def go(self, d):
        self.day = d
        if self.view == "agenda":
            self.open_agenda()

    def open_agenda(self):
        """Show the agenda around the selected day, the cursor on its first event."""
        if self.view != "agenda":
            self.back = self.view
        self.view = "agenda"
        self.anchor = self.day
        self.ensure(*self.agenda_span())
        rows = self.agenda()
        picks = self.selectable(rows)
        later = [i for i in picks if rows[i][1] >= self.day]
        self.cursor = later[0] if later else (picks[-1] if picks else 0)
        # Start the list at the day's header, so the day is on top.
        self.top = next((i for i, row in enumerate(rows) if row[1] >= self.day), 0)

    def move_cursor(self, steps):
        rows = self.agenda()
        picks = self.selectable(rows)
        if not picks:
            return
        at = picks.index(self.cursor) if self.cursor in picks else 0
        self.cursor = picks[max(0, min(len(picks) - 1, at + steps))]
        self.day = rows[self.cursor][1]

    def goto(self):
        error = None
        text = ""
        while True:
            label = f"Go to ({error}):" if error else "Go to (fri, +2w, 25-12, dec, 2027-01-31):"
            text = self.prompt(label, text)
            if text is None or not text.strip():
                return
            d, error = parse_goto(text, date.today())
            if not error:
                self.go(d)
                return

    def search(self):
        """Filter as you type; Enter keeps the filter, Esc drops it."""
        def live(text):
            self.query = text.strip()
            self.reindex()

        if self.prompt("/", self.query, on_change=live) is None:
            self.query = ""
            self.reindex()
        if self.view == "agenda":
            self.open_agenda()

    def shift(self, step):
        """[ and ]: a month back or on (a week, in the week view)."""
        if self.view == "week":
            self.go(self.day + timedelta(days=7 * step))
        else:
            self.go(add_months(self.day, step))

    # ---------------------------------------------------------- loop

    def handle(self, ch):
        """Act on one key. Returns False to quit."""
        if ch in ("q", "Q"):
            return False
        if ch == curses.KEY_RESIZE:
            return True
        agenda = self.view == "agenda"
        page = max(1, self.stdscr.getmaxyx()[0] - DETAIL_HEIGHT - 4)

        if ch in ("j", curses.KEY_DOWN):
            self.move_cursor(1) if agenda else self.go(self.day + timedelta(days=7))
        elif ch in ("k", curses.KEY_UP):
            self.move_cursor(-1) if agenda else self.go(self.day - timedelta(days=7))
        elif ch in ("l", curses.KEY_RIGHT):
            if not agenda:
                self.go(self.day + timedelta(days=1))
        elif ch in ("h", curses.KEY_LEFT):
            if not agenda:
                self.go(self.day - timedelta(days=1))
        elif ch == curses.KEY_NPAGE:
            self.move_cursor(page) if agenda else self.shift(1)
        elif ch == curses.KEY_PPAGE:
            self.move_cursor(-page) if agenda else self.shift(-1)
        elif ch == curses.KEY_HOME and agenda:
            self.move_cursor(-len(self.agenda()))
        elif ch == curses.KEY_END and agenda:
            self.move_cursor(len(self.agenda()))
        elif ch == "]":
            self.shift(1)
        elif ch == "[":
            self.shift(-1)
        elif ch == "t":
            self.go(date.today())
        elif ch == "m":
            self.view = "month"
        elif ch == "w":
            self.view = "week"
        elif ch in ("a", "\n", "\r", curses.KEY_ENTER):
            if not agenda:
                self.open_agenda()
        elif ch == "\t":
            following = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
            if following == "agenda":
                self.open_agenda()
            else:
                self.view = following
        elif ch == "\x1b":
            if self.query:
                self.query = ""
                self.reindex()
            elif agenda:
                self.view = self.back
        elif ch == "g":
            self.goto()
        elif ch == "c":
            self.pick_calendars()
        elif ch == "/":
            self.search()
        elif ch == "r":
            if self.fetch(*load_range(*self.needed())):
                self.say("Reloaded")
        elif ch == "?":
            self.show_help()
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
                    self.fetch(*self.loaded, quiet=True)
                continue
            if not self.handle(ch):
                return


def load_range(lo, hi):
    """The range one fetch covers: whole months around [lo, hi)."""
    return (add_months(lo.replace(day=1), -LOAD_MONTHS_BEFORE),
            add_months((hi - timedelta(days=1)).replace(day=1), LOAD_MONTHS_AFTER + 1))


def run_curses(stdscr, helper, data, loaded):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
    App(stdscr, helper, data, loaded).run()


# ---------------------------------------------------------------- --once


def ansi_color(hex_color):
    if not hex_color:
        return ""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"\033[38;2;{r};{g};{b}m"


def print_once(data, start, days):
    today = date.today()
    colors = {c["id"]: c["color"] for c in data["calendars"]}
    by_day = index_by_day(to_event(raw) for raw in data["events"])
    rows = agenda_rows(by_day, start, start + timedelta(days=days), keep={today})
    for n, row in enumerate(rows):
        kind, d = row[0], row[1]
        if kind == "day":
            label = long_day(d, today)
            print(f"{chr(10) * bool(n)}{BOLD}{RED if d == today else ''}{'Today · ' if d == today else ''}{label}{RESET}")
        elif kind == "empty":
            print(f"  {DIM}No events{RESET}")
        else:
            e = row[2]
            title = one_line(e.title)
            if e.declined or e.canceled:
                title = f"{DIM}{title}{RESET}"
            where = f"  {DIM}{one_line(e.location)}{RESET}" if e.location else ""
            print(f"  {ansi_color(colors.get(e.calendar))}●{RESET} "
                  f"{DIM}{span_text(e, d):<11}{RESET} {title}{where}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Apple Calendar in the terminal.")
    parser.add_argument("--once", action="store_true", help="print the coming days and exit")
    parser.add_argument("--days", type=int, default=7, help="how many days --once prints (default 7)")
    parser.add_argument("--rebuild", action="store_true", help="recompile the EventKit helper")
    args = parser.parse_args()

    if sys.platform != "darwin":
        sys.exit("Apple Calendar lives on macOS only.")
    helper, error = build_helper(force=args.rebuild)
    if error:
        sys.exit(error)

    today = date.today()
    if args.once:
        days = max(1, args.days)
        data, error = call_helper(helper, today, today + timedelta(days=days))
        if error:
            sys.exit(f"Calendar: {error}")
        print_once(data, today, days)
        return

    weeks = month_weeks(today.year, today.month)
    loaded = load_range(weeks[0][0], weeks[-1][-1] + timedelta(days=1))
    data, error = call_helper(helper, *loaded)
    if error:
        sys.exit(f"Calendar: {error}")
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run_curses, helper, data, loaded)


if __name__ == "__main__":
    main()
