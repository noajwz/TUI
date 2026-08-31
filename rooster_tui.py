#!/usr/bin/env python3
"""Terminal UI for a work roster published as an .ics subscription feed.

The feed republishes itself — shifts appear, move and disappear — so this
re-fetches on a timer rather than showing a snapshot.

Times are converted to the local zone on purpose.  The publisher moves its UTC
stamps across a daylight-saving boundary so that local times stay put: a 06:00Z
and an 07:00Z morning shift on consecutive days are both 08:00 locally.  Reading
the UTC values straight would show the roster drifting an hour every October.
"""

import argparse
import curses
import locale
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

USER_AGENT = "rooster-tui/1.0"
NETWORK_TIMEOUT = 10
REFRESH_SECONDS = 15 * 60   # the feed advertises X-PUBLISHED-TTL:PT1H

URL_ENV = "ESS_ICS_URL"
URL_FILES = (
    "~/.ess_calendar_url",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ess_url"),
)

DAYS_NL = ("ma", "di", "wo", "do", "vr", "za", "zo")
MONTHS_NL = ("Januari", "Februari", "Maart", "April", "Mei", "Juni",
             "Juli", "Augustus", "September", "Oktober", "November", "December")

RULE_WIDTH = 46
DURATION_RE = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


# ---------------------------------------------------------------- config


def read_url_file(path):
    """First meaningful line of a file, or None. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        return None
    return None


def read_url(explicit=None):
    """Return (url, error).

    The URL is a bearer token in disguise — anyone holding it can read the
    roster — so it lives outside the repository rather than in this file.
    """
    if explicit:
        return explicit.strip(), None
    from_env = os.environ.get(URL_ENV, "").strip()
    if from_env:
        return from_env, None
    for path in URL_FILES:
        url = read_url_file(os.path.expanduser(path))
        if url:
            return url, None
    return None, (
        f"No calendar URL configured. Put the .ics URL in {URL_FILES[0]}, "
        f"or set ${URL_ENV}, or pass --url."
    )


# ---------------------------------------------------------------- fetching


def fetch_ics(url):
    """Return (text, error). A tuple rather than an exception, like the siblings.

    file:// URLs go through the same path, which is what lets the whole program
    run against a saved fixture with no network.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except (TimeoutError, OSError, ValueError) as e:
        return None, str(e)
    return raw.decode("utf-8", "replace"), None


# ---------------------------------------------------------------- parsing


def unfold(text):
    """Logical .ics lines: one starting with a space or tab continues the previous."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def split_line(line):
    """'NAME;PARAM=X:value' to ('NAME', {'PARAM': 'X'}, 'value'), or None.

    The value may itself contain colons, and a parameter may be quoted, so the
    split is on the first colon outside quotes rather than on the first colon.
    """
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None

    fields, field, quoted = [], [], False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            fields.append("".join(field))
            field = []
        else:
            field.append(ch)
    fields.append("".join(field))

    params = {}
    for item in fields[1:]:
        key, sep, val = item.partition("=")
        if sep:
            params[key.upper()] = val
    return fields[0].upper(), params, value


def unescape(value):
    r"""Undo .ics text escaping: \n \, \; \\ ."""
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse_duration(value):
    """An ISO-8601 duration (P1DT8H30M) as a timedelta."""
    m = DURATION_RE.match(value.strip().upper())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups()[1:])
    span = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -span if m.group(1) == "-" else span


def parse_dt(value, params=None):
    """An .ics date-time as an aware *local* datetime, plus whether it is all-day.

    Naive values are read as local, which is what .astimezone() does with them.
    """
    params = params or {}
    value = value.strip()

    if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
        return datetime.strptime(value, "%Y%m%d").astimezone(), True

    moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=timezone.utc).astimezone(), False

    tzid = params.get("TZID", "").strip('"')
    if tzid:
        try:
            return moment.replace(tzinfo=ZoneInfo(tzid)).astimezone(), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return moment.astimezone(), False


class Shift:
    """One VEVENT: a shift code and the local-time span it covers."""

    __slots__ = ("uid", "code", "start", "end", "all_day")

    def __init__(self, uid, code, start, end, all_day=False):
        self.uid = uid
        self.code = code
        self.start = start
        self.end = end
        self.all_day = all_day

    @property
    def hours(self):
        return (self.end - self.start).total_seconds() / 3600

    @property
    def crosses_midnight(self):
        return self.end.date() != self.start.date()

    def __repr__(self):
        return f"<Shift {self.code} {self.start:%Y-%m-%d %H:%M}-{self.end:%H:%M}>"


def build_shift(fields):
    """One collected VEVENT to a Shift, or None when it is unusable."""
    if "DTSTART" not in fields:
        return None
    try:
        start, all_day = parse_dt(fields["DTSTART"][1], fields["DTSTART"][0])
        if "DTEND" in fields:
            end, _ = parse_dt(fields["DTEND"][1], fields["DTEND"][0])
        elif "DURATION" in fields:
            end = start + parse_duration(fields["DURATION"][1])
        else:
            end = start + timedelta(days=1) if all_day else start
    except (ValueError, OverflowError, OSError):
        return None
    if end < start:
        end = start
    code = unescape(fields.get("SUMMARY", ({}, ""))[1]).strip() or "?"
    return Shift(fields.get("UID", ({}, ""))[1].strip(), code, start, end, all_day)


def parse_events(text):
    """Every VEVENT in the feed as a Shift, sorted by start time.

    A block that does not parse is skipped rather than fatal: this feed changes
    shape without warning, and one bad event must not blank the whole roster.
    """
    shifts, fields = [], None
    for line in unfold(text):
        parsed = split_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            fields = {}
        elif fields is None:
            continue
        elif name == "END" and value.strip().upper() == "VEVENT":
            shift = build_shift(fields)
            if shift is not None:
                shifts.append(shift)
            fields = None
        else:
            fields[name] = (params, value)
    shifts.sort(key=lambda s: (s.start, s.code))
    return shifts


# ---------------------------------------------------------------- layout


class Row:
    """One line of the rendered roster: 'month', 'rule', 'gap', 'blank' or 'shift'."""

    __slots__ = ("kind", "text", "shift", "today")

    def __init__(self, kind, text="", shift=None, today=False):
        self.kind = kind
        self.text = text
        self.shift = shift
        self.today = today


def format_shift(shift):
    when = "hele dag" if shift.all_day else f"{shift.start:%H:%M}-{shift.end:%H:%M}"
    plus = " +1" if shift.crosses_midnight and not shift.all_day else "   "
    return (f"{DAYS_NL[shift.start.weekday()]} {shift.start:%d-%m}   "
            f"{when:<11}{plus}   {shift.hours:>4.1f}u   {shift.code}")


def build_rows(shifts, today=None):
    """Shifts to display rows, grouped by month with runs of free days collapsed."""
    today = today or date.today()
    rows, month, previous = [], None, None

    for shift in shifts:
        day = shift.start.date()
        if (day.year, day.month) != month:
            month = (day.year, day.month)
            if rows:
                rows.append(Row("blank"))
            rows.append(Row("month", f"{MONTHS_NL[day.month - 1]} {day.year}"))
            rows.append(Row("rule", "-" * RULE_WIDTH))
            previous = None
        elif previous is not None:
            free = (day - previous).days - 1
            if free > 0:
                rows.append(Row("gap", f"  {free} dag{'en' if free > 1 else ''} vrij"))
        previous = day
        rows.append(Row("shift", format_shift(shift), shift, day == today))

    return rows


def today_index(rows, today=None):
    """Row to scroll to: the first shift that has not happened yet."""
    today = today or date.today()
    for i, row in enumerate(rows):
        if row.kind == "shift" and row.shift.start.date() >= today:
            return max(0, i - 2)
    return 0


def summary(shifts):
    hours = sum(s.hours for s in shifts if not s.all_day)
    return f"{len(shifts)} diensten - {hours:.1f} uur"


def fmt_age(when, now=None):
    if not when:
        return "nog niet"
    seconds = int(max(0, (now or time.time()) - when))
    if seconds < 60:
        return "zojuist"
    if seconds < 3600:
        return f"{seconds // 60} min geleden"
    return f"{seconds // 3600} uur geleden"


# ---------------------------------------------------------------- curses UI

TITLE_PAIR, TODAY_PAIR, WEEKEND_PAIR, DIM_PAIR, ALERT_PAIR = 1, 2, 3, 4, 5

KEYS = "q afsluiten   r verversen   t vandaag   jk/pgup/pgdn/gG bladeren"


def main(stdscr, url, refresh_seconds=REFRESH_SECONDS):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(TITLE_PAIR, curses.COLOR_CYAN, -1)      # title, month headings
    curses.init_pair(TODAY_PAIR, curses.COLOR_GREEN, -1)     # today's shift
    curses.init_pair(WEEKEND_PAIR, curses.COLOR_YELLOW, -1)  # saturday, sunday
    curses.init_pair(DIM_PAIR, curses.COLOR_WHITE, -1)       # chrome
    curses.init_pair(ALERT_PAIR, curses.COLOR_RED, -1)       # fetch failed

    stdscr.timeout(1000)

    shifts, rows, error = [], [], None
    last_fetch, top = 0.0, 0
    busy, jump = True, True   # fetch on the first pass, then land on today

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, text, attr=0, h=h, w=w):
            if 0 <= y < h and 0 <= x < w - 1:
                stdscr.addnstr(y, x, text, w - x - 1, attr)

        title = "ESS rooster"
        put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(TITLE_PAIR) | curses.A_BOLD)

        body_h = max(1, h - 4)
        top = max(0, min(top, max(0, len(rows) - body_h)))

        if rows:
            for i, row in enumerate(rows[top:top + body_h]):
                y = 2 + i
                if row.kind == "month":
                    put(y, 2, row.text, curses.color_pair(TITLE_PAIR) | curses.A_BOLD)
                elif row.kind in ("rule", "gap"):
                    put(y, 2, row.text, curses.color_pair(DIM_PAIR) | curses.A_DIM)
                elif row.kind == "shift":
                    if row.today:
                        attr = curses.color_pair(TODAY_PAIR) | curses.A_BOLD
                    elif row.shift.start.weekday() >= 5:
                        attr = curses.color_pair(WEEKEND_PAIR)
                    else:
                        attr = curses.A_NORMAL
                    if row.today:
                        put(y, 0, ">", curses.color_pair(TODAY_PAIR) | curses.A_BOLD)
                    put(y, 2, row.text, attr)
        elif busy:
            put(2, 2, "Laden...", curses.color_pair(DIM_PAIR))
        elif error:
            put(2, 2, error, curses.color_pair(ALERT_PAIR))
        else:
            put(2, 2, "Geen diensten in de agenda.", curses.color_pair(DIM_PAIR))

        totals = summary(shifts) if shifts else ""
        if busy:
            note, attr = "verversen...", curses.color_pair(DIM_PAIR)
        elif error:
            note, attr = error, curses.color_pair(ALERT_PAIR)
        else:
            note, attr = f"bijgewerkt {fmt_age(last_fetch)}", curses.color_pair(DIM_PAIR)

        # Totals on the left, status on the right, sharing one line. A fetch error is
        # far longer than the window, so it gets trimmed rather than allowed to run
        # back over the totals; on a narrow window the status wins and the totals go.
        room = w - 6 - len(totals)
        if room < 12:
            totals, room = "", w - 6
        if len(note) > room:
            note = note[:max(0, room - 3)] + "..."
        if totals:
            put(h - 2, 2, totals, curses.color_pair(DIM_PAIR))
        put(h - 2, max(0, w - len(note) - 3), note, attr)
        put(h - 1, 2, KEYS, curses.color_pair(DIM_PAIR))

        stdscr.refresh()

        if busy:
            # The frame just painted says "verversen..."; now block on the network.
            # A failed fetch keeps the roster already on screen and reports in the
            # footer, so a dead network leaves something readable rather than exiting.
            text, err = fetch_ics(url)
            busy = False
            last_fetch = time.time()
            error = err
            if not err:
                shifts = parse_events(text)
                rows = build_rows(shifts)
                if jump:
                    top, jump = today_index(rows), False
            continue

        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            break
        if key in (ord("r"), ord("R")):
            busy = True
        elif key in (curses.KEY_DOWN, ord("j")):
            top += 1
        elif key in (curses.KEY_UP, ord("k")):
            top -= 1
        elif key == curses.KEY_NPAGE:
            top += body_h
        elif key == curses.KEY_PPAGE:
            top -= body_h
        elif key in (ord("g"), curses.KEY_HOME):
            top = 0
        elif key in (ord("G"), curses.KEY_END):
            top = len(rows)
        elif key in (ord("t"), ord("T")):
            top = today_index(rows)
        elif key == -1 and time.time() - last_fetch >= refresh_seconds:
            busy = True


# ---------------------------------------------------------------- one-shot

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def print_once(url, color=True):
    """Plain dump for pipes and 'just show me the roster'."""
    def paint(text, code):
        return f"{code}{text}{RESET}" if code and color else text

    text, error = fetch_ics(url)
    if error:
        print(paint(f"Rooster: {error}", RED), file=sys.stderr)
        return 1

    shifts = parse_events(text)
    if not shifts:
        print("Rooster: geen diensten in de agenda.")
        return 0

    print(paint("ESS rooster", CYAN + BOLD))
    for row in build_rows(shifts):
        if row.kind == "blank":
            print()
        elif row.kind == "month":
            print("  " + paint(row.text, CYAN + BOLD))
        elif row.kind in ("rule", "gap"):
            print("  " + paint(row.text, DIM))
        elif row.kind == "shift":
            if row.today:
                code = GREEN + BOLD
            elif row.shift.start.weekday() >= 5:
                code = YELLOW
            else:
                code = ""
            print(("> " if row.today else "  ") + paint(row.text, code))
    print("\n  " + paint(summary(shifts), DIM))
    return 0


def cli():
    parser = argparse.ArgumentParser(description="Show an .ics shift roster in the terminal.")
    parser.add_argument("--url", help=f"the .ics feed (default: ${URL_ENV}, or {URL_FILES[0]})")
    parser.add_argument("--once", action="store_true",
                        help="print the roster once and exit instead of running the TUI")
    parser.add_argument("--no-color", action="store_true", help="plain text output with --once")
    parser.add_argument("--refresh", type=int, default=REFRESH_SECONDS, metavar="SECONDS",
                        help=f"how often the TUI re-fetches (default: {REFRESH_SECONDS})")
    args = parser.parse_args()

    url, error = read_url(args.url)
    if error:
        print(error, file=sys.stderr)
        return 1

    if args.once:
        return print_once(url, color=not args.no_color and sys.stdout.isatty())

    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(main, url, max(30, args.refresh))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
