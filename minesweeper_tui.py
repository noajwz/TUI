#!/usr/bin/env python3
"""Terminal UI Minesweeper: clear the field without stepping on a mine."""

import argparse
import curses
import random
import time

# key: (name, rows, cols, mines)
LEVELS = {
    "1": ("Beginner", 9, 9, 10),
    "2": ("Intermediate", 16, 16, 40),
    "3": ("Expert", 16, 30, 99),
}
LEVEL_NAMES = {name.lower(): key for key, (name, *_) in LEVELS.items()}

TICK_MS = 250  # how often the clock repaints while waiting for a key

# Columns per cell, roomiest first. draw() takes the first that fits, so Expert
# still plays in an 80-column window, just more tightly packed.
CELL_WIDTHS = (3, 2)

HIDDEN, FLAG, MINE, WRONG = "■", "⚑", "✱", "✗"

# Colour pair numbers. 1-8 are the classic per-count colours.
P_HIDDEN, P_FLAG, P_BOOM, P_TITLE, P_WON, P_LOST = 9, 10, 11, 12, 13, 14
COUNT_COLORS = (
    curses.COLOR_BLUE,
    curses.COLOR_GREEN,
    curses.COLOR_RED,
    curses.COLOR_MAGENTA,
    curses.COLOR_YELLOW,
    curses.COLOR_CYAN,
    curses.COLOR_WHITE,
    curses.COLOR_WHITE,
)

MOVES = {
    curses.KEY_UP: (-1, 0), ord("k"): (-1, 0), ord("w"): (-1, 0),
    curses.KEY_DOWN: (1, 0), ord("j"): (1, 0), ord("s"): (1, 0),
    curses.KEY_LEFT: (0, -1), ord("h"): (0, -1), ord("a"): (0, -1),
    curses.KEY_RIGHT: (0, 1), ord("l"): (0, 1), ord("d"): (0, 1),
}  # fmt: skip
DIG_KEYS = {ord(" "), ord("\n"), curses.KEY_ENTER}

HELP = "←↑↓→/hjkl move  space dig  f flag  n new  1-3 level  q quit"


def neighbours(p, rows, cols):
    r, c = p
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if (dr or dc) and 0 <= r + dr < rows and 0 <= c + dc < cols:
                yield r + dr, c + dc


class Board:
    """All game rules, no curses. Cells are (row, col) tuples."""

    def __init__(self, rows, cols, mines, rng=None):
        self.rows, self.cols, self.n_mines = rows, cols, mines
        self.rng = rng or random.Random()
        self.mines = set()
        self.counts = {}
        self.revealed = set()
        self.flags = set()
        self.state = "ready"  # ready -> playing -> won | lost
        self.boom = None
        self.started = self.finished = None

    @property
    def over(self):
        return self.state in ("won", "lost")

    @property
    def mines_left(self):
        return self.n_mines - len(self.flags)

    def elapsed(self):
        if self.started is None:
            return 0
        return int((self.finished or time.monotonic()) - self.started)

    def place_mines(self, safe):
        """Lay the mines after the first dig, keeping it and (room allowing) its ring clear."""
        cells = [(r, c) for r in range(self.rows) for c in range(self.cols)]
        keep_out = {safe, *neighbours(safe, self.rows, self.cols)}
        pool = [p for p in cells if p not in keep_out]
        if len(pool) < self.n_mines:
            pool = [p for p in cells if p != safe]
        self.mines = set(self.rng.sample(pool, self.n_mines))
        self.counts = {
            p: sum(n in self.mines for n in neighbours(p, self.rows, self.cols)) for p in cells
        }

    def dig(self, p):
        """Open a hidden cell, or chord a revealed number. Flags protect a cell from both."""
        if self.over or p in self.flags:
            return
        if self.state == "ready":
            self.place_mines(p)
            self.state = "playing"
            self.started = time.monotonic()
        if p in self.revealed:
            self._chord(p)
        else:
            self._open([p])

    def toggle_flag(self, p):
        if not self.over and p not in self.revealed:
            self.flags ^= {p}

    def _chord(self, p):
        around = list(neighbours(p, self.rows, self.cols))
        if self.counts[p] and sum(n in self.flags for n in around) == self.counts[p]:
            self._open(around)

    def _open(self, start):
        stack = list(start)
        while stack:
            p = stack.pop()
            if p in self.revealed or p in self.flags:
                continue
            if p in self.mines:
                self.boom = p
                self._finish("lost")
                return
            self.revealed.add(p)
            if self.counts[p] == 0:
                stack.extend(neighbours(p, self.rows, self.cols))
        if len(self.revealed) == self.rows * self.cols - self.n_mines:
            self.flags = set(self.mines)
            self._finish("won")

    def _finish(self, state):
        self.state = state
        self.finished = time.monotonic()


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    for i, color in enumerate(COUNT_COLORS, 1):
        curses.init_pair(i, color, -1)
    grey = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
    curses.init_pair(P_HIDDEN, grey, -1)
    curses.init_pair(P_FLAG, curses.COLOR_RED, -1)
    curses.init_pair(P_BOOM, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(P_TITLE, curses.COLOR_CYAN, -1)
    curses.init_pair(P_WON, curses.COLOR_GREEN, -1)
    curses.init_pair(P_LOST, curses.COLOR_RED, -1)


def pair(n):
    return curses.color_pair(n) if curses.has_colors() else 0


def cell_look(board, p):
    """(glyph, attr) for one cell in the board's current state."""
    if p in board.revealed:
        n = board.counts[p]
        attr = pair(n) | curses.A_BOLD
        if n == 8:
            attr = pair(n) | curses.A_DIM
        return (str(n), attr) if n else (" ", 0)
    if p in board.flags:
        if board.state == "lost" and p not in board.mines:
            return WRONG, pair(P_FLAG) | curses.A_BOLD
        return FLAG, pair(P_FLAG) | curses.A_BOLD
    if board.state == "lost" and p in board.mines:
        if p == board.boom:
            return MINE, pair(P_BOOM) | curses.A_BOLD
        return MINE, curses.A_BOLD
    return HIDDEN, pair(P_HIDDEN)


def put(win, y, x, text, attr=0):
    """addstr that clips at the window edge instead of raising."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        text = text[: max(0, w - x - (y == h - 1))]
        if text:
            win.addstr(y, max(0, x), text, attr)


def box_width(cols, cell_w):
    # A 3-wide cell is " X " and self-spacing; a 2-wide one is " X" and needs a pad
    # before the right border.
    return cols * cell_w + 2 + (cell_w == 2)


def draw(stdscr, board, cursor, level_name):
    """Paint a frame. Returns (top, left, cell_w) of the grid for mouse hits, or None."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    rows_needed = board.rows + 4  # header, box top, grid, box bottom, footer
    cell_w = next((cw for cw in CELL_WIDTHS if box_width(board.cols, cw) <= w), None)
    if cell_w is None or h < rows_needed:
        need = f"{box_width(board.cols, CELL_WIDTHS[-1])}×{rows_needed}"
        msg = f"Window too small: need {need}, have {w}×{h}"
        put(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg)
        stdscr.refresh()
        return None

    box_w = box_width(board.cols, cell_w)
    top = max(0, (h - rows_needed) // 2)
    left = (w - box_w) // 2

    title = f"{level_name} {board.cols}×{board.rows}"
    stats = f"{FLAG} {board.mines_left:>3}   ⏱ {board.elapsed():>3}"
    put(stdscr, top, left, title, pair(P_TITLE) | curses.A_BOLD)
    put(stdscr, top, left + box_w - len(stats), stats, curses.A_BOLD)

    gy = top + 2
    put(stdscr, gy - 1, left, "┌" + "─" * (box_w - 2) + "┐")
    for r in range(board.rows):
        put(stdscr, gy + r, left, "│")
        for c in range(board.cols):
            glyph, attr = cell_look(board, (r, c))
            text = glyph.center(cell_w) if cell_w == 3 else " " + glyph
            if (r, c) == cursor and not board.over:
                attr |= curses.A_REVERSE
            put(stdscr, gy + r, left + 1 + c * cell_w, text, attr)
        put(stdscr, gy + r, left + box_w - 1, "│")
    put(stdscr, gy + board.rows, left, "└" + "─" * (box_w - 2) + "┘")

    fy = gy + board.rows + 1
    if board.state == "won":
        msg, attr = f"Cleared in {board.elapsed()}s!  n for a new game", pair(P_WON)
    elif board.state == "lost":
        msg, attr = "Boom.  n for a new game", pair(P_LOST)
    else:
        msg, attr = HELP, curses.A_DIM
    msg = msg[:w]
    put(stdscr, fy, max(0, (w - len(msg)) // 2), msg, attr | curses.A_BOLD * board.over)
    stdscr.refresh()
    return gy, left + 1, cell_w


def run(stdscr, args):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    init_colors()
    # Report presses straight away rather than waiting to assemble clicks.
    curses.mouseinterval(0)
    curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON2_PRESSED | curses.BUTTON3_PRESSED)

    rng = random.Random(args.seed)
    name, rows, cols, mines = args.size

    def new_board():
        return Board(rows, cols, mines, rng)

    board = new_board()
    cursor = (rows // 2, cols // 2)
    while True:
        geo = draw(stdscr, board, cursor, name)
        key = stdscr.getch()
        if key == ord("q"):
            return
        if key in MOVES:
            dr, dc = MOVES[key]
            cursor = (min(max(cursor[0] + dr, 0), rows - 1), min(max(cursor[1] + dc, 0), cols - 1))
        elif key in DIG_KEYS:
            board.dig(cursor)
        elif key == ord("f"):
            board.toggle_flag(cursor)
        elif key == ord("n"):
            board = new_board()
        elif 0 <= key < 256 and chr(key) in LEVELS:
            name, rows, cols, mines = LEVELS[chr(key)]
            board = new_board()
            cursor = (rows // 2, cols // 2)
        elif key == curses.KEY_MOUSE and geo:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            top, left, cell_w = geo
            r, c = my - top, (mx - left) // cell_w
            if mx >= left and 0 <= r < rows and 0 <= c < cols:
                cursor = (r, c)
                if bstate & curses.BUTTON1_PRESSED:
                    board.dig(cursor)
                else:
                    board.toggle_flag(cursor)


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--level",
        default="beginner",
        type=str.lower,
        choices=[*LEVEL_NAMES, *LEVELS],
        help="beginner (9×9), intermediate (16×16) or expert (30×16); or 1/2/3",
    )
    ap.add_argument("--rows", type=int, help="custom board height")
    ap.add_argument("--cols", type=int, help="custom board width")
    ap.add_argument("--mines", type=int, help="custom mine count")
    ap.add_argument("--seed", type=int, help="seed the mine layout, for repeatable games")
    args = ap.parse_args()

    name, rows, cols, mines = LEVELS[LEVEL_NAMES.get(args.level, args.level)]
    if args.rows or args.cols or args.mines:
        name = "Custom"
        rows, cols = args.rows or rows, args.cols or cols
        if not (2 <= rows <= 99 and 2 <= cols <= 99):
            ap.error("--rows and --cols must be between 2 and 99")
        if args.mines is None:
            mines = round(rows * cols * 0.16)
        else:
            mines = args.mines
        if not 1 <= mines < rows * cols:
            ap.error(f"--mines must be between 1 and {rows * cols - 1} for a {cols}×{rows} board")
    args.size = (name, rows, cols, mines)
    return args


def main():
    args = parse_args()
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
