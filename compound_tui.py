#!/usr/bin/env python3
"""Terminal compound interest calculator, with a graph of deposits against interest earned."""

import argparse
import curses
import locale
import math

# (key, label, lowest, highest, smallest step of the arrow keys)
FIELDS = [
    ("start", "Start amount", 0, 1e12, 100),
    ("monthly", "Monthly add", 0, 1e9, 10),
    ("rate", "Annual rate", 0, 100, 0.25),
    ("years", "Years", 1, 100, 1),
]
SCALED_STEPS = {"start", "monthly"}            # these step by a tenth of their order of magnitude
SYMBOLS = ["€", "$"]                          # `c` cycles these; --symbol accepts any
COMPOUNDING = [("yearly", 1), ("quarterly", 4), ("monthly", 12), ("daily", 365)]
COMPOUNDING_ROW = len(FIELDS)
ROWS = COMPOUNDING_ROW + 1

DEFAULTS = {"start": 10_000, "monthly": 200, "rate": 7, "years": 30, "compounding": "monthly"}
MONTH_STEP_YEARS = 5                           # up to this horizon the graph is read by the month
MAX_DRAFT = 15                                 # characters that fit before the results column

MIN_W, MIN_H = 66, 20
Y_LABEL_W = 11
PLOT_X = Y_LABEL_W
RESULT_X = 36
RESULT_VALUE_X = RESULT_X + 14
MIN_TICK_GAP = 6                               # columns between year labels

# Tops for the y axis, as (leading digits, equal divisions), so the area nearly fills the graph.
NICE_TOPS = [(1, 5), (1.5, 3), (2, 4), (2.5, 5), (3, 3), (4, 4), (5, 5), (6, 3), (8, 4), (10, 5)]

BARS = " ▁▂▃▄▅▆▇█"
DEPOSITS,INTEREST, BOTH, GRID = range(1, 5)   # what a graph cell shows; 0 is empty


# ---------------------------------------------------------------- maths


def monthly_growth(rate, per_year):
    """The factor a balance grows by in a month, `rate` percent a year compounded `per_year` times."""
    return (1 + rate / 100 / per_year) ** (per_year / 12)


def project(start, monthly, rate, years, per_year):
    """Returns (balances, deposited), one entry per month from month 0. Deposits land at the end of
    each month, and growth between compounding dates is at the equivalent rate, so a lump sum
    matches P(1 + r/n)^(nt) exactly and the curve has no stairs at yearly compounding."""
    growth = monthly_growth(rate, per_year)
    balances, deposited = [start], [start]
    for _ in range(years * 12):
        balances.append(balances[-1] * growth + monthly)
        deposited.append(deposited[-1] + monthly)
    return balances, deposited


def effective_rate(rate, per_year):
    """What the account really pays in a year, in percent, once compounding is counted."""
    return (monthly_growth(rate, per_year) ** 12 - 1) * 100


def doubling_years(rate, per_year):
    """Years for money left alone to double, or None at 0%."""
    if rate <= 0:
        return None
    return math.log(2) / (12 * math.log(monthly_growth(rate, per_year)))


def crossover_month(balances, deposited):
    """The first month the interest earned so far is more than everything deposited, or None."""
    for month, (balance, paid) in enumerate(zip(balances, deposited)):
        if balance - paid > paid:
            return month
    return None


def parse_number(text):
    """The number in what has been typed so far, or None. A comma is a decimal point."""
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def clamp_field(key, value):
    _, _, lo, hi, _ = next(f for f in FIELDS if f[0] == key)
    value = max(lo, min(hi, value))
    return int(value) if key == "years" else round(value, 4 if key == "rate" else 2)


def field_step(key, value, smallest):
    if key in SCALED_STEPS and value > 0:
        return max(smallest, 10 ** (math.floor(math.log10(value)) - 1))
    return smallest


def nudge(key, value, direction, smallest):
    """The next value up or down, landing on a multiple of the step so typed values round off."""
    if direction > 0:
        step = field_step(key, value, smallest)
        return (math.floor(value / step) + 1) * step
    step = field_step(key, value * 0.999, smallest)   # so 10,000 steps down to 9,900, not 9,000
    return (math.ceil(value / step) - 1) * step


# ---------------------------------------------------------------- formatting


def fmt_money(value, sym):
    if value >= 1e15:
        return f"{sym}{value:.2e}"
    for size, suffix in ((1e12, "T"), (1e9, "B")):
        if value >= size:
            return f"{sym}{value / size:.2f}{suffix}"
    return f"{sym}{value:,.2f}"


def fmt_short(value, sym):
    """A compact amount for the y axis, at most Y_LABEL_W - 2 columns."""
    if value >= 1e15:
        return f"{sym}{value:.2g}"
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return f"{sym}{value / size:.3g}{suffix}"
    return f"{sym}{value:.3g}"


def fmt_span(months):
    years, rest = divmod(months, 12)
    if not rest:
        return f"{years} year" + ("" if years == 1 else "s")
    return f"{years}y {rest}m" if years else f"{rest} month" + ("" if rest == 1 else "s")


def fmt_field(key, value, sym):
    if key == "rate":
        return f"{value:g}%"
    if key == "years":
        return f"{value} year" + ("" if value == 1 else "s")
    return fmt_money(value, sym)


def plain_number(value):
    """A value as it would be typed, for editing: no separators and no exponent."""
    return f"{value:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- graph


def nice_top(value):
    """Returns (top of the y axis, number of equal divisions): a round number at or above value."""
    if value <= 0:
        return 1000, 5
    magnitude = 10 ** math.floor(math.log10(value))
    return next((m * magnitude, divisions) for m, divisions in NICE_TOPS if m * magnitude >= value * (1 - 1e-9))


def sample(series, width):
    """`series` read at `width` evenly spaced columns, first to last, interpolating between items."""
    last = len(series) - 1
    out = []
    for col in range(width):
        x = col / (width - 1) * last
        i = min(int(x), last - 1)
        out.append(series[i] + (series[i + 1] - series[i]) * (x - i))
    return out


def area_cell(paid, total, floor):
    """The (character, kind) of one cell of the stacked area. `paid` and `total` are the heights of
    the deposits and of the whole balance in eighths of a cell; `floor` is this cell's bottom edge."""
    if total <= floor:
        return " ", 0
    if paid >= floor + 8:
        return BARS[8], DEPOSITS
    if paid <= floor:
        return BARS[min(8, total - floor)], INTEREST
    if total >= floor + 8:
        return BARS[paid - floor], BOTH               # deposits as foreground, interest as background
    # Both tops fall in this one cell, and a cell above its top has no colour left for a third part.
    return BARS[total - floor], DEPOSITS if paid - floor >= total - paid else INTEREST


def area_rows(deposited, balances, height, top):
    """The stacked area as `height` rows, top row first, each a list of (character, kind) per column."""
    scale = height * 8 / top
    columns = [(round(p * scale), round(b * scale)) for p, b in zip(deposited, balances)]
    return [[area_cell(p, b, (height - 1 - r) * 8) for p, b in columns] for r in range(height)]


def tick_rows(height, top, divisions):
    """{row: value} for the y axis, row 0 being the top. Each label sits in the row its value tops out in."""
    ticks = {}
    for i in range(1, divisions + 1):
        value = top * i / divisions
        ticks[height - math.ceil(i / divisions * height)] = value
    return ticks


def year_step(years, width):
    """The smallest round number of years whose labels keep MIN_TICK_GAP columns apart."""
    for step in (1, 2, 5, 10, 20, 25, 50):
        if step / years * (width - 1) >= MIN_TICK_GAP:
            return step
    return 50


# ---------------------------------------------------------------- app


class App:
    def __init__(self, values, compounding, sym):
        self.values = values
        self.compounding = compounding        # index into COMPOUNDING
        self.symbols = SYMBOLS if sym in SYMBOLS else [sym] + SYMBOLS
        self.sym = sym
        self.row = 0
        self.draft = None                     # text being typed into the selected field
        self.inspecting = False
        self.cursor = 0                       # month the graph is being read at

    def field_key(self):
        return FIELDS[self.row][0] if self.row < COMPOUNDING_ROW else None

    def current(self):
        """The values with what is being typed already applied, so the graph follows the typing."""
        values = dict(self.values)
        number = parse_number(self.draft) if self.draft else None
        if number is not None:
            values[self.field_key()] = clamp_field(self.field_key(), number)
        return values

    def commit(self):
        self.values = self.current()
        self.draft = None

    def adjust(self, direction):
        key = self.field_key()
        if key is None:
            self.compounding = (self.compounding + direction) % len(COMPOUNDING)
            return
        smallest = FIELDS[self.row][4]
        self.values[key] = clamp_field(key, nudge(key, self.values[key], direction, smallest))

    def type_char(self, ch):
        draft = self.draft or ""
        if len(draft) >= MAX_DRAFT:
            return
        if ch in ".,":
            if "." in draft or self.field_key() == "years":
                return
            ch = "."
        self.draft = draft + ch

    def move_cursor(self, direction):
        years = self.values["years"]
        step = 1 if years <= MONTH_STEP_YEARS else 12
        self.cursor = max(0, min(years * 12, self.cursor + direction * step))

    def next_symbol(self):
        return self.symbols[(self.symbols.index(self.sym) + 1) % len(self.symbols)]

    def start_inspecting(self):
        self.inspecting = True
        self.cursor = self.values["years"] * 12

    # ------------------------------------------------------------ drawing

    def draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            put(stdscr, 0, 0, f"Window too small ({w}×{h}), need {MIN_W}×{MIN_H}.", w)
            return
        values = self.current()
        name, per_year = COMPOUNDING[self.compounding]
        rate, years = values["rate"], values["years"]
        balances, deposited = project(values["start"], values["monthly"], rate, years, per_year)
        final, paid = balances[-1], deposited[-1]

        put(stdscr, 0, 1, "Compound interest", w, curses.color_pair(1) | curses.A_BOLD)
        note = "deposits land at the end of each month"
        put(stdscr, 0, w - len(note) - 1, note, w, curses.color_pair(4))

        self.draw_form(stdscr, values, name, w)
        self.draw_results(stdscr, final, paid, rate, per_year, w)
        self.draw_legend(stdscr, 6, final, paid, w)

        chart_h = h - 11                              # title, form, legend; axis, labels, status, keys
        self.draw_chart(stdscr, 7, chart_h, balances, deposited, final, years, w)
        self.draw_status(stdscr, h - 2, balances, deposited, years, w)
        put_runs(stdscr, h - 1, 0, [(f" {k} ", curses.color_pair(4)) for k in self.key_hints(years)], w)

    def draw_form(self, stdscr, values, compounding_name, w):
        for row in range(ROWS):
            selected = row == self.row and not self.inspecting
            attr = curses.color_pair(1) | curses.A_BOLD if selected else curses.A_NORMAL
            y = row + 1
            if row == COMPOUNDING_ROW:
                label, text = "Compounding", compounding_name
            else:
                key, label, *_ = FIELDS[row]
                text = fmt_field(key, values[key], self.sym)
            put(stdscr, y, 0, (" ▶ " if selected else "   ") + label, w, attr)
            if selected and self.draft is not None:
                put(stdscr, y, 16, self.draft + " ", w, curses.A_REVERSE)
            elif selected:
                put(stdscr, y, 16, f"◂ {text} ▸", w, attr)
            else:
                put(stdscr, y, 18, text, w)

    def draw_results(self, stdscr, final, paid, rate, per_year, w):
        doubling = doubling_years(rate, per_year)
        rows = [
            ("Final balance", fmt_money(final, self.sym), curses.A_BOLD),
            ("You put in", fmt_money(paid, self.sym), curses.color_pair(5)),
            ("Interest", fmt_money(final - paid, self.sym), curses.color_pair(2)),
            ("Effective", f"{effective_rate(rate, per_year):.2f}% a year", curses.A_NORMAL),
            ("Doubles in", "never" if doubling is None else f"{doubling:.1f} years", curses.A_NORMAL),
        ]
        for i, (label, text, attr) in enumerate(rows):
            put(stdscr, i + 1, RESULT_X, label, w, curses.color_pair(4))
            put(stdscr, i + 1, RESULT_VALUE_X, text, w, attr)

    def draw_legend(self, stdscr, y, final, paid, w):
        put(stdscr, y, 0, "─" * w, w, curses.color_pair(4))
        share = paid / final * 100 if final else 0
        interest = 100 - share if final else 0
        put_runs(stdscr, y, 2, [(" █", curses.color_pair(5)), (f" deposits {share:.0f}% ", curses.A_NORMAL),
                                (" █", curses.color_pair(2)), (f" interest {interest:.0f}% ", curses.A_NORMAL)], w)

    def draw_chart(self, stdscr, y0, height, balances, deposited, final, years, w):
        width = w - PLOT_X - 1
        top, divisions = nice_top(final)
        rows = area_rows(sample(deposited, width), sample(balances, width), height, top)
        ticks = tick_rows(height, top, divisions)
        attrs = {0: 0, DEPOSITS: curses.color_pair(5), INTEREST: curses.color_pair(2),
                 BOTH: curses.color_pair(6), GRID: curses.color_pair(4)}
        months = years * 12
        cursor_col = round(self.cursor / months * (width - 1)) if self.inspecting else None

        for r, cells in enumerate(rows):
            y = y0 + r
            if r in ticks:
                put(stdscr, y, 0, f"{fmt_short(ticks[r], self.sym):>{Y_LABEL_W - 2}} ┤", w, curses.color_pair(4))
                cells = [("·", GRID) if kind == 0 else (ch, kind) for ch, kind in cells]
            else:
                put(stdscr, y, Y_LABEL_W - 1, "│", w, curses.color_pair(4))
            for x, text, kind in runs_of(cells):
                put(stdscr, y, PLOT_X + x, text, w, attrs[kind])
            if cursor_col is not None:
                ch, kind = rows[r][cursor_col]
                ch = {0: "│", BOTH: BARS[8]}.get(kind, ch)
                put(stdscr, y, PLOT_X + cursor_col, ch, w, curses.color_pair(3))

        # X axis: a tick and a label on each round number of years.
        y = y0 + height
        axis = ["─"] * width
        step = year_step(years, width)
        put(stdscr, y + 1, 0, f"{'years':>{Y_LABEL_W - 2}}", w, curses.color_pair(4))
        for year in range(0, years + 1, step):
            col = round(year / years * (width - 1))
            axis[col] = "┬"
            label = str(year)
            put(stdscr, y + 1, PLOT_X + col - (len(label) - 1) // 2, label, w, curses.color_pair(4))
        put(stdscr, y, Y_LABEL_W - 1, "└" + "".join(axis), w, curses.color_pair(4))
        if cursor_col is not None:
            put(stdscr, y, PLOT_X + cursor_col, "┴", w, curses.color_pair(3))

    def draw_status(self, stdscr, y, balances, deposited, years, w):
        dim = curses.color_pair(4)
        if self.inspecting:
            month = self.cursor
            balance, paid = balances[month], deposited[month]
            for fmt in (fmt_money, fmt_short):        # compact amounts when the full ones don't fit
                runs = [(fmt_span(month) if month else "Start", curses.color_pair(3)),
                        ("  balance ", dim), (fmt(balance, self.sym), curses.A_BOLD),
                        ("  put in ", dim), (fmt(paid, self.sym), curses.color_pair(5)),
                        ("  interest ", dim), (fmt(balance - paid, self.sym), curses.color_pair(2))]
                if sum(len(text) for text, _ in runs) < w - 1:
                    break
            put_runs(stdscr, y, 1, runs, w)
            return
        month = crossover_month(balances, deposited)
        if balances[-1] <= 0:
            text = "Nothing to grow yet: enter a start amount or a monthly deposit."
        elif month is None:
            text = f"Your deposits stay ahead of the interest for all of {fmt_span(years * 12)}."
        else:
            text = f"Interest overtakes everything you put in after {fmt_span(month)}."
        put(stdscr, y, 1, text, w)

    def key_hints(self, years):
        if self.draft is not None:
            return ["enter ok", "esc cancel", "↑↓ next field"]
        if self.inspecting:
            unit = "month" if years <= MONTH_STEP_YEARS else "year"
            return [f"←→ {unit}", "tab back", f"c {self.next_symbol()}", "q quit"]
        return ["↑↓ field", "←→ change", "0-9 type", "tab read graph", f"c {self.next_symbol()}", "q quit"]


def runs_of(cells):
    """Joins neighbouring (character, kind) cells of one kind into (column, text, kind) runs."""
    runs = []
    for x, (ch, kind) in enumerate(cells):
        if runs and runs[-1][2] == kind:
            runs[-1][1] += ch
        else:
            runs.append([x, ch, kind])
    return runs


def put(stdscr, y, x, text, w, attr=0):
    h, _ = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0:
        return
    text = text[: w - x - (1 if y == h - 1 else 0)]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def put_runs(stdscr, y, x, runs, w):
    """Draws runs left to right, stopping at the first one that doesn't fit whole."""
    for text, attr in runs:
        if x + len(text) > w - 1:
            return
        put(stdscr, y, x, text, w, attr)
        x += len(text)


def handle_draft_key(app, key):
    if key in (curses.KEY_ENTER, 10, 13):
        app.commit()
    elif key == 27:
        app.draft = None
    elif key in (curses.KEY_BACKSPACE, 127, 8):
        app.draft = app.draft[:-1]
    elif key in (curses.KEY_UP, curses.KEY_DOWN, ord("\t")):
        app.commit()
        app.row = (app.row + (-1 if key == curses.KEY_UP else 1)) % ROWS
    elif 0 <= key < 256 and chr(key) in "0123456789.,":
        app.type_char(chr(key))


def handle_inspect_key(app, key):
    if key in (curses.KEY_LEFT, ord("h")):
        app.move_cursor(-1)
    elif key in (curses.KEY_RIGHT, ord("l")):
        app.move_cursor(1)
    elif key in (ord("\t"), curses.KEY_BTAB, 27, curses.KEY_ENTER, 10, 13):
        app.inspecting = False


def handle_form_key(app, key):
    if key in (curses.KEY_UP, ord("k")):
        app.row = (app.row - 1) % ROWS
    elif key in (curses.KEY_DOWN, ord("j")):
        app.row = (app.row + 1) % ROWS
    elif key in (curses.KEY_LEFT, ord("h")):
        app.adjust(-1)
    elif key in (curses.KEY_RIGHT, ord("l")):
        app.adjust(1)
    elif key in (ord("\t"), curses.KEY_BTAB):
        app.start_inspecting()
    elif key in (curses.KEY_ENTER, 10, 13):
        if app.field_key() is None:
            app.adjust(1)
        else:
            app.draft = plain_number(app.values[app.field_key()])
    elif app.field_key() and 0 <= key < 256 and chr(key) in "0123456789.,":
        app.type_char(chr(key))


def run(stdscr, app):
    curses.curs_set(0)
    curses.set_escdelay(25)
    curses.start_color()
    curses.use_default_colors()
    rich = curses.COLORS >= 256
    blue, green = (39, 78) if rich else (curses.COLOR_BLUE, curses.COLOR_GREEN)
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, selection
    curses.init_pair(2, green, -1)                 # interest
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # graph cursor
    curses.init_pair(4, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(5, blue, -1)                  # deposits
    curses.init_pair(6, blue, green)               # the cell where deposits meet interest

    while True:
        app.draw(stdscr)
        stdscr.refresh()
        key = stdscr.getch()
        if app.draft is not None:
            handle_draft_key(app, key)
        elif key in (ord("q"), ord("Q")):
            return
        elif key == ord("c"):
            app.sym = app.next_symbol()
        elif app.inspecting:
            handle_inspect_key(app, key)
        else:
            handle_form_key(app, key)


def main():
    parser = argparse.ArgumentParser(description="Compound interest calculator with a graph.")
    parser.add_argument("--start", type=float, default=DEFAULTS["start"], help="starting amount")
    parser.add_argument("--monthly", type=float, default=DEFAULTS["monthly"], help="deposit at each month's end")
    parser.add_argument("--rate", type=float, default=DEFAULTS["rate"], help="annual rate in percent")
    parser.add_argument("--years", type=int, default=DEFAULTS["years"], help="how many years, 1 to 100")
    parser.add_argument("--compounding", default=DEFAULTS["compounding"], type=str.lower,
                        choices=[c[0] for c in COMPOUNDING], help="how often interest compounds")
    parser.add_argument("--symbol", default=SYMBOLS[0], help=f"currency symbol, e.g. $ (default {SYMBOLS[0]})")
    args = parser.parse_args()

    values = {f[0]: clamp_field(f[0], getattr(args, f[0])) for f in FIELDS}
    compounding = next(i for i, c in enumerate(COMPOUNDING) if c[0] == args.compounding)
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, App(values, compounding, args.symbol))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
