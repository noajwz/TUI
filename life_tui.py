#!/usr/bin/env python3
"""Game of Life — Conway's cellular automaton, and a few of its cousins, in the terminal.

The world fills the window: in braille dots (eight cells to a character), half blocks (two square
cells to a character) or big cells. Draw with the cursor or the mouse, stamp patterns from a
library — gliders, spaceships, the Gosper glider gun, pulsars, methuselahs — turned and flipped
as you like, or load an RLE file from the LifeWiki. Run it, step it, rewind it. Newborn cells show
in another colour, and it notices when the world settles into a still life or an oscillator.

Each row of the world is one Python integer, a bit per cell, and a generation is a few dozen
bitwise operations per row (an adder tree over the eight neighbours), so even a few hundred
thousand cells keep up.
"""

import argparse
import curses
import locale
import os
import random
import re
import time

TICK_MS = 30
MIN_W, MIN_H = 50, 12
SPEEDS = (1, 2, 4, 8, 15, 30, 60, 120, "max")     # generations a second
HISTORY = 200                                     # generations kept for rewinding
CYCLE_WINDOW = 64                                 # how far back repeats are noticed
DENSITY = 0.3
ZOOMS = ("braille", "half", "full")
DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))   # braille bit for (row, column) in a char
HALF = {(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}

RULES = (  # name, born on, survives on
    ("Conway's Life", "3", "23"), ("HighLife", "36", "23"), ("Seeds", "2", ""),
    ("Day & Night", "3678", "34678"), ("Life without Death", "3", "012345678"), ("Maze", "3", "12345"),
    ("Replicator", "1357", "1357"), ("Diamoeba", "35678", "5678"), ("2×2", "36", "125"), ("Morley", "368", "245"),
)

PATTERNS = {  # name: (what it is, the cells: O alive, . dead — or RLE when it ends in !)
    "glider": ("the smallest spaceship; flies diagonally", ".O.\n..O\nOOO"),
    "lightweight spaceship": ("flies sideways, every 4 generations", ".O..O\nO....\nO...O\nOOOO."),
    "heavyweight spaceship": ("a bigger, faster-looking ship", "...OO..\n.O....O\nO......\nO.....O\nOOOOOO."),
    "Gosper glider gun": ("the first gun ever found: a glider every 30 generations",
                          "24bo$22bobo$12b2o6b2o12b2o$11bo3bo4b2o12b2o$2o8bo5bo3b2o$2o8bo3bob2o4bobo$"
                          "10bo5bo7bo$11bo3bo$12b2o!"),
    "pulsar": ("an oscillator of period 3",
               "..OOO...OOO..\n.............\nO....O.O....O\nO....O.O....O\nO....O.O....O\n..OOO...OOO..\n"
               ".............\n..OOO...OOO..\nO....O.O....O\nO....O.O....O\nO....O.O....O\n.............\n"
               "..OOO...OOO.."),
    "pentadecathlon": ("an oscillator of period 15", "..O....O..\nOO.OOOO.OO\n..O....O.."),
    "blinker": ("the smallest oscillator, period 2", "OOO"),
    "toad": ("period 2", ".OOO\nOOO."),
    "beacon": ("period 2", "OO..\nOO..\n..OO\n..OO"),
    "R-pentomino": ("five cells that take 1,103 generations to settle", ".OO\nOO.\n.O."),
    "acorn": ("seven cells that run for 5,206 generations", ".O.....\n...O...\nOO..OOO"),
    "diehard": ("vanishes completely after 130 generations", "......O.\nOO......\n.O...OOO"),
    "pi heptomino": ("a methuselah of 173 generations", "OOO\nO.O\nO.O"),
    "infinite growth": ("ten cells whose pattern never stops growing", "OOO.O\nO....\n...OO\n.OO.O\nO.O.O"),
    "eater": ("a still life that eats gliders", "OO..\nO.O.\n..O.\n..OO"),
    "block": ("the simplest still life", "OO\nOO"),
}


# -- patterns --

def parse_cells(text):
    """Plain text (O or * alive, . dead, ! comments) or RLE, as a set of (x, y)."""
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith(("!", "#"))]
    body = "".join(line.strip() for line in lines if not line.lstrip().startswith("x"))
    if body.endswith("!") or re.fullmatch(r"[0-9bo$!\s]+", body or "x") and ("$" in body or "b" in body):
        return parse_rle(body)
    cells = set()
    for y, line in enumerate(lines):
        for x, ch in enumerate(line):
            if ch in "O*o#":
                cells.add((x, y))
    return cells


def parse_rle(body):
    cells, x, y, n = set(), 0, 0, ""
    for ch in body:
        if ch.isdigit():
            n += ch
            continue
        k = int(n) if n else 1
        n = ""
        if ch == "b" or ch == ".":
            x += k
        elif ch in "oA":
            for _ in range(k):
                cells.add((x, y))
                x += 1
        elif ch == "$":
            y += k
            x = 0
        elif ch == "!":
            break
    return cells


def load_file(path):
    """Cells, and the rule if the file names one (RLE's rule = B3/S23)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    rule = None
    m = re.search(r"rule\s*=\s*B?([0-8]*)/S?([0-8]*)", text, re.I)
    if m:
        rule = (m[1], m[2])
    return parse_cells(text), rule


def normalise(cells):
    if not cells:
        return set()
    mx, my = min(x for x, _ in cells), min(y for _, y in cells)
    return {(x - mx, y - my) for x, y in cells}


def rotate(cells):
    return normalise({(-y, x) for x, y in cells})


def flip(cells):
    return normalise({(-x, y) for x, y in cells})


# -- the world --

class World:
    """Rows of bits. Bit x of rows[y] is the cell at (x, y)."""

    def __init__(self, w, h, wrap=True):
        self.w, self.h, self.wrap = w, h, wrap
        self.rows = [0] * h
        self.born, self.survive = {3}, {2, 3}
        self.gen = 0
        self.births = self.deaths = 0
        self.fresh = [0] * h              # cells born in the last step, for colouring

    @property
    def mask(self):
        return (1 << self.w) - 1

    def get(self, x, y):
        return (self.rows[y] >> x) & 1

    def set(self, x, y, alive):
        if 0 <= x < self.w and 0 <= y < self.h:
            if alive:
                self.rows[y] |= 1 << x
            else:
                self.rows[y] &= ~(1 << x)

    def population(self):
        return sum(r.bit_count() for r in self.rows)

    def cells(self):
        out = set()
        for y, r in enumerate(self.rows):
            while r:
                low = r & -r
                out.add((low.bit_length() - 1, y))
                r ^= low
        return out

    def place(self, cells, ox, oy):
        for x, y in cells:
            x, y = x + ox, y + oy
            if self.wrap:
                x, y = x % self.w, y % self.h
            self.set(x, y, True)

    def clear(self):
        self.rows = [0] * self.h
        self.fresh = [0] * self.h

    def randomise(self, density=DENSITY, rnd=random):
        self.rows = [sum(1 << x for x in range(self.w) if rnd.random() < density) for _ in range(self.h)]
        self.fresh = [0] * self.h

    def step(self):
        """One generation. The eight neighbour planes of each row are added bit-parallel into
        a 4-bit count (ones, twos, fours, eights), and the rule picks which counts live."""
        w, h, mask, rows = self.w, self.h, self.mask, self.rows
        if self.wrap:
            left = [((r << 1) | (r >> (w - 1))) & mask for r in rows]
            right = [(r >> 1) | ((r & 1) << (w - 1)) for r in rows]
        else:
            left = [(r << 1) & mask for r in rows]
            right = [r >> 1 for r in rows]
        born, survive = self.born, self.survive
        conway = born == {3} and survive == {2, 3}
        out = []
        births = deaths = 0
        for y in range(h):
            if y > 0:
                up = y - 1
            elif self.wrap:
                up = h - 1
            else:
                up = None
            down = y + 1 if y + 1 < h else (0 if self.wrap else None)
            b = rows[y]
            planes = [left[y], right[y]]
            if up is not None:
                planes += [left[up], rows[up], right[up]]
            if down is not None:
                planes += [left[down], rows[down], right[down]]
            planes += [0] * (8 - len(planes))
            n0, n1, n2, n3, n4, n5, n6, n7 = planes
            # full adders: 8 one-bit inputs into a 4-bit count, all columns at once
            s1 = n0 ^ n1 ^ n2
            c1 = (n0 & n1) | (n2 & (n0 ^ n1))
            s2 = n3 ^ n4 ^ n5
            c2 = (n3 & n4) | (n5 & (n3 ^ n4))
            s3 = n6 ^ n7
            c3 = n6 & n7
            ones = s1 ^ s2 ^ s3
            c4 = (s1 & s2) | (s3 & (s1 ^ s2))
            t = c1 ^ c2 ^ c3
            d1 = (c1 & c2) | (c3 & (c1 ^ c2))
            twos = t ^ c4
            d2 = t & c4
            fours = d1 ^ d2
            eights = d1 & d2
            if conway:
                nxt = twos & ~fours & ~eights & (ones | b)
            else:
                nxt = 0
                bits = (ones, twos, fours, eights)
                inv = tuple(~x & mask for x in bits)

                def eq(k):
                    m = mask
                    for i in range(4):
                        m &= bits[i] if k >> i & 1 else inv[i]
                    return m
                for k in born:
                    nxt |= eq(k) & ~b
                for k in survive:
                    nxt |= eq(k) & b
            nxt &= mask
            born_here = nxt & ~b
            births += born_here.bit_count()
            deaths += (b & ~nxt).bit_count()
            out.append(nxt)
            self.fresh[y] = born_here
        self.rows = out
        self.gen += 1
        self.births, self.deaths = births, deaths

    def resized(self, w, h):
        """The same cells in a world of another size, centred the same way."""
        cells = self.cells()
        new = World(w, h, self.wrap)
        new.born, new.survive, new.gen = self.born, self.survive, self.gen
        dx, dy = (w - self.w) // 2, (h - self.h) // 2
        for x, y in cells:
            new.set(x + dx, y + dy, True)
        return new


# -- the window --

class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.zoom = args.zoom
        self.speed_i = SPEEDS.index(15)
        self.running = False
        self.rule_i = 0
        self.colour = True
        self.stamp = None                     # (name, cells) while placing a pattern
        self.history = []
        self.seen = {}
        self.cycle = None                     # what the world settled into, once noticed
        self.message, self.message_at = "", 0.0
        self.drawing = None                   # True/False while the mouse paints cells alive/dead
        self.credit = 0.0
        w, h = self.world_size()
        self.world = World(w, h, not args.no_wrap)
        self.cx, self.cy = w // 2, h // 2
        if args.rule:
            self.set_rule(args.rule)
        if args.file:
            try:
                cells, rule = load_file(args.file)
            except OSError as e:
                raise SystemExit(f"life_tui: can't read {args.file}: {e.strerror}")
            if rule:
                self.set_rule(f"B{rule[0]}/S{rule[1]}")
            self.put_centred(normalise(cells))
            self.say(f"loaded {os.path.basename(args.file)}: {len(cells):,} cells")
        elif args.pattern:
            name = next((n for n in PATTERNS if n.lower().startswith(args.pattern.lower())), None)
            if not name:
                raise SystemExit(f"life_tui: no pattern called {args.pattern} — there are: " + ", ".join(PATTERNS))
            self.put_centred(parse_cells(PATTERNS[name][1]))
        else:
            self.world.randomise()
        self.running = not args.paused

    def say(self, text):
        self.message, self.message_at = text, time.monotonic()

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    # -- the world and its size --

    def world_size(self):
        h, w = self.s.getmaxyx()
        rows = max(1, h - 2)
        if self.zoom == "braille":
            return w * 2, rows * 4
        if self.zoom == "half":
            return w, rows * 2
        return w // 2, rows

    def refit(self):
        w, h = self.world_size()
        if (w, h) != (self.world.w, self.world.h):
            old = self.world
            self.world = old.resized(w, h)
            self.cx = min(w - 1, max(0, self.cx + (w - old.w) // 2))
            self.cy = min(h - 1, max(0, self.cy + (h - old.h) // 2))
            self.history.clear()
            self.forget()

    def put_centred(self, cells):
        if not cells:
            return
        wid = max(x for x, _ in cells) + 1
        hei = max(y for _, y in cells) + 1
        if wid > self.world.w or hei > self.world.h:
            self.say(f"the pattern is {wid}×{hei}; this world is {self.world.w}×{self.world.h} — z zooms out")
        self.world.place(cells, (self.world.w - wid) // 2, (self.world.h - hei) // 2)

    def set_rule(self, text):
        m = re.fullmatch(r"\s*B?([0-8]*)\s*/\s*S?([0-8]*)\s*", text, re.I)
        if not m:
            raise SystemExit(f"life_tui: a rule looks like B3/S23, not {text}")
        self.world.born, self.world.survive = {int(c) for c in m[1]}, {int(c) for c in m[2]}
        self.rule_i = next((i for i, (_, b, s) in enumerate(RULES) if set(b) == set(m[1]) and set(s) == set(m[2])), -1)

    def rule_name(self):
        w = self.world
        code = "B" + "".join(map(str, sorted(w.born))) + "/S" + "".join(map(str, sorted(w.survive)))
        return f"{RULES[self.rule_i][0]} {code}" if self.rule_i >= 0 else code

    # -- time --

    def remember(self):
        w = self.world
        self.history.append((list(w.rows), w.gen))
        del self.history[:-HISTORY]

    def forget(self):
        self.seen.clear()
        self.cycle = None

    def advance(self):
        w = self.world
        self.remember()
        w.step()
        key = hash(tuple(w.rows))
        if self.cycle is None:
            if key in self.seen:
                period = w.gen - self.seen[key]
                pop = w.population()
                self.cycle = ("everything died" if pop == 0 else "a still life now" if period == 1
                              else f"oscillating with period {period}")
                self.say(f"generation {w.gen:,}: {self.cycle}")
            self.seen[key] = w.gen
            if len(self.seen) > CYCLE_WINDOW:
                for k in [k for k, g in self.seen.items() if g < w.gen - CYCLE_WINDOW]:
                    del self.seen[k]

    def rewind(self):
        if not self.history:
            self.say("nothing further back")
            return
        rows, gen = self.history.pop()
        self.world.rows, self.world.gen = rows, gen
        self.world.fresh = [0] * self.world.h
        self.forget()

    def tick(self):
        if not self.running:
            return
        sp = SPEEDS[self.speed_i]
        if sp == "max":
            deadline = time.monotonic() + TICK_MS / 1000 * 0.7
            while time.monotonic() < deadline:
                self.advance()
            return
        self.credit += sp * TICK_MS / 1000
        while self.credit >= 1:
            self.credit -= 1
            self.advance()

    # -- editing --

    def edit(self, fn):
        self.remember()
        fn()
        self.world.fresh = [0] * self.world.h
        self.forget()

    def toggle(self):
        w = self.world
        alive = not w.get(self.cx, self.cy)
        self.edit(lambda: w.set(self.cx, self.cy, alive))

    def stamp_here(self):
        name, cells = self.stamp
        wid = max(x for x, _ in cells) + 1
        hei = max(y for _, y in cells) + 1
        self.edit(lambda: self.world.place(cells, self.cx - wid // 2, self.cy - hei // 2))
        self.say(f"placed a {name} — Enter places another, Esc stops")

    def choose_pattern(self):
        names = list(PATTERNS)
        sel, top = 0, 0
        self.s.timeout(-1)
        try:
            while True:
                self.draw()
                h, w = self.s.getmaxyx()
                bh, bw = min(h - 2, len(names) + 3), min(w - 4, 76)
                y0, x0 = (h - bh) // 2, (w - bw) // 2
                body = bh - 3
                top = max(0, min(top, len(names) - body))
                if sel < top:
                    top = sel
                elif sel >= top + body:
                    top = sel - body + 1
                for y in range(y0, y0 + bh):
                    self.put(y, x0, " " * bw, curses.A_REVERSE)
                self.put(y0, x0 + 2, " stamp a pattern ", curses.A_REVERSE | curses.A_BOLD)
                for n, name in enumerate(names[top:top + body]):
                    cells = parse_cells(PATTERNS[name][1])
                    line = f" {name:<22} {len(cells):>3} cells  {PATTERNS[name][0]}"
                    self.put(y0 + 1 + n, x0 + 1, line.ljust(bw - 2)[:bw - 2],
                             curses.A_BOLD if top + n == sel else curses.A_REVERSE)
                self.put(y0 + bh - 1, x0 + 2, "↑↓ Enter · Esc"[:bw - 4], curses.A_REVERSE)
                self.s.refresh()
                ch = self.s.getch()
                if ch in (curses.KEY_UP, ord("k")):
                    sel = (sel - 1) % len(names)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    sel = (sel + 1) % len(names)
                elif ch in (10, 13, curses.KEY_ENTER):
                    self.stamp = (names[sel], parse_cells(PATTERNS[names[sel]][1]))
                    self.say(f"{names[sel]}: move it with the arrows, r turns, f flips, Enter places, Esc stops")
                    return
                elif ch in (27, ord("q"), ord("p")):
                    return
        finally:
            self.s.timeout(TICK_MS)

    # -- drawing --

    def cell_to_screen(self, x, y):
        if self.zoom == "braille":
            return 1 + y // 4, x // 2
        if self.zoom == "half":
            return 1 + y // 2, x
        return 1 + y, x * 2

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        world = self.world
        ghost = set()
        if self.stamp:
            cells = self.stamp[1]
            wid = max(x for x, _ in cells) + 1
            hei = max(y for _, y in cells) + 1
            for x, y in cells:
                gx, gy = x + self.cx - wid // 2, y + self.cy - hei // 2
                if world.wrap:
                    gx, gy = gx % world.w, gy % world.h
                ghost.add((gx, gy))
        self.draw_world(world, ghost, h - 2, w)
        if not self.running or self.stamp:
            r, c = self.cell_to_screen(self.cx, self.cy)
            text = "▒▒" if self.zoom == "full" else self.cursor_char()
            self.put(r, c, text, curses.color_pair(3) | curses.A_BOLD)
        self.draw_bars(h, w)
        s.refresh()

    def cursor_char(self):
        """The cursor's cell alone in its character, so you can see which of the dots it is."""
        if self.zoom == "half":
            return "▀" if self.cy % 2 == 0 else "▄"
        bit = DOTS[self.cy % 4][self.cx % 2]
        return chr(0x2800 + bit)

    def draw_world(self, world, ghost, rows, cols):
        rowsb = world.rows
        fresh = world.fresh
        plain = curses.color_pair(7)
        new = curses.color_pair(2) | curses.A_BOLD if self.colour else plain
        ghost_attr = curses.color_pair(3)
        for sy in range(rows):
            run, run_attr, run_x = [], None, 0
            for sx in range(cols):
                ch, attr = self.char_at(sx, sy, rowsb, fresh, ghost, plain, new, ghost_attr)
                if attr != run_attr:
                    if run:
                        self.put(1 + sy, run_x, "".join(run), run_attr)
                    run, run_attr, run_x = [], attr, sx
                run.append(ch)
            if run:
                self.put(1 + sy, run_x, "".join(run), run_attr)

    def char_at(self, sx, sy, rows, fresh, ghost, plain, new, ghost_attr):
        world = self.world
        if self.zoom == "full":
            x, y = sx // 2, sy
            if x >= world.w or y >= world.h:
                return " ", 0
            if (x, y) in ghost:
                return "█", ghost_attr
            if (rows[y] >> x) & 1:
                return "█", new if (fresh[y] >> x) & 1 else plain
            return " ", 0
        if self.zoom == "half":
            x, y = sx, sy * 2
            if x >= world.w or y >= world.h:
                return " ", 0
            top = (rows[y] >> x) & 1
            bot = (rows[y + 1] >> x) & 1 if y + 1 < world.h else 0
            g = (x, y) in ghost or (x, y + 1) in ghost
            if g:
                top |= (x, y) in ghost
                bot |= (x, y + 1) in ghost
                return HALF[(top, bot)], ghost_attr
            isnew = ((fresh[y] >> x) & 1) or (y + 1 < world.h and (fresh[y + 1] >> x) & 1)
            return HALF[(top, bot)], new if isnew else plain
        x0, y0 = sx * 2, sy * 4
        if x0 >= world.w or y0 >= world.h:
            return " ", 0
        bits = 0
        isnew = False
        g = False
        for dy in range(4):
            y = y0 + dy
            if y >= world.h:
                break
            pair = (rows[y] >> x0) & 3
            if pair & 1:
                bits |= DOTS[dy][0]
            if pair & 2:
                bits |= DOTS[dy][1]
            if (fresh[y] >> x0) & 3:
                isnew = True
            if ghost:
                for dx in (0, 1):
                    if (x0 + dx, y) in ghost:
                        bits |= DOTS[dy][dx]
                        g = True
        if not bits:
            return " ", 0
        return chr(0x2800 + bits), ghost_attr if g else new if isnew else plain

    def draw_bars(self, h, w):
        world = self.world
        state = "running" if self.running else "paused"
        speed = SPEEDS[self.speed_i]
        top = (f" Life · {state} at {speed}{'' if speed == 'max' else '/s'} · generation {world.gen:,} · "
               f"{world.population():,} alive (+{world.births:,} −{world.deaths:,}) · {self.rule_name()} · "
               f"{world.w}×{world.h}{', wrapping' if world.wrap else ''}")
        self.put(0, 0, top.ljust(w), curses.color_pair(6) | curses.A_REVERSE)
        if self.cycle:
            tag = f" {self.cycle} "
            self.put(0, w - len(tag) - 1, tag, curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD)
        if self.message and time.monotonic() - self.message_at < 4:
            self.put(h - 1, 1, self.message, curses.color_pair(3))
            return
        if self.stamp:
            keys = ["←→↑↓ move", "r turn", "f flip", "⏎ place", "Esc done"]
        else:
            keys = ["␣ run/pause", "n step", "u back", "←→↑↓ cursor", "x cell", "p pattern", "R random",
                    "c clear", "+- speed", "b rule", "w wrap", "z zoom", "v colour", "? help", "q quit"]
        while len(keys) > 1 and len("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(h - 1, 1, "  ".join(keys), curses.color_pair(8))

    def help(self):
        lines = ["Game of Life", "",
                 "  space      run / pause              n  one generation    u  one back (or undo an edit)",
                 "  arrows     move the cursor (hjkl too; capitals move 8)   x  toggle the cell under it",
                 "  mouse      click or drag to draw; drag over live cells to erase",
                 "  p          stamp a pattern: arrows move it, r turns, f flips, Enter places, Esc stops",
                 "  R          random world    c  clear    + -  speed    b  next rule    w  wrapping edges",
                 "  z          zoom: braille (8 cells a character), half blocks, full cells",
                 "  v          newborn cells in green, or not", "",
                 "  Conway's rules: a dead cell with exactly 3 live neighbours is born; a live one with",
                 "  2 or 3 stays alive; every other cell dies or stays dead. Other rules are written the",
                 "  same way: B for the counts that give birth, S for the counts that survive.", "",
                 "  life_tui.py --file pattern.rle loads a pattern from the LifeWiki (RLE or plain text).",
                 "", "  any key to close"]
        self.s.timeout(-1)
        self.s.erase()
        for n, line in enumerate(lines):
            self.put(1 + n, 2, line, curses.A_BOLD if n == 0 else 0)
        self.s.refresh()
        self.s.getch()
        self.s.timeout(TICK_MS)

    # -- keys --

    def move(self, dx, dy):
        w = self.world
        self.cx = (self.cx + dx) % w.w
        self.cy = (self.cy + dy) % w.h

    def mouse(self):
        try:
            _, mx, my, _, state = curses.getmouse()
        except curses.error:
            return
        if self.zoom == "braille":
            x, y = mx * 2, (my - 1) * 4
        elif self.zoom == "half":
            x, y = mx, (my - 1) * 2
        else:
            x, y = mx // 2, my - 1
        if not (0 <= x < self.world.w and 0 <= y < self.world.h):
            return
        self.cx, self.cy = x, y
        if state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            if self.stamp:
                self.stamp_here()
                return
            self.drawing = not self.world.get(x, y)
            self.edit(lambda: self.world.set(x, y, self.drawing))
        elif state & curses.BUTTON1_RELEASED:
            self.drawing = None
        elif self.drawing is not None:
            self.world.set(x, y, self.drawing)
            self.world.fresh = [0] * self.world.h

    def handle(self, ch):
        moves = {curses.KEY_LEFT: (-1, 0), curses.KEY_RIGHT: (1, 0), curses.KEY_UP: (0, -1), curses.KEY_DOWN: (0, 1),
                 ord("h"): (-1, 0), ord("l"): (1, 0), ord("k"): (0, -1), ord("j"): (0, 1),
                 ord("H"): (-8, 0), ord("L"): (8, 0), ord("K"): (0, -8), ord("J"): (0, 8)}
        if ch == curses.KEY_MOUSE:
            self.mouse()
            return True
        if ch == curses.KEY_RESIZE:
            self.refit()
            return True
        if self.stamp:
            if ch in moves:
                self.move(*moves[ch])
            elif ch == ord("r"):
                self.stamp = (self.stamp[0], rotate(self.stamp[1]))
            elif ch == ord("f"):
                self.stamp = (self.stamp[0], flip(self.stamp[1]))
            elif ch in (10, 13, curses.KEY_ENTER, ord("x")):
                self.stamp_here()
            elif ch in (27, ord("p")):
                self.stamp = None
            elif ch == ord(" "):
                self.running = not self.running
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in moves:
            self.move(*moves[ch])
        elif ch == ord(" "):
            self.running = not self.running
        elif ch in (ord("n"), ord(".")):
            self.running = False
            self.advance()
        elif ch == ord("u"):
            self.running = False
            self.rewind()
        elif ch in (ord("x"), 10, 13, curses.KEY_ENTER):
            self.toggle()
        elif ch == ord("p"):
            self.choose_pattern()
        elif ch == ord("R"):
            self.edit(self.world.randomise)
            self.world.gen = 0
        elif ch == ord("c"):
            self.edit(self.world.clear)
            self.world.gen = 0
            self.running = False
        elif ch in (ord("+"), ord("=")):
            self.speed_i = min(len(SPEEDS) - 1, self.speed_i + 1)
        elif ch in (ord("-"), ord("_")):
            self.speed_i = max(0, self.speed_i - 1)
        elif ch == ord("b"):
            self.rule_i = (self.rule_i + 1) % len(RULES)
            name, born, survive = RULES[self.rule_i]
            self.world.born, self.world.survive = {int(c) for c in born}, {int(c) for c in survive}
            self.forget()
            self.say(f"rule: {self.rule_name()}")
        elif ch == ord("w"):
            self.world.wrap = not self.world.wrap
            self.forget()
            self.say("the edges wrap round" if self.world.wrap else "the edges are walls now")
        elif ch == ord("z"):
            self.zoom = ZOOMS[(ZOOMS.index(self.zoom) + 1) % len(ZOOMS)]
            self.refit()
            self.say(f"zoom: {self.zoom}")
        elif ch == ord("v"):
            self.colour = not self.colour
        elif ch == ord("?"):
            self.help()
        return True


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)
    print("\033[?1002h", end="", flush=True)          # report drags with the button held
    stdscr.timeout(TICK_MS)
    app = App(stdscr, args)
    try:
        while True:
            started = time.monotonic()
            app.tick()
            app.draw()
            stdscr.timeout(max(1, TICK_MS - int((time.monotonic() - started) * 1000)))
            ch = stdscr.getch()
            if ch != -1 and not app.handle(ch):
                return
    finally:
        print("\033[?1002l", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Conway's Game of Life, and its cousins, in the terminal.")
    parser.add_argument("-f", "--file", help="start from a pattern file: RLE (.rle) or plain text (.cells)")
    parser.add_argument("-p", "--pattern", help="start from a built-in pattern: " + ", ".join(PATTERNS))
    parser.add_argument("-r", "--rule", help="a rule like B36/S23 (HighLife); the default is Conway's B3/S23")
    parser.add_argument("-z", "--zoom", choices=ZOOMS, default="half")
    parser.add_argument("--no-wrap", action="store_true", help="edges are walls instead of wrapping round")
    parser.add_argument("--paused", action="store_true", help="start paused")
    parser.add_argument("--list", action="store_true", help="list the patterns and rules and exit")
    args = parser.parse_args()
    if args.list:
        for name, (what, _) in PATTERNS.items():
            print(f"{name:<24}{what}")
        print()
        for name, born, survive in RULES:
            print(f"{name:<24}B{born}/S{survive}")
        return
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
