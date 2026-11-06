#!/usr/bin/env python3
"""Every US presidential election, 1788-2024, with the electoral map of each. All data is embedded."""
import argparse
import curses
import locale
import shutil
import sys
import textwrap
from collections import namedtuple
from functools import lru_cache

MAP_W, MAP_H = 939, 591            # size of the coordinate space the state shapes are drawn in
PANEL_MIN, PANEL_MAX = 36, 60                  # results panel beside the map
SIDE_BY_SIDE_MIN = 90                          # narrower windows stack the panel under the map
MIN_W, MIN_H = 50, 16
CALLOUT_W = 6
NOTE_MIN_ROWS = 2

NOT_A_STATE = 237                              # xterm-256 colours throughout; basic() maps them down
NO_VOTE = 245
TIMELINE_NOW = 15

PARTY_COLOURS = {
    "Democratic": 33, "Republican": 160, "Federalist": 173, "Democratic-Republican": 71, "Whig": 179,
    "National Republican": 180, "Anti-Masonic": 133, "Nullifier": 96, "Free Soil": 107, "Know Nothing": 136,
    "Southern Democratic": 98, "Northern Democratic": 33, "Constitutional Union": 179, "National Union": 160,
    "Liberal Republican": 74, "Populist": 142, "Progressive": 35, "Dixiecrat": 172, "Unpledged Democratic": 172,
    "American Independent": 172, "Libertarian": 179, "Green": 71, "Reform": 133, "Independent": 141,
    "Anti-Federalist": 71,
}
SPARE_COLOURS = [179, 71, 133, 74, 173, 142, 168, 109, 215, 139, 65, 101, 96, 180]     # when a party recurs in one race

# Before the 12th Amendment each elector cast two votes, so the map shows only the two real rivals
MAP_RIVALS = {1796: ("John Adams", "Thomas Jefferson"), 1800: ("Thomas Jefferson", "John Adams")}
TWO_VOTE_YEARS = (1788, 1792, 1796, 1800)

# Land that belonged to another state, painted as that state until the year given (modern borders throughout)
PARENT_UNTIL = {"ME": ("MA", 1820), "WV": ("VA", 1864), "KY": ("VA", 1792), "TN": ("NC", 1792)}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

HELP = [
    "←/→ or h/l   previous / next election",
    "Home/End     first / last election",
    "PgUp/PgDn    five elections back / forward",
    "/ or a digit jump to a year",
    "Tab          map → states → all elections",
    "↑/↓          scroll the states table or the list",
    "Enter        open the selected election on the map",
    "q            quit",
    "",
    "Map: states are drawn with today's borders. Maine is shown as Massachusetts before 1820, West",
    "Virginia as Virginia before 1864, and Kentucky and Tennessee with their parents in 1788.",
    "Stripes mean a state split its votes. Small states are listed beside the map when their label",
    "doesn't fit; make the window bigger and they move onto the map.",
    "",
    "1788-1800: each elector cast two votes for president, so the totals add up to twice the electors.",
    "Popular votes are shown from 1824, the first election with a meaningful national count; before",
    "then many states chose electors in their legislatures. A † in the list marks a winner who lost",
    "the popular vote.",
    "",
    "Sources: Wikipedia's state-by-state tables for each election; shapes from the US Atlas (Census).",
]

Candidate = namedtuple("Candidate", "name party vp ev pop")
StateResult = namedtuple("StateResult", "ev alloc pop total")
Election = namedtuple("Election", "year winner votes no_vote note cands states")


def parse_elections(text):
    elections = []
    for block in text.strip().split("@")[1:]:
        lines = block.split("\n")
        year, winner, votes, no_vote, note = lines[0].split("|", 4)
        cands, states = [], {}
        for line in lines[1:]:
            if "|" in line:
                name, party, vp, ev, pop = line.split("|")
                cands.append(Candidate(name, party, vp, int(ev), int(pop) if pop else None))
            elif line:
                parts = line.split(" ")
                alloc = [int(v) for v in parts[2].split(",")]
                pop = [int(v) if v else None for v in parts[3].split(",")] if len(parts) > 3 else None
                total = int(parts[4]) if len(parts) > 4 and parts[4] else None
                states[parts[0]] = StateResult(int(parts[1]), alloc, pop, total)
        elections.append(Election(int(year), int(winner), int(votes) if votes else None,
                                  no_vote.split(), note, cands, states))
    return elections


def parse_shapes(text):
    shapes = {}
    for line in text.strip().split("\n"):
        ab, rest = line.split(" ", 1)
        shapes[ab] = [[tuple(int(v) for v in p.split(",")) for p in ring.split(" ")] for ring in rest.split("|")]
    return shapes


def check_data(elections):
    """Assert the embedded numbers add up. Run it after editing the data: returns the number of elections."""
    for e in elections:
        for i, c in enumerate(e.cands):
            if e.year in (1788, 1792) and i > 0:
                continue        # the second votes of the first two elections have no per-state breakdown
            got = sum(r.alloc[i] for r in e.states.values())
            assert got == c.ev, f"{e.year} {c.name}: states add up to {got}, not {c.ev}"
        assert not set(e.no_vote) & set(e.states), e.year
    return len(elections)


def fmt_int(n):
    return f"{n:,}" if n is not None else "–"


def fmt_pct(part, whole):
    return f"{100 * part / whole:.1f}%" if part and whole else "–"


def last_name(name):
    words = name.split()
    return words[-2] if words[-1] in ("Jr.", "I", "II") else words[-1]


def short_names(cands):
    """Column headers: last names, with a first initial where a race has two with the same one."""
    lasts = [last_name(c.name) for c in cands]
    return [f"{c.name[0]}.{n}" if lasts.count(n) > 1 else n for c, n in zip(cands, lasts)]


def candidate_colours(e):
    """One colour per candidate, unique within the race, so 1824's four Democratic-Republicans stay apart."""
    used, out = set(), []
    for c in e.cands:
        colour = PARTY_COLOURS.get(c.party)
        if colour is None or colour in used:
            colour = next((s for s in SPARE_COLOURS if s not in used), 245)
        used.add(colour)
        out.append(colour)
    return out


def runner_up(e):
    others = [c for i, c in enumerate(e.cands) if i != e.winner]
    return max(others, key=lambda c: (c.ev, c.pop or 0)) if others else None


def lost_popular(e):
    """True if the winner has a counted popular vote and someone else has more of it."""
    w = e.cands[e.winner].pop
    return bool(w) and any(c.pop and c.pop > w for c in e.cands)


def majority(e):
    """Electoral votes needed to win, once each elector has a single vote; None before that."""
    if e.year in TWO_VOTE_YEARS:
        return None
    return sum(r.ev for r in e.states.values()) // 2 + 1


# Colour arithmetic on xterm-256 indices

def rgb(c):
    if c < 16:
        return [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128), (128, 0, 128), (0, 128, 128),
                (192, 192, 192), (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255),
                (255, 0, 255), (0, 255, 255), (255, 255, 255)][c]
    if c >= 232:
        v = 8 + (c - 232) * 10
        return v, v, v
    c -= 16
    return tuple(0 if v == 0 else 55 + v * 40 for v in (c // 36, (c // 6) % 6, c % 6))


def darker(c):
    """The state-border shade of a fill colour: one step down the colour cube."""
    if c >= 232:
        return max(232, c - 4)
    if c < 16:
        return c
    c -= 16
    r, g, b = c // 36, (c // 6) % 6, c % 6
    return 16 + 36 * max(r - 1, 0) + 6 * max(g - 1, 0) + max(b - 1, 0)


def text_on(c):
    r, g, b = rgb(c)
    return 16 if 0.3 * r + 0.59 * g + 0.11 * b > 140 else 15


def basic(c):
    """Nearest of the eight basic colours, for terminals without 256."""
    if c < 0:
        return c
    r, g, b = rgb(c)
    table = {curses.COLOR_BLACK: (0, 0, 0), curses.COLOR_RED: (205, 0, 0), curses.COLOR_GREEN: (0, 205, 0),
             curses.COLOR_YELLOW: (205, 205, 0), curses.COLOR_BLUE: (0, 0, 238), curses.COLOR_MAGENTA: (205, 0, 205),
             curses.COLOR_CYAN: (0, 205, 205), curses.COLOR_WHITE: (229, 229, 229)}
    return min(table, key=lambda k: sum((a - b_) ** 2 for a, b_ in zip((r, g, b), table[k])))


# The map: a grid of half-block pixels, two per character cell

Raster = namedtuple("Raster", "w h grid scale ox oy order")


@lru_cache(maxsize=8)
def rasterise(cols, rows):
    """Fill every state's rings into a cols x rows*2 grid of state indices, -1 for water."""
    order = sorted(SHAPES)
    px_w, px_h = cols, rows * 2
    scale = min(px_w / MAP_W, px_h / MAP_H)
    ox, oy = (px_w - MAP_W * scale) / 2, (px_h - MAP_H * scale) / 2
    grid = [[-1] * px_w for _ in range(px_h)]
    for sid, ab in enumerate(order):
        crossings = {}
        for ring in SHAPES[ab]:
            points = [(x * scale + ox, y * scale + oy) for x, y in ring]
            for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
                if y0 == y1:
                    continue
                low, high = (y0, y1) if y0 < y1 else (y1, y0)
                for py in range(max(0, int(low - 0.5) + 1), min(px_h - 1, int(high - 0.5)) + 1):
                    if low <= py + 0.5 < high:
                        crossings.setdefault(py, []).append(x0 + (py + 0.5 - y0) * (x1 - x0) / (y1 - y0))
        filled = False
        for py, xs in crossings.items():
            xs.sort()
            for a, b in zip(xs[::2], xs[1::2]):
                for px in range(max(0, int(a + 0.5)), min(px_w - 1, int(b - 0.5)) + 1):
                    grid[py][px] = sid
                    filled = True
        if not filled:          # too small for any pixel centre: give it the one pixel under its middle
            pts = [p for ring in SHAPES[ab] for p in ring]
            px = int(sum(p[0] for p in pts) / len(pts) * scale + ox)
            py = int(sum(p[1] for p in pts) / len(pts) * scale + oy)
            grid[min(py, px_h - 1)][min(px, px_w - 1)] = sid
    return Raster(px_w, px_h, grid, scale, ox, oy, order)


def state_parts(e, ab, colours):
    """[(colour, weight)] for a state's fill, biggest first; a single entry unless the state split."""
    r = e.states.get(ab)
    if r is None:
        parent = PARENT_UNTIL.get(ab)
        if parent and e.year < parent[1]:
            return state_parts(e, parent[0], colours)
        return [(NO_VOTE if ab in e.no_vote else NOT_A_STATE, 1)]
    picks = range(len(e.cands))
    rivals = MAP_RIVALS.get(e.year)
    if rivals:
        picks = [i for i, c in enumerate(e.cands) if c.name in rivals]
    parts = [(colours[i], r.alloc[i]) for i in picks if r.alloc[i]]
    if not parts:
        parts = [(colours[i], v) for i, v in enumerate(r.alloc) if v]
    return sorted(parts, key=lambda p: -p[1]) or [(NO_VOTE, 1)]


def stripe_table(parts, period=8):
    """How far along a repeating diagonal each colour runs, in proportion and at least one pixel each."""
    if len(parts) == 1:
        return [parts[0][0]]
    total = sum(w for _, w in parts)
    period = max(period, 2 * len(parts))
    runs = [max(1, round(w * period / total)) for _, w in parts]
    table = []
    for (colour, _), run in zip(parts, runs):
        table += [colour] * run
    return table


def fitting_spots(cells, width, cx, cy):
    """Start columns where `width` cells in a row all belong to the state, nearest its middle first."""
    best = None
    for row, cols in cells.items():
        run_start = prev = None
        for col in sorted(cols) + [None]:
            if col is None or prev is None or col != prev + 1:
                if prev is not None and prev - run_start + 1 >= width:
                    start = min(max(run_start, round(cx - width / 2)), prev - width + 1)
                    dist = (start + width / 2 - cx) ** 2 + (2 * (row - cy)) ** 2
                    if best is None or dist < best[0]:
                        best = (dist, row, start)
                run_start = col
            prev = col
    return best


def place_label(cells, ab, ev, cx, cy):
    """Where this state's label goes, trying wide to narrow: [(row, col, text)], or None for a callout."""
    if ev is None:
        spot = fitting_spots(cells, 2, cx, cy)
        return [(spot[1], spot[2], ab)] if spot else None
    text = f"{ab} {ev}"
    spot = fitting_spots(cells, len(text), cx, cy)
    if spot:
        return [(spot[1], spot[2], text)]
    two = {row: cols for row, cols in cells.items() if row + 1 in cells}
    width = max(2, len(str(ev)))
    best = None
    for row in sorted(two):
        shared = {row: two[row] & cells[row + 1]}
        spot = fitting_spots(shared, width, cx, cy)
        if spot and (best is None or spot[0] < best[0]):
            best = spot
    if best:
        return [(best[1], best[2], ab.ljust(width)), (best[1] + 1, best[2], str(ev).ljust(width))]
    return None


Picture = namedtuple("Picture", "rows labels callouts")


@lru_cache(maxsize=128)
def picture(year, cols, rows):
    """Everything the map needs for one election: pixel colours, labels on it, callouts off to the right."""
    e = BY_YEAR[year]
    raster = rasterise(max(cols - 1, 1), rows)
    colours = candidate_colours(e)
    tables = {}
    sizes, cells, sums = {}, {}, {}
    for py, line in enumerate(raster.grid):
        for px, sid in enumerate(line):
            if sid < 0:
                continue
            sizes[sid] = sizes.get(sid, 0) + 1
            s = sums.setdefault(sid, [0, 0])
            s[0] += px
            s[1] += py
            if py % 2 == 0 and py + 1 < raster.h and raster.grid[py + 1][px] == sid:
                cells.setdefault(sid, {}).setdefault(py // 2, set()).add(px)
    for sid, ab in enumerate(raster.order):
        tables[sid] = stripe_table(state_parts(e, ab, colours))

    pixels = []
    for py, line in enumerate(raster.grid):
        out = []
        below = raster.grid[py + 1] if py + 1 < raster.h else None
        for px, sid in enumerate(line):
            if sid < 0:
                out.append(None)
                continue
            table = tables[sid]
            colour = table[(px + py) % len(table)]
            right = line[px + 1] if px + 1 < raster.w else -1
            down = below[px] if below else -1
            edge = (right >= 0 and right != sid) or (down >= 0 and down != sid)
            out.append(darker(colour) if edge and sizes.get(sid, 0) >= 40 else colour)
        pixels.append(out)

    labels, callouts = [], []
    taken = set()           # label cells, and the cell either side, so two states' labels never run together
    for sid, ab in sorted(enumerate(raster.order), key=lambda s: -sizes.get(s[0], 0)):
        r = e.states.get(ab)
        if r is None and ab not in e.no_vote:
            continue
        ev = r.ev if r else None
        main = tables[sid][0]
        if sid in sums:
            cx, cy = sums[sid][0] / sizes[sid], sums[sid][1] / sizes[sid] / 2
        else:
            cx, cy = 0, 0
        spots = place_label(cells.get(sid, {}), ab, ev, cx, cy)
        if spots and any((row, col + i) in taken for row, col, text in spots for i in range(len(text))):
            spots = None
        if spots:
            labels += [(row, col, text, text_on(main), main) for row, col, text in spots]
            taken |= {(row, col + i) for row, col, text in spots for i in range(-1, len(text) + 1)}
        else:
            callouts.append((cy, f"{ab} {ev if ev is not None else '–':>2}", text_on(main), main))
    callouts.sort(key=lambda c: c[0])

    cell_rows = [(pixels[2 * i], pixels[2 * i + 1]) for i in range(rows)]
    return Picture(cell_rows, labels, [c[1:] for c in callouts])


def callout_slots(n, cols, rows):
    """Screen spots for n callouts: down the ocean side of the map, wrapping into a second column."""
    x = max(0, cols - CALLOUT_W)
    first = min(max(0, int(rows * 0.3)), max(0, rows - n))
    slots = [(first + i, x) for i in range(rows - first)]
    for x2 in range(x - CALLOUT_W, -1, -CALLOUT_W):
        if len(slots) >= n:
            break
        slots += [(first + i, x2) for i in range(rows - first)]
    return slots[:n]


def cell_runs(top, bottom):
    """One row of pixels as (col, text, fg, bg) runs. bg -1 is the terminal's own background."""
    runs = []
    for col, (t, b) in enumerate(zip(top, bottom)):
        if t is None and b is None:
            continue
        if t == b:
            key = (" ", -1, t)
        elif t is None:
            key = ("▄", b, -1)
        else:
            key = ("▀", t, -1 if b is None else b)
        if runs and runs[-1][0] + len(runs[-1][1]) == col and runs[-1][2:] == key[1:]:
            runs[-1][1] += key[0]
        else:
            runs.append([col, key[0], key[1], key[2]])
    return runs


# Non-curses output: --once

def ansi(fg, bg):
    codes = []
    if fg >= 0:
        codes.append(f"38;5;{fg}")
    if bg >= 0:
        codes.append(f"48;5;{bg}")
    return "\x1b[" + ";".join(codes) + "m" if codes else ""


def print_map(e, cols, rows):
    pic = picture(e.year, cols, rows)
    grid = [[" "] * cols for _ in range(rows)]
    for y, (top, bottom) in enumerate(pic.rows):
        for col, text, fg, bg in cell_runs(top, bottom):
            for i, ch in enumerate(text):
                grid[y][col + i] = ansi(fg, bg) + ch + "\x1b[0m"
    for row, col, text, fg, bg in pic.labels:
        for i, ch in enumerate(text):
            grid[row][col + i] = ansi(fg, bg) + ch + "\x1b[0m"
    for (row, col), (text, fg, bg) in zip(callout_slots(len(pic.callouts), cols, rows), pic.callouts):
        for i, ch in enumerate(text):
            grid[row][col + i] = ansi(fg, bg) + ch + "\x1b[0m"
    print("\n".join("".join(line) for line in grid))


def print_once(e, show_map):
    size = shutil.get_terminal_size()
    if show_map:
        cols = min(size.columns, 120)
        print_map(e, cols, min(max(size.lines - 16, 12), round(cols / 3.2)))
    print(f"\n{e.year}: {e.cands[e.winner].name} ({e.cands[e.winner].party})")
    for c in e.cands:
        print(f"  {c.name:24} {c.party:24} {c.ev:>4} EV  {fmt_int(c.pop):>12}  {fmt_pct(c.pop, e.votes):>6}")
    print("\n" + textwrap.fill(e.note, min(size.columns, 100)))


# The curses half

class Colours:
    """Colour pairs on demand; the map alone can use dozens of fg/bg combinations."""

    def __init__(self):
        self.pairs = {}
        self.ok = curses.has_colors()
        self.low = self.ok and curses.COLORS < 256
        self.limit = curses.COLOR_PAIRS - 1 if self.ok else 0

    def attr(self, fg, bg=-1):
        if not self.ok:
            return 0
        n = self.pairs.get((fg, bg))
        if n is None:
            if len(self.pairs) >= self.limit:
                self.pairs.clear()
            n = len(self.pairs) + 1
            curses.init_pair(n, basic(fg) if self.low else fg, basic(bg) if self.low else bg)
            self.pairs[(fg, bg)] = n
        return curses.color_pair(n)


class App:
    def __init__(self, stdscr, index):
        self.scr = stdscr
        self.index = index
        self.view = "map"
        self.show_help = False
        self.table_top = 0
        self.list_top = 0
        self.colours = Colours()

    @property
    def e(self):
        return ELECTIONS[self.index]

    def put(self, y, x, text, attr=0):
        """addstr that clips to the window rather than raising at its edge."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            text, x = text[-x:], 0
        text = text[:w - x]
        if not text:
            return
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass            # the bottom-right cell always raises, after drawing

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"Window too small: needs {MIN_W}x{MIN_H}, q quits")
        elif self.show_help:
            self.draw_help(h, w)
        elif self.view == "map":
            self.draw_map_view(h, w)
        elif self.view == "states":
            self.draw_states_view(h, w)
        else:
            self.draw_list_view(h, w)
        self.scr.refresh()

    def draw_footer(self, h, w):
        keys = {"map": "←/→ election  Tab states  / year  ? help  q quit",
                "states": "←/→ election  ↑/↓ scroll  Tab all elections  / year  ? help  q quit",
                "list": "↑/↓ select  Enter map  Tab map  / year  ? help  q quit"}[self.view]
        self.put(h - 1, 0, keys[:w - 1], curses.A_DIM)
        self.draw_timeline(h - 2, w)

    def draw_timeline(self, y, w):
        """One tick per election, in the winner's colour."""
        n = len(ELECTIONS)
        step = max(1, min((w - 12) // n, 3))
        if w < n + 12:
            return
        x = (w - (step * n + 10)) // 2
        self.put(y, x, "1788", curses.A_DIM)
        x += 5
        for i, e in enumerate(ELECTIONS):
            colour = candidate_colours(e)[e.winner]
            if i == self.index:
                self.put(y, x + i * step, "█", self.colours.attr(TIMELINE_NOW) | curses.A_BOLD)
            else:
                self.put(y, x + i * step, "▮", self.colours.attr(colour))
        self.put(y, x + n * step + 1, "2024", curses.A_DIM)

    def draw_map_view(self, h, w):
        body = h - 2
        if w >= SIDE_BY_SIDE_MIN:
            panel = max(PANEL_MIN, min(PANEL_MAX, w // 4))
            map_w = w - panel - 2
            map_h = min(body, round((map_w - 1) * MAP_H / MAP_W / 2) + 1)
            self.draw_map(max(0, (body - map_h) // 2), 0, map_w, map_h)
            self.draw_panel(0, w - panel, panel, body)
        else:
            map_h = max(6, min(round((w - 1) * MAP_H / MAP_W / 2) + 1, body - 8))
            self.draw_map(0, 0, w, map_h)
            self.draw_panel(map_h, 1, w - 2, body - map_h)
        self.draw_footer(h, w)

    def draw_map(self, top, left, cols, rows):
        if not self.colours.ok:
            self.put(top, left, "The map needs a colour terminal.")
            return
        pic = picture(self.e.year, cols, rows)
        attr = self.colours.attr
        for y, (upper, lower) in enumerate(pic.rows):
            for col, text, fg, bg in cell_runs(upper, lower):
                self.put(top + y, left + col, text, attr(fg, bg) if fg >= 0 else attr(15, bg))
        for row, col, text, fg, bg in pic.labels:
            self.put(top + row, left + col, text, attr(fg, bg) | curses.A_BOLD)
        slots = callout_slots(len(pic.callouts), cols, rows)
        for (row, col), (text, fg, bg) in zip(slots, pic.callouts):
            self.put(top + row, left + col, text.ljust(CALLOUT_W - 1), attr(fg, bg) | curses.A_BOLD)

    def draw_panel(self, y, x, w, rows):
        e = self.e
        colours = candidate_colours(e)
        winner = e.cands[e.winner]
        attr = self.colours.attr
        end = y + rows
        self.put(y, x, str(e.year), curses.A_BOLD)
        self.put(y, x + 5, winner.name[:w - 5], attr(colours[e.winner]) | curses.A_BOLD)
        y += 1
        line = winner.party + (f" · with {winner.vp}" if winner.vp else "")
        self.put(y, x, line[:w], curses.A_DIM)
        y += 2 if rows >= 20 else 1

        need = majority(e)
        cast = sum(c.ev for c in e.cands) or 1
        if rows >= 10:
            if need:
                mark = round((w - 1) * need / max(sum(r.ev for r in e.states.values()), cast))
                self.put(y, x + mark, "▼", curses.A_DIM)
                label = f"{need} to win"
                self.put(y, x + (mark - len(label) - 1 if mark > w / 2 else mark + 2), label, curses.A_DIM)
                y += 1
            col = 0
            ranked = sorted(range(len(e.cands)), key=lambda i: -e.cands[i].ev)
            total = max(cast, sum(r.ev for r in e.states.values())) if need else cast
            for i in ranked:
                width = round(w * e.cands[i].ev / total)
                if width:
                    self.put(y, x + col, "█" * min(width, w - col), attr(colours[i]))
                    col += width
            y += 2

        ranked = sorted(range(len(e.cands)), key=lambda i: (-e.cands[i].ev, -(e.cands[i].pop or 0)))
        note_lines = textwrap.wrap(e.note, w)
        legend = self.legend(e)
        reserve = (min(len(note_lines), NOTE_MIN_ROWS) + 1) + (len(legend) if rows >= 14 else 0)
        room = end - y - reserve
        per = 2 if room >= 2 * min(len(ranked), 4) else 1
        shown = ranked[:max(room // per, 1)]
        if len(shown) < len(ranked):
            shown = ranked[:max(room // per - 1, 1)]
        for i in shown:
            c = e.cands[i]
            self.put(y, x, "■", attr(colours[i]))
            ev = str(c.ev)
            share = fmt_pct(c.pop, e.votes) if c.pop and per == 1 else ""
            self.put(y, x + 2, c.name[:w - 8 - len(share)], curses.A_BOLD if i == e.winner else 0)
            self.put(y, x + w - len(ev), ev, curses.A_BOLD if c.ev else curses.A_DIM)
            self.put(y, x + w - len(ev) - 2 - len(share), share, curses.A_DIM)
            y += 1
            detail = c.party
            if c.vp and i != e.winner:
                detail += f" · {c.vp}"
            if per == 2:
                votes = f"{fmt_int(c.pop)} {fmt_pct(c.pop, e.votes)}" if c.pop else ""
                self.put(y, x + 2, detail[:max(w - 3 - len(votes), 0)], curses.A_DIM)
                self.put(y, x + w - len(votes), votes)
                y += 1
        if len(shown) < len(ranked):
            self.put(y, x, f"  + {len(ranked) - len(shown)} more, on the states screen", curses.A_DIM)
            y += 1
        y += 1
        if rows >= 14:
            for text, colour in legend:
                if y >= end - 1:
                    break
                self.put(y, x, "■", attr(colour))
                self.put(y, x + 2, text[:w - 2], curses.A_DIM)
                y += 1
            y += 1 if y < end - 2 else 0
        room = end - y
        if room > 0 and len(note_lines) > room:
            note_lines = note_lines[:room]
            note_lines[-1] = note_lines[-1][:w - 1].rstrip() + "…"
        for line in note_lines:
            self.put(y, x, line)
            y += 1

    def legend(self, e):
        items = []
        if any(ab not in e.states and ab not in e.no_vote and
               not (ab in PARENT_UNTIL and e.year < PARENT_UNTIL[ab][1]) for ab in SHAPES):
            items.append(("not yet a state", NOT_A_STATE))
        if e.no_vote or any(not sum(r.alloc) for r in e.states.values()):
            items.append(("cast no counted votes", NO_VOTE))
        return items

    # The state-by-state table

    def table_columns(self, width):
        """Which candidates get columns: everyone with electoral votes, then the biggest popular votes."""
        e = self.e
        ranked = sorted(range(len(e.cands)), key=lambda i: (-e.cands[i].ev, -(e.cands[i].pop or 0)))
        with_ev = [i for i in ranked if e.cands[i].ev]
        with_pop = sorted((i for i in ranked if e.cands[i].pop), key=lambda i: -e.cands[i].pop)[:3]
        names = short_names(e.cands)
        left = 22 + 4
        ev_w = [max(len(names[i]), 4) + 1 for i in with_ev]
        fit_ev = []
        for i, cw in zip(with_ev, ev_w):
            if left + cw > width - 10:
                break
            fit_ev.append((i, cw))
            left += cw
        fit_pop = []
        for i in with_pop:
            cw = max(len(names[i]), 5) + 3
            if left + cw + 10 > width:
                break
            fit_pop.append((i, cw))
            left += cw
        return fit_ev, fit_pop, len(with_ev) - len(fit_ev)

    def state_rows(self):
        e = self.e
        rows = sorted(e.states, key=lambda ab: STATE_NAMES[ab])
        return rows + sorted(e.no_vote, key=lambda ab: STATE_NAMES[ab])

    def draw_states_view(self, h, w):
        e = self.e
        colours = candidate_colours(e)
        attr = self.colours.attr
        names = short_names(e.cands)
        fit_ev, fit_pop, hidden = self.table_columns(w)
        title = f"{e.year}  {e.cands[e.winner].name} wins"
        self.put(0, 0, title, curses.A_BOLD)
        if e.year in TWO_VOTE_YEARS:
            self.put(0, len(title) + 2, "each elector cast two votes", curses.A_DIM)
        if hidden:
            self.put(0, max(len(title) + 2, w - 26), f"{hidden} more don't fit, widen", curses.A_DIM)

        y = 2
        x = 22
        self.put(y, 0, "State", curses.A_BOLD)
        self.put(y, x, "EV".rjust(3), curses.A_BOLD)
        x += 4
        spots = []
        for i, cw in fit_ev:
            self.put(y, x, names[i][:cw - 1].rjust(cw - 1), attr(colours[i]) | curses.A_BOLD)
            spots.append((x, cw, i))
            x += cw
        pop_spots = []
        for i, cw in fit_pop:
            self.put(y, x, (names[i] + " %")[:cw + 1].rjust(cw), attr(colours[i]))
            pop_spots.append((x, cw, i))
            x += cw
        if e.votes:
            self.put(y, x + 1, "Margin".rjust(8), curses.A_BOLD)
        margin_x = x + 1

        rows = self.state_rows()
        room = h - 2 - 4
        self.table_top = max(0, min(self.table_top, max(0, len(rows) - room)))
        y = 3
        for ab in rows[self.table_top:self.table_top + room]:
            r = e.states.get(ab)
            self.put(y, 0, STATE_NAMES[ab][:21], 0)
            if r is None:
                self.put(y, 22, "cast no counted votes", curses.A_DIM)
                y += 1
                continue
            cast = sum(r.alloc)
            self.put(y, 22, str(r.ev).rjust(3), 0)
            for sx, cw, i in spots:
                v = r.alloc[i]
                self.put(y, sx, (str(v) if v else "·").rjust(cw - 1),
                         attr(colours[i]) | curses.A_BOLD if v and v * 2 > cast else (attr(colours[i]) if v else curses.A_DIM))
            if r.pop:
                for sx, cw, i in pop_spots:
                    v = r.pop[i]
                    text = f"{100 * v / r.total:.1f}" if v and r.total else "·"
                    self.put(y, sx, text.rjust(cw), 0 if v else curses.A_DIM)
                self.draw_margin(y, margin_x, e, r, colours)
            y += 1
        self.put(y + 1 if y + 1 < h - 2 else h - 3, 0, self.table_summary(e)[:w - 1], curses.A_DIM)
        if len(rows) > room:
            self.put(h - 3, w - 16, f"{self.table_top + 1}-{min(self.table_top + room, len(rows))} of {len(rows)}", curses.A_DIM)
        self.draw_footer(h, w)

    def draw_margin(self, y, x, e, r, colours):
        order = sorted((i for i, v in enumerate(r.pop) if v), key=lambda i: -r.pop[i])
        if not order or not r.total:
            return
        lead = 100 * (r.pop[order[0]] - (r.pop[order[1]] if len(order) > 1 else 0)) / r.total
        short = e.cands[order[0]].party[:1]
        self.put(y, x, f"{short} +{lead:.1f}".rjust(8), self.colours.attr(colours[order[0]]))

    def table_summary(self, e):
        cast = sum(c.ev for c in e.cands)
        in_play = sum(r.ev for r in e.states.values())
        if e.year in TWO_VOTE_YEARS:
            return f"{in_play} electors, {cast} votes cast"
        if cast != in_play:
            return f"{in_play} electoral votes, {cast} counted; the others were not cast"
        return f"{in_play} electoral votes"

    # Every election, one line each

    def draw_list_view(self, h, w):
        attr = self.colours.attr
        title = "Every US presidential election"
        self.put(0, 0, title, curses.A_BOLD)
        self.put(0, len(title) + 2, "† lost the popular vote", curses.A_DIM)
        name_w = max(18, min(26, (w - 40) // 2))
        wide = w >= 2 * name_w + 42
        header = f"{'Year':<6}{'Winner':<{name_w}} {'EV':>4} {'Vote':>6}  {'Runner-up':<{name_w}} {'EV':>4}"
        if wide:
            header += f" {'Vote':>6}"
        self.put(1, 0, header, curses.A_BOLD)
        room = h - 2 - 2
        n = len(ELECTIONS)
        if self.index < self.list_top:
            self.list_top = self.index
        elif self.index >= self.list_top + room:
            self.list_top = self.index - room + 1
        self.list_top = max(0, min(self.list_top, n - room))
        y = 2
        for i in range(self.list_top, min(self.list_top + room, n)):
            e = ELECTIONS[i]
            colours = candidate_colours(e)
            win, other = e.cands[e.winner], runner_up(e)
            selected = i == self.index
            base = curses.A_REVERSE if selected else 0
            if selected:
                self.put(y, 0, " " * (w - 1), base)
            mark = "†" if lost_popular(e) else " "
            self.put(y, 0, f"{e.year}{mark}", base | curses.A_BOLD)
            x = 6
            self.put(y, x, win.name[:name_w].ljust(name_w), base | (0 if selected else attr(colours[e.winner])))
            x += name_w + 1
            self.put(y, x, f"{win.ev:>4} {fmt_pct(win.pop, e.votes):>6}", base)
            x += 13
            if other:
                oc = colours[e.cands.index(other)]
                self.put(y, x, other.name[:name_w].ljust(name_w), base | (0 if selected else attr(oc)))
                x += name_w + 1
                self.put(y, x, f"{other.ev:>4}", base)
                if wide:
                    self.put(y, x + 4, f" {fmt_pct(other.pop, e.votes):>6}", base)
            y += 1
        self.draw_footer(h, w)

    def draw_help(self, h, w):
        self.put(0, 0, "Help  (any key closes)", curses.A_BOLD)
        y = 2
        for line in HELP:
            for part in textwrap.wrap(line, w - 2) or [""]:
                self.put(y, 1, part)
                y += 1

    # Input

    def prompt_year(self, first=""):
        h, w = self.scr.getmaxyx()
        text = first
        set_cursor(1)
        try:
            while True:
                self.put(h - 1, 0, " " * (w - 1))
                self.put(h - 1, 0, f"Year: {text}", curses.A_BOLD)
                self.scr.refresh()
                key = self.scr.getch()
                if key in (10, 13, curses.KEY_ENTER):
                    break
                if key == 27:
                    return
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    text = text[:-1]
                elif 48 <= key <= 57 and len(text) < 4:
                    text += chr(key)
        finally:
            set_cursor(0)
        if text:
            self.go_to_year(int(text))

    def go_to_year(self, year):
        self.index = min(range(len(ELECTIONS)), key=lambda i: abs(ELECTIONS[i].year - year))

    def move(self, delta):
        self.index = max(0, min(len(ELECTIONS) - 1, self.index + delta))

    def handle(self, key):
        """False when it's time to quit."""
        if self.show_help:
            self.show_help = False
            return key not in (ord("q"),)
        if key == ord("q"):
            return False
        if key == ord("?"):
            self.show_help = True
        elif key == 9:
            self.view = {"map": "states", "states": "list", "list": "map"}[self.view]
            self.table_top = 0
        elif key == curses.KEY_LEFT or key == ord("h"):
            self.move(-1)
        elif key == curses.KEY_RIGHT or key == ord("l"):
            self.move(1)
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = len(ELECTIONS) - 1
        elif key == curses.KEY_PPAGE:
            self.page(-1)
        elif key == curses.KEY_NPAGE:
            self.page(1)
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll(1)
        elif key in (10, 13, curses.KEY_ENTER) and self.view == "list":
            self.view = "map"
        elif key == ord("/"):
            self.prompt_year()
        elif 48 <= key <= 57:
            self.prompt_year(chr(key))
        return True

    def page(self, direction):
        h = self.scr.getmaxyx()[0]
        if self.view == "states":
            self.table_top += direction * max(1, h - 8)
        else:
            self.move(direction * 5)

    def scroll(self, delta):
        if self.view == "states":
            self.table_top = max(0, self.table_top + delta)
        elif self.view == "list":
            self.move(delta)

    def run(self):
        self.draw()
        while True:
            key = self.scr.getch()
            if not self.handle(key):
                return
            self.draw()


def set_cursor(visibility):
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass            # not every terminal can hide its cursor


def run(stdscr, index):
    set_cursor(0)
    if curses.has_colors():
        curses.use_default_colors()
    stdscr.keypad(True)
    App(stdscr, index).run()


def main():
    parser = argparse.ArgumentParser(description="Every US presidential election, with its electoral map.")
    parser.add_argument("year", nargs="?", type=int, help="election to open on (default: the latest)")
    parser.add_argument("--once", action="store_true", help="print the election and exit")
    args = parser.parse_args()
    index = len(ELECTIONS) - 1
    if args.year is not None:
        index = min(range(len(ELECTIONS)), key=lambda i: abs(ELECTIONS[i].year - args.year))
    if args.once:
        print_once(ELECTIONS[index], sys.stdout.isatty())
        return
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, index)
    except KeyboardInterrupt:
        pass


# State shapes: Albers-projected outlines from the US Atlas (Census), simplified along shared borders.
# A line is `AB x,y x,y ...|x,y ...`, one ring per island or hole, filled even-odd.
SHAPES = parse_shapes("""
AK 75,561 77,559 79,560 80,556 85,556 86,559 78,566 79,563 77,565 75,561|26,533 31,532 32,537 30,538 26,533|32,490 45,485 47,485 47,490 56,489 51,484 54,488 55,483 48,481 48,478 41,470 43,466 51,466 57,457 68,454 71,451 74,452 73,454 75,452 78,455 83,454 84,457 90,456 103,459 108,457 114,460 129,533 136,531 136,533 145,540 151,531 162,539 173,551 182,552 184,558 183,564 179,556 180,561 176,559 176,556 174,559 174,554 173,557 171,556 172,553 165,549 163,545 165,546 160,541 158,542 152,535 156,543 152,543 149,539 147,540 152,543 149,545 134,538 135,534 132,537 122,536 116,539 117,537 114,536 114,534 112,535 110,533 107,536 110,533 105,530 101,533 100,531 102,540 100,538 96,541 96,539 95,542 94,541 93,544 92,543 87,547 87,545 90,542 87,542 88,534 92,531 97,532 93,530 95,528 88,532 82,543 78,545 78,548 81,549 77,555 70,559 67,564 59,567 58,571 46,575 44,573 42,577 40,577 39,575 39,578 36,577 37,580 28,580 30,577 36,578 43,571 48,570 48,572 50,572 50,569 58,565 62,559 66,549 61,551 60,547 58,552 53,546 46,550 47,542 45,537 38,538 34,532 36,529 33,524 33,522 41,512 46,513 49,510 53,511 55,509 53,503 55,501 54,500 48,504 47,501 41,502 37,500 35,495 37,494 32,490|16,503 17,502 21,503 26,508 22,510 16,503|166,559 166,555 168,555 174,560 173,561 176,562 177,566 171,563 174,567 170,564 171,562 167,560 168,558 166,559|162,553 164,553 163,550 167,550 170,552 165,554 165,559 162,553|156,542 162,545 163,548 161,551 156,542|155,550 156,549 159,549 162,558 155,550|151,547 151,544 153,543 157,544 159,548 155,548 154,550 151,547
AL 623,370 671,365 684,413 689,424 691,426 688,429 687,436 690,443 690,450 692,454 642,460 642,463 647,467 648,472 645,476 637,478 641,476 635,467 633,476 628,475 624,441 625,372 623,370
AR 505,341 585,338 587,343 582,350 593,349 595,351 590,355 592,356 591,360 589,359 588,363 586,362 588,363 587,365 589,369 584,372 586,374 581,375 582,378 580,379 582,380 580,380 580,386 574,391 576,391 574,393 575,395 571,396 573,401 570,402 573,403 569,403 571,405 569,409 570,407 570,410 572,409 571,419 517,420 516,408 510,408 508,406 508,366 505,341
AZ 127,393 132,391 132,386 128,384 129,380 134,375 135,366 144,359 140,355 138,342 140,338 141,317 148,317 151,321 154,317 158,299 246,314 228,442 190,436 124,397 127,393
CA 14,146 82,165 65,231 138,342 140,355 144,359 135,366 134,375 129,380 128,384 132,386 132,391 127,393 81,387 78,368 69,357 64,355 63,348 53,344 47,335 30,330 28,326 31,314 28,311 28,306 23,299 16,282 17,276 20,275 21,268 17,267 13,260 15,246 17,246 16,251 21,256 17,243 20,240 17,239 17,245 15,246 10,239 8,239 10,233 2,215 6,195 0,183 0,179 9,167 13,159 12,150 14,146
CO 259,223 343,232 377,235 375,257 371,326 354,325 246,314 259,223
CT 853,170 880,164 883,179 866,184 856,194 854,192 857,188 853,170
DC 807,253 808,251 811,253 809,256 807,253
DE 826,230 828,228 832,227 831,230 831,235 838,246 843,248 845,256 834,258 826,230
FL 775,590 782,583 787,586 775,590|692,454 696,461 747,457 749,462 751,461 752,451 762,452 769,472 786,497 788,508 804,535 805,560 803,559 802,569 805,563 805,566 798,579 801,572 787,575 782,563 775,560 773,561 768,550 765,547 763,548 761,544 763,544 762,538 759,543 747,527 749,527 752,517 749,519 749,516 746,515 749,520 747,525 743,519 742,512 744,515 745,506 743,496 740,491 736,492 721,476 714,472 709,473 708,478 693,484 696,486 702,482 695,486 689,485 687,482 689,485 690,482 687,479 670,472 660,472 645,476 648,472 647,467 642,463 642,460 692,454
GA 671,365 694,362 716,359 713,367 723,371 730,382 743,391 747,397 753,400 755,408 760,411 763,419 768,420 763,428 764,430 762,433 764,433 763,440 760,446 761,446 762,452 752,451 751,461 749,462 747,457 696,461 692,454 690,450 690,443 687,436 688,429 691,426 689,424 684,413 671,365
HI 287,570 292,565 291,558 304,564 313,576 299,584 295,589 290,584 291,579 287,570|273,544 275,541 278,544 283,543 288,548 280,551 278,547 273,544|265,544 269,544 271,546 267,548 265,544|260,539 261,537 273,538 269,541 260,539|240,529 246,526 253,535 243,535 240,529|208,518 212,514 218,516 215,522 208,518|198,522 202,519 199,524 198,522
IA 472,181 558,179 561,184 559,187 561,196 567,198 569,201 577,209 577,217 573,223 563,226 562,230 565,234 565,237 563,243 558,247 558,250 552,245 484,247 481,244 481,228 478,225 477,216 474,211 472,204 469,198 472,190 470,181 472,181
ID 152,88 152,80 165,22 180,25 175,47 178,54 179,60 177,60 182,65 188,79 195,82 190,93 191,99 187,101 187,105 190,108 196,104 198,107 198,113 202,120 201,124 206,127 208,137 210,135 216,137 218,134 231,137 233,132 238,140 229,195 179,187 130,176 139,136 143,129 139,126 139,122 157,98 152,88
IL 558,250 558,247 563,243 565,237 565,234 562,230 563,226 573,223 577,217 577,209 569,201 616,198 615,202 622,215 627,269 625,274 628,282 621,296 621,305 619,309 621,313 614,315 615,323 605,320 603,326 598,325 595,319 597,317 595,310 589,305 587,306 587,304 579,299 582,284 576,282 574,284 571,275 560,265 557,256 558,250
IN 622,215 626,217 633,213 667,210 667,211 674,270 675,277 669,280 664,280 665,284 659,291 656,298 650,294 646,302 641,299 637,304 628,301 628,304 623,302 623,305 621,305 621,296 628,282 625,274 627,269 622,215
KS 375,257 491,261 495,264 498,263 499,267 495,271 500,279 504,280 504,329 371,326 375,257
KY 603,326 605,320 615,323 614,315 621,313 619,309 621,305 623,305 623,302 628,304 628,301 637,304 641,299 646,302 650,294 656,298 659,291 665,284 664,280 669,280 675,277 674,270 680,269 685,275 695,278 698,276 702,278 708,274 710,277 715,280 715,286 718,291 722,297 729,299 717,310 710,320 701,324 622,331 623,335 598,337 599,334 602,335 603,333 603,326
LA 517,420 571,419 570,420 572,419 573,421 571,425 573,425 572,427 574,427 573,429 575,430 572,431 577,434 572,437 575,438 572,440 574,441 569,445 569,447 570,447 568,450 566,451 568,452 566,453 567,456 565,456 566,458 564,459 565,464 564,465 601,463 599,471 606,481 601,485 600,487 605,488 607,483 610,487 613,485 610,489 611,490 610,490 608,493 607,491 604,495 608,499 614,499 618,503 616,507 613,506 610,509 613,503 610,505 601,501 594,507 590,501 581,508 579,505 573,504 573,502 576,504 576,502 574,500 572,501 571,498 569,499 565,496 566,493 561,494 562,492 554,494 558,497 552,499 535,494 522,496 520,493 525,488 524,477 528,466 521,453 521,449 517,444 517,420
MA 853,153 866,150 884,146 891,140 893,144 896,144 892,148 892,154 897,155 898,159 903,163 910,161 908,156 905,156 908,155 912,162 912,165 911,163 906,165 898,173 902,169 899,165 897,171 894,172 892,168 891,168 887,162 880,164 853,170 853,153
MD 764,243 826,230 834,258 845,256 844,266 837,269 833,271 832,261 830,264 829,261 828,265 823,260 824,256 828,257 823,254 822,255 823,250 821,252 821,247 823,248 821,245 822,240 824,239 824,235 821,240 820,238 820,241 818,240 818,244 815,243 819,248 818,254 825,270 816,267 815,265 815,267 814,266 811,263 808,265 809,259 809,256 811,253 808,251 807,253 800,250 800,247 798,246 795,246 792,240 786,239 782,244 776,242 774,247 771,247 766,254 764,243
ME 892,136 885,129 872,88 876,89 876,85 879,84 877,82 882,72 879,65 881,59 881,53 887,33 890,33 892,37 894,38 901,32 911,35 920,66 926,65 928,73 931,75 934,74 938,78 937,85 933,85 932,91 928,90 925,96 923,95 924,99 920,99 920,95 917,97 919,105 916,100 913,100 913,96 910,98 912,108 909,112 906,110 906,113 897,120 892,136
MI 633,213 637,210 641,196 640,184 634,170 635,166 633,160 637,154 636,145 646,133 645,142 647,143 648,138 648,143 649,131 656,128 652,124 653,119 657,118 678,126 683,133 680,133 684,140 684,148 681,150 681,155 676,159 677,164 681,165 684,158 691,153 697,158 703,177 702,185 700,188 701,184 698,185 690,207 667,211 667,210 633,213|567,109 588,97 597,88 603,85 607,87 604,87 600,92 597,97 597,102 602,97 600,100 605,98 610,100 615,106 620,105 623,107 633,101 648,97 652,97 651,103 657,102 658,104 665,100 666,107 664,109 671,112 671,109 675,109 676,113 663,114 658,112 657,117 653,114 644,113 642,116 633,118 628,126 627,124 629,119 625,120 623,124 621,121 614,139 611,137 612,132 609,133 610,126 605,124 604,121 572,114 567,109
MN 461,56 492,56 492,48 495,48 500,62 512,64 513,67 525,64 531,66 529,68 533,68 535,73 536,70 540,70 547,76 557,71 560,74 579,75 559,87 540,105 542,107 537,108 538,121 529,130 529,133 533,136 531,152 554,168 557,173 558,179 472,181 472,140 466,133 471,126 467,109 466,88 462,75 463,63 461,56
MO 484,247 552,245 558,250 557,256 560,265 571,275 574,284 576,282 582,284 579,299 587,304 587,306 589,305 595,310 597,317 595,319 598,325 603,326 603,333 602,335 599,334 598,337 597,337 596,335 596,337 597,342 594,343 596,346 593,349 582,350 587,343 585,338 505,341 504,329 504,280 500,279 495,271 499,267 498,263 495,264 491,261 484,247
MS 571,419 572,409 570,410 570,407 569,409 571,405 569,403 573,403 570,402 573,401 571,396 575,395 574,393 576,391 574,391 580,386 580,380 582,380 580,379 582,378 581,375 586,374 584,372 623,370 625,372 624,441 628,475 618,475 611,478 610,476 606,481 599,471 601,463 564,465 565,464 564,459 566,458 565,456 567,456 566,453 568,452 566,451 568,450 570,447 569,447 569,445 574,441 572,440 575,438 572,437 577,434 572,431 575,430 573,429 574,427 572,427 573,425 571,425 573,421 572,419 570,420 571,419
MT 180,25 266,40 358,51 353,120 351,141 239,128 238,140 233,132 231,137 218,134 216,137 210,135 208,137 206,127 201,124 202,120 198,113 198,107 196,104 190,108 187,105 187,101 191,99 190,93 195,82 188,79 182,65 177,60 179,60 178,54 175,47 180,25
NC 737,320 776,314 840,302 850,317 838,303 844,312 841,309 842,310 839,310 836,308 839,311 835,311 836,313 832,312 835,313 831,316 828,316 827,311 827,314 829,318 840,315 841,322 843,314 846,318 847,323 841,330 835,330 832,328 835,326 832,328 833,330 825,329 836,333 831,341 828,340 832,341 837,337 838,339 840,338 837,346 835,344 835,346 838,347 844,336 838,348 835,346 828,348 819,356 815,370 811,369 804,372 780,354 759,357 759,354 756,351 754,353 753,350 729,353 716,359 694,362 694,357 699,355 701,349 716,341 717,337 722,334 723,336 728,331 732,331 734,327 737,325 737,320
ND 358,51 461,56 463,63 462,75 466,88 467,109 471,126 353,120 358,51
NE 347,187 438,192 446,197 449,195 459,196 472,204 474,211 477,216 478,225 481,228 481,244 484,247 491,261 375,257 377,235 343,232 347,187
NH 868,96 867,91 872,88 885,129 892,136 891,140 884,146 866,150 863,147 862,131 865,119 863,114 869,108 867,102 868,96
NJ 831,231 831,230 832,227 842,216 835,212 831,206 833,202 831,199 837,190 852,195 851,202 850,203 848,207 853,207 855,223 846,244 844,245 844,239 840,240 832,235 831,232 831,231
NM 246,314 354,325 353,336 344,439 277,433 278,438 246,434 244,444 228,442 246,314
NV 82,165 130,176 179,187 158,299 154,317 151,321 148,317 141,317 140,338 138,342 65,231 82,165
NY 749,186 762,172 756,162 765,157 777,156 781,158 793,153 801,146 798,140 801,138 796,136 796,132 802,127 810,114 816,109 839,103 845,135 847,134 849,136 853,153 853,170 857,188 854,192 856,194 855,199 859,194 872,190 878,184 877,186 881,187 885,185 865,200 853,205 851,202 852,195 837,190 831,188 828,181 822,178 750,192 749,186
OH 667,211 690,207 699,211 701,209 707,213 715,209 719,209 726,202 738,195 743,225 740,227 742,232 740,249 735,255 731,255 728,259 726,263 728,267 725,268 723,265 719,279 715,280 710,277 708,274 702,278 698,276 695,278 685,275 680,269 674,270 667,211
OK 354,325 371,326 504,329 505,341 508,366 508,406 494,399 493,401 488,400 478,402 473,405 469,401 465,402 461,399 458,404 457,400 453,402 448,398 445,401 442,400 443,398 441,398 440,394 433,396 430,394 420,392 417,387 416,389 411,388 405,384 407,340 353,336 354,325
OR 52,56 61,63 61,72 66,77 75,75 83,78 84,80 94,80 97,81 120,80 152,88 157,98 139,122 139,126 143,129 139,136 130,176 82,165 14,146 13,142 15,132 14,126 27,106 40,75 45,55 47,57 52,56
PA 738,195 749,186 750,192 822,178 828,181 831,188 837,190 831,199 833,202 831,206 835,212 842,216 832,227 828,228 826,230 764,243 746,246 743,225 738,195
RI 883,179 880,164 887,162 891,168 891,170 888,167 889,176 883,179
SC 716,359 729,353 753,350 754,353 756,351 759,354 759,357 780,354 804,372 798,378 793,394 788,394 789,396 784,400 782,405 774,409 774,413 769,414 771,415 768,420 763,419 760,411 755,408 753,400 747,397 743,391 730,382 723,371 713,367 716,359
SD 351,141 353,120 471,126 466,133 472,140 472,181 470,181 472,190 469,198 472,204 459,196 449,195 446,197 438,192 347,187 351,141
TN 593,349 596,346 594,343 597,342 596,337 597,337 598,337 623,335 622,331 701,324 737,320 737,325 734,327 732,331 728,331 723,336 722,334 717,337 716,341 701,349 699,355 694,357 694,362 671,365 623,370 584,372 589,369 587,365 588,363 586,362 588,363 589,359 591,360 592,356 590,355 595,351 593,349
TX 353,336 407,340 405,384 411,388 416,389 417,387 420,392 430,394 433,396 440,394 441,398 443,398 442,400 445,401 448,398 453,402 457,400 458,404 461,399 465,402 469,401 473,405 478,402 488,400 493,401 494,399 508,406 510,408 516,408 517,420 517,444 521,449 521,453 528,466 524,477 525,488 520,493 522,496 504,504 509,500 504,500 505,496 504,494 500,496 499,499 501,501 501,505 496,510 505,504 492,515 473,525 459,537 453,548 452,556 456,578 453,567 452,567 452,549 458,537 464,530 471,527 470,525 466,528 464,526 463,530 461,532 459,530 455,533 459,532 456,539 449,537 454,541 451,550 449,551 449,549 446,552 451,551 450,566 453,577 456,578 456,581 450,583 446,579 433,578 422,571 416,570 409,556 409,543 401,538 400,534 394,526 387,507 373,492 355,488 354,491 348,491 344,502 338,507 332,505 313,491 306,468 297,461 278,438 277,433 344,439 353,336
UT 179,187 229,195 225,218 259,223 246,314 158,299 179,187
VA 837,269 844,266 836,290 834,282 837,269|729,299 730,302 735,306 740,302 743,304 748,301 749,298 755,297 758,295 757,291 764,276 765,269 772,272 776,261 778,262 784,252 784,245 794,251 795,246 798,246 800,247 800,250 807,253 809,256 809,259 807,260 807,267 811,264 813,268 820,268 827,273 827,280 815,273 824,280 828,281 830,286 827,285 828,287 826,288 830,290 830,293 829,294 823,289 818,290 823,290 828,296 836,294 840,302 776,314 737,320 701,324 710,320 717,310 729,299
VT 839,103 868,96 867,102 869,108 863,114 865,119 862,131 863,147 866,150 853,153 849,136 847,134 845,135 839,103
WA 73,7 75,9 77,6 80,7 77,12 73,7|165,22 152,80 152,88 120,80 97,81 94,80 84,80 83,78 75,75 66,77 61,72 61,63 52,56 44,53 47,45 47,51 49,46 51,45 47,43 48,39 52,40 49,36 47,39 49,20 47,14 49,5 58,13 70,16 74,21 76,19 77,25 72,28 73,26 65,34 69,34 66,33 78,25 74,38 73,39 72,36 71,41 71,36 69,37 71,41 74,37 78,38 78,30 83,24 82,18 82,22 80,20 82,16 79,12 82,13 84,10 83,6 81,6 82,0 165,22
WI 542,107 560,100 561,102 558,108 562,106 567,109 572,114 604,121 605,124 610,126 609,133 612,132 611,137 614,139 608,148 608,151 610,152 614,144 617,144 619,137 622,133 623,135 617,151 617,159 614,166 612,181 616,198 569,201 567,198 561,196 559,187 561,184 558,179 557,173 554,168 531,152 533,136 529,133 529,130 538,121 537,108 542,107
WV 715,280 719,279 723,265 725,268 728,267 726,263 728,259 731,255 735,255 740,249 742,232 740,227 743,225 746,246 764,243 766,254 771,247 774,247 776,242 782,244 786,239 792,240 795,246 794,251 784,245 784,252 778,262 776,261 772,272 765,269 764,276 757,291 758,295 755,297 749,298 748,301 743,304 740,302 735,306 730,302 729,299 722,297 718,291 715,286 715,280
WY 351,141 347,187 343,232 259,223 225,218 229,195 238,140 239,128 351,141
""")

# One block per election, from Wikipedia's state-by-state tables:
#   @year|winner's index|national votes|states with no counted votes|note
#   name|party|running mate|electoral votes|popular votes             one per candidate
#   AB electoral-votes alloc,alloc,... popular,popular,... state-total one per state, columns as the candidates
ELECTIONS = parse_elections("""
@1788|0||NY NC RI|First election. Every elector voted for Washington; each cast two votes and the runner-up, John Adams, became vice president. New York failed to appoint electors; North Carolina and Rhode Island had not yet ratified the Constitution.
George Washington|Independent||69|
John Adams|Federalist||34|
John Jay|Federalist||9|
Robert H. Harrison|Federalist||6|
John Rutledge|Federalist||6|
John Hancock|Federalist||4|
George Clinton|Anti-Federalist||3|
Samuel Huntington|Federalist||2|
John Milton|Federalist||2|
James Armstrong|Federalist||1|
Benjamin Lincoln|Federalist||1|
Edward Telfair|Anti-Federalist||1|
CT 7 7,0,0,0,0,0,0,0,0,0,0,0
DE 3 3,0,0,0,0,0,0,0,0,0,0,0
GA 5 5,0,0,0,0,0,0,0,0,0,0,0
MA 10 10,0,0,0,0,0,0,0,0,0,0,0
MD 8 6,0,0,0,0,0,0,0,0,0,0,0
NH 5 5,0,0,0,0,0,0,0,0,0,0,0
NJ 6 6,0,0,0,0,0,0,0,0,0,0,0
PA 10 10,0,0,0,0,0,0,0,0,0,0,0
SC 7 7,0,0,0,0,0,0,0,0,0,0,0
VA 12 10,0,0,0,0,0,0,0,0,0,0,0
@1792|0|||Every elector again voted for Washington, the only president ever chosen unanimously, and he did it twice. Electors still cast two votes; Adams came second and stayed vice president.
George Washington|Independent||132|
John Adams|Federalist||77|
George Clinton|Democratic-Republican||50|
Thomas Jefferson|Democratic-Republican||4|
Aaron Burr|Democratic-Republican||1|
CT 9 9,0,0,0,0
DE 3 3,0,0,0,0
GA 4 4,0,0,0,0
KY 4 4,0,0,0,0
MA 16 16,0,0,0,0
MD 10 8,0,0,0,0
NC 12 12,0,0,0,0
NH 6 6,0,0,0,0
NJ 7 7,0,0,0,0
NY 12 12,0,0,0,0
PA 15 15,0,0,0,0
RI 4 4,0,0,0,0
SC 8 8,0,0,0,0
VA 21 21,0,0,0,0
VT 4 3,0,0,0,0
@1796|0|||First contested election. Two votes per elector put rivals in office together: Adams president, Jefferson vice president.
John Adams|Federalist||71|
Thomas Jefferson|Democratic-Republican||68|
Thomas Pinckney|Federalist||59|
Aaron Burr|Democratic-Republican||30|
Samuel Adams|Democratic-Republican||15|
Oliver Ellsworth|Federalist||11|
George Clinton|Democratic-Republican||7|
John Jay|Federalist||5|
James Iredell|Federalist||3|
John Henry|Democratic-Republican||2|
Samuel Johnston|Federalist||2|
George Washington|Independent||2|
C. C. Pinckney|Federalist||1|
CT 9 9,0,4,0,0,0,0,5,0,0,0,0,0
DE 3 3,0,3,0,0,0,0,0,0,0,0,0,0
GA 4 0,4,0,0,0,0,4,0,0,0,0,0,0
KY 4 0,4,0,4,0,0,0,0,0,0,0,0,0
MA 16 16,0,13,0,0,1,0,0,0,0,2,0,0
MD 10 7,4,4,3,0,0,0,0,0,2,0,0,0
NC 12 1,11,1,6,0,0,0,0,3,0,0,1,1
NH 6 6,0,0,0,0,6,0,0,0,0,0,0,0
NJ 7 7,0,7,0,0,0,0,0,0,0,0,0,0
NY 12 12,0,12,0,0,0,0,0,0,0,0,0,0
PA 15 1,14,2,13,0,0,0,0,0,0,0,0,0
RI 4 4,0,0,0,0,4,0,0,0,0,0,0,0
SC 8 0,8,8,0,0,0,0,0,0,0,0,0,0
TN 3 0,3,0,3,0,0,0,0,0,0,0,0,0
VA 21 1,20,1,1,15,0,3,0,0,0,0,1,0
VT 4 4,0,4,0,0,0,0,0,0,0,0,0,0
@1800|0|||Jefferson and his own running mate Burr tied at 73. The House chose Jefferson on the 36th ballot, which led to the 12th Amendment and separate votes for vice president.
Thomas Jefferson|Democratic-Republican|Aaron Burr|73|
Aaron Burr|Democratic-Republican||73|
John Adams|Federalist|C. C. Pinckney|65|
C. C. Pinckney|Federalist||64|
John Jay|Federalist||1|
CT 9 0,0,9,9,0
DE 3 0,0,3,3,0
GA 4 4,4,0,0,0
KY 4 4,4,0,0,0
MA 16 0,0,16,16,0
MD 10 5,5,5,5,0
NC 12 8,8,4,4,0
NH 6 0,0,6,6,0
NJ 7 0,0,7,7,0
NY 12 12,12,0,0,0
PA 15 8,8,7,7,0
RI 4 0,0,4,3,1
SC 8 8,8,0,0,0
TN 3 3,3,0,0,0
VA 21 21,21,0,0,0
VT 4 0,0,4,4,0
@1804|0|||First election under the 12th Amendment, with separate ballots for president and vice president. Jefferson carried all but two states.
Thomas Jefferson|Democratic-Republican|George Clinton|162|
C. C. Pinckney|Federalist|Rufus King|14|
CT 9 0,9
DE 3 0,3
GA 6 6,0
KY 8 8,0
MA 19 19,0
MD 11 9,2
NC 14 14,0
NH 7 7,0
NJ 8 8,0
NY 19 19,0
OH 3 3,0
PA 20 20,0
RI 4 4,0
SC 10 10,0
TN 5 5,0
VA 24 24,0
VT 6 6,0
@1808|0|||Madison holds the presidency for the Democratic-Republicans despite anger over the Embargo Act. Vice President Clinton took six votes for president from New York.
James Madison|Democratic-Republican|George Clinton|122|
C. C. Pinckney|Federalist|Rufus King|47|
George Clinton|Democratic-Republican||6|
CT 9 0,9,0
DE 3 0,3,0
GA 6 6,0,0
KY 8 7,0,0
MA 19 0,19,0
MD 11 9,2,0
NC 14 11,3,0
NH 7 0,7,0
NJ 8 8,0,0
NY 19 13,0,6
OH 3 3,0,0
PA 20 20,0,0
RI 4 0,4,0
SC 10 10,0,0
TN 5 5,0,0
VA 24 24,0,0
VT 6 6,0,0
@1812|0|||A wartime election: Madison wins as the War of 1812 divides the country along regional lines. One Ohio elector did not vote.
James Madison|Democratic-Republican|Elbridge Gerry|128|
DeWitt Clinton|Federalist|Jared Ingersoll|89|
CT 9 0,9
DE 4 0,4
GA 8 8,0
KY 12 12,0
LA 3 3,0
MA 22 0,22
MD 11 6,5
NC 15 15,0
NH 8 0,8
NJ 8 0,8
NY 29 0,29
OH 8 7,0
PA 25 25,0
RI 4 0,4
SC 11 11,0
TN 8 8,0
VA 25 25,0
VT 8 8,0
@1816|0|||The Federalists' last presidential run: they carry only Massachusetts, Connecticut and Delaware. Monroe's win opens the Era of Good Feelings. Four electors did not vote.
James Monroe|Democratic-Republican|Daniel Tompkins|183|
Rufus King|Federalist|John E. Howard|34|
CT 9 0,9
DE 4 0,3
GA 8 8,0
IN 3 3,0
KY 12 12,0
LA 3 3,0
MA 22 0,22
MD 11 8,0
NC 15 15,0
NH 8 8,0
NJ 8 8,0
NY 29 29,0
OH 8 8,0
PA 25 25,0
RI 4 4,0
SC 11 11,0
TN 8 8,0
VA 25 25,0
VT 8 8,0
@1820|0|||Effectively unopposed. Monroe won every state; a single New Hampshire elector voted for John Quincy Adams, and three electors did not vote.
James Monroe|Democratic-Republican|Daniel Tompkins|231|
John Quincy Adams|Democratic-Republican|Richard Rush|1|
AL 3 3,0
CT 9 9,0
DE 4 4,0
GA 8 8,0
IL 3 3,0
IN 3 3,0
KY 12 12,0
LA 3 3,0
MA 15 15,0
MD 11 11,0
ME 9 9,0
MO 3 3,0
MS 3 2,0
NC 15 15,0
NH 8 7,1
NJ 8 8,0
NY 29 29,0
OH 8 8,0
PA 25 24,0
RI 4 4,0
SC 11 11,0
TN 8 7,0
VA 25 25,0
VT 8 8,0
@1824|1|374026||Four Democratic-Republicans split the vote and nobody reached a majority. The House chose Adams over Jackson, who led in both the popular and electoral votes. Jackson called it the 'corrupt bargain'.
Andrew Jackson|Democratic-Republican|John C. Calhoun|99|151309
John Quincy Adams|Democratic-Republican|John C. Calhoun|84|122440
William H. Crawford|Democratic-Republican|Nathaniel Macon|41|41222
Henry Clay|Democratic-Republican|Nathan Sanford|37|48606
AL 5 5,0,0,0 9461,2422,1663,96 13642
CT 8 0,8,0,0 ,9261,1978, 11266
DE 3 0,1,2,0 ,,,
GA 9 0,0,9,0 ,,,
IL 3 2,1,0,0 1272,1541,847,1047 4707
IN 5 5,0,0,0 7444,3093,,5321 15859
KY 14 0,0,0,14 6658,120,3,17207 23988
LA 5 3,2,0,0 ,,,
MA 15 0,15,0,0 ,37891,, 44824
MD 11 7,3,1,0 14473,14191,3371,695 32730
ME 9 0,9,0,0 ,9412,3081, 12494
MO 3 0,0,0,3 1166,191,34,2042 3433
MS 3 3,0,0,0 3313,1718,121,21 5173
NC 15 15,0,0,0 20418,,15629, 36048
NH 8 0,8,0,0 ,10203,, 13478
NJ 8 8,0,0,0 10342,8405,1232, 20190
NY 36 1,26,5,4 ,,,
OH 16 0,0,0,16 18411,12686,,19165 50262
PA 28 28,0,0,0 36091,5424,4189,1703 47407
RI 4 0,4,0,0 ,2144,200, 2344
SC 11 11,0,0,0 ,,,
TN 11 11,0,0,0 19285,224,316,890 20715
VA 24 0,0,24,0 2975,3514,8558,419 15371
VT 7 0,7,0,0 ,,,
@1828|0|1145788||Jackson's rematch and revenge on Adams, as the franchise widened and the modern Democratic Party took shape.
Andrew Jackson|Democratic|John C. Calhoun|178|638348
John Quincy Adams|National Republican|Richard Rush|83|507440
AL 5 5,0 16750,1976 18726
CT 8 0,8 4488,13838 18326
DE 3 0,3 ,
GA 9 9,0 17703,605 18308
IL 3 3,0 9582,4681 14263
IN 5 5,0 22140,16978 39118
KY 14 14,0 39085,31456 70541
LA 5 5,0 4603,4076 8679
MA 15 0,15 6016,29842 35858
MD 11 5,6 24565,25517
ME 9 1,8 13808,20558 34366
MO 3 3,0 8287,3672 11959
MS 3 3,0 7086,1602 8688
NC 15 15,0 37634,13938 51572
NH 8 0,8 21182,24120 45302
NJ 8 0,8 21951,23764 45715
NY 36 20,16 139412,131563 270975
OH 16 16,0 67596,63456 131052
PA 28 28,0 102151,50783 152934
RI 4 0,4 820,2753 3573
SC 11 11,0 ,
TN 11 11,0 44092,2240
VA 24 24,0 26842,11989 38831
VT 7 0,7 8335,24365 32700
@1832|0|1276659||Jackson re-elected on his war against the Bank of the United States. The first election with national nominating conventions; the Anti-Masons were the first third party to run one.
Andrew Jackson|Democratic|Martin Van Buren|219|702735
Henry Clay|National Republican|John Sergeant|49|474107
John Floyd|Nullifier|Henry Lee|11|
William Wirt|Anti-Masonic|Amos Ellmaker|7|99817
AL 7 7,0,0,0 14286,5,, 14291
CT 8 0,8,0,0 11269,18155,,3409 32833
DE 3 0,3,0,0 4110,4276,, 8386
GA 11 11,0,0,0 20750,,, 20750
IL 5 5,0,0,0 14609,6745,,97 21481
IN 9 9,0,0,0 31551,25472,,27 57050
KY 15 0,15,0,0 36292,43449,, 79741
LA 5 5,0,0,0 3908,2429,, 6337
MA 14 0,14,0,0 13933,31963,,14692 67619
MD 10 3,5,0,0 19156,19160,,
ME 10 10,0,0,0 33978,27331,,844 62153
MO 4 4,0,0,0 8904,4760,, 5192
MS 4 4,0,0,0 5750,,, 5750
NC 15 15,0,0,0 25261,4538,, 29799
NH 7 7,0,0,0 24855,18938,, 43793
NJ 8 8,0,0,0 23826,23466,,468 47760
NY 42 42,0,0,0 168497,154896,, 323393
OH 21 21,0,0,0 81246,76539,,509 158294
PA 30 30,0,0,0 91949,,,66689 158638
RI 4 0,4,0,0 2051,2871,,819 5742
SC 11 0,0,11,0 ,,,
TN 15 15,0,0,0 28078,1347,, 29425
VA 23 23,0,0,0 34243,11436,,3 45682
VT 7 0,0,0,7 7870,11152,,13106 32128
@1836|0|1502811||The Whigs ran regional candidates hoping to force the race into the House; it failed. Virginia's electors withheld their votes from Van Buren's running mate, so the Senate picked the vice president, the only time that has happened.
Martin Van Buren|Democratic|Richard M. Johnson|170|763291
William Henry Harrison|Whig|Francis Granger|73|549907
Hugh L. White|Whig|John Tyler|26|146107
Daniel Webster|Whig|Francis Granger|14|41201
Willie P. Mangum|Whig|John Tyler|11|
AL 7 7,0,0,0,0 20638,,16658,, 37296
AR 3 3,0,0,0,0 2380,,1334,, 3714
CT 8 8,0,0,0,0 19294,18799,,, 38093
DE 3 0,3,0,0,0 4154,4736,,, 8895
GA 11 0,0,11,0,0 22778,,24481,, 47259
IL 5 5,0,0,0,0 18369,15220,,, 33589
IN 9 0,9,0,0,0 32478,41281,,, 73759
KY 15 0,15,0,0,0 33229,36861,,, 70090
LA 5 5,0,0,0,0 3842,,3583,, 7425
MA 14 0,0,0,14,0 33486,,,41201, 74687
MD 10 0,10,0,0,0 22267,25852,,, 48119
ME 10 10,0,0,0,0 22825,14803,,, 38740
MI 3 3,0,0,0,0 7122,5545,,, 12667
MO 4 4,0,0,0,0 10995,,7337,, 18332
MS 4 4,0,0,0,0 10297,,9782,, 20079
NC 15 15,0,0,0,0 26631,,23521,, 50153
NH 7 7,0,0,0,0 18697,6228,,, 24925
NJ 8 0,8,0,0,0 25592,26137,,, 51729
NY 42 42,0,0,0,0 166795,138548,,, 305343
OH 21 0,21,0,0,0 96238,104958,,, 202333
PA 30 30,0,0,0,0 91457,87235,,, 178692
RI 4 4,0,0,0,0 2964,2710,,, 5674
SC 11 0,0,0,0,11 ,,,,
TN 15 0,0,15,0,0 26170,,36027,, 62197
VA 23 23,0,0,0,0 30556,,23384,, 53945
VT 7 0,7,0,0,0 14037,20994,,, 35031
@1840|0|2412694||'Tippecanoe and Tyler Too': the log-cabin campaign, in the wake of the Panic of 1837. Harrison died a month into his term, the first president to die in office.
William Henry Harrison|Whig|John Tyler|234|1275585
Martin Van Buren|Democratic|Richard M. Johnson|60|1129645
AL 7 0,7 28515,33996 62511
AR 3 0,3 5160,6679 11839
CT 8 8,0 31598,25281 56879
DE 3 3,0 5967,4872 10852
GA 11 11,0 40339,31983 72322
IL 5 0,5 45574,47441 93175
IN 9 9,0 65302,51604 116906
KY 15 15,0 58488,32616 91104
LA 5 5,0 11296,7616 18912
MA 14 14,0 72852,52355 126825
MD 10 10,0 33528,28752 62280
ME 10 10,0 46612,46190 92802
MI 3 3,0 22933,21096 44350
MO 4 0,4 22954,29969 52923
MS 4 4,0 19515,17010 36525
NC 15 15,0 46567,34168 80735
NH 7 0,7 26310,32774 59956
NJ 8 8,0 33351,31034 64454
NY 42 42,0 226001,212733 441543
OH 21 21,0 148157,124782 273842
PA 30 30,0 144010,143676 288026
RI 4 4,0 5278,3301 8621
SC 11 0,11 ,
TN 15 15,0 60194,47951 108145
VA 23 0,23 42639,43757 86394
VT 7 7,0 32445,18009 50773
@1844|0|2703864||Polk, the first 'dark horse', ran on expansion: Texas and Oregon. Clay lost New York narrowly, with Liberty Party votes the likely difference.
James K. Polk|Democratic|George M. Dallas|170|1339570
Henry Clay|Whig|Theodore Frelinghuysen|105|1300157
AL 9 9,0 37401,26002 63403
AR 3 3,0 9546,5604 15150
CT 6 0,6 29841,32832 64616
DE 3 0,3 5970,6271 12247
GA 10 10,0 44147,42100 86247
IL 9 9,0 58795,45854 109057
IN 12 12,0 70181,67867 140154
KY 12 0,12 51988,61249 116865
LA 6 6,0 13782,13083 26865
MA 12 0,12 53039,67062 132037
MD 8 0,8 32706,35984 68690
ME 9 9,0 45719,34378 84933
MI 5 5,0 27737,24375 55751
MO 7 7,0 41322,31200 72522
MS 6 6,0 25846,19158 45004
NC 11 0,11 39287,43232 82521
NH 6 6,0 27160,17866 49187
NJ 7 0,7 37495,38318 75944
NY 36 36,0 237588,232482 485882
OH 23 0,23 149061,155113 312224
PA 26 26,0 167447,161125 331572
RI 4 0,4 4867,7322 12296
SC 9 9,0 ,
TN 13 0,13 59917,60040 119957
VA 17 17,0 50679,44860 95539
VT 6 0,6 18049,26780 48829
@1848|0|2876818||War hero Taylor wins after the Mexican War. Former president Van Buren's Free Soil run against the spread of slavery took a tenth of the vote. The first election held on one day nationwide.
Zachary Taylor|Whig|Millard Fillmore|163|1360235
Lewis Cass|Democratic|William O. Butler|127|1222353
Martin Van Buren|Free Soil|Charles F. Adams|0|291475
AL 9 0,9,0 30482,31173, 61655
AR 3 0,3,0 7587,9301, 16888
CT 6 6,0,0 30318,27051,5005 62398
DE 3 3,0,0 6440,5910,82 12423
FL 3 3,0,0 4120,3083, 7203
GA 10 10,0,0 47532,44785, 92317
IA 4 0,4,0 9930,11238,1103 22271
IL 9 0,9,0 52853,55952,15702 124596
IN 12 0,12,0 69907,74745,8100 152752
KY 12 12,0,0 67145,49720, 116865
LA 6 6,0,0 18487,15379, 33866
MA 12 12,0,0 61072,35281,38333 134748
MD 8 8,0,0 37702,34528,129 72359
ME 9 0,9,0 35273,40195,12157 87625
MI 5 0,5,0 23947,30742,10393 65082
MO 7 0,7,0 32671,40077, 72748
MS 6 0,6,0 25911,26545, 52456
NC 11 11,0,0 44054,35772, 79826
NH 6 0,6,0 14781,27763,7560 50104
NJ 7 7,0,0 40015,36901,819 77735
NY 36 36,0,0 218583,114319,120497 455944
OH 23 0,23,0 138359,154773,35347 328479
PA 26 26,0,0 185313,171976,11263 368552
RI 4 4,0,0 6779,3646,730 11155
SC 9 0,9,0 ,,
TN 13 13,0,0 64321,58142, 122463
TX 4 0,4,0 4509,10668, 15177
VA 17 0,17,0 45265,46739, 92004
VT 6 6,0,0 23132,10948,13837 47922
WI 4 0,4,0 13747,15001,10418 39166
@1852|0|3156800||Pierce routs Scott. The Whig Party, split over slavery, never recovers; this is its last real national campaign.
Franklin Pierce|Democratic|William R. King|254|1598363
Winfield Scott|Whig|William A. Graham|42|1385255
John P. Hale|Free Soil|George W. Julian|0|155441
AL 9 9,0,0 26881,15061, 44147
AR 4 4,0,0 12179,7430, 19609
CA 4 4,0,0 40585,35752,62 76337
CT 6 6,0,0 33249,30359,3161 66769
DE 3 3,0,0 6330,6299,63 12692
FL 3 3,0,0 4318,2875, 7193
GA 10 10,0,0 34708,16639, 62472
IA 4 4,0,0 17824,16195,1612 35631
IL 11 11,0,0 80368,64733,9863 154964
IN 13 13,0,0 94890,80901,6928 182719
KY 12 0,12,0 53807,57108,256 111171
LA 6 6,0,0 18653,17255, 35908
MA 13 0,13,0 45875,52863,28023 128431
MD 8 8,0,0 40428,35080,56 75564
ME 8 8,0,0 41609,32543,8030 82182
MI 6 6,0,0 41842,33860,7237 82939
MO 9 9,0,0 38610,29947, 68557
MS 7 7,0,0 26110,16773, 42883
NC 10 10,0,0 39784,39108,59 78951
NH 5 5,0,0 28503,15486,6546 50535
NJ 7 7,0,0 44301,38551,336 83926
NY 35 35,0,0 262083,234896,25435 522873
OH 23 23,0,0 169190,152577,31133 352900
PA 27 27,0,0 198591,179216,8596 388080
RI 4 4,0,0 8735,7626,644 17005
SC 8 8,0,0 ,,
TN 12 0,12,0 57056,58807, 115863
TX 4 4,0,0 11519,4187, 15706
VA 15 15,0,0 73833,58732, 132565
VT 5 0,5,0 13044,22156,8621 43821
WI 5 5,0,0 33658,22240,8842 64682
@1856|0|4051605||The new Republican Party's first run, with Frémont, wins most of the North. Fillmore's Know Nothings take a fifth of the vote, and Maryland.
James Buchanan|Democratic|John C. Breckinridge|174|1835140
John C. Frémont|Republican|William L. Dayton|114|1340668
Millard Fillmore|Know Nothing|Andrew J. Donelson|8|872703
AL 9 9,0,0 46739,,28552 75291
AR 4 4,0,0 21910,,10732 32642
CA 4 4,0,0 53342,20704,36195 110255
CT 6 0,6,0 34997,42717,2615 80329
DE 3 3,0,0 8004,310,6275 14589
FL 3 3,0,0 6358,,4833 11191
GA 10 10,0,0 56581,,42439 99020
IA 4 0,4,0 37568,45073,9669 92310
IL 11 11,0,0 105528,96275,37531 239334
IN 13 13,0,0 118670,94375,22386 235431
KY 12 12,0,0 74642,,67416 142058
LA 6 6,0,0 22164,,20709 42873
MA 13 0,13,0 39244,108172,19626 170048
MD 8 0,0,8 39123,285,47452 86860
ME 8 0,8,0 39140,67279,3270 109689
MI 6 0,6,0 52139,71762,1660 125561
MO 9 9,0,0 57964,,48522 106486
MS 7 7,0,0 35456,,24191 59647
NC 10 10,0,0 48243,,36720 84963
NH 5 0,5,0 31891,37473,410 69774
NJ 7 7,0,0 46943,28338,24115 99396
NY 35 0,35,0 195878,276004,124604 596486
OH 23 0,23,0 170874,187497,28126 386497
PA 27 27,0,0 230686,147286,82189 460161
RI 4 0,4,0 6680,11467,1675 19822
SC 8 8,0,0 ,,
TN 12 12,0,0 69704,,63878 133582
TX 4 4,0,0 31169,,15639 46808
VA 15 15,0,0 90083,,60150 150223
VT 5 0,5,0 10577,39561,545 50748
WI 5 0,5,0 52843,66090,579 119512
@1860|0|4676645||The country splits four ways over slavery. Lincoln wins without a single Southern electoral vote and was not even on the ballot in most of the South. Secession follows within weeks.
Abraham Lincoln|Republican|Hannibal Hamlin|180|1855276
John C. Breckinridge|Southern Democratic|Joseph Lane|72|672601
John Bell|Constitutional Union|Edward Everett|39|590980
Stephen A. Douglas|Northern Democratic|Herschel V. Johnson|12|1004042
AL 9 0,9,0,0 ,48669,27835,13618 90122
AR 4 0,4,0,0 ,28732,20095,5390 54217
CA 4 4,0,0,0 38734,33975,9136,38025 119870
CT 6 6,0,0,0 43486,16558,3337,17364 80745
DE 3 0,3,0,0 3822,7339,3886,1066 16113
FL 3 0,3,0,0 ,8155,4731,221 13107
GA 10 0,10,0,0 ,52181,42954,11687 106822
IA 4 4,0,0,0 70316,1038,1763,55091 128208
IL 11 11,0,0,0 171106,2291,4852,158264 336548
IN 13 13,0,0,0 139013,12295,5339,115166 271818
KY 12 0,0,12,0 1364,53143,66058,25641 146206
LA 6 0,6,0,0 ,22681,20204,7625 50510
MA 13 13,0,0,0 106533,6105,22332,34370 169340
MD 8 0,8,0,0 2296,42505,41768,6080 92649
ME 8 8,0,0,0 62915,6377,2046,29761 101099
MI 6 6,0,0,0 88450,805,415,64889 154559
MN 4 4,0,0,0 22076,744,53,11923 34796
MO 9 0,0,0,9 17029,31312,58261,58804 165406
MS 7 0,7,0,0 ,40797,25045,3288 69130
NC 10 0,10,0,0 ,48539,44990,2701 96230
NH 5 5,0,0,0 37519,2125,412,25887 65943
NJ 7 4,0,0,3 58344,,, 121213
NY 35 35,0,0,0 353804,,, 658905
OH 23 23,0,0,0 231808,11404,12194,187419 442961
OR 3 3,0,0,0 5344,5074,212,4131 14761
PA 27 27,0,0,0 268030,,12770,16765 476436
RI 4 4,0,0,0 12244,,,7707 19951
SC 8 0,8,0,0 ,,,
TN 12 0,0,12,0 ,65053,69710,11384 146147
TX 4 0,4,0,0 ,47639,15422,18 63061
VA 15 0,0,15,0 1909,74350,74691,16183 167133
VT 5 5,0,0,0 33808,1866,218,8649 44541
WI 5 5,0,0,0 86114,889,153,65024 152180
@1864|0|4018202|AL AR FL GA LA MS NC SC TN TX VA|Held during the Civil War. Eleven Confederate states cast no counted votes, including Tennessee and Louisiana, whose returns Congress rejected. Lincoln ran on a National Union ticket with Andrew Johnson.
Abraham Lincoln|National Union|Andrew Johnson|212|2211317
George B. McClellan|Democratic|George H. Pendleton|21|1806227
CA 5 5,0 62140,43838 105986
CT 6 6,0 44693,42288 86981
DE 3 0,3 8155,8767 16922
IA 8 8,0 88500,49525 138025
IL 16 16,0 189512,158724 348236
IN 13 13,0 150422,130233 280655
KS 3 3,0 17089,3836 21580
KY 11 0,11 27787,64301 92088
MA 12 12,0 126742,48745 175490
MD 7 7,0 40153,32739 72892
ME 7 7,0 67805,46992 114797
MI 8 8,0 79149,68513 147662
MN 4 4,0 25055,17357 42422
MO 11 11,0 72750,31596 104346
NH 5 5,0 36596,33034 69630
NJ 7 0,7 60723,68024 128747
NV 2 2,0 9826,6594 16420
NY 33 33,0 368735,361986 730721
OH 21 21,0 265654,205599 471253
OR 3 3,0 9888,8457 18345
PA 26 26,0 296391,276316 572707
RI 4 4,0 13962,8470 22432
VT 5 5,0 42420,13322 55742
WI 8 8,0 83458,65884 149342
WV 5 5,0 23799,11078 34877
@1868|0|5722440|MS TX VA|First election after the war. Virginia, Mississippi and Texas were not yet readmitted and cast no votes. Black men voted in large numbers in the South for the first time.
Ulysses S. Grant|Republican|Schuyler Colfax|214|3013790
Horatio Seymour|Democratic|Francis P. Blair Jr.|80|2708980
AL 8 8,0 76667,72921 149594
AR 5 5,0 22112,19078 41190
CA 5 5,0 54592,54078 108670
CT 6 6,0 50788,47844 98632
DE 3 0,3 7614,10957 18571
FL 3 3,0 0,0
GA 9 0,9 57109,102707 159816
IA 8 8,0 120399,74040 194439
IL 16 16,0 250304,199116 449420
IN 13 13,0 176552,166980 343532
KS 3 3,0 30027,13600 43630
KY 11 0,11 39566,115889 155455
LA 7 0,7 33263,80225 113488
MA 12 12,0 136379,59103 195508
MD 7 0,7 30438,62357 92795
ME 7 7,0 70502,42460 112962
MI 8 8,0 128560,97060 225620
MN 4 4,0 43722,28096 71818
MO 11 11,0 86860,65628 152488
NC 9 9,0 96939,84559 181498
NE 3 3,0 9772,5519 15291
NH 5 5,0 37718,30575 68304
NJ 7 0,7 80131,83001 163132
NV 3 3,0 6480,5218 11698
NY 33 0,33 419888,429883 849771
OH 21 21,0 280167,238621 518788
OR 3 0,3 10961,11125 22086
PA 26 26,0 342280,313382 655662
RI 4 4,0 12993,6548 19541
SC 6 6,0 62301,45237 107538
TN 10 10,0 56628,26129 82757
VT 5 5,0 44167,12045 56212
WI 8 8,0 108900,84703 193603
WV 5 5,0 29015,20306 49321
@1872|0|6471983|AR LA|Greeley died after the vote and before the electors met. The 66 electors shown as his went: Hendricks 42, Brown 18, Jenkins 2, Davis 1, and 3 for Greeley himself that Congress rejected. Arkansas's and Louisiana's returns were thrown out.
Ulysses S. Grant|Republican|Henry Wilson|286|3597439
Horace Greeley|Liberal Republican|B. Gratz Brown|66|2833710
AL 10 10,0 90272,79444 169716
CA 6 6,0 51181,39060 91387
CT 6 6,0 50314,45695 96009
DE 3 3,0 11129,10205 21822
FL 4 4,0 17763,15427 33190
GA 11 0,11 62550,76356 138906
IA 11 11,0 131566,81636 216365
IL 21 21,0 241936,184884 429971
IN 15 15,0 186147,163632 351196
KS 5 5,0 66805,32970 100512
KY 12 0,12 88766,99995 191135
MA 13 13,0 133455,59195 192864
MD 8 0,8 66760,67687 134447
ME 7 7,0 61426,29097 90523
MI 11 11,0 138758,78551 221455
MN 5 5,0 55708,35211 90919
MO 15 0,15 119196,151434 273059
MS 8 8,0 82175,47282 129457
NC 10 10,0 94772,70130 165163
NE 3 3,0 18329,7603 25932
NH 5 5,0 37168,31425 68906
NJ 9 9,0 91656,76456 168112
NV 3 3,0 8413,6236 14649
NY 35 35,0 440738,387282 828020
OH 22 22,0 281852,244321 529436
OR 3 3,0 11818,7742 20147
PA 29 29,0 349589,212041 563262
RI 4 4,0 13665,5329 18994
SC 7 7,0 72290,22699 95452
TN 12 0,12 85655,93391 179046
TX 8 0,8 47468,66546 116594
VA 11 11,0 93463,91647 185195
VT 5 5,0 41480,10926 52980
WI 10 10,0 104994,86477 192305
WV 5 5,0 32320,29532 62467
@1876|0|8418659||Tilden won the popular vote, but 20 electoral votes were disputed. A congressional commission awarded them all to Hayes, who won by one; the bargain around it ended Reconstruction.
Rutherford B. Hayes|Republican|William A. Wheeler|185|4034142
Samuel J. Tilden|Democratic|Thomas A. Hendricks|184|4286808
Peter Cooper|Greenback|Samuel F. Cary|0|83726
AL 10 0,10,0 68708,102989,0 171699
AR 6 0,6,0 38649,58086,211 96946
CA 6 6,0,0 78614,75845,47 154544
CO 3 3,0,0 0,0,0
CT 6 0,6,0 59033,61927,774 122134
DE 3 0,3,0 10752,13381,0 24133
FL 4 4,0,0 23849,22927,0 46776
GA 11 0,11,0 50533,130157,0 180690
IA 11 11,0,0 171326,112121,9431 292878
IL 21 21,0,0 278232,258611,17207 554227
IN 15 0,15,0 208011,213526,17233 438911
KS 5 5,0,0 78324,37902,7770 124134
KY 12 0,12,0 97568,160060,0 260626
LA 8 8,0,0 75315,70508,0 145823
MA 13 13,0,0 150064,108777,0 259620
MD 8 0,8,0 71980,91779,0 163759
ME 7 7,0,0 66300,49917,0 117045
MI 11 11,0,0 166901,141685,9023 318450
MN 5 5,0,0 72955,48587,2389 124075
MO 15 0,15,0 145027,202086,3497 350610
MS 8 0,8,0 52603,112173,0 164776
NC 10 0,10,0 108484,125427,0 233911
NE 3 3,0,0 31915,17413,0 49328
NH 5 5,0,0 41540,38510,0 80141
NJ 9 0,9,0 103517,115962,714 220193
NV 3 3,0,0 10383,9308,0 19691
NY 35 0,35,0 489207,521949,1978 1015503
OH 22 22,0,0 330698,323182,3057 658649
OR 3 3,0,0 15214,14157,510 29881
PA 29 29,0,0 384184,366204,7204 758993
RI 4 4,0,0 15787,10712,68 26627
SC 7 7,0,0 91786,90897,0 182683
TN 12 0,12,0 89566,133177,0 222743
TX 8 0,8,0 44800,104755,0 149555
VA 11 0,11,0 95518,140770,0 236288
VT 5 5,0,0 44091,20254,0 64553
WI 10 10,0,0 130067,123926,1509 257177
WV 5 0,5,0 41997,56546,1104 99647
@1880|0|9219477||The closest popular vote ever, about a tenth of a point apart. Garfield was shot four months into his term and died that September.
James A. Garfield|Republican|Chester A. Arthur|214|4454433
Winfield S. Hancock|Democratic|William H. English|155|4444976
James B. Weaver|Greenback|Benjamin J. Chambers|0|308649
AL 10 0,10,0 56221,91185,4642 152048
AR 6 0,6,0 42436,60775,4079 107290
CA 6 1,5,0 80348,80442,3394 164321
CO 3 3,0,0 27450,24647,1435 53546
CT 6 6,0,0 67071,64415,868 132802
DE 3 0,3,0 14133,15275,120 29528
FL 4 0,4,0 23654,27964,0 51618
GA 11 0,11,0 54086,102470,0 156556
IA 11 11,0,0 183927,105845,32701 323498
IL 21 21,0,0 318037,277321,26358 622312
IN 15 15,0,0 232164,225522,12986 470672
KS 5 5,0,0 121549,59801,19851 201226
KY 12 0,12,0 106306,149068,11499 267104
LA 8 0,8,0 38637,65067,439 104143
MA 13 13,0,0 165205,111690,4548 282125
MD 8 0,8,0 78515,93706,818 173039
ME 7 7,0,0 74056,65171,4480 143903
MI 11 11,0,0 185341,131597,34895 352777
MN 5 5,0,0 93903,53315,3267 150771
MO 15 0,15,0 153567,208609,35135 400311
MS 8 0,8,0 34854,75750,5797 116401
NC 10 0,10,0 115874,124208,1126 241208
NE 3 3,0,0 54979,28523,3950 87452
NH 5 5,0,0 44852,40794,528 86354
NJ 9 0,9,0 120555,122565,2617 245928
NV 3 0,3,0 8732,9613,0 18345
NY 35 35,0,0 555544,534511,12373 1103945
OH 22 22,0,0 375048,340821,6456 724967
OR 3 3,0,0 20619,19955,245 40819
PA 29 29,0,0 444704,407428,20668 874783
RI 4 4,0,0 18195,10779,236 29235
SC 7 0,7,0 58071,112312,566 170949
TN 12 0,12,0 107677,129191,5917 242828
TX 8 0,8,0 57893,156428,27405 241726
VA 11 0,11,0 83533,128083,0 211616
VT 5 5,0,0 45567,18316,1215 65203
WI 10 10,0,0 144400,114649,7986 267195
WV 5 0,5,0 46243,57391,9079 112713
@1884|0|10060145||A scandal-heavy campaign. Cleveland becomes the first Democrat elected since before the Civil War, on a thin margin in New York.
Grover Cleveland|Democratic|Thomas A. Hendricks|219|4914482
James G. Blaine|Republican|John A. Logan|182|4856903
John St. John|Prohibition|William Daniel|0|150890
Benjamin Butler|Greenback|Absolom M. West|0|134294
AL 10 10,0,0,0 92736,59444,610,762 153624
AR 7 7,0,0,0 72734,51198,0,1847 125779
CA 8 0,8,0,0 89288,102369,2965,2037 196988
CO 3 0,3,0,0 27723,36084,756,1956 66519
CT 6 6,0,0,0 67182,65898,2493,1684 137257
DE 3 3,0,0,0 16957,12953,64,10 29984
FL 4 4,0,0,0 31769,28031,72,0 59990
GA 12 12,0,0,0 94667,48603,195,145 143610
IA 13 0,13,0,0 177316,197089,1499,0 377201
IL 22 0,22,0,0 312351,337469,12074,10776 672670
IN 15 15,0,0,0 245005,238489,3028,8810 495332
KS 9 0,9,0,0 90132,154406,4495,16346 265848
KY 13 13,0,0,0 152961,118690,3139,1691 276481
LA 8 8,0,0,0 62594,46347,338,120 109399
MA 14 0,14,0,0 122352,146724,9923,24382 303383
MD 8 8,0,0,0 96866,85748,2827,578 186019
ME 6 0,6,0,0 52153,72217,2160,3955 130491
MI 13 0,13,0,0 189361,192669,18403,753 401186
MN 7 0,7,0,0 70065,111685,4684,3583 190017
MO 16 16,0,0,0 236023,203081,2164,0 441268
MS 9 9,0,0,0 77653,43035,0,0 120688
NC 11 11,0,0,0 142905,125021,430,0 268356
NE 5 0,5,0,0 54391,76912,2899,0 134202
NH 4 0,4,0,0 39198,43254,1580,554 84586
NJ 9 9,0,0,0 127798,123440,6159,3496 260921
NV 3 0,3,0,0 5578,7193,0,26 12797
NY 36 36,0,0,0 563154,562005,25006,17004 1167169
OH 23 0,23,0,0 368280,400082,11069,5179 784610
OR 3 0,3,0,0 24604,26860,492,726 52682
PA 30 0,30,0,0 392785,478804,15283,16992 903864
RI 4 0,4,0,0 12391,19030,928,422 32771
SC 9 9,0,0,0 69845,21730,0,0 92812
TN 12 12,0,0,0 133770,124101,1150,957 259978
TX 13 13,0,0,0 225309,93141,3534,3321 325305
VA 12 12,0,0,0 145491,139356,130,0 284977
VT 4 0,4,0,0 17331,39514,1753,785 59401
WI 11 0,11,0,0 146453,161135,7649,4598 319835
WV 6 6,0,0,0 67311,63096,939,799 132145
@1888|0|11388846||Cleveland won the popular vote and lost the Electoral College, with New York decisive. The tariff was the central issue.
Benjamin Harrison|Republican|Levi P. Morton|233|5443633
Grover Cleveland|Democratic|Allen G. Thurman|168|5538163
Clinton B. Fisk|Prohibition|John A. Brooks|0|250017
Alson Streeter|Union Labor|Charles E. Cunningham|0|149115
AL 10 0,10,0,0 57177,117314,594,0 175085
AR 7 0,7,0,0 59752,86062,614,10630 157058
CA 8 8,0,0,0 124816,117729,5761,0 251339
CO 3 3,0,0,0 50772,37549,2182,1266 91946
CT 6 0,6,0,0 74584,74920,4234,240 153978
DE 3 0,3,0,0 12950,16414,399,0 29764
FL 4 0,4,0,0 26529,39557,414,0 66500
GA 12 0,12,0,0 40499,100493,1808,136 142936
IA 13 13,0,0,0 211603,179877,3550,9105 404135
IL 22 22,0,0,0 370475,348351,21703,7134 747813
IN 15 15,0,0,0 263361,261013,9881,2694 536949
KS 9 9,0,0,0 182904,102745,6779,37788 331149
KY 13 0,13,0,0 155138,183830,5223,677 344868
LA 8 0,8,0,0 30660,85032,160,39 115891
MA 14 14,0,0,0 183892,151590,8701,0 344243
MD 8 0,8,0,0 99986,106188,4767,0 210941
ME 6 6,0,0,0 73730,50472,2691,1344 128253
MI 13 13,0,0,0 236387,213469,20945,4555 475356
MN 7 7,0,0,0 142492,104385,15311,1097 263285
MO 16 0,16,0,0 236252,261943,4539,18626 521360
MS 9 0,9,0,0 30095,85451,240,0 115786
NC 11 0,11,0,0 134784,147902,2840,0 285563
NE 5 5,0,0,0 108425,80552,9429,4226 202632
NH 4 4,0,0,0 45728,43456,1593,0 90835
NJ 9 0,9,0,0 144360,151508,7933,0 303801
NV 3 3,0,0,0 7088,5149,41,0 12278
NY 36 36,0,0,0 650338,635965,30231,627 1319748
OH 23 23,0,0,0 416054,396455,24356,3496 840361
OR 3 3,0,0,0 33291,26522,1677,0 61853
PA 30 30,0,0,0 526091,446633,20947,3873 997568
RI 4 4,0,0,0 21969,17530,1251,18 40775
SC 9 0,9,0,0 13736,65824,0,0 79997
TN 12 0,12,0,0 138978,158699,5969,48 303694
TX 13 0,13,0,0 88422,234883,4749,29459 357513
VA 12 0,12,0,0 150399,152004,1684,0 304087
VT 4 4,0,0,0 45192,16788,1460,1977 65452
WI 11 11,0,0,0 176553,155232,14277,8552 354614
WV 6 0,6,0,0 78171,78677,1084,1508 159440
@1892|0|12068027||Cleveland returns, the first president to serve non-consecutive terms. Weaver's Populists win 22 electoral votes across the West. Michigan chose its electors by district this one time.
Grover Cleveland|Democratic|Adlai Stevenson I|277|5553898
Benjamin Harrison|Republican|Whitelaw Reid|145|5190799
James B. Weaver|Populist|James G. Field|22|1026595
John Bidwell|Prohibition|James B. Cranfill|0|270889
AL 11 11,0,0,0 138135,9184,84984,240 232543
AR 8 8,0,0,0 87834,47072,11831,113 148117
CA 9 8,1,0,0 118174,118027,25311,8096 269609
CO 4 0,0,4,0 0,38620,53584,1687 93891
CT 6 6,0,0,0 82395,77032,809,4026 164595
DE 3 3,0,0,0 18581,18077,0,564 37235
FL 4 4,0,0,0 30153,0,4843,475 35471
GA 13 13,0,0,0 129446,48408,41939,988 223126
IA 13 0,13,0,0 196367,219795,20595,6402 443159
ID 3 0,0,3,0 0,8599,10520,288 19407
IL 24 24,0,0,0 426281,399288,22207,25871 873647
IN 15 15,0,0,0 262740,255615,22208,13050 553613
KS 10 0,0,10,0 0,157241,163111,4553 324905
KY 13 13,0,0,0 175461,135462,23500,6441 340864
LA 8 8,0,0,0 87926,26963,0,0 114889
MA 15 0,15,0,0 176813,202814,3210,7539 391028
MD 8 8,0,0,0 113866,92736,796,5877 213275
ME 6 0,6,0,0 48049,62936,2396,3066 116451
MI 14 5,9,0,0 201624,222708,19931,20857 466045
MN 9 0,9,0,0 100920,122823,29313,14182 267238
MO 17 17,0,0,0 268400,227646,41204,4333 541583
MS 9 9,0,0,0 40030,1398,10118,973 52519
MT 3 0,3,0,0 17690,18871,7338,562 44461
NC 11 11,0,0,0 132951,100346,44336,2637 280270
ND 3 1,1,1,0 0,17519,17700,899 36118
NE 8 0,8,0,0 24943,87213,83134,4902 200192
NH 4 0,4,0,0 42081,45658,293,1297 89329
NJ 10 10,0,0,0 171066,156101,985,8134 337623
NV 3 0,0,3,0 714,2811,7264,89 10878
NY 36 36,0,0,0 654868,609350,16429,38190 1336793
OH 23 1,22,0,0 404115,405187,14850,26012 850164
OR 4 0,3,1,0 14243,35002,26965,2281 78491
PA 32 0,32,0,0 452264,516011,8714,25123 1003010
RI 4 0,4,0,0 24336,26975,228,1654 53196
SC 9 9,0,0,0 54680,13345,2407,0 70504
SD 4 0,4,0,0 9081,34888,26544,0 70513
TN 12 12,0,0,0 136468,100537,23918,4809 265732
TX 15 15,0,0,0 239148,81144,99688,2165 422145
VA 12 12,0,0,0 164136,113098,12275,2729 292238
VT 4 0,4,0,0 16325,37992,44,1424 55796
WA 4 0,4,0,0 29802,36460,19165,2542 87969
WI 12 12,0,0,0 177325,171101,10019,13136 371581
WV 6 6,0,0,0 84467,80292,4167,2153 171079
WY 3 0,3,0,0 0,8454,7722,530 16735
@1896|0|13938674||Bryan's 'Cross of Gold' against McKinley's front-porch campaign, on gold versus silver, reshaped the parties for a generation.
William McKinley|Republican|Garret Hobart|271|7112138
William Jennings Bryan|Democratic|Arthur Sewall|176|6510807
John M. Palmer|National Democratic|Simon B. Buckner|0|133537
AL 11 0,11,0 55673,130298,6375 194580
AR 8 0,8,0 37512,110103,0 149347
CA 9 8,1,0 146688,144766,1730 298419
CO 4 0,4,0 26271,161005,1 189539
CT 6 6,0,0 110285,56740,4336 174390
DE 3 3,0,0 20450,16574,966 38456
FL 4 0,4,0 11298,32756,1778 46488
GA 13 0,13,0 59395,93885,3670 162480
IA 13 13,0,0 289293,223741,4516 521547
ID 3 0,3,0 6314,23135,0 29621
IL 24 24,0,0 607130,465613,6390 1090869
IN 15 15,0,0 323754,305573,2145 637119
KS 10 0,10,0 159345,171675,1209 334547
KY 13 12,1,0 218171,217894,5084 445928
LA 8 0,8,0 22037,77175,1834 101046
MA 15 15,0,0 278976,105711,11749 401568
MD 8 8,0,0 136959,104150,2499 250249
ME 6 6,0,0 80403,34587,1867 118419
MI 14 14,0,0 293336,237166,6923 545585
MN 9 9,0,0 193503,139735,3222 341762
MO 17 0,17,0 304940,363667,2365 673906
MS 9 0,9,0 4819,63355,1021 69591
MT 3 0,3,0 10509,42628,0 53330
NC 11 0,11,0 155122,174408,578 331337
ND 3 3,0,0 26335,20686,0 47391
NE 8 0,8,0 103064,115007,2885 223182
NH 4 4,0,0 57444,21650,3520 83670
NJ 10 10,0,0 221535,133695,6378 371211
NV 3 0,3,0 1938,8376,0 10314
NY 36 36,0,0 819838,551369,18950 1423876
OH 23 23,0,0 525991,477497,1858 1014295
OR 4 4,0,0 48779,46739,977 97414
PA 32 32,0,0 728300,433228,11000 1194355
RI 4 4,0,0 37437,14459,1166 54785
SC 9 0,9,0 9313,58801,824 68938
SD 4 0,4,0 41042,41225,0 82950
TN 12 0,12,0 148683,167168,1953 320903
TX 15 0,15,0 167520,370434,5046 544786
UT 3 0,3,0 13491,64607,0 78119
VA 12 0,12,0 135379,154708,2129 294674
VT 4 4,0,0 51127,10640,1331 63847
WA 4 0,4,0 39153,53314,0 93583
WI 12 12,0,0 268135,165523,4584 447409
WV 6 6,0,0 105379,94480,678 201757
WY 3 0,3,0 10072,10861,0 21092
@1900|0|13997429||A rematch fought on prosperity and imperialism. McKinley was assassinated in 1901, and his running mate Theodore Roosevelt became president.
William McKinley|Republican|Theodore Roosevelt|292|7228864
William Jennings Bryan|Democratic|Adlai Stevenson I|155|6370932
John G. Woolley|Prohibition|Henry B. Metcalf|0|210867
Eugene V. Debs|Social Democratic|Job Harriman|0|87945
AL 11 0,11,0,0 55612,97129,2763,0 159692
AR 8 0,8,0,0 44800,81142,584,0 127866
CA 9 9,0,0,0 164755,124985,5024,0 302318
CO 4 0,4,0,0 93072,122733,3790,714 221408
CT 6 6,0,0,0 102572,74014,1617,1029 180195
DE 3 3,0,0,0 22535,18852,546,56 41989
FL 4 0,4,0,0 7355,28273,2244,634 39649
GA 13 0,13,0,0 34260,81180,1402,0 121410
IA 13 13,0,0,0 307808,209265,9502,2742 530355
ID 3 0,3,0,0 27198,29414,857,0 57914
IL 24 24,0,0,0 597985,503061,17626,9687 1131897
IN 15 15,0,0,0 336063,309584,13718,2374 664094
KS 10 10,0,0,0 185955,162601,3605,1605 353766
KY 13 0,13,0,0 227132,235126,2890,766 468265
LA 8 0,8,0,0 14234,53668,0,0 67906
MA 15 15,0,0,0 238866,156997,6202,9607 414804
MD 8 8,0,0,0 136185,122238,4574,904 264434
ME 6 6,0,0,0 65412,36822,2581,878 105693
MI 14 14,0,0,0 316269,211685,11859,2826 544379
MN 9 9,0,0,0 190461,112901,8555,3065 316311
MO 17 0,17,0,0 314092,351922,5965,6139 683656
MS 9 0,9,0,0 5707,51706,0,0 59055
MT 3 0,3,0,0 25409,37311,306,711 63856
NC 11 0,11,0,0 132997,157733,990,0 292518
ND 3 3,0,0,0 35898,20531,731,520 57791
NE 8 8,0,0,0 121835,114013,3655,823 241430
NH 4 4,0,0,0 54799,35489,1270,790 92364
NJ 10 10,0,0,0 221707,164808,7183,4609 401050
NV 3 0,3,0,0 3849,6347,0,0 10196
NY 36 36,0,0,0 822013,678462,22077,12869 1548042
OH 23 23,0,0,0 543918,474882,10203,4847 1040073
OR 4 4,0,0,0 46172,32810,2536,1464 83251
PA 32 32,0,0,0 712665,424232,27908,4831 1173210
RI 4 4,0,0,0 33784,19812,1529,0 56548
SC 9 0,9,0,0 3579,47233,0,0 50812
SD 4 4,0,0,0 54530,39544,1542,169 96124
TN 12 0,12,0,0 123108,145240,3844,346 273860
TX 15 0,15,0,0 130641,267432,2644,1846 423706
UT 3 3,0,0,0 47139,45006,209,720 93189
VA 12 0,12,0,0 115769,146079,2130,0 264208
VT 4 4,0,0,0 42569,12849,383,39 56212
WA 4 4,0,0,0 57456,44833,2363,2006 107524
WI 12 12,0,0,0 265760,159163,10027,7048 442501
WV 6 6,0,0,0 119829,98807,1628,286 220796
WY 3 3,0,0,0 14482,10164,0,21 24687
@1904|0|13525095||Roosevelt wins a term of his own by a landslide, and promises not to seek another.
Theodore Roosevelt|Republican|Charles W. Fairbanks|336|7630557
Alton B. Parker|Democratic|Henry G. Davis|140|5083880
Eugene V. Debs|Socialist|Benjamin Hanford|0|402810
Silas C. Swallow|Prohibition|George W. Carroll|0|259103
AL 11 0,11,0,0 22472,79797,853,612 108785
AR 9 0,9,0,0 46860,64434,1816,993 116421
CA 10 10,0,0,0 205226,89404,29535,7380 331878
CO 5 5,0,0,0 134661,100105,4304,3438 243667
CT 7 7,0,0,0 111089,72909,4543,1506 191128
DE 3 3,0,0,0 23705,19347,146,607 43856
FL 5 0,5,0,0 8314,26449,2337,0 38705
GA 13 0,13,0,0 24004,83466,196,685 130986
IA 13 13,0,0,0 308158,149276,14849,11603 486093
ID 3 3,0,0,0 47783,18480,4949,1013 72578
IL 27 27,0,0,0 632645,327606,69225,34770 1076499
IN 15 15,0,0,0 368289,274345,12013,23496 682185
KS 10 10,0,0,0 212955,86174,15869,7306 328561
KY 13 0,13,0,0 205457,217170,3599,6603 435946
LA 9 0,9,0,0 5205,47708,995,0 53908
MA 16 16,0,0,0 257822,165746,13604,4279 445109
MD 8 1,7,0,0 109497,109446,2247,3034 224229
ME 6 6,0,0,0 65432,27642,2102,1510 97023
MI 14 14,0,0,0 364957,135392,9042,13441 525027
MN 11 11,0,0,0 216651,55187,11692,6253 292860
MO 18 18,0,0,0 321449,296312,13009,7191 643861
MS 10 0,10,0,0 3280,53480,462,0 58721
MT 3 3,0,0,0 34932,21773,5676,335 64444
NC 12 0,12,0,0 82442,124091,124,342 207818
ND 4 4,0,0,0 52595,14273,2009,1137 70014
NE 8 8,0,0,0 138558,52921,7412,6323 225732
NH 4 4,0,0,0 54163,34074,1090,750 90161
NJ 12 12,0,0,0 245164,164566,9587,6845 432547
NV 3 3,0,0,0 6864,3982,925,0 12115
NY 39 39,0,0,0 859533,683981,36883,20787 1617770
OH 23 23,0,0,0 600095,344674,36260,19339 1004393
OR 4 4,0,0,0 60455,17521,7619,3806 90154
PA 34 34,0,0,0 840949,337998,21863,33717 1236738
RI 4 4,0,0,0 41605,24839,956,768 68656
SC 9 0,9,0,0 2554,52563,0,0 55118
SD 4 4,0,0,0 72083,21969,3138,2965 101395
TN 12 0,12,0,0 105363,131653,1354,1889 242750
TX 18 0,18,0,0 51242,167200,2791,4292 234008
UT 3 3,0,0,0 62446,33413,5767,0 101672
VA 12 0,12,0,0 48180,80649,202,1379 130410
VT 4 4,0,0,0 40459,9777,859,792 51888
WA 5 5,0,0,0 101540,28098,10023,3229 145151
WI 13 13,0,0,0 280315,124205,28240,9872 443441
WV 7 7,0,0,0 132620,100855,1573,4599 239986
WY 3 3,0,0,0 20489,8930,1072,217 30708
@1908|0|14889239||Roosevelt's chosen successor beats Bryan, whose third run is his last.
William Howard Taft|Republican|James S. Sherman|321|7678335
William Jennings Bryan|Democratic|John W. Kern|162|6408979
Eugene V. Debs|Socialist|Benjamin Hanford|0|420852
Eugene W. Chafin|Prohibition|Aaron S. Watkins|0|254087
AL 11 0,11,0,0 25561,74391,1450,690 105152
AR 9 0,9,0,0 56624,87015,5842,1026 151822
CA 10 10,0,0,0 214398,127492,28659,11770 386597
CO 5 0,5,0,0 123693,126644,7960,5559 263858
CT 7 7,0,0,0 112915,68255,5113,2380 190003
DE 3 3,0,0,0 25014,22055,239,670 48007
FL 5 0,5,0,0 10654,31104,3747,1356 49360
GA 13 0,13,0,0 41355,72350,584,1452 132504
IA 13 13,0,0,0 275209,200771,8287,9837 494769
ID 3 3,0,0,0 52621,36162,6400,1986 97293
IL 27 27,0,0,0 629932,450810,34711,29364 1155254
IN 15 15,0,0,0 348993,338262,13476,18045 721126
KS 10 10,0,0,0 197216,161209,12420,5033 375946
KY 13 0,13,0,0 235711,244092,4093,5885 490719
LA 9 0,9,0,0 8958,63568,2514,0 75117
MA 16 16,0,0,0 265966,155543,10779,4374 456919
MD 8 2,6,0,0 116513,115908,2323,3302 238531
ME 6 6,0,0,0 66987,35403,1758,1487 106336
MI 14 14,0,0,0 335580,175771,11586,16974 541830
MN 11 11,0,0,0 195843,109401,14527,11107 331304
MO 18 18,0,0,0 347203,346574,15431,4284 715927
MS 10 0,10,0,0 4363,60287,978,0 66904
MT 3 3,0,0,0 32333,29326,5855,827 68822
NC 12 0,12,0,0 114887,136928,372,354 252554
ND 4 4,0,0,0 57680,32885,2421,1496 94525
NE 8 0,8,0,0 126997,131099,3524,5179 266799
NH 4 4,0,0,0 53149,33655,1299,905 89600
NJ 12 12,0,0,0 265298,182522,10249,4930 467111
NV 3 0,3,0,0 10775,11212,2103,0 24526
NY 39 39,0,0,0 870070,667468,38451,22667 1638350
OH 23 23,0,0,0 572312,502721,33795,11402 1121552
OK 7 0,7,0,0 110550,123907,21752,0 256917
OR 4 4,0,0,0 62530,38049,7339,2682 110889
PA 34 34,0,0,0 745779,448782,33914,36694 1267450
RI 4 4,0,0,0 43942,24706,1365,1016 72317
SC 9 0,9,0,0 3945,62288,100,0 66379
SD 4 4,0,0,0 67536,40266,2846,4039 114775
TN 12 0,12,0,0 117977,135608,1870,301 257180
TX 18 0,18,0,0 65666,217302,7870,1634 293757
UT 3 3,0,0,0 61028,42601,4895,0 108613
VA 12 0,12,0,0 52572,82946,255,1111 137065
VT 4 4,0,0,0 39552,11496,0,799 52680
WA 5 5,0,0,0 106062,58691,14177,4700 183879
WI 13 13,0,0,0 247747,166662,28147,11565 454441
WV 7 7,0,0,0 137869,111418,3679,5139 258105
WY 3 3,0,0,0 20846,14918,1715,66 37609
@1912|0|15044278||Roosevelt bolts the party he once led; the Republican split hands Wilson the White House. A sitting president finishes third. Taft's running mate died days before the vote.
Woodrow Wilson|Democratic|Thomas R. Marshall|435|6296284
Theodore Roosevelt|Progressive|Hiram Johnson|88|4122721
William Howard Taft|Republican|James S. Sherman|8|3486242
Eugene V. Debs|Socialist|Emil Seidel|0|901551
Eugene W. Chafin|Prohibition|Aaron S. Watkins|0|208156
AL 12 12,0,0,0,0 82438,22680,9807,3029,0 117959
AR 9 9,0,0,0,0 68814,21644,25585,8153,908 125104
AZ 3 3,0,0,0,0 10324,6949,3021,3163,265 23722
CA 13 2,11,0,0,0 283436,283610,3914,79201,23366 673527
CO 6 6,0,0,0,0 114232,72306,58386,16418,5063 266880
CT 7 7,0,0,0,0 74561,34129,68324,10056,2068 190398
DE 3 3,0,0,0,0 22631,8886,15998,556,623 48694
FL 6 6,0,0,0,0 35343,4555,4279,4806,1854 50837
GA 14 14,0,0,0,0 93087,21985,5191,1058,149 121470
IA 13 13,0,0,0,0 185325,161819,119805,16967,8440 492356
ID 4 4,0,0,0,0 33921,25527,32810,11960,1536 105754
IL 29 29,0,0,0,0 405048,386478,253593,81278,15710 1146173
IN 15 15,0,0,0,0 281890,162007,151267,36931,19249 654474
KS 10 10,0,0,0,0 143663,120210,74845,26779,0 365497
KY 13 13,0,0,0,0 219484,101766,115510,11646,3253 452714
LA 10 10,0,0,0,0 60871,9283,3833,5261,0 79248
MA 18 18,0,0,0,0 173408,142228,155948,12616,2754 488056
MD 8 8,0,0,0,0 112674,57789,54956,3996,2244 231981
ME 6 6,0,0,0,0 51113,48495,26545,2541,946 129640
MI 15 0,15,0,0,0 150751,214584,152244,23211,8934 550976
MN 12 0,12,0,0,0 106426,125856,64334,27505,7886 334219
MO 18 18,0,0,0,0 330746,124375,207821,28466,5380 698566
MS 10 10,0,0,0,0 57324,3549,1560,2050,0 64483
MT 4 4,0,0,0,0 27941,22456,18512,10885,32 79826
NC 12 12,0,0,0,0 144407,69135,29129,987,118 243776
ND 5 5,0,0,0,0 29555,25726,23090,6966,1243 86580
NE 8 8,0,0,0,0 109008,72681,54226,10185,3383 249483
NH 4 4,0,0,0,0 34724,17794,32927,1981,535 87961
NJ 14 14,0,0,0,0 178289,145410,88835,15948,2936 432739
NM 3 3,0,0,0,0 20437,8347,17733,2859,0 49376
NV 3 3,0,0,0,0 7986,5620,3196,3313,0 20115
NY 45 45,0,0,0,0 655573,390093,455487,63434,19455 1588315
OH 24 24,0,0,0,0 424834,229807,278168,90144,11511 1037094
OK 10 10,0,0,0,0 119156,0,90786,41674,2185 253801
OR 5 5,0,0,0,0 47064,37600,34673,13343,4360 137040
PA 38 0,38,0,0,0 395637,444894,273360,83614,19525 1217736
RI 5 5,0,0,0,0 30412,16878,27703,2049,616 77894
SC 9 9,0,0,0,0 48357,1293,536,164,0 50350
SD 5 0,5,0,0,0 48942,58811,0,4662,3910 116325
TN 12 12,0,0,0,0 133021,54041,60475,3564,832 251933
TX 20 20,0,0,0,0 221589,28853,26755,25743,1738 305120
UT 4 0,0,4,0,0 36579,24174,42100,9023,0 112386
VA 12 12,0,0,0,0 90332,21776,23288,820,709 136975
VT 4 0,0,4,0,0 15354,22132,23332,928,1095 62841
WA 7 0,7,0,0,0 86840,113698,70445,40134,9810 322799
WI 13 13,0,0,0,0 164230,62448,130596,33476,8584 399966
WV 8 8,0,0,0,0 113197,79112,56754,15248,4517 268828
WY 3 3,0,0,0,0 15310,9232,14560,2760,434 42296
@1916|0|18536585||'He kept us out of war.' Wilson's re-election turned on California, by under 4,000 votes. Hughes went to bed thinking he had won.
Woodrow Wilson|Democratic|Thomas R. Marshall|277|9126868
Charles Evans Hughes|Republican|Charles W. Fairbanks|254|8548728
Allan L. Benson|Socialist|George R. Kirkpatrick|0|590524
J. Frank Hanly|Prohibition|Ira Landrith|0|221302
AL 12 12,0,0,0 99409,28662,1916,741 130728
AR 9 9,0,0,0 112211,48879,6999,2015 170104
AZ 3 3,0,0,0 33170,20524,3174,1153 58021
CA 13 13,0,0,0 466289,462516,42898,27713 999603
CO 6 6,0,0,0 178816,102308,10049,2793 294375
CT 7 0,7,0,0 99786,106514,5179,1789 213874
DE 3 0,3,0,0 24753,26011,480,566 51810
FL 6 6,0,0,0 55984,14611,5353,4786 80734
GA 14 14,0,0,0 127754,11294,941,0 160681
IA 13 0,13,0,0 218699,280439,10973,3371 513942
ID 4 4,0,0,0 70054,55368,8066,1127 134615
IL 29 0,29,0,0 950229,1152549,61394,26047 2192707
IN 15 0,15,0,0 334063,341005,21855,16368 718848
KS 10 10,0,0,0 314588,277658,24685,12882 629813
KY 13 13,0,0,0 269990,241854,4734,3039 520078
LA 10 10,0,0,0 79875,6466,292,0 92982
MA 18 0,18,0,0 247885,268784,11058,2993 531823
MD 8 8,0,0,0 138359,117347,2674,2903 262039
ME 6 0,6,0,0 64033,69508,2177,596 136314
MI 15 0,15,0,0 286775,339097,16120,8139 650973
MN 12 0,12,0,0 179152,179544,20117,7793 387364
MO 18 18,0,0,0 398032,369339,14612,3884 786769
MS 10 10,0,0,0 80422,4253,1484,0 86679
MT 4 4,0,0,0 101063,66750,9564,0 177679
NC 12 12,0,0,0 168383,120890,509,55 289837
ND 5 5,0,0,0 55206,53471,5716,997 115390
NE 8 8,0,0,0 158827,117771,7141,2952 287315
NH 4 4,0,0,0 43781,43725,1318,303 89127
NJ 14 0,14,0,0 211018,268982,10405,3182 494442
NM 3 3,0,0,0 33527,31152,1996,112 66787
NV 3 3,0,0,0 17776,12127,3065,348 33316
NY 45 0,45,0,0 759426,879238,45944,19031 1706305
OH 24 24,0,0,0 604161,514753,38092,8080 1165086
OK 10 10,0,0,0 148113,97233,45527,1646 292753
OR 5 0,5,0,0 120087,126813,9711,4729 261650
PA 38 0,38,0,0 521784,703823,42638,28525 1297189
RI 5 0,5,0,0 40394,44858,1914,470 87816
SC 9 9,0,0,0 61846,1550,135,0 63952
SD 5 0,5,0,0 59191,64217,3760,1774 128942
TN 12 12,0,0,0 153280,116223,2542,145 272190
TX 20 20,0,0,0 286514,64999,18969,1985 372467
UT 4 4,0,0,0 84145,54137,4460,149 143146
VA 12 12,0,0,0 101840,48384,1056,678 152025
VT 4 0,4,0,0 22708,40250,798,709 64475
WA 7 7,0,0,0 183388,167208,22800,6868 380994
WI 13 0,13,0,0 191363,220822,27631,7318 447134
WV 8 1,7,0,0 140403,143124,6150,175 289852
WY 3 3,0,0,0 28316,21698,1453,373 51840
@1920|0|26765180||The first election after women won the vote nationwide, and a 'return to normalcy' landslide. Debs drew nearly a million votes from a federal prison cell.
Warren G. Harding|Republican|Calvin Coolidge|404|16144093
James M. Cox|Democratic|Franklin D. Roosevelt|127|9139661
Eugene V. Debs|Socialist|Seymour Stedman|0|913693
Parley P. Christensen|Farmer-Labor|Max S. Hayes|0|265398
AL 12 0,12,0,0 74556,159965,2369,0 237638
AR 9 0,9,0,0 71117,107409,5111,0 183637
AZ 3 3,0,0,0 37016,29546,222,15 66562
CA 13 13,0,0,0 624992,229191,64076,0 944050
CO 6 6,0,0,0 173248,104936,8046,3016 292053
CT 7 7,0,0,0 229238,120721,10350,1947 365518
DE 3 3,0,0,0 52858,39911,988,93 94875
FL 6 0,6,0,0 44853,90515,5189,0 145681
GA 14 0,14,0,0 41089,107162,465,0 148251
IA 13 13,0,0,0 634674,227921,16981,10321 895082
ID 4 4,0,0,0 88975,46579,38,0 135624
IL 29 29,0,0,0 1420480,534395,74747,49630 2094714
IN 15 15,0,0,0 696370,511364,24703,16499 1262964
KS 10 10,0,0,0 369268,185464,15511,0 570318
KY 13 0,13,0,0 452480,456497,6409,0 918708
LA 10 0,10,0,0 38538,87519,0,0 126396
MA 18 18,0,0,0 681153,276691,32267,0 993718
MD 8 8,0,0,0 236117,180626,8876,1645 428443
ME 6 6,0,0,0 136355,58961,2214,0 197840
MI 15 15,0,0,0 762865,233450,28947,10480 1048411
MN 12 12,0,0,0 519421,142994,56106,0 735838
MO 18 18,0,0,0 727162,574799,20242,3291 1332800
MS 10 0,10,0,0 11576,69277,1639,0 82492
MT 4 4,0,0,0 109430,57372,0,12204 179006
NC 12 0,12,0,0 232848,305447,446,0 538741
ND 5 5,0,0,0 160072,37422,8282,0 205776
NE 8 8,0,0,0 247498,119608,9600,0 382743
NH 4 4,0,0,0 95196,62662,1234,0 159092
NJ 14 14,0,0,0 611541,256887,27141,2200 903943
NM 3 3,0,0,0 57634,46668,0,1104 105406
NV 3 3,0,0,0 15479,9851,1864,0 27194
NY 45 45,0,0,0 1871167,781238,203201,18413 2898513
OH 24 24,0,0,0 1182022,780037,57147,0 2021653
OK 10 10,0,0,0 243831,217053,25726,0 486610
OR 5 5,0,0,0 143592,80019,9801,0 238522
PA 38 38,0,0,0 1218216,503843,70571,15704 1852616
RI 5 5,0,0,0 107463,55062,4351,0 167981
SC 9 0,9,0,0 2610,64170,28,0 66808
SD 5 5,0,0,0 110692,35938,0,34707 182237
TN 12 12,0,0,0 219829,206558,2239,0 428626
TX 20 0,20,0,0 114538,288767,8121,0 486641
UT 4 4,0,0,0 81555,56639,3159,4475 145828
VA 12 0,12,0,0 87456,141670,807,243 231033
VT 4 4,0,0,0 68212,20919,0,0 89961
WA 7 7,0,0,0 223137,84298,8913,77246 398715
WI 13 13,0,0,0 498576,113422,80635,0 701280
WV 8 8,0,0,0 282007,220789,5618,0 509942
WY 3 3,0,0,0 35091,17429,0,2180 54700
@1924|0|29097107||Coolidge wins easily in a divided field, as La Follette's Progressives take a sixth of the vote and Wisconsin.
Calvin Coolidge|Republican|Charles G. Dawes|382|15723789
John W. Davis|Democratic|Charles W. Bryan|136|8386242
Robert M. La Follette|Progressive|Burton K. Wheeler|13|4831706
AL 12 0,12,0 45005,112966,8084 166593
AR 9 0,9,0 40564,84795,13173 138532
AZ 3 3,0,0 30516,26235,17210 73961
CA 13 13,0,0 733250,105514,424649 1281900
CO 6 6,0,0 195171,75238,69945 342260
CT 7 7,0,0 246322,110184,42416 400295
DE 3 3,0,0 52441,33445,4979 90885
FL 6 0,6,0 30633,62083,8625 109154
GA 14 0,14,0 30300,123200,12691 166577
IA 13 13,0,0 537635,162600,272243 976960
ID 4 4,0,0 69879,24256,54160 148295
IL 29 29,0,0 1453321,576975,432027 2470067
IN 15 15,0,0 703042,492245,71700 1272390
KS 10 10,0,0 407671,156319,98461 662454
KY 13 13,0,0 398966,374855,38465 815332
LA 10 0,10,0 24670,93218,0 121951
MA 18 18,0,0 703476,280831,141225 1129837
MD 8 8,0,0 162414,148072,47157 358630
ME 6 6,0,0 138440,41964,11382 192192
MI 15 15,0,0 874631,152359,122014 1160419
MN 12 12,0,0 420759,55913,339192 822146
MO 18 18,0,0 648486,572753,84160 1307958
MS 10 0,10,0 8494,100474,3494 112462
MT 4 4,0,0 74138,33805,66123 174423
NC 12 0,12,0 191753,284270,6651 482687
ND 5 5,0,0 94931,13858,89922 199081
NE 8 8,0,0 218585,137289,106701 464173
NH 4 4,0,0 98575,57201,8993 164769
NJ 14 14,0,0 675162,297743,108901 1086079
NM 3 3,0,0 54745,48542,9543 112830
NV 3 3,0,0 11243,5909,9769 26921
NY 45 45,0,0 1820058,950796,474913 3263939
OH 24 24,0,0 1176130,477888,357948 2016237
OK 10 0,10,0 226242,255798,46375 528415
OR 5 5,0,0 142579,67589,68403 279488
PA 38 38,0,0 1401481,409192,307567 2144850
RI 5 5,0,0 125286,76606,7628 210115
SC 9 0,9,0 1123,49008,620 50752
SD 5 5,0,0 101299,27214,75355 203868
TN 12 0,12,0 130882,158537,10656 300275
TX 20 0,20,0 130023,484605,42881 657509
UT 4 4,0,0 77327,47001,32662 156990
VA 12 0,12,0 73312,139716,10377 223602
VT 4 4,0,0 80498,16124,5964 102917
WA 7 7,0,0 220224,42842,150727 421549
WI 13 0,0,13 311614,68115,453678 840826
WV 8 8,0,0 288635,257232,36723 583662
WY 3 3,0,0 41858,12868,25174 79900
@1928|0|36807012||Hoover beats Al Smith, the first Catholic major-party nominee. Religion and Prohibition cost Smith much of the once-solid South.
Herbert Hoover|Republican|Charles Curtis|444|21427123
Al Smith|Democratic|Joseph T. Robinson|87|15015464
Norman Thomas|Socialist|James H. Maurer|0|267478
AL 12 0,12,0 120725,127797,460 248982
AR 9 0,9,0 77751,119196,429 197693
AZ 3 3,0,0 52533,38537,0 91254
CA 13 13,0,0 1162323,614365,19595 1796656
CO 6 6,0,0 253872,133131,3472 392242
CT 7 7,0,0 296614,252040,3019 553031
DE 3 3,0,0 68860,36643,329 105891
FL 6 6,0,0 144168,101764,4036 253672
GA 14 0,14,0 99369,129602,124 229159
IA 13 13,0,0 623570,379311,2960 1009489
ID 4 4,0,0 97322,52926,1293 151541
IL 29 29,0,0 1769141,1313817,19138 3107489
IN 15 15,0,0 848290,562691,3871 1421314
KS 10 10,0,0 513672,193003,6205 713200
KY 13 13,0,0 558064,381070,837 940604
LA 10 0,10,0 51160,164655,0 215833
MA 18 0,18,0 775566,792758,6262 1577823
MD 8 8,0,0 301479,223626,1701 528348
ME 6 6,0,0 179923,81179,1068 262171
MI 15 15,0,0 965396,396762,3516 1372082
MN 12 12,0,0 560977,396451,6774 970976
MO 18 18,0,0 834080,662562,3739 1500721
MS 10 0,10,0 27153,124539,0 151692
MT 4 4,0,0 113300,78578,1667 194108
NC 12 12,0,0 348923,286227,0 635150
ND 5 5,0,0 131441,106648,936 239867
NE 8 8,0,0 345745,197959,3434 547144
NH 4 4,0,0 115404,80715,465 196757
NJ 14 14,0,0 925285,616162,4866 1548195
NM 3 3,0,0 69645,48211,0 118014
NV 3 3,0,0 18327,14090,0 32417
NY 45 45,0,0 2193344,2089863,107332 4405626
OH 24 24,0,0 1627546,864210,8683 2508346
OK 10 10,0,0 394046,219174,3924 618427
OR 5 5,0,0 205341,109223,2720 319942
PA 38 38,0,0 2055382,1067586,18647 3150610
RI 5 0,5,0 117522,118973,0 237194
SC 9 0,9,0 5858,62700,47 68605
SD 5 5,0,0 157603,102660,443 261865
TN 12 12,0,0 195388,167343,631 363473
TX 20 20,0,0 367036,341032,722 708999
UT 4 4,0,0 94618,80985,954 176603
VA 12 12,0,0 164609,140146,250 305358
VT 4 4,0,0 90404,44440,0 135191
WA 7 7,0,0 335844,156772,2615 500840
WI 13 13,0,0 544205,450259,18213 1016831
WV 8 8,0,0 375551,263784,1313 642752
WY 3 3,0,0 52748,29299,788 82835
@1932|0|39751898||The Depression sweeps Hoover out, and Roosevelt promises a New Deal. The realignment holds for a generation.
Franklin D. Roosevelt|Democratic|John Nance Garner|472|22821277
Herbert Hoover|Republican|Charles Curtis|59|15761254
Norman Thomas|Socialist|James H. Maurer|0|884885
AL 11 11,0,0 207910,34675,2030 245354
AR 9 9,0,0 189602,28467,1269 220562
AZ 3 3,0,0 79264,36104,2618 118251
CA 22 22,0,0 1324157,847902,63299 2267966
CO 6 6,0,0 250877,189617,13591 457696
CT 8 0,8,0 281632,288420,20480 594183
DE 3 0,3,0 54319,57073,1376 112901
FL 7 7,0,0 206307,69170,775 276252
GA 12 12,0,0 234118,19863,461 255590
IA 11 11,0,0 598019,414433,20467 1036687
ID 4 4,0,0 109479,71417,526 186625
IL 29 29,0,0 1882304,1432756,67258 3407926
IN 14 14,0,0 862054,677184,21388 1576927
KS 9 9,0,0 424204,349498,18276 791978
KY 11 11,0,0 580574,394716,3853 983063
LA 10 10,0,0 249418,18853,0 268804
MA 17 17,0,0 800148,736959,34305 1580114
MD 8 8,0,0 314314,184184,10489 511054
ME 5 0,5,0 128907,166631,2489 298444
MI 19 19,0,0 871700,739894,39205 1664765
MN 11 11,0,0 600806,363959,25476 1002843
MO 15 15,0,0 1025406,564713,16374 1609894
MS 9 9,0,0 140168,5180,686 146034
MT 4 4,0,0 127286,78078,7891 216479
NC 13 13,0,0 497566,208344,5591 711501
ND 4 4,0,0 178350,71772,3521 256290
NE 7 7,0,0 359082,201177,9876 570137
NH 4 0,4,0 100680,103629,947 205520
NJ 16 16,0,0 806394,775406,42988 1629507
NM 3 3,0,0 95089,54217,1776 151606
NV 3 3,0,0 28756,12674,0 41430
NY 47 47,0,0 2534959,1937963,177397 4688614
OH 26 26,0,0 1301695,1227319,64094 2609728
OK 11 11,0,0 516468,188165,0 704633
OR 5 5,0,0 213871,136019,15450 368808
PA 36 0,36,0 1295948,1453540,91223 2859177
RI 4 4,0,0 146604,115266,3138 266170
SC 8 8,0,0 102347,1978,82 104407
SD 4 4,0,0 183515,99212,1551 288438
TN 11 11,0,0 259473,126752,1796 390256
TX 23 23,0,0 760348,97959,4450 863426
UT 4 4,0,0 116750,84795,4087 206578
VA 11 11,0,0 203979,89637,2382 297942
VT 3 0,3,0 56266,78984,1533 136980
WA 8 8,0,0 353260,208645,17080 614814
WI 12 12,0,0 707410,347741,53379 1114808
WV 8 8,0,0 405124,330731,5133 743774
WY 3 3,0,0 54370,39583,2829 96962
@1936|0|45647699||A historic landslide: Landon carries only Maine and Vermont. The Literary Digest poll called it for Landon, and never recovered.
Franklin D. Roosevelt|Democratic|John Nance Garner|523|27752648
Alf Landon|Republican|Frank Knox|8|16681862
William Lemke|Union|Thomas C. O'Brien|0|892378
AL 11 11,0,0 238136,35358,551 275244
AR 9 9,0,0 146765,32039,4 179423
AZ 3 3,0,0 86722,33433,3307 124163
CA 22 22,0,0 1766836,836431,0 2638882
CO 6 6,0,0 295021,181267,9962 488684
CT 8 8,0,0 382129,278685,21805 690723
DE 3 3,0,0 69702,57236,442 127603
FL 7 7,0,0 249117,78248,0 327365
GA 12 12,0,0 255364,36942,141 293175
IA 11 11,0,0 621756,487977,29687 1142733
ID 4 4,0,0 125683,66256,7678 199617
IL 29 29,0,0 2282999,1570393,89439 3956522
IN 14 14,0,0 934974,691570,19407 1650897
KS 9 9,0,0 464520,397727,497 865014
KY 11 11,0,0 541944,369702,12501 926203
LA 10 10,0,0 292894,36791,0 329778
MA 17 17,0,0 942716,768613,118639 1840357
MD 8 8,0,0 389612,231435,0 624896
ME 5 0,5,0 126333,168823,7581 304240
MI 19 19,0,0 1016794,699733,75795 1805098
MN 11 11,0,0 698811,350461,74296 1129975
MO 15 15,0,0 1111043,697891,14630 1828635
MS 9 9,0,0 157318,4443,0 162090
MT 4 4,0,0 159690,63598,5549 230512
NC 13 13,0,0 616141,223283,2 839464
ND 4 4,0,0 163148,72751,36708 273716
NE 7 7,0,0 347445,247731,12847 608023
NH 4 4,0,0 108460,104642,4819 218114
NJ 16 16,0,0 1083850,720322,9407 1820437
NM 3 3,0,0 106037,61727,924 169176
NV 3 3,0,0 31925,11923,0 43848
NY 47 47,0,0 3293222,2180670,0 5596398
OH 26 26,0,0 1747140,1127855,132212 3012589
OK 11 11,0,0 501069,245122,0 749740
OR 5 5,0,0 266733,122706,21831 414021
PA 36 36,0,0 2353987,1690200,67468 4138426
RI 4 4,0,0 165238,125031,19569 311178
SC 8 8,0,0 113791,1646,0 115437
SD 4 4,0,0 160137,125977,10338 296472
TN 11 11,0,0 328083,146520,296 476538
TX 23 23,0,0 734485,103874,3281 843482
UT 4 4,0,0 150246,64555,1121 216677
VA 11 11,0,0 234980,98336,233 334590
VT 3 0,3,0 62124,81023,0 143689
WA 8 8,0,0 459579,206892,17463 692338
WI 12 12,0,0 802984,380828,60297 1258560
WV 8 8,0,0 502582,325358,0 829945
WY 3 3,0,0 62624,38739,1653 103382
@1940|0|49902113||Roosevelt breaks the two-term tradition with war already under way in Europe.
Franklin D. Roosevelt|Democratic|Henry A. Wallace|449|27313945
Wendell Willkie|Republican|Charles L. McNary|82|22347744
AL 11 11,0 250726,42184 294219
AR 9 9,0 158622,42121 200743
AZ 3 3,0 95267,54030 150039
CA 22 22,0 1877618,1351419 3268791
CO 6 0,6 265554,279576 549004
CT 8 8,0 417621,361819 781502
DE 3 3,0 74599,61440 136374
FL 7 7,0 359334,126158 485492
GA 12 12,0 265194,46360 312551
IA 11 0,11 578800,632370 1215430
ID 4 4,0 127842,106553 235168
IL 29 29,0 2149934,2047240 4217935
IN 14 0,14 874063,899466 1782747
KS 9 0,9 364725,489169 860297
KY 11 11,0 557222,410384 970063
LA 10 10,0 319751,52446 372305
MA 17 17,0 1076522,939700 2026993
MD 8 8,0 384546,269534 660117
ME 5 0,5 156478,163951 320840
MI 19 0,19 1032991,1039917 2085929
MN 11 11,0 644196,596274 1251188
MO 15 15,0 958476,871009 1833729
MS 9 9,0 168267,7364 175824
MT 4 4,0 145698,99579 247873
NC 13 13,0 609015,213633 822648
ND 4 0,4 124036,154590 280775
NE 7 0,7 263677,352201 615878
NH 4 4,0 125292,110127 235419
NJ 16 16,0 1016404,944876 1974214
NM 3 3,0 103699,79315 183258
NV 3 3,0 31945,21229 53174
NY 47 47,0 3251918,3027478 6301596
OH 26 26,0 1733139,1586773 3319912
OK 11 11,0 474313,348872 826212
OR 5 5,0 258415,219555 481240
PA 36 36,0 2171035,1889848 4078714
RI 4 4,0 182182,138653 321148
SC 8 8,0 95470,4360 99832
SD 4 0,4 131362,177065 308427
TN 11 11,0 351601,169153 522823
TX 23 23,0 909974,212692 1124531
UT 4 4,0 154277,93151 247819
VA 11 11,0 235961,109363 346607
VT 3 0,3 64269,78371 143062
WA 8 8,0 462145,322123 793833
WI 12 12,0 704821,679206 1405522
WV 8 8,0 495662,372414 868076
WY 3 3,0 59287,52633 112240
@1944|0|47977063||A fourth term, in wartime. Roosevelt died in April 1945 and Harry Truman, newly on the ticket, succeeded him.
Franklin D. Roosevelt|Democratic|Harry S. Truman|432|25612916
Thomas E. Dewey|Republican|John W. Bricker|99|22017929
AL 11 11,0 198918,44540 244743
AR 9 9,0 148965,63551 212954
AZ 4 4,0 80926,56287 137634
CA 25 25,0 1988564,1512965 3520875
CO 6 0,6 234331,268731 505039
CT 8 8,0 435146,390527 831990
DE 3 3,0 68166,56747 125361
FL 8 8,0 339377,143215 482592
GA 12 12,0 268187,59880 328109
IA 10 0,10 499876,547267 1052599
ID 4 4,0 107399,100137 208321
IL 28 28,0 2079479,1939314 4036061
IN 13 0,13 781403,875891 1672091
KS 8 0,8 287458,442096 733776
KY 11 11,0 472589,392448 867921
LA 10 10,0 281564,67750 349383
MA 16 16,0 1035296,921350 1960665
MD 8 8,0 315490,292949 608439
ME 5 0,5 140631,155434 296400
MI 19 19,0 1106899,1084423 2205223
MN 11 11,0 589864,527416 1125529
MO 15 15,0 807804,761524 1572474
MS 9 9,0 168479,11601 180080
MT 4 4,0 112556,93163 207355
NC 14 14,0 527399,263155 790554
ND 4 0,4 100144,118535 220171
NE 6 0,6 233246,329880 563126
NH 4 4,0 119663,109916 229625
NJ 16 16,0 987874,961335 1963761
NM 4 4,0 81389,70688 152225
NV 3 3,0 29623,24611 54234
NY 47 47,0 3304238,2987647 6316790
OH 25 0,25 1570763,1582293 3153056
OK 10 10,0 401549,319424 722636
OR 6 6,0 248635,225365 480147
PA 35 35,0 1940479,1835054 3794793
RI 4 4,0 175356,123487 299276
SC 8 8,0 90601,4610 103375
SD 4 0,4 96711,135365 232076
TN 12 12,0 308707,200311 510692
TX 23 23,0 821605,191425 1150331
UT 4 4,0 150088,97891 248319
VA 11 11,0 242276,145243 388485
VT 3 0,3 53820,71527 125361
WA 8 8,0 486774,361689 856328
WI 12 0,12 650413,674532 1339152
WV 8 8,0 392777,322819 715596
WY 3 0,3 49419,51921 101340
@1948|0|48792535||'Dewey Defeats Truman' ran the headline. Truman won anyway, despite Thurmond's Dixiecrats breaking away in the South and Wallace's Progressives on the left.
Harry S. Truman|Democratic|Alben W. Barkley|303|24178347
Thomas E. Dewey|Republican|Earl Warren|189|21991292
Strom Thurmond|Dixiecrat|Fielding L. Wright|39|1176023
Henry A. Wallace|Progressive|Glen H. Taylor|0|1157328
AL 11 0,0,11,0 0,40930,171443,1522 214980
AR 9 9,0,0,0 149659,50959,40068,751 242475
AZ 4 4,0,0,0 95251,77597,0,3310 177065
CA 25 25,0,0,0 1913134,1895269,1228,190381 4021538
CO 6 6,0,0,0 267288,239714,0,6115 515237
CT 8 0,8,0,0 423297,437754,0,13713 883518
DE 3 0,3,0,0 67813,69588,0,1050 139073
FL 8 8,0,0,0 281988,194280,89755,11620 577643
GA 12 12,0,0,0 254646,76691,85135,1636 418764
IA 10 10,0,0,0 522380,494018,0,12125 1038264
ID 4 4,0,0,0 107370,101514,0,4972 214816
IL 28 28,0,0,0 1994715,1961103,0,0 3984046
IN 13 0,13,0,0 807833,821079,0,9649 1656214
KS 8 0,8,0,0 351902,423039,0,4603 788819
KY 11 11,0,0,0 466756,341210,10411,1567 822658
LA 10 0,0,10,0 136344,72657,204290,3035 416336
MA 16 16,0,0,0 1151788,909370,0,38157 2107146
MD 8 0,8,0,0 286521,294814,2489,9983 596735
ME 5 0,5,0,0 111916,150234,0,1884 264787
MI 19 0,19,0,0 1003448,1038595,0,46515 2109609
MN 11 11,0,0,0 692966,483617,0,27866 1212226
MO 15 15,0,0,0 917315,655039,42,3998 1578628
MS 9 0,0,9,0 19384,5043,167538,225 192190
MT 4 4,0,0,0 119071,96770,0,7313 224278
NC 14 14,0,0,0 459070,258572,69652,3915 791209
ND 4 0,4,0,0 95812,115139,374,8391 220716
NE 6 0,6,0,0 224165,264774,0,0 488940
NH 4 0,4,0,0 107995,121299,7,1970 231440
NJ 16 0,16,0,0 895455,981124,0,42683 1949555
NM 4 4,0,0,0 105464,80303,0,1037 187063
NV 3 3,0,0,0 31291,29357,0,1469 62117
NY 47 0,47,0,0 2780204,2841163,0,509559 6177337
OH 25 25,0,0,0 1452791,1445684,0,37596 2936071
OK 10 10,0,0,0 452782,268817,0,0 721599
OR 6 0,6,0,0 243147,260904,0,14978 524080
PA 35 0,35,0,0 1752426,1902197,0,55161 3735148
RI 4 4,0,0,0 188736,135787,0,2619 327702
SC 8 0,0,8,0 34423,5386,102607,154 142571
SD 4 0,4,0,0 117653,129651,0,2801 250105
TN 12 11,0,1,0 270402,202914,73815,1864 550283
TX 23 23,0,0,0 824235,303467,113776,3920 1249577
UT 4 4,0,0,0 149151,124402,0,2679 276305
VA 11 11,0,0,0 200786,172070,43393,2047 419256
VT 3 0,3,0,0 45557,75926,0,1279 123382
WA 8 8,0,0,0 475165,386315,0,31692 904058
WI 12 12,0,0,0 647310,590959,0,25282 1276800
WV 8 8,0,0,0 429188,316251,0,3311 748750
WY 3 3,0,0,0 52354,47947,0,931 101425
@1952|0|61751942||'I Like Ike.' The war hero ends twenty years of Democratic presidents, with the first big television campaign.
Dwight D. Eisenhower|Republican|Richard Nixon|442|34075529
Adlai Stevenson II|Democratic|John Sparkman|89|27375090
AL 11 0,11 149231,275075 426120
AR 8 0,8 177155,226300 404800
AZ 4 4,0 152042,108528 260570
CA 32 32,0 3035587,2257646 5341603
CO 6 6,0 379782,245504 630103
CT 8 8,0 611012,481649 1096911
DE 3 3,0 90059,83315 174025
FL 10 10,0 544036,444950 989337
GA 12 0,12 198979,456823 655803
IA 10 10,0 808906,451513 1268773
ID 4 4,0 180707,95081 276231
IL 27 27,0 2457327,2013920 4481058
IN 13 13,0 1136259,801530 1955325
KS 8 8,0 616302,273296 896166
KY 10 0,10 495029,495729 993148
LA 10 0,10 306925,345027 651952
MA 16 16,0 1292325,1083525 2383398
MD 9 9,0 499424,395337 902074
ME 5 5,0 232353,118806 351786
MI 20 20,0 1551529,1230657 2798592
MN 11 11,0 763211,608458 1379483
MO 13 13,0 959429,929830 1892062
MS 8 0,8 112966,172566 285532
MT 4 4,0 157394,106213 265037
NC 14 0,14 558107,652803 1210910
ND 4 4,0 191712,76694 270127
NE 6 6,0 421603,188057 609660
NH 4 4,0 166287,106663 272950
NJ 16 16,0 1374613,1015902 2419554
NM 4 4,0 132170,105661 238608
NV 3 3,0 50502,31688 82190
NY 45 45,0 3952815,3104601 7128241
OH 25 25,0 2100391,1600367 3700758
OK 8 8,0 518045,430939 948984
OR 6 6,0 420815,270579 695059
PA 32 32,0 2415789,2146269 4580969
RI 4 4,0 210935,203293 414498
SC 8 0,8 168082,173004 341086
SD 4 4,0 203857,90426 294283
TN 11 11,0 446147,443710 892553
TX 24 24,0 1102878,969228 2075946
UT 4 4,0 194190,135364 329554
VA 12 12,0 349037,268677 619689
VT 3 3,0 109717,43355 153557
WA 9 9,0 599107,492845 1102708
WI 12 12,0 979744,622175 1607370
WV 8 0,8 419970,453578 873548
WY 3 3,0 81047,47934 129251
@1956|0|62021979||A rematch, and a bigger win for Eisenhower. One Alabama elector broke away to vote for Walter B. Jones.
Dwight D. Eisenhower|Republican|Richard Nixon|457|35579180
Adlai Stevenson II|Democratic|Estes Kefauver|73|26028028
Walter B. Jones|Democratic|Herman Talmadge|1|
AL 11 0,10,1 195694,280844, 496871
AR 8 0,8,0 186287,213277, 406572
AZ 4 4,0,0 176990,112880, 290173
CA 32 32,0,0 3027668,2420135, 5466355
CO 6 6,0,0 394479,263997, 663074
CT 8 8,0,0 711837,405079, 1117121
DE 3 3,0,0 98057,79421, 177988
FL 10 10,0,0 643849,480371, 1124220
GA 12 0,12,0 216652,441094, 663480
IA 10 10,0,0 729187,501858, 1234564
ID 4 4,0,0 166979,105868, 272989
IL 27 27,0,0 2623327,1775682, 4407407
IN 13 13,0,0 1182811,783908, 1974607
KS 8 8,0,0 566878,296317, 866243
KY 10 10,0,0 572192,476453, 1053805
LA 10 10,0,0 329047,243977, 617544
MA 16 16,0,0 1393197,948190, 2348506
MD 9 9,0,0 559738,372613, 932351
ME 5 5,0,0 249238,102468, 351706
MI 20 20,0,0 1713647,1359898, 3080468
MN 11 11,0,0 719302,617525, 1340005
MO 13 0,13,0 914289,918273, 1832562
MS 8 0,8,0 60685,144498, 248149
MT 4 4,0,0 154933,116238, 271171
NC 14 0,14,0 575062,590530, 1165592
ND 4 4,0,0 156766,96742, 253991
NE 6 6,0,0 378108,199029, 577137
NH 4 4,0,0 176519,90364, 266994
NJ 16 16,0,0 1606942,850337, 2484312
NM 4 4,0,0 146788,106098, 253926
NV 3 3,0,0 56049,40640, 96689
NY 45 45,0,0 4340340,2750769, 7093336
OH 25 25,0,0 2262610,1439655, 3702265
OK 8 8,0,0 473769,385581, 859350
OR 6 6,0,0 406393,329204, 735597
PA 32 32,0,0 2585252,1981769, 4576503
RI 4 4,0,0 225819,161790, 387611
SC 8 0,8,0 75700,136372, 300583
SD 4 4,0,0 171569,122288, 293857
TN 11 11,0,0 462288,456507, 939404
TX 24 24,0,0 1080619,859958, 1955545
UT 4 4,0,0 215631,118364, 333995
VA 12 12,0,0 386459,267760, 697978
VT 3 3,0,0 110390,42549, 152978
WA 9 9,0,0 620430,523002, 1150889
WI 12 12,0,0 954844,586768, 1550558
WV 8 8,0,0 449297,381534, 830831
WY 3 3,0,0 74573,49554, 124127
@1960|0|68832482||The first televised debates, the first votes from Alaska and Hawaii, and a margin of about 0.2%. Unpledged Southern electors, with one Oklahoma Republican, voted for Harry Byrd.
John F. Kennedy|Democratic|Lyndon B. Johnson|303|34220984
Richard Nixon|Republican|Henry Cabot Lodge Jr.|219|34108157
Harry F. Byrd|Unpledged Democratic|Strom Thurmond|15|610409
AK 3 0,3,0 29809,30953,0 60762
AL 11 5,0,6 318303,237981,324050 880334
AR 8 8,0,0 215049,184508,0 428509
AZ 4 0,4,0 176781,221241,0 398491
CA 32 0,32,0 3224099,3259722,0 6506578
CO 6 0,6,0 330629,402242,0 736246
CT 8 8,0,0 657055,565813,0 1222883
DE 3 3,0,0 99590,96373,0 196683
FL 10 0,10,0 748700,795476,0 1544176
GA 12 12,0,0 458638,274472,0 733349
HI 3 3,0,0 92410,92295,0 184705
IA 10 0,10,0 550565,722381,0 1273810
ID 4 0,4,0 138853,161597,0 300450
IL 27 27,0,0 2377846,2368988,0 4757409
IN 13 0,13,0 952358,1175120,0 2135360
KS 8 0,8,0 363213,561474,0 928825
KY 10 0,10,0 521855,602607,0 1124462
LA 10 10,0,0 407339,230980,169572 807891
MA 16 16,0,0 1487174,976750,0 2469480
MD 9 9,0,0 565808,489538,0 1055349
ME 5 0,5,0 181159,240608,0 421767
MI 20 20,0,0 1687269,1620428,539 3318097
MN 11 11,0,0 779933,757915,0 1541887
MO 13 13,0,0 972201,962221,0 1934422
MS 8 0,0,8 108362,73561,116248 298171
MT 4 0,4,0 134891,141841,0 277579
NC 14 14,0,0 713136,655420,0 1368556
ND 4 0,4,0 123963,154310,0 278431
NE 6 0,6,0 232542,380553,0 613095
NH 4 0,4,0 137772,157989,0 295761
NJ 16 16,0,0 1385415,1363324,0 2773111
NM 4 4,0,0 156027,153733,0 311107
NV 3 3,0,0 54880,52387,0 107267
NY 45 45,0,0 3830085,3446419,0 7291079
OH 25 0,25,0 1944248,2217611,0 4161859
OK 8 0,7,1 370111,533039,0 903150
OR 6 0,6,0 367402,408060,0 776421
PA 32 32,0,0 2556282,2439956,0 5006541
RI 4 4,0,0 258032,147502,0 405535
SC 8 8,0,0 198129,188558,0 386688
SD 4 0,4,0 128070,178417,0 306487
TN 11 0,11,0 481453,556577,0 1051792
TX 24 24,0,0 1167567,1121310,0 2311084
UT 4 0,4,0 169248,205361,0 374709
VA 12 0,12,0 362327,404521,0 771449
VT 3 0,3,0 69186,98131,0 167324
WA 9 0,9,0 599298,629273,0 1241572
WI 12 0,12,0 830805,895175,0 1729082
WV 8 8,0,0 441786,395995,0 837781
WY 3 0,3,0 63331,77451,0 140782
@1964|0|70641751||A landslide a year after Kennedy's assassination. Goldwater's five Deep South states hinted at the realignment to come. D.C. voted for the first time.
Lyndon B. Johnson|Democratic|Hubert Humphrey|486|43129040
Barry Goldwater|Republican|William E. Miller|52|27175754
AK 3 3,0 44329,22930 67259
AL 10 0,10 0,479085 689817
AR 6 6,0 314197,243264 560426
AZ 5 0,5 237753,242535 480770
CA 40 40,0 4171877,2879108 7057586
CO 6 6,0 476024,296767 776986
CT 8 8,0 826269,390996 1218578
DC 3 3,0 169796,28801 198597
DE 3 3,0 122704,78078 201320
FL 14 14,0 948540,905941 1854481
GA 12 0,12 522557,616584 1139336
HI 4 4,0 163249,44022 207271
IA 9 9,0 733030,449148 1184539
ID 4 4,0 148920,143557 292477
IL 26 26,0 2796833,1905946 4702841
IN 13 13,0 1170848,911118 2091606
KS 7 7,0 464028,386579 857901
KY 9 9,0 669659,372977 1046105
LA 10 0,10 387068,509225 896293
MA 14 14,0 1786422,549727 2344798
MD 10 10,0 730912,385495 1116457
ME 4 4,0 262264,118701 381221
MI 21 21,0 2136615,1060152 3203102
MN 10 10,0 991117,559624 1554462
MO 12 12,0 1164344,653535 1817879
MS 7 0,7 52618,356528 409146
MT 4 4,0 164246,113032 278628
NC 13 13,0 800139,624844 1424983
ND 4 4,0 149784,108207 258389
NE 5 5,0 307307,276847 584154
NH 4 4,0 184064,104029 288093
NJ 17 17,0 1867671,963843 2846770
NM 4 4,0 194017,131838 327615
NV 3 3,0 79339,56094 135433
NY 43 43,0 4913156,2243559 7166015
OH 26 26,0 2498331,1470865 3969196
OK 8 8,0 519834,412665 932499
OR 6 6,0 501017,282779 786305
PA 29 29,0 3130954,1673657 4822690
RI 4 4,0 315463,74615 390091
SC 8 0,8 215700,309048 524756
SD 4 4,0 163010,130108 293118
TN 11 11,0 634947,508965 1143946
TX 25 25,0 1663185,958566 2626811
UT 4 4,0 219628,180682 400310
VA 12 12,0 558038,481334 1042267
VT 3 3,0 108127,54942 163089
WA 9 9,0 779881,470366 1258556
WI 12 12,0 1050424,638495 1691815
WV 7 7,0 538087,253953 792040
WY 3 3,0 80718,61998 142716
@1968|0|73199998||A year of assassinations and war in Vietnam. Nixon's narrow win came as George Wallace carried five Southern states, the last third-party candidate to win any.
Richard Nixon|Republican|Spiro Agnew|301|31783783
Hubert Humphrey|Democratic|Edmund Muskie|191|31271839
George Wallace|American Independent|Curtis LeMay|46|9901118
AK 3 3,0,0 37600,35411,10024 83035
AL 10 0,0,10 146923,196579,691425 1049917
AR 6 0,0,6 189062,184901,235627 609590
AZ 5 5,0,0 266721,170514,46573 486936
CA 40 40,0,0 3467664,3244318,487270 7251587
CO 6 6,0,0 409345,335174,60813 811199
CT 8 0,8,0 556721,621561,76650 1256232
DC 3 0,3,0 31012,139566,0 170578
DE 3 3,0,0 96714,89194,28459 214367
FL 14 14,0,0 886804,676794,624207 2187805
GA 12 0,0,12 380111,334440,535550 1250266
HI 4 0,4,0 91425,141324,3469 236218
IA 9 9,0,0 619106,476699,66422 1167931
ID 4 4,0,0 165369,89273,36541 291183
IL 26 26,0,0 2174774,2039814,390958 4619749
IN 13 13,0,0 1067885,806659,243108 2123597
KS 7 7,0,0 478674,302996,88921 872783
KY 9 9,0,0 462411,397541,193098 1055893
LA 10 0,0,10 257535,309615,530300 1097450
MA 14 0,14,0 766844,1469218,87088 2331752
MD 10 0,10,0 517995,538310,178734 1235039
ME 4 0,4,0 169254,217312,6370 392936
MI 21 0,21,0 1370665,1593082,331968 3306250
MN 10 0,10,0 658643,857738,68931 1588510
MO 12 12,0,0 811932,791444,206126 1809502
MS 7 0,0,7 88516,150644,415349 654509
MT 4 4,0,0 138835,114117,20015 274404
NC 13 12,0,1 627192,464113,496188 1587493
ND 4 4,0,0 138669,94769,14244 247882
NE 5 5,0,0 321163,170784,44904 536851
NH 4 4,0,0 154903,130589,11173 297298
NJ 17 17,0,0 1325467,1264206,262187 2875395
NM 4 4,0,0 169692,130081,25737 327281
NV 3 3,0,0 73188,60598,20432 154218
NY 43 0,43,0 3007932,3378470,358864 6790066
OH 26 26,0,0 1791014,1700586,467495 3959698
OK 8 8,0,0 449697,301658,191731 943086
OR 6 6,0,0 408433,358866,49683 819622
PA 29 0,29,0 2090017,2259405,378582 4747928
RI 4 0,4,0 122359,246518,15678 385000
SC 8 8,0,0 254062,197486,215430 666982
SD 4 4,0,0 149841,118023,13400 281264
TN 11 11,0,0 472592,351233,424792 1248617
TX 25 0,25,0 1227844,1266804,584269 3079406
UT 4 4,0,0 238728,156665,26906 422568
VA 12 12,0,0 590319,442387,321833 1361491
VT 3 3,0,0 85142,70255,5104 161404
WA 9 0,9,0 588510,616037,96990 1304281
WI 12 12,0,0 809997,748804,127835 1691538
WV 7 0,7,0 307555,374091,72560 754206
WY 3 3,0,0 70927,45173,11105 127205
@1972|0|77744027||Nixon wins 49 states, as the Watergate burglary goes largely unnoticed; he resigned less than two years later. Tonie Nathan, on Hospers's ticket, was the first woman to win an electoral vote.
Richard Nixon|Republican|Spiro Agnew|520|47168710
George McGovern|Democratic|Sargent Shriver|17|29173222
John Hospers|Libertarian|Tonie Nathan|1|3674
John G. Schmitz|American Independent|Thomas J. Anderson|0|1100868
AK 3 3,0,0,0 55349,32967,0,6903 95219
AL 9 9,0,0,0 728701,256923,0,11918 1006093
AR 6 6,0,0,0 445751,198899,0,3016 647666
AZ 6 6,0,0,0 402812,198540,0,21208 653505
CA 45 45,0,0,0 4602096,3475847,980,232554 8367862
CO 7 7,0,0,0 597189,329980,1111,17269 953884
CT 8 8,0,0,0 810763,555498,0,17239 1384277
DC 3 0,3,0,0 35226,127627,0,0 163421
DE 3 3,0,0,0 140357,92283,0,2638 235516
FL 17 17,0,0,0 1857759,718117,0,0 2583283
GA 12 12,0,0,0 881496,289529,0,812 1174772
HI 4 4,0,0,0 168865,101409,0,0 270274
IA 8 8,0,0,0 706207,496206,0,22056 1225944
ID 4 4,0,0,0 199384,80826,0,28869 310379
IL 26 26,0,0,0 2788179,1913472,0,2471 4723236
IN 13 13,0,0,0 1405154,708568,0,0 2125529
KS 7 7,0,0,0 619812,270287,0,21808 916095
KY 9 9,0,0,0 676446,371159,0,17627 1067499
LA 10 10,0,0,0 686852,298142,0,52099 1051491
MA 14 0,14,0,0 1112078,1332540,43,2877 2458756
MD 10 10,0,0,0 829305,505781,0,18726 1353812
ME 4 4,0,0,0 256458,160584,1,117 417271
MI 21 21,0,0,0 1961721,1459435,0,63321 3490325
MN 10 10,0,0,0 898269,802346,0,31407 1741652
MO 12 12,0,0,0 1154058,698531,0,0 1852589
MS 7 7,0,0,0 505125,126782,0,11598 645963
MT 4 4,0,0,0 183976,120197,0,13430 317603
NC 13 13,0,0,0 1054889,438705,0,25018 1518612
ND 3 3,0,0,0 174109,100384,0,5646 280514
NE 5 5,0,0,0 406298,169991,0,0 576289
NH 4 4,0,0,0 213724,116435,0,3386 334055
NJ 17 17,0,0,0 1845502,1102211,0,34378 2997229
NM 4 4,0,0,0 235606,141084,0,8767 385931
NV 3 3,0,0,0 115750,66016,0,0 181766
NY 41 41,0,0,0 4192778,2951084,0,0 7161830
OH 25 25,0,0,0 2441827,1558889,0,80067 4094787
OK 8 8,0,0,0 759025,247147,0,23728 1029900
OR 6 6,0,0,0 486686,392760,0,46211 927946
PA 27 27,0,0,0 2714521,1796951,0,70593 4592105
RI 4 4,0,0,0 220383,194645,2,25 415808
SC 8 8,0,0,0 478427,189270,0,10166 677880
SD 4 4,0,0,0 166476,139945,0,0 307415
TN 10 10,0,0,0 813147,357293,0,30373 1201182
TX 26 26,0,0,0 2298896,1154291,0,7098 3472714
UT 4 4,0,0,0 323643,126284,0,28549 478476
VA 12 11,0,1,0 988493,438887,0,19721 1457019
VT 3 3,0,0,0 117149,68174,0,0 186947
WA 9 9,0,0,0 837135,568334,1537,58906 1470847
WI 11 11,0,0,0 989430,810174,0,47525 1852890
WV 6 6,0,0,0 484964,277435,0,0 762399
WY 3 3,0,0,0 100464,44358,0,748 145570
@1976|0|81531584||Post-Watergate, Carter beats Ford, who had never been elected. One Washington elector voted for Reagan.
Jimmy Carter|Democratic|Walter Mondale|297|40831881
Gerald Ford|Republican|Bob Dole|240|39148634
Ronald Reagan|Republican|Bob Dole|1|
Eugene McCarthy|Independent||0|740460
AK 3 0,3,0,0 44058,71555,,0 123574
AL 9 9,0,0,0 659170,504070,,0 1182850
AR 6 6,0,0,0 499614,268753,,647 769396
AZ 6 0,6,0,0 295602,418642,,19229 742719
CA 45 0,45,0,0 3742284,3882244,,58412 7867117
CO 7 0,7,0,0 460353,584367,,26107 1081135
CT 8 0,8,0,0 647895,719261,,0 1381526
DC 3 3,0,0,0 137818,27873,,0 168830
DE 3 3,0,0,0 122596,109831,,2437 235834
FL 17 17,0,0,0 1636000,1469531,,23643 3150631
GA 12 12,0,0,0 979409,483743,,991 1467458
HI 4 4,0,0,0 147375,140003,,0 291301
IA 8 0,8,0,0 619931,632863,,20051 1279306
ID 4 0,4,0,0 126549,204151,,0 340932
IL 26 0,26,0,0 2271295,2364269,,55939 4718833
IN 13 0,13,0,0 1014714,1183958,,0 2220362
KS 7 0,7,0,0 430421,502752,,13185 957845
KY 9 9,0,0,0 615717,531852,,6837 1167142
LA 10 10,0,0,0 661365,587446,,6588 1278439
MA 14 14,0,0,0 1429475,1030276,,65637 2547557
MD 10 10,0,0,0 759612,672661,,0 1432273
ME 4 0,4,0,0 232289,236320,,10874 483208
MI 21 0,21,0,0 1696714,1893742,,47905 3653749
MN 10 10,0,0,0 1070440,819395,,35490 1949931
MO 12 12,0,0,0 998387,927443,,24029 1953600
MS 7 7,0,0,0 381309,366846,,4074 769360
MT 4 0,4,0,0 149259,173703,,0 328734
NC 13 13,0,0,0 927365,741960,,0 1677906
ND 3 0,3,0,0 136078,153470,,2952 297094
NE 5 0,5,0,0 233692,359705,,9409 607668
NH 4 0,4,0,0 147635,185935,,4095 339618
NJ 17 0,17,0,0 1444653,1509688,,32717 3014472
NM 4 0,4,0,0 201148,211419,,0 416590
NV 3 0,3,0,0 92479,101273,,0 201876
NY 41 41,0,0,0 3389558,3100791,,4303 6525225
OH 25 25,0,0,0 2011621,2000505,,58258 4111873
OK 8 0,8,0,0 532442,545708,,14101 1092251
OR 6 0,6,0,0 490407,492120,,40207 1029876
PA 27 27,0,0,0 2328677,2205604,,50584 4620787
RI 4 4,0,0,0 227636,181249,,479 411170
SC 8 8,0,0,0 450825,346140,,0 802594
SD 4 0,4,0,0 147068,151505,,0 300678
TN 10 10,0,0,0 825879,633969,,5004 1476346
TX 26 26,0,0,0 2082319,1953300,,20118 4071884
UT 4 0,4,0,0 182110,337908,,3907 541198
VA 12 0,12,0,0 813896,836554,,0 1697094
VT 3 0,3,0,0 81044,102085,,4001 187855
WA 9 0,8,1,0 717323,777732,,36986 1555534
WI 11 11,0,0,0 1040232,1004987,,34943 2101336
WV 6 6,0,0,0 435914,314760,,0 750674
WY 3 0,3,0,0 62239,92717,,624 156343
@1980|0|86509678||Inflation and the Iran hostage crisis turn a close race into a Reagan landslide.
Ronald Reagan|Republican|George H. W. Bush|489|43903230
Jimmy Carter|Democratic|Walter Mondale|49|35480115
John B. Anderson|Independent|Patrick Lucey|0|5719850
Ed Clark|Libertarian|David Koch|0|921128
AK 3 3,0,0,0 86112,41842,11155,18479 158445
AL 9 9,0,0,0 654192,636730,16481,13318 1341929
AR 6 6,0,0,0 403164,398041,22468,8970 837582
AZ 6 6,0,0,0 529688,246843,76952,18784 873945
CA 45 45,0,0,0 4524858,3083661,739833,148434 8587063
CO 7 7,0,0,0 652264,367973,130633,25744 1184415
CT 8 8,0,0,0 677210,541732,171807,8570 1406285
DC 3 0,3,0,0 23313,130231,16131,1104 173889
DE 3 3,0,0,0 111252,105754,16288,1974 235668
FL 17 17,0,0,0 2046951,1419475,189692,30524 3687026
GA 12 0,12,0,0 654168,890733,36055,15627 1597467
HI 4 0,4,0,0 130112,135879,32021,3269 303287
IA 8 8,0,0,0 676026,508672,115633,13123 1317661
ID 4 4,0,0,0 290699,110192,27058,8425 437431
IL 26 26,0,0,0 2358049,1981413,346754,38939 4749721
IN 13 13,0,0,0 1255656,844197,111639,19627 2242033
KS 7 7,0,0,0 566812,326150,68231,14470 979795
KY 9 9,0,0,0 635274,616417,31127,5531 1294627
LA 10 10,0,0,0 792853,708453,26345,8240 1548591
MA 14 14,0,0,0 1057631,1053802,382539,22038 2524298
MD 10 0,10,0,0 680606,726161,119537,14192 1540496
ME 4 4,0,0,0 238522,220974,53327,5119 523011
MI 21 21,0,0,0 1915225,1661532,275223,41597 3909725
MN 10 0,10,0,0 873241,954174,174990,31592 2051953
MO 12 12,0,0,0 1074181,931182,77920,14422 2099824
MS 7 7,0,0,0 441089,429281,12036,5465 892620
MT 4 4,0,0,0 206814,118032,29281,9825 363952
NC 13 13,0,0,0 915018,875635,52800,9677 1855833
ND 3 3,0,0,0 193695,79189,23640,3743 301545
NE 5 5,0,0,0 419937,166851,44993,9073 640854
NH 4 4,0,0,0 221705,108864,49693,2067 383999
NJ 17 17,0,0,0 1546557,1147364,234632,20652 2975684
NM 4 4,0,0,0 250779,167826,29459,4365 456237
NV 3 3,0,0,0 155017,66666,17651,4358 247885
NY 41 41,0,0,0 2893831,2728372,467801,52648 6201959
OH 25 25,0,0,0 2206545,1752414,254472,49033 4283603
OK 8 8,0,0,0 695570,402026,38284,13828 1149708
OR 6 6,0,0,0 571044,456890,112389,25838 1181516
PA 27 27,0,0,0 2261872,1937540,292921,33263 4561501
RI 4 0,4,0,0 154793,198342,59819,2458 416072
SC 8 8,0,0,0 441207,427560,14150,4975 890083
SD 4 4,0,0,0 198343,103855,21431,3824 327703
TN 10 10,0,0,0 787761,783051,35991,7116 1617616
TX 26 26,0,0,0 2510705,1881147,111613,37643 4541637
UT 4 4,0,0,0 439687,124266,30284,7226 604222
VA 12 12,0,0,0 989609,752174,95418,12821 1866032
VT 3 3,0,0,0 94598,81891,31760,1900 213207
WA 9 9,0,0,0 865244,650193,185073,29213 1742394
WI 11 11,0,0,0 1088845,981584,160657,29135 2273221
WV 6 0,6,0,0 334206,367462,31691,4356 737715
WY 3 3,0,0,0 110700,49427,12072,4514 176713
@1984|0|92653233||'Morning in America.' Reagan wins 49 states; Mondale carries only Minnesota and D.C. Geraldine Ferraro is the first woman on a major-party ticket.
Ronald Reagan|Republican|George H. W. Bush|525|54455472
Walter Mondale|Democratic|Geraldine Ferraro|13|37577352
AK 3 3,0 138377,62007 207605
AL 9 9,0 872849,551899 1441713
AR 6 6,0 534774,338646 884406
AZ 7 7,0 681416,333854 1025897
CA 47 47,0 5467009,3922519 9505423
CO 8 8,0 821818,454974 1295381
CT 8 8,0 890877,569597 1466900
DC 3 0,3 29009,180408 211288
DE 3 3,0 152190,101656 254572
FL 21 21,0 2730350,1448816 4180051
GA 12 12,0 1068722,706628 1776093
HI 4 4,0 185050,147154 335846
IA 8 8,0 703088,605620 1319805
ID 4 4,0 297523,108510 411144
IL 24 24,0 2707103,2086499 4819088
IN 12 12,0 1377230,841481 2233069
KS 7 7,0 677296,333149 1021991
KY 9 9,0 822782,539589 1370461
LA 10 10,0 1037299,651586 1706822
MA 13 13,0 1310936,1239606 2559453
MD 10 10,0 879918,787935 1675873
ME 4 4,0 336500,214515 553144
MI 20 20,0 2251571,1529638 3801658
MN 10 0,10 1032603,1036364 2084449
MO 11 11,0 1274188,848583 2122771
MS 7 7,0 581477,352192 940192
MT 4 4,0 232450,146742 384377
NC 13 13,0 1346481,824287 2175361
ND 3 3,0 200336,104429 308971
NE 5 5,0 460054,187866 652090
NH 4 4,0 267051,120395 388954
NJ 16 16,0 1933630,1261323 3217862
NM 5 5,0 307101,201769 514370
NV 4 4,0 188770,91655 286667
NY 36 36,0 3664763,3119609 6806810
OH 23 23,0 2678560,1825440 4547619
OK 8 8,0 861530,385080 1255676
OR 7 7,0 685700,536479 1226527
PA 25 25,0 2584323,2228131 4844903
RI 4 4,0 212080,197106 410492
SC 8 8,0 615539,344470 968540
SD 3 3,0 200267,116113 317867
TN 11 11,0 990212,711714 1711993
TX 29 29,0 3433428,1949276 5397571
UT 5 5,0 469105,155369 629656
VA 12 12,0 1337078,796250 2146635
VT 3 3,0 135865,95730 234561
WA 10 10,0 1051670,807352 1883910
WI 11 11,0 1198800,995847 2212016
WV 6 6,0 405483,328125 735742
WY 3 3,0 133241,53370 188968
@1988|0|91594686||The last time a party has won a third straight term. One West Virginia elector swapped the Democratic ticket, voting for Bentsen for president.
George H. W. Bush|Republican|Dan Quayle|426|48886597
Michael Dukakis|Democratic|Lloyd Bentsen|111|41809476
Lloyd Bentsen|Democratic|Michael Dukakis|1|
AK 3 3,0,0 119251,72584, 200116
AL 9 9,0,0 815576,549506, 1378476
AR 6 6,0,0 466578,349237, 827738
AZ 7 7,0,0 702541,454029, 1171873
CA 47 47,0,0 5054917,4702233, 9887064
CO 8 8,0,0 728177,621453, 1372394
CT 8 8,0,0 750241,676584, 1443394
DC 3 0,3,0 27590,159407, 192877
DE 3 3,0,0 139639,108647, 249891
FL 21 21,0,0 2618885,1656701, 4302313
GA 12 12,0,0 1081331,714792, 1809672
HI 4 0,4,0 158625,192364, 354461
IA 8 0,8,0 545355,670557, 1225614
ID 4 4,0,0 253881,147272, 408968
IL 24 24,0,0 2310939,2215940, 4559120
IN 12 12,0,0 1297763,860643, 2168621
KS 7 7,0,0 554049,422636, 993044
KY 9 9,0,0 734281,580368, 1322517
LA 10 10,0,0 883702,734281, 1628202
MA 13 0,13,0 1194644,1401406, 2632805
MD 10 10,0,0 876167,826304, 1714358
ME 4 4,0,0 307131,243569, 555035
MI 20 20,0,0 1965486,1675783, 3669163
MN 10 0,10,0 962337,1109471, 2096790
MO 11 11,0,0 1084953,1001619, 2093228
MS 7 7,0,0 557890,363921, 931527
MT 4 4,0,0 190412,168936, 365674
NC 13 13,0,0 1237258,890167, 2134370
ND 3 3,0,0 166559,127739, 297261
NE 5 5,0,0 398447,259646, 662372
NH 4 4,0,0 281537,163696, 450525
NJ 16 16,0,0 1743192,1320352, 3099553
NM 5 5,0,0 270341,244497, 521287
NV 4 4,0,0 206040,132738, 350067
NY 36 0,36,0 3081871,3347882, 6485683
OH 23 23,0,0 2416549,1939629, 4393699
OK 8 8,0,0 678367,483423, 1171036
OR 7 0,7,0 560126,616206, 1201694
PA 25 25,0,0 2300087,2194944, 4536251
RI 4 0,4,0 177761,225123, 404620
SC 8 8,0,0 606443,370554, 986009
SD 3 3,0,0 165415,145560, 312991
TN 11 11,0,0 947233,679794, 1636250
TX 29 29,0,0 3036829,2352748, 5427410
UT 5 5,0,0 428442,207343, 647008
VA 12 12,0,0 1309162,859799, 2191609
VT 3 3,0,0 124331,115775, 243333
WA 10 0,10,0 903835,933516, 1865253
WI 11 0,11,0 1047499,1126794, 2191608
WV 6 0,5,1 310065,341016, 653311
WY 3 3,0,0 106867,67113, 176551
@1992|0|104426659||'It's the economy, stupid.' Perot takes 19% of the vote, the most for a third-party candidate since 1912, without carrying a state.
Bill Clinton|Democratic|Al Gore|370|44909889
George H. W. Bush|Republican|Dan Quayle|168|39104545
Ross Perot|Independent|James Stockdale|0|19742267
AK 3 0,3,0 78294,102000,73481 258506
AL 9 0,9,0 690080,804283,183109 1688060
AR 6 6,0,0 505823,337324,99132 950653
AZ 8 0,8,0 543050,572086,353741 1486975
CA 54 54,0,0 5121325,3630574,2296006 11131721
CO 8 8,0,0 629681,562850,366010 1569180
CT 8 8,0,0 682318,578313,348771 1616332
DC 3 3,0,0 192619,20698,9681 227572
DE 3 3,0,0 126055,102313,59213 289735
FL 25 0,25,0 2072798,2173310,1053067 5314392
GA 13 13,0,0 1008966,995252,309657 2321125
HI 4 4,0,0 179310,136822,53003 372842
IA 7 7,0,0 586353,504891,253468 1354607
ID 4 0,4,0 137013,202645,130395 482142
IL 22 22,0,0 2453350,1734096,840515 5050157
IN 12 0,12,0 848420,989375,455934 2305871
KS 6 0,6,0 390434,449951,312358 1157236
KY 8 8,0,0 665104,617178,203944 1492900
LA 9 9,0,0 815971,733386,211478 1790017
MA 12 12,0,0 1318639,805039,630731 2773664
MD 10 10,0,0 988571,707094,281414 1985046
ME 4 4,0,0 263420,206504,206820 679499
MI 18 18,0,0 1871182,1554940,824813 4274673
MN 10 10,0,0 1020997,747841,562506 2347947
MO 11 11,0,0 1053873,811159,518741 2391565
MS 7 0,7,0 400258,487793,85626 981793
MT 3 3,0,0 154507,144207,107225 410611
NC 14 0,14,0 1114042,1134661,357864 2611850
ND 3 0,3,0 99168,136244,71084 308133
NE 5 0,5,0 217344,344346,174687 739283
NH 4 4,0,0 209040,202484,121337 537945
NJ 15 15,0,0 1436206,1356865,521829 3343594
NM 5 5,0,0 261617,212824,91895 569986
NV 4 4,0,0 189148,175828,132580 506318
NY 33 33,0,0 3444450,2346649,1090721 6926933
OH 21 21,0,0 1984942,1894310,1036426 4939964
OK 8 0,8,0 473066,592929,319878 1390359
OR 7 7,0,0 621314,475757,354091 1462643
PA 23 23,0,0 2239164,1791841,902667 4959810
RI 4 4,0,0 213302,131601,105045 453478
SC 8 0,8,0 479514,577507,138872 1202527
SD 3 0,3,0 124888,136718,73295 336254
TN 11 11,0,0 933521,841300,199968 1982638
TX 32 0,32,0 2281815,2496071,1354781 6154018
UT 5 0,5,0 183429,322632,203400 743999
VA 13 0,13,0 1038650,1150517,348639 2558665
VT 3 3,0,0 133592,88122,65991 289701
WA 11 11,0,0 993039,731235,541780 2288228
WI 11 11,0,0 1041066,930855,544479 2531114
WV 5 5,0,0 331001,241974,108829 683677
WY 3 0,3,0 68160,79347,51263 200587
@1996|0|96277223||Clinton comfortably re-elected in a strong economy. Perot runs again, now on the Reform ticket.
Bill Clinton|Democratic|Al Gore|379|47402357
Bob Dole|Republican|Jack Kemp|159|39198755
Ross Perot|Reform|Pat Choate|0|8085402
Ralph Nader|Green|Winona LaDuke|0|684902
AK 3 0,3,0,0 80380,122746,26333,7597 241620
AL 9 0,9,0,0 662165,769044,92149,0 1534349
AR 6 6,0,0,0 475171,325416,69884,3649 884262
AZ 8 8,0,0,0 653288,622073,112072,2062 1404405
CA 54 54,0,0,0 5119835,3828380,697847,237016 10019484
CO 8 0,8,0,0 671152,691848,99629,25070 1510704
CT 8 8,0,0,0 735740,483109,139523,24321 1392614
DC 3 3,0,0,0 158220,17339,3611,4780 185726
DE 3 3,0,0,0 140355,99062,28719,18 270845
FL 25 25,0,0,0 2546870,2244536,483870,4101 5303794
GA 13 0,13,0,0 1053849,1080843,146337,0 2299071
HI 4 4,0,0,0 205012,113943,27358,10386 360120
IA 7 7,0,0,0 620258,492644,105159,6550 1234075
ID 4 0,4,0,0 165443,256595,62518,0 491719
IL 22 22,0,0,0 2341744,1587021,346408,1447 4311391
IN 12 0,12,0,0 887424,1006693,224299,1121 2135842
KS 6 0,6,0,0 387659,583245,92639,914 1074300
KY 8 8,0,0,0 636614,623283,120396,701 1388708
LA 9 9,0,0,0 927837,712586,123293,4719 1783959
MA 12 12,0,0,0 1571763,718107,227217,4565 2556786
MD 10 10,0,0,0 966207,681530,115812,2606 1780870
ME 4 4,0,0,0 312788,186378,85970,15279 605897
MI 18 18,0,0,0 1989653,1481212,336670,2322 3848844
MN 10 10,0,0,0 1120438,766476,257704,24908 2192640
MO 11 11,0,0,0 1025935,890016,217188,534 2158065
MS 7 0,7,0,0 394022,439838,52222,0 893857
MT 3 0,3,0,0 167922,179652,55229,0 407261
NC 14 0,14,0,0 1107849,1225938,168059,2108 2515807
ND 3 0,3,0,0 106905,125050,32515,0 266411
NE 5 0,5,0,0 236761,363467,71278,0 677415
NH 4 4,0,0,0 246214,196532,48390,0 499175
NJ 15 15,0,0,0 1652329,1103078,262134,32465 3075807
NM 5 5,0,0,0 273495,232751,32257,13218 556074
NV 4 4,0,0,0 203974,199244,43986,4730 464279
NY 33 33,0,0,0 3756177,1933492,503458,75956 6316129
OH 21 21,0,0,0 2148222,1859883,483207,2962 4534434
OK 8 0,8,0,0 488105,582315,130788,0 1206713
OR 7 7,0,0,0 649641,538152,121221,49415 1377760
PA 23 23,0,0,0 2215819,1801169,430984,3086 4506118
RI 4 4,0,0,0 233050,104683,43723,6040 390284
SC 8 0,8,0,0 506283,573458,64386,0 1151689
SD 3 0,3,0,0 139333,150543,31250,0 323826
TN 11 11,0,0,0 909146,863530,105918,6427 1894105
TX 32 0,32,0,0 2459683,2736167,378537,4810 5611644
UT 5 0,5,0,0 221633,361911,66461,4615 665629
VA 13 0,13,0,0 1091060,1138350,159861,0 2416642
VT 3 3,0,0,0 137894,80352,31024,5585 258449
WA 11 11,0,0,0 1123323,840712,201003,60322 2253837
WI 11 11,0,0,0 1071971,845029,227339,28723 2196169
WV 5 5,0,0,0 327812,233946,71639,0 636459
WY 3 0,3,0,0 77934,105388,25928,0 211571
@2000|0|105405100||Decided in Florida by 537 votes after Bush v. Gore stopped the recount. Gore won the popular vote. One D.C. elector left the ballot blank in protest.
George W. Bush|Republican|Dick Cheney|271|50456002
Al Gore|Democratic|Joe Lieberman|266|50999897
Ralph Nader|Green|Winona LaDuke|0|2882955
AK 3 3,0,0 167398,79004,28747 285560
AL 9 9,0,0 941173,692611,18323 1666272
AR 6 6,0,0 472940,422768,13421 921781
AZ 8 8,0,0 781652,685341,45645 1532016
CA 54 0,54,0 4567429,5861203,418707 10965856
CO 8 8,0,0 883748,738227,91434 1741368
CT 8 0,8,0 561094,816015,64452 1459525
DC 3 0,2,0 18073,171923,10576 201894
DE 3 0,3,0 137288,180068,8307 327622
FL 25 25,0,0 2912790,2912253,97488 5963110
GA 13 13,0,0 1419720,1116230,13432 2596804
HI 4 0,4,0 137845,205286,21623 367951
IA 7 0,7,0 634373,638517,29374 1315563
ID 4 4,0,0 336937,138637,12292 501621
IL 22 0,22,0 2019421,2589026,103759 4742123
IN 12 12,0,0 1245836,901980,18531 2199302
KS 6 6,0,0 622332,399276,36086 1072218
KY 8 8,0,0 872492,638898,23192 1544187
LA 9 9,0,0 927871,792344,20473 1765656
MA 12 0,12,0 878502,1616487,173564 2702984
MD 10 0,10,0 813797,1145782,53768 2025480
ME 4 0,4,0 286616,319951,37127 651817
MI 18 0,18,0 1953139,2170418,84165 4232501
MN 10 0,10,0 1109659,1168266,126696 2438685
MO 11 11,0,0 1189924,1111138,38515 2359892
MS 7 7,0,0 572844,404614,8122 994184
MT 3 3,0,0 240178,137126,24437 410997
NC 14 14,0,0 1631163,1257692,0 2911262
ND 3 3,0,0 174852,95284,9486 288256
NE 5 5,0,0 433862,231780,24540 697019
NH 4 4,0,0 273559,266348,22198 569081
NJ 15 0,15,0 1284173,1788850,94554 3187226
NM 5 0,5,0 286417,286783,21251 598605
NV 4 4,0,0 301575,279978,15008 608970
NY 33 0,33,0 2403374,4107697,244030 6821999
OH 21 21,0,0 2351209,2186190,117857 4705457
OK 8 8,0,0 744337,474276,0 1234229
OR 7 0,7,0 713577,720342,77357 1533968
PA 23 0,23,0 2281127,2485967,103392 4913119
RI 4 0,4,0 130555,249508,25052 409112
SC 8 8,0,0 785937,565561,20200 1382717
SD 3 3,0,0 190700,118804,0 316269
TN 11 11,0,0 1061949,981720,19781 2076181
TX 32 32,0,0 3799639,2433746,137994 6407637
UT 5 5,0,0 515096,203053,35850 770754
VA 13 13,0,0 1437490,1217290,59398 2739447
VT 3 0,3,0 119775,149022,20374 294308
WA 11 0,11,0 1108864,1247652,103002 2487433
WI 11 0,11,0 1237279,1242987,94070 2598607
WV 5 5,0,0 336475,295497,10680 648124
WY 3 3,0,0 147947,60481,4625 218351
@2004|0|122290630||A wartime election that came down to Ohio. A Minnesota elector voted for John Edwards, apparently by mistake.
George W. Bush|Republican|Dick Cheney|286|62040610
John Kerry|Democratic|John Edwards|251|59028444
John Edwards|Democratic|John Edwards|1|
AK 3 3,0,0 190889,111025, 312598
AL 9 9,0,0 1176394,693933, 1883449
AR 6 6,0,0 572898,469953, 1054945
AZ 10 10,0,0 1104294,893524, 2012585
CA 55 0,55,0 5509826,6745485, 12421852
CO 9 9,0,0 1101255,1001732, 2130330
CT 7 0,7,0 693826,857488, 1578769
DC 3 0,3,0 21256,202970, 227586
DE 3 0,3,0 171660,200152, 375190
FL 27 27,0,0 3964522,3583544, 7609810
GA 15 15,0,0 1914254,1366149, 3301875
HI 4 0,4,0 194191,231708, 429013
IA 7 7,0,0 751957,741898, 1506908
ID 4 4,0,0 409235,181098, 598447
IL 21 0,21,0 2345946,2891550, 5274322
IN 11 11,0,0 1479438,969011, 2468002
KS 6 6,0,0 736456,434993, 1187756
KY 8 8,0,0 1069439,712733, 1795882
LA 9 9,0,0 1102169,820299, 1943106
MA 12 0,12,0 1071109,1803800, 2912388
MD 10 0,10,0 1024703,1334493, 2386678
ME 4 0,4,0 330201,396842, 740752
MI 17 0,17,0 2313746,2479183, 4839252
MN 10 0,9,1 1346695,1445014, 2828387
MO 11 11,0,0 1455713,1259171, 2731364
MS 6 6,0,0 684981,458094, 1152145
MT 3 3,0,0 266063,173710, 450445
NC 15 15,0,0 1961166,1525849, 3501007
ND 3 3,0,0 196651,111052, 312833
NE 5 5,0,0 512814,254328, 778186
NH 4 0,4,0 331237,340511, 677738
NJ 15 0,15,0 1670003,1911430, 3611691
NM 5 5,0,0 376930,370942, 756304
NV 5 5,0,0 418690,397190, 829587
NY 31 0,31,0 2962567,4314280, 7391036
OH 20 20,0,0 2859768,2741167, 5627908
OK 7 7,0,0 959792,503966, 1463758
OR 7 0,7,0 866831,943163, 1836782
PA 21 0,21,0 2793847,2938095, 5769590
RI 4 0,4,0 169046,259765, 437134
SC 8 8,0,0 937974,661699, 1617730
SD 3 3,0,0 232584,149244, 388215
TN 11 11,0,0 1384375,1036477, 2437319
TX 34 34,0,0 4526917,2832704, 7410765
UT 5 5,0,0 663742,241199, 927844
VA 13 13,0,0 1716959,1454742, 3198367
VT 3 0,3,0 121180,184067, 312309
WA 11 0,11,0 1304894,1510201, 2859084
WI 10 0,10,0 1478120,1489504, 2997007
WV 5 5,0,0 423778,326541, 755887
WY 3 3,0,0 167629,70776, 243428
@2008|0|131313820||Obama becomes the first Black president, amid the financial crisis. Nebraska splits for the first time, as its 2nd district goes Democratic.
Barack Obama|Democratic|Joe Biden|365|69498516
John McCain|Republican|Sarah Palin|173|59948323
AK 3 0,3 123594,193841 326197
AL 9 0,9 813479,1266546 2099819
AR 6 0,6 422310,638017 1086617
AZ 10 0,10 1034707,1230111 2293475
CA 55 55,0 8274473,5011781 13561900
CO 9 9,0 1288633,1073629 2401462
CT 7 7,0 997772,629428 1646797
DC 3 3,0 245800,17367 265853
DE 3 3,0 255459,152374 412412
FL 27 27,0 4282074,4045624 8390744
GA 15 0,15 1844123,2048759 3924486
HI 4 4,0 325871,120566 453568
IA 7 7,0 828940,682379 1537123
ID 4 0,4 236440,403012 655122
IL 21 21,0 3419348,2031179 5522371
IN 11 11,0 1374039,1345648 2751054
KS 6 0,6 514765,699655 1235872
KY 8 0,8 751985,1048462 1826620
LA 9 0,9 782989,1148275 1960761
MA 12 12,0 1904097,1108854 3080985
MD 10 10,0 1629467,959862 2631596
ME 4 4,0 421923,295273 731163
MI 17 17,0 2872579,2048639 5001766
MN 10 10,0 1573354,1275409 2910369
MO 11 0,11 1441911,1445814 2925205
MS 6 0,6 554662,724597 1289865
MT 3 0,3 231667,242763 490302
NC 15 15,0 2142651,2128474 4310789
ND 3 0,3 141278,168601 316621
NE 5 1,4 333319,452979 801281
NH 4 4,0 384826,316534 710970
NJ 15 15,0 2215422,1613207 3868237
NM 5 5,0 472422,346832 830158
NV 5 5,0 533736,412827 967848
NY 31 31,0 4804945,2752771 7640931
OH 20 20,0 2940044,2677820 5708350
OK 7 0,7 502496,960165 1462661
OR 7 7,0 1037291,738475 1827864
PA 21 21,0 3276363,2655885 6013272
RI 4 4,0 296571,165391 471766
SC 8 0,8 862449,1034896 1920969
SD 3 0,3 170924,203054 381975
TN 11 0,11 1087437,1479178 2599749
TX 34 0,34 3528633,4479328 8077795
UT 5 0,5 327670,596030 952370
VA 13 13,0 1959532,1725005 3723260
VT 3 3,0 219262,98974 325046
WA 11 11,0 1750848,1229216 3036878
WI 10 10,0 1677211,1262393 2983417
WV 5 0,5 303857,397466 713451
WY 3 0,3 82868,164958 254658
@2012|0|129085410||Obama re-elected over Romney, losing only Indiana and North Carolina from his 2008 map.
Barack Obama|Democratic|Joe Biden|332|65915795
Mitt Romney|Republican|Paul Ryan|206|60933504
Gary Johnson|Libertarian|Jim Gray|0|1275971
AK 3 0,3,0 122640,164676,7392 300495
AL 9 0,9,0 795696,1255925,12328 2074338
AR 6 0,6,0 394409,647744,16276 1069468
AZ 11 0,11,0 1025232,1233654,32100 2299254
CA 55 55,0,0 7854285,4839958,143221 13038547
CO 9 9,0,0 1323102,1185243,35545 2569518
CT 7 7,0,0 905083,634892,12580 1558960
DC 3 3,0,0 267070,21381,2083 293764
DE 3 3,0,0 242584,165484,3882 413921
FL 29 29,0,0 4237756,4163447,44726 8474179
GA 16 0,16,0 1773827,2078688,45324 3900050
HI 4 4,0,0 306658,121015,3840 434697
IA 6 6,0,0 822544,730617,12926 1582180
ID 4 0,4,0 212787,420911,9453 656742
IL 20 20,0,0 3019512,2135216,56229 5242014
IN 11 0,11,0 1152887,1420543,50111 2624534
KS 6 0,6,0 440726,692634,20456 1159971
KY 8 0,8,0 679370,1087190,17063 1797212
LA 8 0,8,0 809141,1152262,18157 1994065
MA 11 11,0,0 1921290,1188314,30920 3167767
MD 10 10,0,0 1677844,971869,30195 2707327
ME 4 4,0,0 401306,292276,9352 713180
MI 16 16,0,0 2564569,2115256,7774 4730961
MN 10 10,0,0 1546167,1320225,35098 2936561
MO 10 0,10,0 1223796,1482440,43151 2757323
MS 6 0,6,0 562949,710746,6676 1285584
MT 3 0,3,0 201839,267928,14165 484048
NC 15 0,15,0 2178391,2270395,44515 4505372
ND 3 0,3,0 124827,188163,5231 322627
NE 5 0,5,0 302081,475064,11109 794379
NH 4 4,0,0 369561,329918,8212 710972
NJ 14 14,0,0 2125101,1477568,21045 3640292
NM 5 5,0,0 415335,335788,27788 783758
NV 6 6,0,0 531373,463567,10968 1014918
NY 29 29,0,0 4485741,2490431,47256 7081159
OH 18 18,0,0 2827709,2661437,49493 5580847
OK 7 0,7,0 443547,891325,0 1334872
OR 7 7,0,0 970488,754175,24089 1789270
PA 20 20,0,0 2990274,2680434,49991 5753670
RI 4 4,0,0 279677,157204,4388 446049
SC 9 0,9,0 865941,1071645,16321 1964118
SD 3 0,3,0 145039,210610,5795 363815
TN 11 0,11,0 960709,1462330,18623 2458577
TX 38 0,38,0 3308124,4569843,88580 7993851
UT 6 0,6,0 251813,740600,12572 1017440
VA 13 13,0,0 1971820,1822522,31216 3854489
VT 3 3,0,0 199239,92698,3487 299290
WA 12 12,0,0 1755396,1290670,42202 3125516
WI 10 10,0,0 1620985,1407966,20439 3068434
WV 5 0,5,0 238269,417655,6302 670438
WY 3 0,3,0 69286,170962,5326 249061
@2016|0|136669237||Trump wins the Electoral College and loses the popular vote by nearly 3 million. Seven faithless electors, the most in over a century, voted for others; Maine splits for the first time.
Donald Trump|Republican|Mike Pence|304|62984825
Hillary Clinton|Democratic|Tim Kaine|227|65853516
Colin Powell|Republican||3|
John Kasich|Republican|Carly Fiorina|1|
Ron Paul|Libertarian|Mike Pence|1|
Bernie Sanders|Independent|Elizabeth Warren|1|
Faith Spotted Eagle|Democratic|Winona LaDuke|1|
Gary Johnson|Libertarian|Bill Weld|0|4489221
Jill Stein|Green|Ajamu Baraka|0|1457216
AK 3 3,0,0,0,0,0,0,0,0 163387,116454,,,,,,18725,5735 318608
AL 9 9,0,0,0,0,0,0,0,0 1318255,729547,,,,,,44467,9391 2123372
AR 6 6,0,0,0,0,0,0,0,0 684872,380494,,,,,,29949,9473 1130676
AZ 11 11,0,0,0,0,0,0,0,0 1252401,1161167,,,,,,106327,34345 2573165
CA 55 0,55,0,0,0,0,0,0,0 4483810,8753788,,,,,,478500,278657 14181595
CO 9 0,9,0,0,0,0,0,0,0 1202484,1338870,,,,,,144121,38437 2780247
CT 7 0,7,0,0,0,0,0,0,0 673215,897572,,,,,,48676,22841 1644920
DC 3 0,3,0,0,0,0,0,0,0 12723,282830,,,,,,4906,4258 311268
DE 3 0,3,0,0,0,0,0,0,0 185127,235603,,,,,,14757,6103 443814
FL 29 29,0,0,0,0,0,0,0,0 4617886,4504975,,,,,,207043,64399 9420039
GA 16 16,0,0,0,0,0,0,0,0 2089104,1877963,,,,,,125306,7674 4114732
HI 4 0,3,0,0,0,1,0,0,0 128847,266891,,,,,,15954,12737 428937
IA 6 6,0,0,0,0,0,0,0,0 800983,653669,,,,,,59186,11479 1566031
ID 4 4,0,0,0,0,0,0,0,0 409055,189765,,,,,,28331,8496 690255
IL 20 0,20,0,0,0,0,0,0,0 2146015,3090729,,,,,,209596,76802 5536424
IN 11 11,0,0,0,0,0,0,0,0 1557286,1033126,,,,,,133993,7841 2734958
KS 6 6,0,0,0,0,0,0,0,0 671018,427005,,,,,,55406,23506 1184402
KY 8 8,0,0,0,0,0,0,0,0 1202971,628854,,,,,,53752,13913 1924149
LA 8 8,0,0,0,0,0,0,0,0 1178638,780154,,,,,,37978,14031 2029032
MA 11 0,11,0,0,0,0,0,0,0 1090893,1995196,,,,,,138018,47661 3325046
MD 10 0,10,0,0,0,0,0,0,0 943169,1677928,,,,,,79605,35945 2781446
ME 4 1,3,0,0,0,0,0,0,0 335593,357735,,,,,,38105,14251 747927
MI 16 16,0,0,0,0,0,0,0,0 2279543,2268839,,,,,,172136,51463 4799284
MN 10 0,10,0,0,0,0,0,0,0 1322951,1367716,,,,,,112972,36985 2944813
MO 10 10,0,0,0,0,0,0,0,0 1594511,1071068,,,,,,97359,25419 2808605
MS 6 6,0,0,0,0,0,0,0,0 700714,485131,,,,,,14435,3731 1209357
MT 3 3,0,0,0,0,0,0,0,0 279240,177709,,,,,,28037,7970 497147
NC 15 15,0,0,0,0,0,0,0,0 2362631,2189316,,,,,,130126,12105 4741564
ND 3 3,0,0,0,0,0,0,0,0 216794,93758,,,,,,21434,3780 344360
NE 5 5,0,0,0,0,0,0,0,0 495961,284494,,,,,,38946,8775 844227
NH 4 0,4,0,0,0,0,0,0,0 345790,348526,,,,,,30777,6496 744296
NJ 14 0,14,0,0,0,0,0,0,0 1601933,2148278,,,,,,72477,37772 3874046
NM 5 0,5,0,0,0,0,0,0,0 319667,385234,,,,,,74541,9879 798319
NV 6 0,6,0,0,0,0,0,0,0 512058,539260,,,,,,37384,0 1125385
NY 29 0,29,0,0,0,0,0,0,0 2819534,4556124,,,,,,176598,107934 7721453
OH 18 18,0,0,0,0,0,0,0,0 2841005,2394164,,,,,,174498,46271 5496487
OK 7 7,0,0,0,0,0,0,0,0 949136,420375,,,,,,83481,0 1452992
OR 7 0,7,0,0,0,0,0,0,0 782403,1002106,,,,,,94231,50002 2001336
PA 20 20,0,0,0,0,0,0,0,0 2970733,2926441,,,,,,146715,49941 6165478
RI 4 0,4,0,0,0,0,0,0,0 180543,252525,,,,,,14746,6220 464144
SC 9 9,0,0,0,0,0,0,0,0 1155389,855373,,,,,,49204,13034 2103027
SD 3 3,0,0,0,0,0,0,0,0 227721,117458,,,,,,20850,0 370093
TN 11 11,0,0,0,0,0,0,0,0 1522925,870695,,,,,,70397,15993 2508027
TX 38 36,0,0,1,1,0,0,0,0 4685047,3877868,,,,,,283492,71558 8969226
UT 6 6,0,0,0,0,0,0,0,0 515231,310676,,,,,,39608,9438 1131430
VA 13 0,13,0,0,0,0,0,0,0 1769443,1981473,,,,,,118274,27638 3984631
VT 3 0,3,0,0,0,0,0,0,0 95369,178573,,,,,,10078,6758 315067
WA 12 0,8,3,0,0,0,1,0,0 1221747,1742718,,,,,,160879,58417 3317019
WI 10 10,0,0,0,0,0,0,0,0 1405284,1382536,,,,,,106674,31072 2976150
WV 5 5,0,0,0,0,0,0,0,0 489371,188794,,,,,,23004,8075 714423
WY 3 3,0,0,0,0,0,0,0,0 174419,55973,,,,,,13287,2515 255849
@2020|0|158429631||A pandemic election with record turnout. Biden wins back the 'blue wall'; Trump refused to concede and his supporters attacked the Capitol on January 6.
Joe Biden|Democratic|Kamala Harris|306|81283501
Donald Trump|Republican|Mike Pence|232|74223975
Jo Jorgensen|Libertarian|Spike Cohen|0|1865535
AK 3 0,3,0 153778,189951,8897 359530
AL 9 0,9,0 849624,1441170,25176 2323282
AR 6 0,6,0 423932,760647,13133 1219069
AZ 11 11,0,0 1672143,1661686,51465 3387326
CA 55 55,0,0 11110639,6006518,187910 17501380
CO 9 9,0,0 1804352,1364607,52460 3256980
CT 7 7,0,0 1080831,714717,20230 1823857
DC 3 3,0,0 317323,18586,2036 344356
DE 3 3,0,0 296268,200603,5000 504346
FL 29 0,29,0 5297045,5668731,70324 11067456
GA 16 16,0,0 2473633,2461854,62229 4999960
HI 4 4,0,0 366130,196864,5539 574469
IA 6 0,6,0 759061,897672,19637 1690871
ID 4 0,4,0 287021,554119,16404 867934
IL 20 20,0,0 3471915,2446891,66544 6033744
IN 11 0,11,0 1242498,1729857,58901 3033210
KS 6 0,6,0 570323,771406,30574 1373986
KY 8 0,8,0 772474,1326646,26234 2136768
LA 8 0,8,0 856034,1255776,21645 2148062
MA 11 11,0,0 2382202,1167202,47013 3631402
MD 10 10,0,0 1985023,976414,33488 3037030
ME 4 3,1,0 435072,360737,14152 819461
MI 16 16,0,0 2804040,2649852,60381 5539302
MN 10 10,0,0 1717077,1484065,34976 3277171
MO 10 0,10,0 1253014,1718736,41205 3025962
MS 6 0,6,0 539398,756764,8026 1313759
MT 3 0,3,0 244786,343602,15252 603674
NC 15 0,15,0 2684292,2758775,48678 5524804
ND 3 0,3,0 115042,235751,9371 362024
NE 5 1,4,0 374583,556846,20283 956383
NH 4 4,0,0 424937,365660,13236 806205
NJ 14 14,0,0 2608400,1883313,31677 4549457
NM 5 5,0,0 501614,401894,12585 923965
NV 6 6,0,0 703486,669890,14783 1405376
NY 29 29,0,0 5244886,3251997,60383 8616861
OH 18 0,18,0 2679165,3154834,67569 5922202
OK 7 0,7,0 503890,1020280,24731 1560699
OR 7 7,0,0 1340383,958448,41582 2374321
PA 20 20,0,0 3458229,3377674,79380 6936976
RI 4 4,0,0 307486,199922,5053 517757
SC 9 0,9,0 1091541,1385103,27916 2513329
SD 3 0,3,0 150471,261043,11095 422609
TN 11 0,11,0 1143711,1852475,29877 3053851
TX 38 0,38,0 5259126,5890347,126243 11315056
UT 6 0,6,0 560282,865140,38447 1488289
VA 13 13,0,0 2413568,1962430,64761 4460524
VT 3 3,0,0 242820,112704,3608 367428
WA 12 12,0,0 2369612,1584651,80500 4087631
WI 10 10,0,0 1630866,1610184,38491 3298041
WV 5 0,5,0 235984,545382,10687 794731
WY 3 0,3,0 73491,193559,5768 276765
@2024|0|155238302||Trump returns, the second president after Cleveland to win non-consecutive terms. Biden withdrew in July and Harris took over. Trump sweeps the seven swing states and wins the popular vote.
Donald Trump|Republican|JD Vance|312|77302580
Kamala Harris|Democratic|Tim Walz|226|75017613
Jill Stein|Green|Butch Ware|0|862049
Robert F. Kennedy Jr.|Independent|Nicole Shanahan|0|756393
AK 3 3,0,0,0 184458,140026,2342,5670 338177
AL 9 9,0,0,0 1462616,772412,4319,12075 2265090
AR 6 6,0,0,0 759241,396905,4275,13255 1182676
AZ 11 11,0,0,0 1770242,1582860,18319,0 3390161
CA 54 0,54,0,0 6081697,9276179,167814,197645 15865475
CO 10 0,10,0,0 1377441,1728159,17344,35623 3192745
CT 7 0,7,0,0 736918,992053,14281,8448 1759010
DC 3 0,3,0,0 21076,294185,0,2778 325869
DE 3 0,3,0,0 214351,289758,914,4636 512912
FL 30 30,0,0,0 6110125,4683038,43155,0 10893752
GA 16 16,0,0,0 2663117,2548017,18229,0 5250905
HI 4 0,4,0,0 193661,313044,4387,0 516701
IA 6 6,0,0,0 927019,707278,0,13122 1663506
ID 4 4,0,0,0 605246,274972,2973,12812 905057
IL 19 0,19,0,0 2449079,3062863,31023,80426 5633310
IN 11 11,0,0,0 1720347,1163603,0,29325 2936677
KS 6 6,0,0,0 758802,544853,0,16322 1327591
KY 8 8,0,0,0 1337494,704043,7566,16769 2074530
LA 8 8,0,0,0 1208505,766870,7138,6641 2006975
MA 11 0,11,0,0 1251303,2126518,26545,0 3473668
MD 10 0,10,0,0 1035550,1902577,33134,28819 3038334
ME 4 1,3,0,0 377977,435652,8967,0 831375
MI 15 15,0,0,0 2816636,2736533,44607,26785 5664186
MN 10 0,10,0,0 1519032,1656979,16275,24001 3253920
MO 10 10,0,0,0 1751986,1200599,17135,0 2995327
MS 6 6,0,0,0 747744,466668,1873,5387 1228008
MT 4 4,0,0,0 352079,231906,2878,11825 602990
NC 16 16,0,0,0 2898423,2715375,24762,0 5699141
ND 3 3,0,0,0 246505,112327,0,0 368155
NE 5 4,1,0,0 564816,369995,2887,0 952182
NH 4 0,4,0,0 395523,418488,3680,0 826189
NJ 14 0,14,0,0 1968215,2220713,39041,23479 4272725
NM 5 0,5,0,0 423391,478802,4611,9553 923403
NV 6 6,0,0,0 751205,705197,0,0 1484840
NY 28 0,28,0,0 3578899,4619195,46698,0 8262495
OH 17 17,0,0,0 3180116,2533699,0,0 5767788
OK 7 7,0,0,0 1036213,499599,0,16020 1566173
OR 8 0,8,0,0 919480,1240600,19099,33733 2244493
PA 19 19,0,0,0 3543308,3423042,34538,0 7058732
RI 4 0,4,0,0 214406,285156,2900,5045 513386
SC 9 9,0,0,0 1483747,1028452,8117,0 2548140
SD 3 3,0,0,0 272081,146859,0,7204 428922
TN 11 11,0,0,0 1966865,1056265,8967,21535 3063942
TX 40 40,0,0,0 6393597,4835250,82701,0 11388674
UT 6 6,0,0,0 883818,562566,8222,0 1488494
VA 13 0,13,0,0 2075085,2335395,34888,0 4505941
VT 3 0,3,0,0 119395,235791,893,5905 369422
WA 12 0,12,0,0 1530923,2245849,29754,54868 3924243
WI 10 10,0,0,0 1697626,1668229,12275,17740 3422918
WV 4 4,0,0,0 533556,214309,2531,8947 762582
WY 3 3,0,0,0 192633,69527,0,0 269048
""")
BY_YEAR = {e.year: e for e in ELECTIONS}

if __name__ == "__main__":
    main()
