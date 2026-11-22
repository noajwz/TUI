#!/usr/bin/env python3
"""Spirograph — the toy, in the terminal: a toothed wheel rolls round a ring, and a pen in one of
its holes draws the curve.

The drawing is in braille dots, eight to a character, so the lines are fine and the circles round.
You see the wheel roll and the pen trace as it goes; designs stack up in layers in different
colours, or in a rainbow along the curve. Like the real toy the sizes are teeth, so the panel can
tell you how many times round the wheel goes before the curve closes, and how many loops it makes.
Inside the ring the curve is a hypotrochoid, outside it an epitrochoid.
"""

import argparse
import colorsys
import curses
import locale
import math
import random
import sys
import time

TICK_MS = 33
STEPS_PER_TURN = 240           # points along one trip of the wheel round the ring
DT = 2 * math.pi / STEPS_PER_TURN
AUTO_SECONDS = 8               # auto speed draws a design in about this long
DEMO_LAYERS = 3                # demo mode clears after this many
MIN_W, MIN_H = 60, 16
PANEL_W = 30
RING = (60, 140)               # teeth on the fixed ring: the range the keys move in
WHEEL = (12, 100)
SPEEDS = ("auto", 2, 5, 10, 20, 40, 80, 160, 320, 640)
COLOURINGS = ("rainbow", "layers", "mono")
DOTS = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20,
        (0, 3): 0x40, (1, 3): 0x80}
PRESETS = (  # ring teeth, wheel teeth, pen hole (share of the wheel's radius), outside
    (96, 36, 0.80, False), (105, 30, 0.90, False), (96, 60, 0.60, False), (120, 45, 1.00, False),
    (100, 63, 0.75, False), (84, 52, 0.85, False), (60, 24, 0.90, True), (75, 27, 0.60, True),
    (90, 41, 1.10, False),
)
HUES = 36

rng = random.Random()


class Design:
    """One curve: a wheel of r teeth rolling inside (or outside) a ring of R, pen at d·r."""

    def __init__(self, ring, wheel, hole, outside, colour):
        self.R, self.r, self.hole, self.outside = ring, wheel, hole, outside
        self.colour = colour                  # the hue for the layers colouring
        self.t = 0.0
        self.last = None

    @property
    def turns(self):
        """Times the wheel goes round the ring before the pen is back where it started."""
        return self.r // math.gcd(self.R, self.r)

    @property
    def loops(self):
        return self.R // math.gcd(self.R, self.r)

    @property
    def period(self):
        return 2 * math.pi * self.turns

    @property
    def steps(self):
        return self.turns * STEPS_PER_TURN

    @property
    def done(self):
        return self.t >= self.period

    def extent(self):
        """How far from the centre the pen can get, in teeth."""
        d = self.hole * self.r
        return (self.R + self.r + d) if self.outside else max(self.R, abs(self.R - self.r) + d)

    def pen(self, t):
        R, r, d = self.R, self.r, self.hole * self.r
        if self.outside:
            k = (R + r) / r
            return (R + r) * math.cos(t) - d * math.cos(k * t), (R + r) * math.sin(t) - d * math.sin(k * t)
        k = (R - r) / r
        return (R - r) * math.cos(t) + d * math.cos(k * t), (R - r) * math.sin(t) - d * math.sin(k * t)

    def wheel_at(self, t):
        """The wheel's centre and the angle it has turned to, for drawing it."""
        R, r = self.R, self.r
        if self.outside:
            c = R + r
            return c * math.cos(t), c * math.sin(t), (R + r) / r * t
        c = R - r
        return c * math.cos(t), c * math.sin(t), -(R - r) / r * t

    def describe(self):
        side = "outside" if self.outside else "inside"
        return f"ring {self.R} · wheel {self.r} {side} · hole {self.hole:.2f}"


class Paper:
    """The braille drawing: which dots are inked, and the colour of each character cell."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.w, self.h = cols * 2, rows * 4
        self.bits = {}
        self.colour = {}

    def dot(self, x, y, colour, bits=None, colours=None):
        """Inks a dot — on the paper, or on an overlay (`bits`, `colours`) drawn over it."""
        bits = self.bits if bits is None else bits
        colours = self.colour if colours is None else colours
        x, y = int(round(x)), int(round(y))
        if 0 <= x < self.w and 0 <= y < self.h:
            key = (x // 2, y // 4)
            bits[key] = bits.get(key, 0) | DOTS[(x % 2, y % 4)]
            colours[key] = colour

    def line(self, a, b, colour, bits=None, colours=None):
        (x0, y0), (x1, y1) = a, b
        n = int(max(abs(x1 - x0), abs(y1 - y0)) * 1.3) + 1
        for k in range(n + 1):
            t = k / n
            self.dot(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, colour, bits, colours)

    def circle(self, cx, cy, radius, colour, bits, colours):
        n = max(12, int(2 * math.pi * radius * 1.2))
        for k in range(n):
            a = 2 * math.pi * k / n
            self.dot(cx + math.cos(a) * radius, cy + math.sin(a) * radius, colour, bits, colours)


class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.args = args
        self.colouring = args.colours
        self.speed_i = 0
        self.show_wheel = True
        self.paused = False
        self.demo = args.demo
        self.done_at = None
        self.layers = []                      # finished designs, kept for the panel
        self.message, self.message_at = "", 0.0
        self.hue = rng.randrange(HUES)
        self.paper = None
        self.scale = 1.0
        self.ramp = []
        first = (args.ring, args.wheel, args.hole, args.outside)
        self.design = Design(*first, self.next_hue())
        self.fit()

    def next_hue(self):
        self.hue = (self.hue + 11) % HUES
        return self.hue

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

    # -- the paper and the scale --

    def area(self):
        h, w = self.s.getmaxyx()
        rows = h - 2
        cols = min(rows * 2, w - 2)
        if w - cols - 3 < PANEL_W and w - 2 - PANEL_W >= 40:
            cols = w - 3 - PANEL_W
            rows = min(rows, cols // 2)
        return cols, rows

    def fit(self, keep=False):
        """A fresh sheet sized to the window and to the biggest of the designs on it."""
        cols, rows = self.area()
        old = self.paper
        self.paper = Paper(cols, rows)
        reach = max([self.design.extent()] + [d.extent() for d in self.layers])
        self.scale = (min(self.paper.w, self.paper.h) / 2 - 2) / reach
        if keep and old:                      # the designs are drawn again at the new scale
            for d in self.layers:
                self.redraw(d)
            t = self.design.t
            self.design.t, self.design.last = 0.0, None
            self.advance(self.design, int(t / DT) + 1, limit=t)

    def to_paper(self, x, y):
        p = self.paper
        return p.w / 2 - 0.5 + x * self.scale, p.h / 2 - 0.5 - y * self.scale

    def ink(self, d, t):
        if self.colouring == "rainbow":
            return int(t / d.period * HUES * 2) % HUES
        if self.colouring == "layers":
            return d.colour
        return None

    def advance(self, d, steps, limit=None):
        end = d.period if limit is None else limit
        for _ in range(steps):
            if d.t >= end:
                break
            d.t = min(end, d.t + DT)
            pt = self.to_paper(*d.pen(d.t))
            if d.last is None:
                d.last = self.to_paper(*d.pen(0.0))
            self.paper.line(d.last, pt, self.ink(d, d.t))
            d.last = pt

    def redraw(self, d):
        t, d.t, d.last = d.t, 0.0, None
        self.advance(d, d.steps + 1, limit=t)

    # -- changing the design --

    def restart(self, **change):
        """The current design with new numbers, drawn again from the start; the layers stay."""
        d = self.design
        ring, wheel, hole, outside = (change.get("R", d.R), change.get("r", d.r), change.get("hole", d.hole),
                                      change.get("outside", d.outside))
        wheel = max(WHEEL[0], min(WHEEL[1], wheel))
        ring = max(RING[0], min(RING[1], ring))
        if not outside and wheel >= ring:
            wheel = ring - 1
        self.design = Design(ring, wheel, max(0.05, min(1.5, hole)), outside, d.colour)
        self.fit(keep=False)
        for layer in self.layers:
            self.redraw(layer)
        self.done_at = None

    def random_numbers(self):
        outside = rng.random() < 0.25
        for _ in range(200):                  # numbers sharing a factor: 2 to 15 trips round, not 87
            ring = rng.randrange(72, 132)
            wheel = rng.randrange(18, 64 if outside else ring - 8)
            if 2 <= wheel // math.gcd(ring, wheel) <= 15:
                break
        return ring, wheel, round(rng.uniform(0.35, 1.15), 2), outside

    def add_layer(self, numbers=None):
        if self.design.t > 0:
            self.layers.append(self.design)
        ring, wheel, hole, outside = numbers or self.random_numbers()
        self.design = Design(ring, wheel, hole, outside, self.next_hue())
        self.fit(keep=False)
        for layer in self.layers:
            self.redraw(layer)
        self.done_at = None

    def clear(self):
        self.layers = []
        d = self.design
        self.design = Design(d.R, d.r, d.hole, d.outside, d.colour)
        self.fit()
        self.done_at = None

    # -- the clock --

    def per_frame(self):
        sp = SPEEDS[self.speed_i]
        if sp == "auto":
            return max(1, int(self.design.steps / (AUTO_SECONDS * 1000 / TICK_MS)) + 1)
        return sp

    def tick(self):
        d = self.design
        if not self.paused and not d.done:
            self.advance(d, self.per_frame())
        if d.done:
            if self.done_at is None:
                self.done_at = time.monotonic()
            elif self.demo and time.monotonic() - self.done_at > 1.2:
                if len(self.layers) + 1 >= DEMO_LAYERS:
                    self.layers = []
                    self.design.t = 0
                self.add_layer()

    # -- drawing --

    def attr(self, hue):
        if hue is None:
            return curses.color_pair(7) | curses.A_BOLD
        return curses.color_pair(16 + hue) | curses.A_BOLD

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        p = self.paper
        x0 = 1
        y0 = 1 + (h - 2 - p.rows) // 2
        if w - p.cols - 3 < PANEL_W:          # no panel: the drawing in the middle
            x0 = (w - p.cols) // 2
        over_bits, over_col = {}, {}
        d = self.design
        if self.show_wheel and not d.done:
            cx, cy = self.to_paper(0, 0)
            p.circle(cx, cy, d.R * self.scale, 8, over_bits, over_col)
            wx, wy, spin = d.wheel_at(d.t)
            wcx, wcy = self.to_paper(wx, wy)
            p.circle(wcx, wcy, d.r * self.scale, 8, over_bits, over_col)
            pen = self.to_paper(*d.pen(d.t))
            p.line((wcx, wcy), pen, 7, over_bits, over_col)
            for ax in (-1, 0, 1):
                for ay in (-1, 0, 1):
                    p.dot(pen[0] + ax, pen[1] + ay, 3, over_bits, over_col)
        cells = set(p.bits) | set(over_bits)
        for key in cells:
            bits = p.bits.get(key, 0) | over_bits.get(key, 0)
            if key in over_bits and key not in p.bits:
                attr = curses.color_pair(over_col[key]) | (curses.A_BOLD if over_col[key] == 3 else 0)
            elif key in over_col and over_col[key] == 3:
                attr = curses.color_pair(3) | curses.A_BOLD
            else:
                attr = self.attr(p.colour.get(key))
            self.put(y0 + key[1], x0 + key[0], chr(0x2800 + bits), attr)
        if w - p.cols - 3 >= PANEL_W:
            self.draw_panel(1, p.cols + 3, w - p.cols - 4, h - 2)
        if self.message and time.monotonic() - self.message_at < 3:
            self.put(h - 1, 1, self.message, curses.color_pair(3))
        s.refresh()

    def draw_panel(self, y, x, width, height):
        d = self.design
        c = curses.color_pair
        lines = [("Spirograph", c(6) | curses.A_BOLD), ("", 0),
                 (f"ring     {d.R} teeth", 0), (f"wheel    {d.r} teeth, {'outside' if d.outside else 'inside'}", 0),
                 (f"pen hole {d.hole:.2f} of the way out" if d.hole <= 1 else f"pen      {d.hole:.2f}: past the rim",
                  0), ("", 0),
                 (f"goes round {d.turns} time{'s' if d.turns != 1 else ''}", c(3)),
                 (f"makes {d.loops} loop{'s' if d.loops != 1 else ''}", c(3)),
                 (f"{'done' if d.done else f'{min(100, d.t / d.period * 100):.0f}% drawn'}"
                  + ("  ·  paused" if self.paused else ""), c(2) if d.done else c(8)),
                 ("", 0),
                 (f"layers   {len(self.layers) + 1}", 0),
                 (f"colours  {self.colouring}", 0),
                 (f"speed    {SPEEDS[self.speed_i]}", 0),
                 (f"demo     {'on' if self.demo else 'off'}", c(8)), ("", 0),
                 ("g G  ring −/+     r R  wheel −/+", c(8)),
                 ("p P  pen hole     o    inside/out", c(8)),
                 ("⏎    random       a    add a layer", c(8)),
                 ("1-9  presets      x    clear", c(8)),
                 ("␣    pause        f    finish it", c(8)),
                 ("+ -  speed        c    colours", c(8)),
                 ("w    the wheel    d    demo", c(8)),
                 ("q    quit", c(8))]
        for n, (text, attr) in enumerate(lines[:height]):
            self.put(y + n, x, text[:width], attr)

    # -- keys --

    def handle(self, ch):
        d = self.design
        if ch in (ord("q"), ord("Q"), 27):
            return False
        if ch == curses.KEY_RESIZE:
            self.fit(keep=True)
        elif ch == ord(" "):
            self.paused = not self.paused
        elif ch == ord("g"):
            self.restart(R=d.R - 1)
        elif ch == ord("G"):
            self.restart(R=d.R + 1)
        elif ch == ord("r"):
            self.restart(r=d.r - 1)
        elif ch == ord("R"):
            self.restart(r=d.r + 1)
        elif ch == ord("p"):
            self.restart(hole=round(d.hole - 0.05, 2))
        elif ch == ord("P"):
            self.restart(hole=round(d.hole + 0.05, 2))
        elif ch == ord("o"):
            self.restart(outside=not d.outside)
        elif ch in (10, 13, curses.KEY_ENTER):
            ring, wheel, hole, outside = self.random_numbers()
            self.restart(R=ring, r=wheel, hole=hole, outside=outside)
            self.say(self.design.describe())
        elif ch == ord("a"):
            self.add_layer()
            self.say("a new layer: " + self.design.describe())
        elif ord("1") <= ch <= ord("9"):
            ring, wheel, hole, outside = PRESETS[ch - ord("1")]
            self.restart(R=ring, r=wheel, hole=hole, outside=outside)
            self.say(f"preset {chr(ch)}: {self.design.describe()}")
        elif ch == ord("x"):
            self.clear()
        elif ch == ord("f"):
            self.advance(d, d.steps + 1)
        elif ch in (ord("+"), ord("=")):
            self.speed_i = min(len(SPEEDS) - 1, self.speed_i + 1)
        elif ch in (ord("-"), ord("_")):
            self.speed_i = max(0, self.speed_i - 1)
        elif ch == ord("c"):
            self.colouring = COLOURINGS[(COLOURINGS.index(self.colouring) + 1) % len(COLOURINGS)]
            self.fit(keep=True)
        elif ch == ord("w"):
            self.show_wheel = not self.show_wheel
        elif ch == ord("d"):
            self.demo = not self.demo
            self.say("demo: a new design every few seconds" if self.demo else "demo off")
        return True


def hue_colours():
    if curses.COLORS < 256:
        basic = (curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_CYAN,
                 curses.COLOR_BLUE, curses.COLOR_MAGENTA)
        return [basic[k * len(basic) // HUES] for k in range(HUES)]
    out = []
    for k in range(HUES):
        r, g, b = colorsys.hsv_to_rgb(k / HUES, 0.8, 1.0)
        out.append(16 + 36 * round(r * 5) + 6 * round(g * 5) + round(b * 5))
    return out


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    for k, colour in enumerate(hue_colours()):
        curses.init_pair(16 + k, colour, -1)
    stdscr.keypad(True)
    app = App(stdscr, args)
    while True:
        started = time.monotonic()
        app.tick()
        app.draw()
        stdscr.timeout(max(1, TICK_MS - int((time.monotonic() - started) * 1000)))
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            return


def print_once(args):
    """A finished design as braille text, coloured when it goes to a terminal."""
    rows = args.rows
    paper = Paper(rows * 2, rows)
    d = Design(args.ring, args.wheel, args.hole, args.outside, 0)
    scale = (min(paper.w, paper.h) / 2 - 2) / d.extent()
    last = None
    for k in range(d.steps + 1):
        t = d.period * k / d.steps
        x, y = d.pen(t)
        pt = (paper.w / 2 - 0.5 + x * scale, paper.h / 2 - 0.5 - y * scale)
        if last:
            paper.line(last, pt, int(t / d.period * HUES * 2) % HUES)
        last = pt
    tty = sys.stdout.isatty()
    for row in range(rows):
        out = []
        for col in range(paper.cols):
            bits = paper.bits.get((col, row), 0)
            ch = chr(0x2800 + bits) if bits else " "
            if tty and bits:
                r, g, b = colorsys.hsv_to_rgb(paper.colour[(col, row)] / HUES, 0.8, 1.0)
                ch = f"\033[38;2;{int(r * 255)};{int(g * 255)};{int(b * 255)}m{ch}"
            out.append(ch)
        print("".join(out).rstrip() + ("\033[0m" if tty else ""))
    print(f"{d.describe()} — goes round {d.turns} times, {d.loops} loops", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="A spirograph: a wheel rolling round a ring, drawing with a pen.")
    parser.add_argument("--ring", type=int, default=96, help=f"teeth on the ring ({RING[0]}–{RING[1]}, default 96)")
    parser.add_argument("--wheel", type=int, default=36, help=f"teeth on the wheel ({WHEEL[0]}–{WHEEL[1]}, default 36)")
    parser.add_argument("--hole", type=float, default=0.8, help="where the pen is: 0 the centre, 1 the rim (default 0.8)")
    parser.add_argument("--outside", action="store_true", help="roll the wheel round the outside of the ring")
    parser.add_argument("-c", "--colours", choices=COLOURINGS, default="rainbow")
    parser.add_argument("-d", "--demo", action="store_true", help="a new design every few seconds")
    parser.add_argument("--once", action="store_true", help="print a finished design and exit")
    parser.add_argument("--rows", type=int, default=20, help="with --once, how many lines tall (default 20)")
    args = parser.parse_args()
    if not args.outside and args.wheel >= args.ring:
        parser.error("inside the ring, the wheel must have fewer teeth than the ring")
    if args.ring < 1 or args.wheel < 1 or args.hole < 0:
        parser.error("teeth must be positive, and the hole can't be negative")
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
