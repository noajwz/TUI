#!/usr/bin/env python3
"""Snake — steer a growing snake to the food without running into anything.

Square cells, a snake shaded from head to tail, and four kinds of food: apples to grow on, and
three that don't wait for long — gold for points, a berry that shrinks you, and a pepper that makes
you fast and doubles what you score while it lasts. Four ways to play:

- classic: the walls and your own tail are deadly
- wrap: leave by one edge and come back by the other
- maze: walls inside the field, a new layout every ten apples
- zen: nothing kills you; biting yourself just cuts the tail off there

Turns are buffered, so a quick left-then-up registers both. Press o and the snake plays itself:
the shortest way to the food that still leaves a way back to its tail, or the long way round when
there isn't one. The best scores of each mode are kept (~/.local/state/snake_tui/scores.json).
"""

import argparse
import colorsys
import curses
import json
import locale
import os
import random
import tempfile
import time
from collections import deque

MODES = ("classic", "wrap", "maze", "zen")
MAX_W, MAX_H = 40, 24                   # the biggest field, in cells
MIN_W, MIN_H = 50, 16
START_LEN = 4
BASE_TICK = 0.13                        # seconds a step at the start
FASTEST = 0.055
PEPPER_SPEED = 1.6
TURN_QUEUE = 3
SCORES_PATH = os.path.expanduser("~/.local/state/snake_tui/scores.json")
FOODS = {  # kind: (points, ticks it stays — 0 for always, chance it appears after an apple, colour)
    "apple": (10, 0, 1.0, 196),
    "gold": (50, 45, 0.12, 220),
    "berry": (5, 70, 0.08, 135),
    "pepper": (15, 55, 0.08, 208),
}
EIGHT = {"apple": 1, "gold": 3, "berry": 5, "pepper": 3}
DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def maze(level, w, h):
    """Walls for a maze level: a few layouts that come round again."""
    walls = set()
    kind = (level - 1) % 5
    cx, cy = w // 2, h // 2
    if kind == 0:                       # a box with a gap in each side
        for x in range(3, w - 3):
            if abs(x - cx) > 2:
                walls |= {(x, 3), (x, h - 4)}
        for y in range(3, h - 3):
            if abs(y - cy) > 2:
                walls |= {(3, y), (w - 4, y)}
    elif kind == 1:                     # a cross
        for x in range(4, w - 4):
            if abs(x - cx) > 1:
                walls.add((x, cy))
        for y in range(3, h - 3):
            if abs(y - cy) > 1:
                walls.add((cx, y))
    elif kind == 2:                     # bars from alternating sides
        for i, x in enumerate(range(5, w - 4, 6)):
            ys = range(0, h - 6) if i % 2 == 0 else range(6, h)
            walls |= {(x, y) for y in ys}
    elif kind == 3:                     # four rooms with doors
        for x in range(w):
            if x not in (cx - 8, cx + 8) and not (cx - 1 <= x <= cx + 1):
                walls.add((x, cy))
        for y in range(h):
            if y not in (cy - 5, cy + 5) and not (cy - 1 <= y <= cy + 1):
                walls.add((cx, y))
    else:                               # pillars
        for x in range(4, w - 3, 5):
            for y in range(3, h - 2, 4):
                walls |= {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)}
    return {(x, y) for x, y in walls if 0 <= x < w and 0 <= y < h}


class Game:
    def __init__(self, mode, w, h, seed=None):
        self.mode, self.w, self.h = mode, w, h
        self.rng = random.Random(seed)
        self.level = 1
        self.walls = maze(1, w, h) if mode == "maze" else set()
        self.start()

    def start(self):
        w, h = self.w, self.h
        x0 = max(START_LEN, w // 4)
        rows = sorted(range(h), key=lambda yy: abs(yy - h // 2))
        y = next((yy for yy in rows                 # a row with room for the snake and a little road ahead
                  if all((x, yy) not in self.walls for x in range(x0 - START_LEN, x0 + 6))), h // 2)
        self.body = deque((x0 - i, y) for i in range(START_LEN))      # the head is body[0]
        self.dir = DIRS["right"]
        self.turns = deque()
        self.grow = 0
        self.food = {}                  # (x, y) -> [kind, ticks left]
        self.score = 0
        self.apples = 0
        self.steps = 0
        self.pepper = 0
        self.over = False
        self.cause = ""
        self.bitten = 0
        self.place_food("apple")

    @property
    def head(self):
        return self.body[0]

    def free_cells(self):
        taken = set(self.body) | self.walls | set(self.food)
        return [(x, y) for x in range(self.w) for y in range(self.h) if (x, y) not in taken]

    def place_food(self, kind):
        cells = self.free_cells()
        if cells:
            life = FOODS[kind][1]
            self.food[self.rng.choice(cells)] = [kind, life]

    def interval(self):
        t = max(FASTEST, BASE_TICK - 0.0025 * (len(self.body) - START_LEN))
        return t / PEPPER_SPEED if self.pepper else t

    def steer(self, name):
        """Queues a turn; one straight back into the neck is ignored."""
        d = DIRS[name]
        last = self.turns[-1] if self.turns else self.dir
        if d == last or (d[0] == -last[0] and d[1] == -last[1]):
            return
        if len(self.turns) < TURN_QUEUE:
            self.turns.append(d)

    def next_cell(self, d):
        x, y = self.head[0] + d[0], self.head[1] + d[1]
        if self.mode in ("wrap", "zen"):
            x, y = x % self.w, y % self.h
        return x, y

    def step(self):
        if self.over:
            return
        if self.turns:
            self.dir = self.turns.popleft()
        nx, ny = self.next_cell(self.dir)
        self.steps += 1
        if not (0 <= nx < self.w and 0 <= ny < self.h):
            self.die("hit the wall")
            return
        if (nx, ny) in self.walls:
            self.die("ran into the maze")
            return
        tail_moves = self.grow == 0
        body = list(self.body)
        hits = body[:-1] if tail_moves else body
        if (nx, ny) in hits:
            if self.mode == "zen":              # cut the tail off at the bite and carry on
                i = body.index((nx, ny))
                lost = len(body) - i
                for _ in range(lost):
                    self.body.pop()
                self.bitten += lost
            else:
                self.die("bit its own tail")
                return
        self.body.appendleft((nx, ny))
        if self.grow:
            self.grow -= 1
        else:
            self.body.pop()
        food = self.food.pop((nx, ny), None)
        if food:
            self.eat(food[0])
        for pos in list(self.food):
            kind, left = self.food[pos]
            if left:
                if left == 1:
                    del self.food[pos]
                else:
                    self.food[pos][1] = left - 1
        if self.pepper:
            self.pepper -= 1

    def eat(self, kind):
        points = FOODS[kind][0] * (2 if self.pepper else 1)
        self.score += points
        if kind == "apple":
            self.grow += 1
            self.apples += 1
            self.place_food("apple")
            for extra in ("gold", "berry", "pepper"):
                if extra not in (k for k, _ in self.food.values()) and self.rng.random() < FOODS[extra][2]:
                    self.place_food(extra)
            if self.mode == "maze" and self.apples % 10 == 0:
                self.next_level()
        elif kind == "gold":
            self.grow += 2
        elif kind == "berry":
            for _ in range(min(3, len(self.body) - 3)):
                self.body.pop()
        elif kind == "pepper":
            self.pepper = 60

    def next_level(self):
        self.level += 1
        self.walls = maze(self.level, self.w, self.h)
        keep_score, keep_apples = self.score, self.apples
        self.start()
        self.score, self.apples = keep_score, keep_apples

    def die(self, cause):
        self.over = True
        self.cause = cause

    # -- the snake that plays itself --

    def neighbours(self, cell):
        for d in DIRS.values():
            x, y = cell[0] + d[0], cell[1] + d[1]
            if self.mode in ("wrap", "zen"):
                x, y = x % self.w, y % self.h
            if 0 <= x < self.w and 0 <= y < self.h and (x, y) not in self.walls:
                yield d, (x, y)

    def path(self, start, goals, blocked):
        """Shortest path from start to any goal, as a list of cells, or None."""
        prev, queue = {start: None}, deque([start])
        while queue:
            cur = queue.popleft()
            if cur in goals and cur != start:
                out = []
                while cur != start:
                    out.append(cur)
                    cur = prev[cur]
                return out[::-1]
            for _, nxt in self.neighbours(cur):
                if nxt not in prev and nxt not in blocked:
                    prev[nxt] = cur
                    queue.append(nxt)
        return None

    def autopilot(self):
        """The turn the snake makes on its own: to the food if it can still get back to its
        tail afterwards, otherwise after its tail, the long way."""
        body = list(self.body)
        wanted = {p for p, (k, _) in self.food.items() if k != "berry" or len(body) > 12}
        route = self.path(self.head, wanted, set(body[:-1]))
        if route:
            sim = deque(body)
            for cell in route:
                sim.appendleft(cell)
                sim.pop()
            sim.append(body[-1])            # it grows on the food
            if self.path(sim[0], {sim[-1]}, set(list(sim)[1:-1])) is not None:
                return self.dir_to(route[0])
        options = []
        for d, cell in self.neighbours(self.head):
            if cell in body[:-1]:
                continue
            sim = deque(body)
            sim.appendleft(cell)
            sim.pop()
            tail_ok = self.path(cell, {sim[-1]}, set(list(sim)[1:-1])) is not None
            far = len(self.path(cell, wanted, set(list(sim)[:-1])) or []) if wanted else 0
            options.append((tail_ok, far, d))
        if not options:
            return self.dir
        options.sort(key=lambda o: (o[0], o[1]), reverse=True)
        return options[0][2]

    def dir_to(self, cell):
        for d, nxt in self.neighbours(self.head):
            if nxt == cell:
                return d
        return self.dir


# -- scores --

def load_scores():
    try:
        with open(SCORES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_score(game, autopilot):
    """Files a finished game (not one the autopilot played); returns its place, or None."""
    if autopilot or game.score == 0:
        return None
    scores = load_scores()
    table = scores.setdefault(game.mode, [])
    entry = {"score": game.score, "length": len(game.body), "apples": game.apples, "level": game.level,
             "date": time.strftime("%Y-%m-%d %H:%M")}
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


def best(mode):
    table = load_scores().get(mode) or []
    return table[0]["score"] if table else None


# -- the window --

class App:
    def __init__(self, stdscr, args):
        self.s = stdscr
        self.mode = args.mode
        self.args = args
        self.game = None
        self.auto = args.auto
        self.paused = False
        self.place = None
        self.filed = False
        self.gradient = []
        self.food_attr = {}

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def field_size(self):
        h, w = self.s.getmaxyx()
        fw = max(12, min(MAX_W, (w - 2 - 24) // 2))
        fh = max(8, min(MAX_H, h - 3))
        return fw, fh

    def new_game(self):
        self.game = Game(self.mode, *self.field_size())
        self.paused = False
        self.place = None
        self.filed = False
        self.due = time.monotonic() + 0.6           # a moment to see where you are

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
        fw = g.w * 2 + 2
        if w < fw or h < g.h + 2:            # shrunk below the field mid-game: hold it there
            self.paused = True
            self.put(0, 0, f"window smaller than the field ({fw}×{g.h + 2}):", curses.color_pair(3))
            self.put(1, 0, "make it bigger, or r for a new game", curses.color_pair(3))
            s.refresh()
            return
        panel = w >= fw + 24
        x0 = max(0, (w - fw - (24 if panel else 0)) // 2)
        y0 = max(0, (h - g.h - 2) // 2)
        dim = curses.color_pair(8)
        self.put(y0, x0, "╭" + "─" * (g.w * 2) + "╮", dim)
        side = "┆" if g.mode in ("wrap", "zen") else "│"     # open edges look open
        for r in range(g.h):
            self.put(y0 + 1 + r, x0, side, dim)
            self.put(y0 + 1 + r, x0 + 1 + g.w * 2, side, dim)
        self.put(y0 + 1 + g.h, x0, "╰" + "─" * (g.w * 2) + "╯", dim)
        if g.mode in ("wrap", "zen"):
            self.put(y0, x0 + 1, "┄" * (g.w * 2), dim)
            self.put(y0 + 1 + g.h, x0 + 1, "┄" * (g.w * 2), dim)
        for y in range(g.h):
            self.put(y0 + 1 + y, x0 + 1, " ·" * g.w, curses.color_pair(8))
        for x, y in g.walls:
            self.put(y0 + 1 + y, x0 + 1 + x * 2, "▓▓", dim)
        blink = int(time.monotonic() * 4) % 2
        for (x, y), (kind, left) in g.food.items():
            if left and left < 12 and blink:
                continue                        # about to go
            self.put(y0 + 1 + y, x0 + 1 + x * 2, "◖◗", self.food_attr[kind])
        n = len(g.body)
        for i, (x, y) in enumerate(reversed(g.body)):
            k = n - 1 - i
            if g.over:
                attr = curses.color_pair(1) | (curses.A_BOLD if blink else 0)
            elif k == 0:
                attr = curses.color_pair(20) | curses.A_BOLD
            else:
                attr = self.gradient[min(len(self.gradient) - 1, k * len(self.gradient) // max(8, n))]
            self.put(y0 + 1 + y, x0 + 1 + x * 2, "██", attr)
        if self.paused or g.over:
            lines = (["paused", "", "space to go on"] if self.paused else
                     [g.cause, f"{g.score:,} points", "a new best!" if self.place == 1 else
                      f"#{self.place} of your best" if self.place else "", "", "r again · m modes"])
            top = y0 + 1 + (g.h - len(lines)) // 2
            for i, line in enumerate(lines):
                self.put(top + i, x0 + 1 + max(0, (g.w * 2 - len(line) - 2) // 2), f" {line} ",
                         curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE)
        if panel:
            self.draw_panel(y0, x0 + fw + 2, g)
        s.refresh()

    def draw_panel(self, y, x, g):
        dim = curses.color_pair(8)
        b = best(g.mode)
        rows = [("snake", curses.color_pair(2) | curses.A_BOLD), (g.mode + (f" · level {g.level}" if g.mode == "maze" else ""),
                                                                   curses.color_pair(6)), ("", 0),
                (f"score   {g.score:,}", curses.A_BOLD), (f"length  {len(g.body)}", 0),
                (f"apples  {g.apples}", 0), (f"best    {b:,}" if b else "best    —", dim)]
        if g.mode == "zen" and g.bitten:
            rows.append((f"bitten  {g.bitten}", dim))
        if g.pepper:
            rows.append((f"PEPPER ×2  {g.pepper}", curses.color_pair(3) | curses.A_BOLD))
        if self.auto:
            rows.append(("autopilot on", curses.color_pair(6) | curses.A_BOLD))
        rows.append(("", 0))
        for kind, label in (("apple", "+1 · 10"), ("gold", "+2 · 50"), ("berry", "−3 · 5"), ("pepper", "fast, ×2")):
            rows.append((f"◖◗ {kind:<6} {label}", self.food_attr[kind]))
        rows += [("", 0), ("arrows/wasd steer", dim), ("space pause", dim), ("o autopilot", dim),
                 ("r restart  m modes", dim), ("q quit", dim)]
        for i, (text, attr) in enumerate(rows):
            self.put(y + i, x, text, attr)

    def menu(self):
        sel = MODES.index(self.mode)
        about = {"classic": "the walls and your tail are deadly", "wrap": "the edges lead round to the other side",
                 "maze": "walls inside, a new layout every ten apples", "zen": "no dying: a bite just cuts the tail"}
        self.s.timeout(-1)
        try:
            while True:
                self.s.erase()
                h, w = self.s.getmaxyx()
                y = max(1, h // 2 - 7)
                title = "S N A K E"
                self.put(y, (w - len(title)) // 2, title, curses.color_pair(2) | curses.A_BOLD)
                for i in range(12):
                    self.put(y + 2, (w - 24) // 2 + i * 2, "██", self.gradient[i * len(self.gradient) // 12])
                self.put(y + 2, (w - 24) // 2 + 26, "◖◗", self.food_attr["apple"])
                for i, m in enumerate(MODES):
                    b = best(m)
                    line = f"  {m:<8} {about[m]:<44} {f'best {b:,}' if b else ''}  "
                    self.put(y + 5 + i * 2, max(0, (w - 66) // 2), line, curses.A_REVERSE | curses.A_BOLD if i == sel else 0)
                self.put(y + 14, (w - 34) // 2, "↑↓ choose · Enter play · q quit", curses.color_pair(8))
                self.s.refresh()
                ch = self.s.getch()
                if ch in (curses.KEY_UP, ord("k"), ord("w")):
                    sel = (sel - 1) % len(MODES)
                elif ch in (curses.KEY_DOWN, ord("j"), ord("s")):
                    sel = (sel + 1) % len(MODES)
                elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                    self.mode = MODES[sel]
                    return True
                elif ch in (ord("q"), 27):
                    return False
        finally:
            self.s.timeout(10)

    # -- keys --

    def handle(self, ch):
        g = self.game
        steer = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
                 ord("w"): "up", ord("s"): "down", ord("a"): "left", ord("d"): "right",
                 ord("k"): "up", ord("j"): "down", ord("h"): "left", ord("l"): "right"}
        if ch in (ord("q"), ord("Q")):
            return "quit"
        if ch in (ord("m"), ord("M")):
            return "menu"
        if ch in (ord("r"), ord("R")) or (g.over and ch in (10, 13)):
            self.new_game()
            return None
        if ch == ord("o"):
            self.auto = not self.auto
            return None
        if ch in (ord(" "), ord("p"), 27) and not g.over:
            self.paused = not self.paused
            self.due = time.monotonic() + g.interval()
            return None
        if ch in steer and not self.paused and not self.auto:
            g.steer(steer[ch])
        return None

    def play(self):
        self.new_game()
        self.s.timeout(10)
        while True:
            g = self.game
            now = time.monotonic()
            if not self.paused and not g.over and now >= self.due:
                if self.auto:
                    g.turns.clear()
                    d = g.autopilot()
                    name = next(k for k, v in DIRS.items() if v == d)
                    g.steer(name)
                g.step()
                self.due = now + g.interval()
            if g.over and not self.filed:
                self.place = save_score(g, self.auto)
                self.filed = True
            self.draw()
            ch = self.s.getch()
            while ch != -1:
                result = self.handle(ch)
                if result:
                    return result
                ch = self.s.getch()


def run(stdscr, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    rich = curses.COLORS >= 256
    app = App(stdscr, args)
    curses.init_pair(20, 154 if rich else curses.COLOR_GREEN, -1)       # the head
    for k in range(12):                 # the body, from bright green at the neck to teal at the tail
        pair = 21 + k
        if rich:
            r, g_, b = colorsys.hsv_to_rgb(0.30 + k * 0.018, 0.85, 1.0 - k * 0.035)
            curses.init_pair(pair, 16 + 36 * round(r * 5) + 6 * round(g_ * 5) + round(b * 5), -1)
        else:
            curses.init_pair(pair, curses.COLOR_GREEN if k < 8 else curses.COLOR_CYAN, -1)
        app.gradient.append(curses.color_pair(pair) | (0 if rich else curses.A_BOLD))
    for k, (kind, spec) in enumerate(FOODS.items()):
        curses.init_pair(40 + k, spec[3] if rich else EIGHT[kind], -1)
        app.food_attr[kind] = curses.color_pair(40 + k) | curses.A_BOLD
    stdscr.keypad(True)
    if not args.mode_given and not app.menu():
        return
    while True:
        result = app.play()
        if result == "quit" or (result == "menu" and not app.menu()):
            return


def main():
    parser = argparse.ArgumentParser(description="Snake: steer to the food, don't run into anything.")
    parser.add_argument("mode", nargs="?", choices=MODES, help="go straight into a mode, without the menu")
    parser.add_argument("--auto", action="store_true", help="start with the snake steering itself")
    parser.add_argument("--scores", action="store_true", help="print the best scores and exit")
    args = parser.parse_args()
    if args.scores:
        scores = load_scores()
        for mode in MODES:
            print(mode)
            for i, e in enumerate(scores.get(mode, []), 1):
                print(f"  {i:>2}. {e['score']:,} points, length {e['length']}  ({e['date']})")
        return
    args.mode_given = args.mode is not None
    args.mode = args.mode or "classic"
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
