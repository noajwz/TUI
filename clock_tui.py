#!/usr/bin/env python3
"""Clock — an analog clock in the terminal, with the date around it (macOS).

The face is drawn in braille dots, eight to a character, so the circle is round and the hands are
fine lines at any window size; the second hand ticks or sweeps. Beside it: the date, the ISO
week number, the day of the year, the quarter, Unix time and UTC, the moon's phase and when the
next full moon is, sunrise and sunset where you are, how far through the day, week, month and
year it is, this month's calendar with week numbers, and the time in a few other places.

Where "where you are" comes from: --lat/--lon, else the city of the Mac's time zone, if it's one
of the few dozen it knows.
"""

import argparse
import calendar
import curses
import datetime as dt
import locale
import math
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PANEL_W = 36                   # the room the facts need beside the clock
SMOOTH_MS = 100                # redraw interval when the second hand sweeps
MIN_W, MIN_H = 60, 16
SYNODIC = 29.530588853         # days from new moon to new moon
NEW_MOON = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)
ZONES = ("UTC", "America/New_York", "Asia/Tokyo", "Australia/Sydney")
ROMAN = ("XII", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI")
NUMERALS = ("arabic", "roman", "quarters", "none")
# braille dot (column, row) inside a character -> its bit
DOTS = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20,
        (0, 3): 0x40, (1, 3): 0x80}
# what a dot belongs to; when several share a character the highest wins its colour
LAYERS = {"rim": 1, "tick": 2, "major": 3, "hour": 4, "minute": 5, "second": 6, "hub": 7}
THEMES = {  # layer -> (colour pair, bold)
    "classic": {"rim": (8, False), "tick": (8, False), "major": (7, True), "hour": (7, True),
                "minute": (7, True), "second": (1, True), "hub": (3, True), "numeral": (3, True)},
    "ocean": {"rim": (4, False), "tick": (4, False), "major": (6, True), "hour": (6, True),
              "minute": (6, True), "second": (3, True), "hub": (5, True), "numeral": (6, False)},
    "ember": {"rim": (1, False), "tick": (1, False), "major": (3, True), "hour": (3, True),
              "minute": (3, False), "second": (7, True), "hub": (1, True), "numeral": (3, False)},
    "mono": {k: (7, k in ("hour", "minute", "major", "hub")) for k in
             ("rim", "tick", "major", "hour", "minute", "second", "hub", "numeral")},
}
CITIES = {  # time zone -> (place, latitude, longitude), for sunrise and sunset
    "Europe/Amsterdam": ("Amsterdam", 52.37, 4.90), "Europe/Brussels": ("Brussels", 50.85, 4.35),
    "Europe/London": ("London", 51.51, -0.13), "Europe/Paris": ("Paris", 48.86, 2.35),
    "Europe/Berlin": ("Berlin", 52.52, 13.40), "Europe/Madrid": ("Madrid", 40.42, -3.70),
    "Europe/Rome": ("Rome", 41.90, 12.50), "Europe/Lisbon": ("Lisbon", 38.72, -9.14),
    "Europe/Dublin": ("Dublin", 53.35, -6.26), "Europe/Copenhagen": ("Copenhagen", 55.68, 12.57),
    "Europe/Stockholm": ("Stockholm", 59.33, 18.07), "Europe/Oslo": ("Oslo", 59.91, 10.75),
    "Europe/Helsinki": ("Helsinki", 60.17, 24.94), "Europe/Vienna": ("Vienna", 48.21, 16.37),
    "Europe/Zurich": ("Zürich", 47.38, 8.54), "Europe/Warsaw": ("Warsaw", 52.23, 21.01),
    "Europe/Prague": ("Prague", 50.08, 14.44), "Europe/Athens": ("Athens", 37.98, 23.73),
    "Europe/Istanbul": ("Istanbul", 41.01, 28.98), "Europe/Moscow": ("Moscow", 55.76, 37.62),
    "America/New_York": ("New York", 40.71, -74.01), "America/Chicago": ("Chicago", 41.88, -87.63),
    "America/Denver": ("Denver", 39.74, -104.99), "America/Los_Angeles": ("Los Angeles", 34.05, -118.24),
    "America/Toronto": ("Toronto", 43.65, -79.38), "America/Mexico_City": ("Mexico City", 19.43, -99.13),
    "America/Sao_Paulo": ("São Paulo", -23.55, -46.63), "Asia/Tokyo": ("Tokyo", 35.68, 139.69),
    "Asia/Shanghai": ("Shanghai", 31.23, 121.47), "Asia/Singapore": ("Singapore", 1.35, 103.82),
    "Asia/Kolkata": ("Kolkata", 22.57, 88.36), "Asia/Dubai": ("Dubai", 25.20, 55.27),
    "Australia/Sydney": ("Sydney", -33.87, 151.21), "Pacific/Auckland": ("Auckland", -36.85, 174.76),
    "Africa/Johannesburg": ("Johannesburg", -26.20, 28.05), "Africa/Cairo": ("Cairo", 30.04, 31.24),
}


# -- the dates and the sky --

def local_zone_name():
    """The Mac's time zone, as a name like Europe/Amsterdam, or None."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None


def moon(now):
    """(age in days, illuminated fraction, name, days to the next full moon, to the next new)."""
    age = ((now - NEW_MOON).total_seconds() / 86400) % SYNODIC
    p = age / SYNODIC
    lit = (1 - math.cos(2 * math.pi * p)) / 2
    names = ("new moon", "waxing crescent", "first quarter", "waxing gibbous", "full moon",
             "waning gibbous", "last quarter", "waning crescent")
    name = names[int((p * 8 + 0.5) % 8)]
    to_full = ((0.5 - p) % 1) * SYNODIC
    to_new = ((1 - p) % 1) * SYNODIC
    return age, lit, name, to_full, to_new


def sun_event(day, lat, lon, rising, zenith=90.833):
    """Sunrise or sunset that day, in hours UTC, or "up"/"down" when the sun doesn't cross the
    horizon (the polar summer and winter). The almanac algorithm: within a minute or two."""
    n = day.timetuple().tm_yday
    lng = lon / 15
    t = n + ((6 if rising else 18) - lng) / 24
    m = 0.9856 * t - 3.289
    rad = math.radians
    lon_sun = (m + 1.916 * math.sin(rad(m)) + 0.020 * math.sin(rad(2 * m)) + 282.634) % 360
    ra = math.degrees(math.atan(0.91764 * math.tan(rad(lon_sun)))) % 360
    ra += (lon_sun // 90) * 90 - (ra // 90) * 90
    ra /= 15
    sin_dec = 0.39782 * math.sin(rad(lon_sun))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(rad(zenith)) - sin_dec * math.sin(rad(lat))) / (cos_dec * math.cos(rad(lat)))
    if cos_h > 1:
        return "down"
    if cos_h < -1:
        return "up"
    h = (360 - math.degrees(math.acos(cos_h)) if rising else math.degrees(math.acos(cos_h))) / 15
    return (h + ra - 0.06571 * t - 6.622 - lng) % 24


def sun_times(day, lat, lon, tz):
    """(sunrise, sunset) as local datetimes, or strings when there is none."""
    out = []
    for rising in (True, False):
        ut = sun_event(day, lat, lon, rising)
        if isinstance(ut, str):
            out.append(ut)
            continue
        base = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc) + dt.timedelta(hours=ut)
        local = base.astimezone(tz)
        if local.date() != day:                 # the UTC hour belongs to the neighbouring day
            local += dt.timedelta(days=(day - local.date()).days)
        out.append(local)
    return tuple(out)


def span(seconds):
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d} d {h} h"
    return f"{h} h {m:02d} m" if h else f"{m} m"


def progress(now):
    """How far through the day, week, month and year: [(label, fraction)]."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = (now - midnight).total_seconds() / 86400
    week = (now.weekday() + day) / 7
    days = calendar.monthrange(now.year, now.month)[1]
    month = (now.day - 1 + day) / days
    ylen = 366 if calendar.isleap(now.year) else 365
    year = (now.timetuple().tm_yday - 1 + day) / ylen
    return [("day", day), ("week", week), ("month", month), ("year", year)]


# -- the face --

class Canvas:
    """Braille dots: each character cell is 2 dots wide and 4 tall."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.w, self.h = cols * 2, rows * 4
        self.px = {}

    def dot(self, x, y, layer):
        x, y = int(round(x)), int(round(y))
        if 0 <= x < self.w and 0 <= y < self.h and LAYERS[layer] > self.px.get((x, y), (0,))[0]:
            self.px[(x, y)] = (LAYERS[layer], layer)

    def line(self, x0, y0, x1, y1, layer):
        n = int(max(abs(x1 - x0), abs(y1 - y0)) * 1.5) + 1
        for k in range(n + 1):
            t = k / n
            self.dot(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, layer)

    def cells(self):
        """{(col, row): (braille character, layer of its most important dot)}."""
        out = {}
        for (x, y), (pri, layer) in self.px.items():
            key = (x // 2, y // 4)
            bits, best, top = out.get(key, (0, 0, None))
            bits |= DOTS[(x % 2, y % 4)]
            if pri > best:
                best, top = pri, layer
            out[key] = (bits, best, top)
        return {k: (chr(0x2800 + bits), layer) for k, (bits, _, layer) in out.items()}


def draw_face(cols, rows, now, numerals, smooth):
    """The clock as {(col, row): (text, layer)} for a box of cols × rows characters."""
    cv = Canvas(cols, rows)
    cx, cy = cv.w / 2 - 0.5, cv.h / 2 - 0.5
    r = min(cv.w, cv.h) / 2 - 1.5

    def at(angle, radius):
        return cx + math.sin(angle) * radius, cy - math.cos(angle) * radius

    for k in range(int(2 * math.pi * r * 1.5)):
        cv.dot(*at(2 * math.pi * k / (2 * math.pi * r * 1.5), r), "rim")
    for m in range(60):
        a = 2 * math.pi * m / 60
        if m % 5:
            cv.dot(*at(a, r - 2.5), "tick")
        else:
            inner = r - 2.5 - max(2.0, r * (0.14 if m % 15 == 0 else 0.09))
            cv.line(*at(a, r - 2.5), *at(a, inner), "major")
            if m % 15 == 0:                     # the quarters a little heavier
                px, py = math.cos(a), math.sin(a)
                x0, y0 = at(a, r - 2.5)
                x1, y1 = at(a, inner)
                cv.line(x0 + px, y0 + py, x1 + px, y1 + py, "major")
    sec = now.second + (now.microsecond / 1e6 if smooth else 0)
    minute = now.minute + sec / 60
    hour = (now.hour % 12) + minute / 60

    def hand(fraction, length, tail, layer, width=0.0):
        a = 2 * math.pi * fraction
        x0, y0 = at(a + math.pi, tail)
        x1, y1 = at(a, length)
        cv.line(x0, y0, x1, y1, layer)
        if width:
            px, py = math.cos(a) * width, math.sin(a) * width
            cv.line(x0 + px, y0 + py, x1 + px * 0.4, y1 + py * 0.4, layer)
            cv.line(x0 - px, y0 - py, x1 - px * 0.4, y1 - py * 0.4, layer)

    hand(hour / 12, r * 0.50, r * 0.08, "hour", width=0.9)
    hand(minute / 60, r * 0.78, r * 0.10, "minute", width=0.5)
    hand(sec / 60, r * 0.86, r * 0.18, "second")
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            cv.dot(cx + dx, cy + dy, "hub")
    out = cv.cells()
    if numerals != "none" and r > 14:
        for k in range(12):
            if numerals == "quarters" and k % 3:
                continue
            text = ROMAN[k] if numerals == "roman" else str(k or 12)
            x, y = at(2 * math.pi * k / 12, r * (0.70 if r > 24 else 0.66))
            col, row = int(round(x / 2 - len(text) / 2 + 0.25)), int(y // 4)
            for i, ch in enumerate(text):
                key = (col + i, row)
                if out.get(key, ("", "rim"))[1] not in ("hour", "minute", "second", "hub"):
                    out[key] = (ch, "numeral")
    return out


# -- the window --

class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.args = args
        self.smooth = args.smooth
        self.numerals = args.numerals
        self.theme = args.theme
        self.h24 = not args.twelve
        self.month_shift = 0
        self.zone_name = local_zone_name()
        self.place = None
        if args.lat is not None and args.lon is not None:
            self.place = (args.place or f"{args.lat:.2f}, {args.lon:.2f}", args.lat, args.lon)
        elif self.zone_name in CITIES:
            self.place = CITIES[self.zone_name]
        self.zones = []
        for name in args.zones or ZONES:
            try:
                self.zones.append((name, ZoneInfo(name)))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        self.face_zone = None                   # None: the clock shows local time
        self.sun_cache = None

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def attr(self, layer):
        pair, bold = THEMES[self.theme][layer]
        return curses.color_pair(pair) | (curses.A_BOLD if bold else 0)

    def now(self):
        local = dt.datetime.now().astimezone()
        if self.face_zone is not None:
            return local, local.astimezone(self.zones[self.face_zone][1])
        return local, local

    def clock_text(self, t, seconds=True):
        if self.h24:
            return t.strftime("%H:%M:%S" if seconds else "%H:%M")
        text = t.strftime("%I:%M:%S %p" if seconds else "%I:%M %p")
        return text[1:] if text.startswith("0") else text

    def sun(self, now):
        if not self.place:
            return None
        key = now.date()
        if not self.sun_cache or self.sun_cache[0] != key:
            name, lat, lon = self.place
            self.sun_cache = (key, sun_times(key, lat, lon, now.tzinfo))
        return self.sun_cache[1]

    # -- the panel --

    def sections(self, now, width):
        """The panel, as sections of [(text, attr)], most wanted first."""
        c = curses.color_pair
        iso = now.isocalendar()
        doy = now.timetuple().tm_yday
        ylen = 366 if calendar.isleap(now.year) else 365
        offset = now.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}"
        dst = time.localtime().tm_isdst > 0
        utc = now.astimezone(dt.timezone.utc)
        first = now.replace(day=1)
        week_of_month = (now.day + first.weekday() - 1) // 7 + 1
        head = [(self.clock_text(now), curses.A_BOLD | c(7)),
                (now.strftime("%A %-d %B %Y"), c(6) | curses.A_BOLD),
                (f"week {iso.week} · {iso.year}-W{iso.week:02d}-{iso.weekday}", c(3) | curses.A_BOLD),
                (f"day {doy} of {ylen} · {ylen - doy} to go", 0),
                (f"Q{(now.month - 1) // 3 + 1} · week {week_of_month} of {now.strftime('%B')}", 0),
                (f"{now.tzname()} UTC{offset}{' summer time' if dst else ''} · UTC {utc.strftime('%H:%M')}", c(8)),
                (f"Unix {int(now.timestamp()):,}".replace(",", " "), c(8))]
        age, lit, name, to_full, to_new = moon(now)
        sky = [(f"moon  {name}, {lit * 100:.0f}% lit", c(7))]
        if to_full < to_new:
            when = f"full {(now + dt.timedelta(days=to_full)).strftime('%a %-d %b')}, in {span(to_full * 86400)}"
        else:
            when = f"new {(now + dt.timedelta(days=to_new)).strftime('%a %-d %b')}, in {span(to_new * 86400)}"
        sky.append((f"      {when}", c(8)))
        sun = self.sun(now)
        if sun:
            rise, set_ = sun
            where = self.place[0]
            if isinstance(rise, str) or isinstance(set_, str):
                sky.append((f"sun   {'up all day' if 'up' in (rise, set_) else 'down all day'} in {where}", c(3)))
            else:
                length = (set_ - rise).total_seconds()
                state = "up" if rise <= now <= set_ else "down"
                sky.append((f"sun   ↑ {self.clock_text(rise, False)}  ↓ {self.clock_text(set_, False)}  · {span(length)}",
                            c(3)))
                if state == "up":
                    tail = f"sets in {span((set_ - now).total_seconds())}"
                elif now < rise:
                    tail = f"rises in {span((rise - now).total_seconds())}"
                else:
                    tail = "set for today"
                sky.append((f"      {where} · {tail}", c(8)))
        bars = []
        barw = max(10, min(30, width - 14))
        for label, f in progress(now):
            filled = int(f * barw)
            bars.append((f"{label:<6}{'█' * filled}{'░' * (barw - filled)} {f * 100:5.1f}%", c(2)))
        cal = self.calendar_lines(now)
        world = []
        for zname, tz in self.zones:
            t = now.astimezone(tz)
            diff = (t.utcoffset() - now.utcoffset()).total_seconds() / 3600
            rel = "same" if not diff else f"{diff:+g} h"      # the weekday shows a different day
            label = zname.split("/")[-1].replace("_", " ")
            mark = "▸ " if self.face_zone is not None and self.zones[self.face_zone][0] == zname else "  "
            world.append((f"{mark}{label:<11}{self.clock_text(t, False):>8} {t.strftime('%a')} {rel}", c(7)))
        return [head, sky, bars, cal, world]

    def calendar_lines(self, now):
        c = curses.color_pair
        y, m = now.year, now.month + self.month_shift
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        lines = [(f"{calendar.month_name[m]} {y}".center(25), c(6) | curses.A_BOLD), ("wk  Mo Tu We Th Fr Sa Su", c(8))]
        for week in calendar.Calendar(0).monthdatescalendar(y, m):
            cells = []
            for d in week:
                cells.append("  " if d.month != m else f"{d.day:>2}")
            lines.append((f"{week[0].isocalendar().week:>2}  " + " ".join(cells),
                          [(d == now.date()) for d in week]))
        return lines

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        local, face_time = self.now()
        rows = h - 2
        cols = min(rows * 2, w - 2)
        if w - cols - 4 < PANEL_W:              # make the clock smaller so the panel fits beside it
            cols = (w - 4 - PANEL_W) // 2 * 2
            rows = cols // 2
        panel_x = cols + 4
        if cols < 24:                           # too narrow for both: the clock alone, centred
            cols, rows = min((h - 2) * 2, w - 2), h - 2
            panel_x = None
        x0 = 1 if panel_x else (w - cols) // 2
        y_face = 1 + (h - 2 - rows) // 2
        for (cx, cy), (ch, layer) in draw_face(cols, rows, face_time, self.numerals, self.smooth).items():
            self.put(y_face + cy, x0 + cx, ch, self.attr(layer))
        if self.face_zone is not None:
            label = self.zones[self.face_zone][0].split("/")[-1].replace("_", " ")
            self.put(h - 1, x0 + max(0, (cols - len(label)) // 2), label, curses.color_pair(6) | curses.A_BOLD)
        if panel_x is not None:
            self.draw_panel(1, panel_x, w - panel_x - 1, h - 2, local)
        hint = "? keys"
        self.put(h - 1, w - len(hint) - 1, hint, curses.color_pair(8))
        s.refresh()

    def draw_panel(self, y0, x0, width, height, now):
        y = y0
        for n, section in enumerate(self.sections(now, width)):
            if y + len(section) > y0 + height and n > 0:
                continue                        # a section that doesn't fit is left out whole
            for text, attr in section:
                if y >= y0 + height:
                    break
                if isinstance(attr, list):      # a calendar week: today stands out
                    self.put(y, x0, text[:4], curses.color_pair(8))
                    for i, today in enumerate(attr):
                        cell = text[4 + i * 3:6 + i * 3]
                        style = curses.A_REVERSE | curses.A_BOLD | curses.color_pair(3) if today else \
                            curses.color_pair(1) if i >= 5 else 0
                        self.put(y, x0 + 4 + i * 3, cell, style)
                else:
                    self.put(y, x0, text[:width], attr)
                y += 1
            y += 1

    def help(self):
        lines = ["Clock", "",
                 "  s       second hand: tick or sweep",
                 "  n       numerals: arabic, roman, quarters, none",
                 "  c       colours: " + ", ".join(THEMES),
                 "  d       12 or 24 hours",
                 "  z       show another of the world clocks on the face (and back to local)",
                 "  ← →     the calendar a month back or on;  t  back to this month",
                 "  q       quit", "",
                 "Sunrise and sunset are for " + (self.place[0] if self.place else "nowhere yet — give --lat and --lon"),
                 "", "any key to close"]
        self.s.timeout(-1)
        self.s.erase()
        for n, line in enumerate(lines):
            self.put(1 + n, 2, line, curses.A_BOLD if n == 0 else 0)
        self.s.refresh()
        self.s.getch()

    def handle(self, ch):
        if ch in (ord("q"), ord("Q"), 27):
            return False
        if ch == ord("s"):
            self.smooth = not self.smooth
        elif ch == ord("n"):
            self.numerals = NUMERALS[(NUMERALS.index(self.numerals) + 1) % len(NUMERALS)]
        elif ch == ord("c"):
            names = list(THEMES)
            self.theme = names[(names.index(self.theme) + 1) % len(names)]
        elif ch == ord("d"):
            self.h24 = not self.h24
        elif ch == ord("z") and self.zones:
            self.face_zone = 0 if self.face_zone is None else self.face_zone + 1
            if self.face_zone >= len(self.zones):
                self.face_zone = None
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("]")):
            self.month_shift += 1
        elif ch in (curses.KEY_LEFT, ord("h"), ord("[")):
            self.month_shift -= 1
        elif ch == ord("t"):
            self.month_shift = 0
        elif ch == ord("?"):
            self.help()
        return True

    def wait_ms(self):
        """Until the next redraw: the next whole second when ticking, so the hand moves on time."""
        if self.smooth:
            return SMOOTH_MS
        frac = time.time() % 1
        return max(10, int((1 - frac) * 1000) + 5)


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    stdscr.keypad(True)
    app = App(stdscr, args)
    while True:
        app.draw()
        stdscr.timeout(app.wait_ms())
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            return


def print_once(args, rows=12):
    """The face and the facts as text, for a pipe or a quick look."""
    now = dt.datetime.now().astimezone()
    face = draw_face(rows * 2, rows, now, args.numerals if rows >= 12 else "none", False)
    grid = [[" "] * (rows * 2) for _ in range(rows)]
    for (x, y), (ch, _) in face.items():
        if 0 <= y < rows and 0 <= x < rows * 2:
            grid[y][x] = ch
    iso = now.isocalendar()
    doy = now.timetuple().tm_yday
    ylen = 366 if calendar.isleap(now.year) else 365
    age, lit, name, to_full, _ = moon(now)
    facts = [now.strftime("%H:%M:%S"), now.strftime("%A %-d %B %Y"),
             f"ISO week {iso.week} ({iso.year}-W{iso.week:02d}-{iso.weekday})",
             f"day {doy} of {ylen}, {ylen - doy} to go, Q{(now.month - 1) // 3 + 1}",
             f"{now.tzname()} UTC{now.strftime('%z')}", f"moon: {name}, {lit * 100:.0f}% lit",
             f"full moon in {span(to_full * 86400)}"]
    zone = local_zone_name()
    place = (args.place or "here", args.lat, args.lon) if args.lat is not None and args.lon is not None else CITIES.get(zone)
    if place:
        rise, set_ = sun_times(now.date(), place[1], place[2], now.tzinfo)
        if not isinstance(rise, str) and not isinstance(set_, str):
            facts.append(f"sun in {place[0]}: up {rise:%H:%M}, down {set_:%H:%M}")
    for label, f in progress(now):
        facts.append(f"{label:<6}{f * 100:5.1f}% through")
    for y in range(rows):
        print("".join(grid[y]) + "   " + (facts[y] if y < len(facts) else ""))
    for extra in facts[rows:]:
        print(" " * (rows * 2 + 3) + extra)


def main():
    parser = argparse.ArgumentParser(
        description="An analog clock with the date, ISO week, moon, sunrise and sunset, and world clocks.")
    parser.add_argument("-s", "--smooth", action="store_true", help="a sweeping second hand instead of a ticking one")
    parser.add_argument("-n", "--numerals", choices=NUMERALS, default="arabic")
    parser.add_argument("-c", "--theme", choices=THEMES, default="classic")
    parser.add_argument("--twelve", action="store_true", help="12-hour times (AM/PM) in the panel")
    parser.add_argument("--lat", type=float, help="latitude for sunrise and sunset (north is positive)")
    parser.add_argument("--lon", type=float, help="longitude (east is positive)")
    parser.add_argument("--place", help="what to call that place")
    parser.add_argument("-z", "--zone", dest="zones", action="append", metavar="ZONE",
                        help="a world clock, like Asia/Tokyo; may be repeated (default: " + ", ".join(ZONES) + ")")
    parser.add_argument("--once", action="store_true", help="print the clock and the facts, without the window")
    args = parser.parse_args()
    if (args.lat is None) != (args.lon is None):
        parser.error("give both --lat and --lon")
    if args.once:
        print_once(args)
        return
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
