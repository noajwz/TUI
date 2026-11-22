#!/usr/bin/env python3
"""Cascade — a falling-block puzzle in the terminal.

Four-cell pieces fall into a well ten wide; fill a row and it clears. There's a ghost showing
where the piece will land, a hold slot, the next three pieces, and a short grace before a piece
locks, so it can still slide along the floor.

Three ways to play:
- Marathon: it speeds up every ten rows; play until the well overflows.
- Sprint: clear forty rows as fast as you can.
- Cascade: when rows clear, whatever is left hanging falls — each loose clump drops as a solid
  piece — and if that fills more rows they clear too, as a chain, each link worth more.

The best scores of each kind are kept (~/.local/state/cascade_tui/scores.json).
"""

import argparse
import curses
import json
import locale
import os
import random
import tempfile
import time
from collections import deque

WIDTH, HEIGHT, HIDDEN = 10, 20, 2       # the well; HIDDEN rows above the top are where pieces appear
TICK_MS = 16
LOCK_DELAY = 0.5                        # seconds a piece may rest on something before it locks
LOCK_RESETS = 15                        # moves that restart that grace, at most
FLASH = 0.18                            # how long full rows flash before they go
SPRINT_ROWS = 40
NEXT_SHOWN = 3
MODES = ("marathon", "sprint", "cascade")
SCORES_PATH = os.path.expanduser("~/.local/state/cascade_tui/scores.json")
MIN_W, MIN_H = 56, 24
CLEAR_POINTS = {1: 100, 2: 300, 3: 500, 4: 800}

# shape: (cells in a box of size n, n, colour)
SHAPES = {
    "bar": ([(0, 1), (1, 1), (2, 1), (3, 1)], 4, 209),
    "square": ([(0, 0), (1, 0), (0, 1), (1, 1)], 2, 141),
    "tee": ([(1, 0), (0, 1), (1, 1), (2, 1)], 3, 149),
    "step": ([(1, 0), (2, 0), (0, 1), (1, 1)], 3, 221),
    "stair": ([(0, 0), (1, 0), (1, 1), (2, 1)], 3, 117),
    "hook": ([(0, 0), (0, 1), (1, 1), (2, 1)], 3, 211),
    "crook": ([(2, 0), (0, 1), (1, 1), (2, 1)], 3, 79),
}
EIGHT = {"bar": 1, "square": 5, "tee": 2, "step": 3, "stair": 6, "hook": 5, "crook": 4}   # colours in 8-colour terminals
NAMES = list(SHAPES)
# where to try a turned piece if it doesn't fit where it is: sideways first, then up a row
KICKS = ((0, 0), (-1, 0), (1, 0), (0, -1), (-2, 0), (2, 0), (-1, -1), (1, -1))


def rotations(name):
    cells, n, _ = SHAPES[name]
    out = [cells]
    for _ in range(3):
        out.append([(n - 1 - y, x) for x, y in out[-1]])
    return out


ROTATIONS = {name: rotations(name) for name in NAMES}


# -- the well --

class Piece:
    def __init__(self, name):
        self.name = name
        self.rot = 0
        n = SHAPES[name][1]
        self.x = (WIDTH - n) // 2
        self.y = HIDDEN - 1 if n > 2 else HIDDEN

    def cells(self, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        return [(x + cx, y + cy) for cx, cy in ROTATIONS[self.name][rot % 4]]


class Game:
    """The rules, with no screen: the window calls the moves and `update(now)` and draws what's
    here."""

    def __init__(self, mode, level=1, seed=None):
        self.mode = mode
        self.rng = random.Random(seed)
        self.grid = [[None] * WIDTH for _ in range(HEIGHT + HIDDEN)]
        self.bag = []
        self.queue = deque(self.draw_bag() for _ in range(NEXT_SHOWN))
        self.hold = None
        self.held = False                   # one hold per piece
        self.score = 0
        self.rows = 0
        self.start_level = level
        self.combo = -1
        self.chain = 0
        self.best_chain = 0
        self.pieces = 0
        self.over = False
        self.won = False
        self.paused = False
        self.started = time.monotonic()
        self.pause_started = None
        self.paused_for = 0.0
        self.flashing = None                # (rows, until) while full rows flash
        self.lock_at = None
        self.resets = 0
        self.fall_due = self.started + self.interval()
        self.last_clear = ""
        self.piece = None
        self.spawn()

    # -- pieces --

    def draw_bag(self):
        if not self.bag:
            self.bag = list(NAMES)
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def spawn(self, name=None):
        """The next piece from the queue — or, swapping with the hold slot, that one."""
        if name is None:
            name = self.queue.popleft()
            self.queue.append(self.draw_bag())
            self.held = False               # a fresh piece may be held again
        self.piece = Piece(name)
        self.lock_at = None
        self.resets = 0
        if not self.fits(self.piece.cells()):
            self.over = True
        self.fall_due = time.monotonic() + self.interval()

    def fits(self, cells):
        for x, y in cells:
            if not (0 <= x < WIDTH and y < HEIGHT + HIDDEN) or (y >= 0 and self.grid[y][x] is not None):
                return False
        return True

    @property
    def level(self):
        if self.mode == "sprint":
            return 1
        return self.start_level + self.rows // 10

    def interval(self):
        """Seconds per row of falling at this level."""
        return max(0.03, 0.8 * 0.84 ** (self.level - 1))

    def elapsed(self):
        now = self.pause_started or time.monotonic()
        return now - self.started - self.paused_for

    # -- moves --

    def touched(self):
        """A move or a turn while resting on something gives it a little longer, a few times."""
        if self.lock_at is not None and self.resets < LOCK_RESETS:
            self.resets += 1
            self.lock_at = time.monotonic() + LOCK_DELAY
        if self.on_ground():
            if self.lock_at is None:
                self.lock_at = time.monotonic() + LOCK_DELAY
        else:
            self.lock_at = None

    def busy(self):
        return self.over or self.paused or self.flashing is not None or self.won

    def shift(self, dx):
        if self.busy():
            return False
        p = self.piece
        if self.fits(p.cells(x=p.x + dx)):
            p.x += dx
            self.touched()
            return True
        return False

    def turn(self, direction):
        if self.busy():
            return False
        p = self.piece
        rot = (p.rot + direction) % 4
        for kx, ky in KICKS:
            if self.fits(p.cells(x=p.x + kx, y=p.y + ky, rot=rot)):
                p.x, p.y, p.rot = p.x + kx, p.y + ky, rot
                self.touched()
                return True
        return False

    def on_ground(self):
        p = self.piece
        return not self.fits(p.cells(y=p.y + 1))

    def soft_drop(self):
        if self.busy():
            return
        p = self.piece
        if self.fits(p.cells(y=p.y + 1)):
            p.y += 1
            self.score += 1
            self.fall_due = time.monotonic() + self.interval()
            self.touched()
        elif self.lock_at is None:
            self.lock_at = time.monotonic() + LOCK_DELAY

    def ghost_y(self):
        p = self.piece
        y = p.y
        while self.fits(p.cells(y=y + 1)):
            y += 1
        return y

    def hard_drop(self):
        if self.busy():
            return
        y = self.ghost_y()
        self.score += 2 * (y - self.piece.y)
        self.piece.y = y
        self.lock()

    def hold_piece(self):
        if self.busy() or self.held:
            return
        current = self.piece.name
        if self.hold is None:
            self.hold = current
            self.spawn()
        else:
            self.hold, swap = current, self.hold
            self.spawn(swap)
        self.held = True

    # -- locking and clearing --

    def lock(self):
        colour = self.piece.name
        if all(y < HIDDEN for _, y in self.piece.cells()):
            self.over = True                # locked entirely above the well
        for x, y in self.piece.cells():
            if y >= 0:
                self.grid[y][x] = colour
        self.pieces += 1
        self.lock_at = None
        self.chain = 0
        self.check_rows(first=True)

    def full_rows(self):
        return [y for y in range(HEIGHT + HIDDEN) if all(c is not None for c in self.grid[y])]

    def check_rows(self, first=False):
        rows = self.full_rows()
        if rows:
            self.flashing = (rows, time.monotonic() + FLASH)
            return
        if first:
            self.combo = -1
        if not self.over:
            self.spawn()

    def finish_clear(self):
        rows, _ = self.flashing
        self.flashing = None
        n = len(rows)
        for y in sorted(rows):
            del self.grid[y]
            self.grid.insert(0, [None] * WIDTH)
        self.chain += 1
        self.best_chain = max(self.best_chain, self.chain)
        if self.chain == 1:
            self.combo += 1
        points = CLEAR_POINTS.get(n, 800 + 400 * (n - 4)) * self.level * self.chain
        points += 50 * max(0, self.combo) * self.level
        before = self.level
        self.rows += n
        self.score += points
        words = {1: "one row", 2: "two rows", 3: "three rows", 4: "four rows!"}.get(n, f"{n} rows!")
        extra = f" · chain ×{self.chain}" if self.chain > 1 else f" · combo {self.combo}" if self.combo > 0 else ""
        self.last_clear = f"{words}{extra}  +{points:,}"
        if self.level > before:
            self.last_clear += f"  · level {self.level}"
        if self.mode == "sprint" and self.rows >= SPRINT_ROWS:
            self.won = True
            self.pause_started = time.monotonic()
            return
        if self.mode == "cascade" and self.settle():
            self.check_rows()
            return
        self.check_rows()

    def settle(self):
        """Cascade: every clump of blocks touching side to side or top to bottom falls as one
        piece until it rests. Returns whether anything moved."""
        grid = self.grid
        moved_any = False
        while True:
            seen, clumps = set(), []
            for y in range(HEIGHT + HIDDEN):
                for x in range(WIDTH):
                    if grid[y][x] is not None and (x, y) not in seen:
                        clump, stack = [], [(x, y)]
                        seen.add((x, y))
                        while stack:
                            cx, cy = stack.pop()
                            clump.append((cx, cy))
                            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                                if 0 <= nx < WIDTH and 0 <= ny < HEIGHT + HIDDEN and grid[ny][nx] is not None \
                                        and (nx, ny) not in seen:
                                    seen.add((nx, ny))
                                    stack.append((nx, ny))
                        clumps.append(clump)
            moved = False
            for clump in sorted(clumps, key=lambda c: -max(y for _, y in c)):   # the lowest first
                mine = set(clump)
                if all(y + 1 < HEIGHT + HIDDEN and (grid[y + 1][x] is None or (x, y + 1) in mine) for x, y in clump):
                    colours = {(x, y): grid[y][x] for x, y in clump}
                    for x, y in clump:
                        grid[y][x] = None
                    for (x, y), c in colours.items():
                        grid[y + 1][x] = c
                    moved = moved_any = True
            if not moved:
                return moved_any

    # -- time --

    def update(self, now):
        if self.over or self.paused or self.won:
            return
        if self.flashing:
            if now >= self.flashing[1]:
                self.finish_clear()
            return
        if now >= self.fall_due:
            p = self.piece
            if self.fits(p.cells(y=p.y + 1)):
                p.y += 1
                if self.on_ground() and self.lock_at is None:
                    self.lock_at = now + LOCK_DELAY
            elif self.lock_at is None:
                self.lock_at = now + LOCK_DELAY
            self.fall_due = now + self.interval()
        if self.lock_at is not None and now >= self.lock_at:
            if self.on_ground():
                self.lock()
            else:
                self.lock_at = None

    def toggle_pause(self):
        if self.over or self.won:
            return
        self.paused = not self.paused
        now = time.monotonic()
        if self.paused:
            self.pause_started = now
        else:
            self.paused_for += now - self.pause_started
            self.pause_started = None
            self.fall_due = now + self.interval()
            if self.lock_at is not None:
                self.lock_at = now + LOCK_DELAY


# -- scores --

def load_scores():
    try:
        with open(SCORES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_score(game):
    """Files the finished game; returns its place in the mode's table (1 is the best) or None."""
    scores = load_scores()
    table = scores.setdefault(game.mode, [])
    entry = {"score": game.score, "rows": game.rows, "level": game.level, "seconds": round(game.elapsed(), 2),
             "chain": game.best_chain, "date": time.strftime("%Y-%m-%d %H:%M")}
    if game.mode == "sprint":
        if not game.won:
            return None
        table.append(entry)
        table.sort(key=lambda e: e["seconds"])
    else:
        table.append(entry)
        table.sort(key=lambda e: -e["score"])
    del table[10:]
    place = table.index(entry) + 1 if entry in table else None
    try:
        os.makedirs(os.path.dirname(SCORES_PATH), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SCORES_PATH), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(scores, f, indent=1)
        os.replace(tmp, SCORES_PATH)
    except OSError:
        pass
    return place


def fmt_time(seconds):
    m, s = divmod(seconds, 60)
    return f"{int(m)}:{s:05.2f}"


# -- the window --

class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.args = args
        self.mode = args.mode
        self.game = None
        self.place = None
        self.colours = {}

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def block(self, name, ghost=False):
        attr = self.colours[name]
        return ("░░", attr) if ghost else ("██", attr)

    def start(self):
        self.game = Game(self.mode, self.args.level)
        self.place = None

    # -- drawing --

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        g = self.game
        bw = WIDTH * 2 + 2
        x0 = (w - bw) // 2
        y0 = max(0, (h - HEIGHT - 3) // 2)
        self.draw_well(g, y0, x0)
        self.draw_side(g, y0, x0, bw)
        s.refresh()

    def draw_well(self, g, y0, x0):
        dim = curses.color_pair(8)
        self.put(y0, x0, "┏" + "━" * (WIDTH * 2) + "┓", dim)
        for r in range(HEIGHT):
            self.put(y0 + 1 + r, x0, "┃", dim)
            self.put(y0 + 1 + r, x0 + 1 + WIDTH * 2, "┃", dim)
        self.put(y0 + 1 + HEIGHT, x0, "┗" + "━" * (WIDTH * 2) + "┛", dim)
        flash_rows = set(g.flashing[0]) if g.flashing else set()
        ghost = set()
        active = set()
        if not g.over and not g.flashing and not g.won:
            gy = g.ghost_y()
            ghost = set(g.piece.cells(y=gy))
            active = set(g.piece.cells())
        for r in range(HEIGHT):
            y = r + HIDDEN
            for x in range(WIDTH):
                sx, sy = x0 + 1 + x * 2, y0 + 1 + r
                cell = g.grid[y][x]
                if (x, y) in active:
                    text, attr = self.block(g.piece.name)
                elif cell is not None:
                    if y in flash_rows:
                        text, attr = "██", curses.color_pair(7) | curses.A_BOLD | curses.A_REVERSE
                    elif g.over:
                        text, attr = "██", dim
                    else:
                        text, attr = self.block(cell)
                elif (x, y) in ghost:
                    text, attr = self.block(g.piece.name, ghost=True)
                else:
                    text, attr = " ·", dim
                self.put(sy, sx, text, attr)
        if g.paused or g.over or g.won:
            self.banner(g, y0, x0)

    def banner(self, g, y0, x0):
        if g.paused:
            lines = ["paused", "", "←→ move  ↓ drop", "↑ x turn  z back", "space slam  c hold", "q quit", "",
                     "p to go on"]
        elif g.won:
            lines = ["40 rows!", fmt_time(g.elapsed()), ""] + self.place_line() + ["r again · m modes"]
        else:
            lines = ["the well is full", f"{g.score:,} points", ""] + self.place_line() + ["r again · m modes"]
        top = y0 + 1 + (HEIGHT - len(lines)) // 2
        for i, line in enumerate(lines):
            text = f" {line} ".center(WIDTH * 2)
            self.put(top + i, x0 + 1, text, curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)

    def place_line(self):
        if self.place == 1:
            return ["a new best!", ""]
        if self.place:
            return [f"#{self.place} of your best", ""]
        return []

    def mini(self, name, y, x, attr=None):
        cells, n, _ = SHAPES[name]
        for cx, cy in cells:
            text, a = self.block(name)
            self.put(y + cy, x + cx * 2, text, attr if attr is not None else a)

    def draw_side(self, g, y0, x0, bw):
        dim = curses.color_pair(8)
        left = x0 - 13
        self.put(y0 + 1, left, "hold", dim)
        if g.hold:
            self.mini(g.hold, y0 + 3, left, dim if g.held else None)
        right = x0 + bw + 2
        self.put(y0 + 1, right, "next", dim)
        for i, name in enumerate(list(g.queue)[:NEXT_SHOWN]):
            self.mini(name, y0 + 3 + i * 3, right)
        stats = [("score", f"{g.score:,}")]
        if g.mode == "sprint":
            stats += [("rows", f"{g.rows}/{SPRINT_ROWS}"), ("time", fmt_time(g.elapsed()))]
        else:
            stats += [("level", str(g.level)), ("rows", str(g.rows)), ("time", fmt_time(g.elapsed()))]
        if g.mode == "cascade":
            stats.append(("best chain", f"×{g.best_chain}" if g.best_chain else "—"))
        stats.append(("pieces", str(g.pieces)))
        y = y0 + 8
        for label, value in stats:
            self.put(y, left, label, dim)
            self.put(y + 1, left, value, curses.A_BOLD)
            y += 3
        self.put(y0 + 13, right, g.mode, curses.color_pair(6) | curses.A_BOLD)
        best = load_best(g.mode)
        if best:
            self.put(y0 + 14, right, "best " + best, dim)
        if g.last_clear:
            self.put(y0 + 16, right, g.last_clear[:28], curses.color_pair(3) | curses.A_BOLD)
        keys = ["←→ move", "↑ x turn", "z turn back", "↓ drop", "␣ slam", "c hold", "p pause", "q quit"]
        if self.s.getmaxyx()[1] - right >= 26:
            for i, k in enumerate(keys):
                self.put(y0 + 18 + i // 2, right + (i % 2) * 13, k, dim)
        else:                               # no room: the pause screen lists them
            self.put(y0 + 18, right, "p pauses and", dim)
            self.put(y0 + 19, right, "shows the keys", dim)

    def menu(self):
        """The choice of mode; returns False to quit."""
        sel = MODES.index(self.mode)
        about = {"marathon": "faster every ten rows, until the well overflows",
                 "sprint": f"clear {SPRINT_ROWS} rows as fast as you can",
                 "cascade": "loose blocks fall after a clear, and can set off chains"}
        self.s.timeout(-1)
        try:
            while True:
                self.s.erase()
                h, w = self.s.getmaxyx()
                y = max(1, h // 2 - 8)
                title = "C A S C A D E"
                self.put(y, (w - len(title)) // 2, title, curses.color_pair(6) | curses.A_BOLD)
                for i, name in enumerate(SHAPES):
                    self.mini(name, y + 2, (w - 7 * 9) // 2 + i * 9)
                for i, m in enumerate(MODES):
                    attr = curses.A_REVERSE | curses.A_BOLD if i == sel else 0
                    line = f"  {m:<9} {about[m]}  "
                    self.put(y + 6 + i * 2, (w - 66) // 2, line, attr)
                    best = load_best(m)
                    if best:
                        self.put(y + 7 + i * 2, (w - 66) // 2 + 12, "best " + best, curses.color_pair(8))
                self.put(y + 13, (w - 40) // 2, "↑↓ choose · Enter play · q quit", curses.color_pair(8))
                self.s.refresh()
                ch = self.s.getch()
                if ch in (curses.KEY_UP, ord("k")):
                    sel = (sel - 1) % len(MODES)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    sel = (sel + 1) % len(MODES)
                elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                    self.mode = MODES[sel]
                    return True
                elif ch in (ord("q"), ord("Q"), 27):
                    return False
        finally:
            self.s.timeout(TICK_MS)

    # -- keys --

    def handle(self, ch):
        g = self.game
        if ch in (ord("q"), ord("Q")):
            return "quit"
        if g.over or g.won:
            if ch in (ord("r"), ord("R"), 10, 13):
                return "again"
            if ch in (ord("m"), ord("M")):
                return "menu"
            return None
        if ch in (ord("p"), ord("P"), 27):
            g.toggle_pause()
        elif g.paused:
            return None
        elif ch in (curses.KEY_LEFT, ord("h"), ord("a")):
            g.shift(-1)
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("d")):
            g.shift(1)
        elif ch in (curses.KEY_UP, ord("x"), ord("k"), ord("w")):
            g.turn(1)
        elif ch in (ord("z"), ord("Z")):
            g.turn(-1)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("s")):
            g.soft_drop()
        elif ch == ord(" "):
            g.hard_drop()
        elif ch in (ord("c"), ord("C")):
            g.hold_piece()
        return None

    def play(self):
        """One game; returns "again", "menu" or "quit"."""
        self.start()
        filed = False
        while True:
            self.game.update(time.monotonic())
            if (self.game.over or self.game.won) and not filed:
                self.place = save_score(self.game)
                filed = True
            self.draw()
            ch = self.s.getch()                 # wait up to a tick for a key, then take all that came
            self.s.nodelay(True)
            while ch != -1:
                result = self.handle(ch)
                if result:
                    self.s.timeout(TICK_MS)
                    return result
                ch = self.s.getch()
            self.s.timeout(TICK_MS)


def load_best(mode):
    table = load_scores().get(mode) or []
    if not table:
        return ""
    top = table[0]
    return fmt_time(top["seconds"]) if mode == "sprint" else f"{top['score']:,}"


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, args)
    for k, name in enumerate(NAMES):
        pair = 20 + k
        colour = SHAPES[name][2] if curses.COLORS >= 256 else EIGHT[name]
        curses.init_pair(pair, colour, -1)
        app.colours[name] = curses.color_pair(pair) | (curses.A_BOLD if curses.COLORS < 256 else 0)
    if not args.mode_given and not app.menu():
        return
    while True:
        result = app.play()
        if result == "quit":
            return
        if result == "menu" and not app.menu():
            return


def main():
    parser = argparse.ArgumentParser(description="Cascade — a falling-block puzzle, in three modes.")
    parser.add_argument("mode", nargs="?", choices=MODES, help="go straight into a mode, without the menu")
    parser.add_argument("--level", type=int, default=1, choices=range(1, 16), metavar="1-15",
                        help="the level to start marathon or cascade at")
    parser.add_argument("--scores", action="store_true", help="print the best scores and exit")
    args = parser.parse_args()
    if args.scores:
        scores = load_scores()
        for mode in MODES:
            print(mode)
            for i, e in enumerate(scores.get(mode, []), 1):
                what = fmt_time(e["seconds"]) if mode == "sprint" else f"{e['score']:,} points, {e['rows']} rows"
                print(f"  {i:>2}. {what}  ({e['date']})")
        return
    args.mode_given = args.mode is not None
    args.mode = args.mode or "marathon"
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
