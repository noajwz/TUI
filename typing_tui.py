#!/usr/bin/env python3
"""Terminal UI typing test: type the words shown as fast as you can, and get your WPM."""

import argparse
import curses
import datetime
import json
import math
import os
import random
import statistics
import time

# The test text is drawn from these. Short common words keep the score about speed
# rather than spelling; the longer ones stop it being a pure home-row drill.
WORDS = """
the be of and a to in he have it that for they with as not on she at by this we you do
but from or which one would all will there say who make when can more if no man out
other so what time up go about than into could state only new year some take come
these know see use get like then first any work now may such give over think most even
find day also after way many must look before great back through long where much
should well people down own just because good each those feel seem how high too place
little world very still nation hand old life tell write become here show house both
between need mean call develop under last right move thing general school never same
another begin while number part turn real leave might want point form off child few
small since against ask late home interest large person end open public follow during
present without again hold govern around possible head consider word program problem
however lead system set order eye plan run keep face fact group play stand increase
early course change help line city put close case force meet once water upon war build
hear light unite live every country bring center let side try provide continue name
certain power pay result question study woman member until far night always service
away report something company week church toward start social room figure nature
though young less enough almost read include president nothing yet better big boy cost
business value second why clear expect family complete act sense mind experience art
next near direct car law industry important girl god several matter usual rather per
often kind among white reason action return foot care simple within love human along
appear doctor believe speak active student month drive concern best door hope example
inform body ever least probable understand reach effect different idea whole control
condition field pass fall note special talk particular today measure walk teach low
hour type carry rate remain full street easy although record sit determine level local
sure receive thus moment spirit train college religion perhaps music grow free cause
serve age book board recent sound office cut step class true history position above
strong friend necessary add court deal tax support party whether either land material
happen education death agree arm mother across quite anything town past view society
manage answer break organize half fire lose money stop actual already effort wait
department able political learn voice air together shall cover common subject draw
short wife treat limit road letter color behind produce send term total university
rise century success minute remember purpose test fight watch situation south ago
difference stage father table rest bear entire market prepare explain offer plant
charge ground west picture hard front lie modern dark surface rule regard dance peace
""".split()

TIMES = (15, 30, 60, 120)
COUNTS = (10, 25, 50, 100)
MODES = {"time": TIMES, "words": COUNTS}

TICK_MS = 100  # repaint rate while waiting for a key, which keeps the countdown live
VISIBLE_LINES = 3  # lines of text on screen; the one being typed is kept second from the top
MAX_WIDTH = 72  # the text column never gets wider than this, however wide the window
MIN_COLS, MIN_ROWS = 50, 10
MAX_EXTRA = 8  # how far past the end of a word you can keep typing
CHUNK = 60  # time mode tops the word list up by this many as you near its end
CHART_ROWS = 4

CHARS_PER_WORD = 5  # the standard definition of a "word" for WPM: five keystrokes
# Faster than anyone types: a run above this was pasted, and is shown but never saved as a best.
MAX_HUMAN_WPM = 350

BEST_PATH = os.path.expanduser("~/.local/state/typing_tui/best.json")

# 3×3 digits for the result screen, two pixel rows per cell.
BIG_DIGITS = {
    "0": ("█▀█", "█ █", "▀▀▀"),
    "1": ("▀█ ", " █ ", "▀▀▀"),
    "2": ("▀▀█", "█▀▀", "▀▀▀"),
    "3": ("▀▀█", " ▀█", "▀▀▀"),
    "4": ("█ █", "▀▀█", "  ▀"),
    "5": ("█▀▀", "▀▀█", "▀▀▀"),
    "6": ("█▀▀", "█▀█", "▀▀▀"),
    "7": ("▀▀█", "  █", "  ▀"),
    "8": ("█▀█", "█▀█", "▀▀▀"),
    "9": ("█▀█", "▀▀█", "▀▀▀"),
}
BARS = " ▁▂▃▄▅▆▇█"

P_DIM, P_WRONG, P_ACCENT, P_GOOD = 1, 2, 3, 4

BACKSPACE_KEYS = {"\x7f", curses.KEY_BACKSPACE}
WORD_BACKSPACE_KEYS = {"\x17", "\x08"}  # ctrl-w, and ctrl-backspace in most terminals


class Test:
    """One run of the test: every rule and all the maths, no curses.

    Times are time.monotonic() values passed in, so a run can be replayed from a test.
    The clock starts on the first keystroke, not when the words appear.
    """

    def __init__(self, mode, amount, rng, words=None):
        self.mode, self.amount, self.rng = mode, amount, rng
        if words is None:
            words = self._draw(amount if mode == "words" else 2 * CHUNK)
        self.words = list(words)
        self.typed = [""]  # one entry per word reached; the last is the one being typed
        self.started = self.finished = None
        self.keys = self.wrong_keys = 0  # every keystroke, so a fixed mistake still counts
        self.per_second = []  # [keys, wrong keys] for each second of the run

    def _draw(self, n, prev=None):
        out = []
        for _ in range(n):
            word = self.rng.choice(WORDS)
            while word == (out[-1] if out else prev):
                word = self.rng.choice(WORDS)
            out.append(word)
        return out

    @property
    def state(self):
        if self.finished is not None:
            return "done"
        return "ready" if self.started is None else "running"

    @property
    def cur(self):
        return len(self.typed) - 1

    def elapsed(self, now):
        if self.started is None:
            return 0.0
        end = self.finished if self.finished is not None else now
        return end - self.started

    def tick(self, now):
        """End a timed run once its time is up. Called before every key and repaint."""
        if self.state == "running" and self.mode == "time" and now - self.started >= self.amount:
            self.finished = self.started + self.amount
            self.per_second += [[0, 0] for _ in range(self.amount - len(self.per_second))]

    def _key(self, now, ok):
        if self.started is None:
            self.started = now
        sec = int(now - self.started)
        if self.mode == "time":
            sec = min(sec, self.amount - 1)
        while len(self.per_second) <= sec:
            self.per_second.append([0, 0])
        self.per_second[sec][0] += 1
        self.keys += 1
        if not ok:
            self.per_second[sec][1] += 1
            self.wrong_keys += 1

    def type_char(self, ch, now):
        self.tick(now)
        if self.state == "done":
            return
        i, word = self.cur, self.words[self.cur]
        typed = self.typed[i]
        if len(typed) >= len(word) + MAX_EXTRA:
            return
        self._key(now, len(typed) < len(word) and word[len(typed)] == ch)
        self.typed[i] = typed + ch
        # The last word ends the run as soon as it is right; no need for a final space.
        if self.mode == "words" and i == len(self.words) - 1 and self.typed[i] == word:
            self.finished = now

    def space(self, now):
        """Submit the current word and move on. Ignored on an empty word."""
        self.tick(now)
        if self.state != "running" or not self.typed[-1]:
            return
        i = self.cur
        self._key(now, len(self.typed[i]) >= len(self.words[i]))
        if i == len(self.words) - 1:  # only words mode ever reaches the end
            self.finished = now
            return
        self.typed.append("")
        if self.mode == "time" and len(self.words) - len(self.typed) < CHUNK:
            self.words += self._draw(CHUNK, self.words[-1])

    def backspace(self, now, whole_word=False):
        """Delete back. From the start of a word this steps back into the previous one,
        but only if that word has a mistake in it: a correct word is locked in."""
        self.tick(now)
        if self.state != "running":
            return
        i = self.cur
        if self.typed[i]:
            self.typed[i] = "" if whole_word else self.typed[i][:-1]
        elif i > 0 and self.typed[i - 1] != self.words[i - 1]:
            self.typed.pop()
            if whole_word:
                self.typed[-1] = ""

    def _submitted(self, i):
        """Has word i been finished with, so that what is missing from it counts as missed?"""
        return i < self.cur or (self.state == "done" and self.mode == "words")

    def counts(self):
        """(correct, incorrect, extra, missed) characters, as the text stands."""
        correct = incorrect = extra = missed = 0
        for i, typed in enumerate(self.typed):
            word = self.words[i]
            for a, b in zip(typed, word):
                if a == b:
                    correct += 1
                else:
                    incorrect += 1
            extra += max(0, len(typed) - len(word))
            if self._submitted(i):
                missed += max(0, len(word) - len(typed))
        return correct, incorrect, extra, missed

    def net_chars(self):
        """Characters that count towards WPM: words typed exactly right, plus the space
        after each, plus the part of an unfinished last word that is right so far."""
        n = 0
        for i, typed in enumerate(self.typed):
            word = self.words[i]
            if typed == word:
                n += len(word) + (i < self.cur)
            elif not self._submitted(i) and word.startswith(typed):
                n += len(typed)
        return n

    def raw_chars(self):
        return sum(len(t) for t in self.typed) + self.cur

    def wpm(self, now):
        minutes = self.elapsed(now) / 60
        return self.net_chars() / CHARS_PER_WORD / minutes if minutes else 0.0

    def speeds(self):
        """[(raw wpm, wrong keys)] for each second of the run, from the keystrokes in it."""
        total = self.elapsed(self.finished)
        out = []
        for i, (keys, wrong) in enumerate(self.per_second):
            # The last second is usually partial; don't let a sliver of one read as a burst.
            span = max(0.5, min(1.0, total - i))
            out.append((keys / span * 60 / CHARS_PER_WORD, wrong))
        return out

    def result(self):
        t = self.elapsed(self.finished)
        minutes = max(t, 1.0) / 60  # a pasted run takes no time at all; don't divide by it
        speeds = self.speeds()
        raw = [s for s, _ in speeds]
        consistency = 0.0
        if len(raw) > 1 and statistics.mean(raw) > 0:
            # 100% is an even pace throughout; it drops with the spread of per-second speed.
            consistency = max(0.0, 100 * (1 - statistics.pstdev(raw) / statistics.mean(raw)))
        return {
            "wpm": self.net_chars() / CHARS_PER_WORD / minutes,
            "raw": self.raw_chars() / CHARS_PER_WORD / minutes,
            "acc": 100 * (self.keys - self.wrong_keys) / self.keys if self.keys else 0.0,
            "consistency": consistency,
            "time": t,
            "chars": self.counts(),
            "speeds": speeds,
        }


def mode_key(mode, amount):
    return f"{mode} {amount}"


def load_best():
    try:
        with open(BEST_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_best(best):
    """Best-effort: a read-only home directory costs the record, not the program."""
    try:
        os.makedirs(os.path.dirname(BEST_PATH), exist_ok=True)
        tmp = BEST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(best, f, indent=1)
        os.replace(tmp, BEST_PATH)
    except OSError:
        pass


def record_result(test, best):
    """Score a finished run and update the personal bests. Returns (result, previous best)."""
    res = test.result()
    key = mode_key(test.mode, test.amount)
    prev = best.get(key)
    if not isinstance(prev, dict):
        prev = None
    if 0 < res["wpm"] <= MAX_HUMAN_WPM and (prev is None or res["wpm"] > prev.get("wpm", 0)):
        best[key] = {
            "wpm": round(res["wpm"], 1),
            "acc": round(res["acc"], 1),
            "date": datetime.date.today().isoformat(),
        }
        save_best(best)
    return res, prev


def wrap(test, width):
    """Split the words into display lines, as [(first index, last index + 1)].

    A word takes the room of whichever is longer, the word or what was typed for it,
    so typing extra letters can push the words after it onto the next line."""
    lines, start, col = [], 0, 0
    for i, word in enumerate(test.words):
        n = len(word)
        if i < len(test.typed):
            n = max(n, len(test.typed[i]))
        if col and col + n > width:
            lines.append((start, i))
            start, col = i, 0
        col += n + 1
    lines.append((start, len(test.words)))
    return lines


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    grey = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
    curses.init_pair(P_DIM, grey, -1)
    curses.init_pair(P_WRONG, curses.COLOR_RED, -1)
    curses.init_pair(P_ACCENT, curses.COLOR_YELLOW, -1)
    curses.init_pair(P_GOOD, curses.COLOR_GREEN, -1)


def pair(n):
    if not curses.has_colors():
        return curses.A_DIM if n == P_DIM else 0
    return curses.color_pair(n)


def put(win, y, x, text, attr=0):
    """addstr that clips at the window edge instead of raising."""
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        text = text[: max(0, w - x - (y == h - 1))]
        if text:
            win.addstr(y, x, text, attr)


def first_fit(width, *texts):
    """The longest wording that fits, falling back to the shortest one."""
    return next((t for t in texts if len(t) <= width), texts[-1])


def put_centered(win, y, left, width, text, attr=0):
    text = text[:width]
    put(win, y, left + (width - len(text)) // 2, text, attr)


def big_number(value):
    """Three rows of block digits, or the plain number on the bottom row past three digits,
    where block digits would run into the next column."""
    text = f"{value:.0f}"
    if len(text) > 3:
        return ["", "", text]
    return [" ".join(BIG_DIGITS[c][r] for c in text) for r in range(3)]


def amount_choices(mode, amount):
    """The presets for a mode, plus a custom amount from the command line."""
    return sorted(set(MODES[mode]) | {amount})


def draw_menu(win, y, left, width, mode, amount):
    parts = []
    for name, presets in MODES.items():
        if parts:
            parts.append(("   ·   ", pair(P_DIM)))
        active = name == mode
        parts.append((name, pair(P_ACCENT) | curses.A_BOLD if active else pair(P_DIM)))
        for n in amount_choices(name, amount) if active else presets:
            on = active and n == amount
            parts.append((f" {n}", pair(P_ACCENT) | curses.A_BOLD if on else pair(P_DIM)))
    x = left + max(0, (width - sum(len(t) for t, _ in parts)) // 2)
    for text, attr in parts:
        put(win, y, x, text, attr)
        x += len(text)


def draw_text(win, test, top, left, width):
    lines = wrap(test, width)
    cur_line = next(n for n, (a, b) in enumerate(lines) if a <= test.cur < b)
    first = max(0, min(cur_line - 1, len(lines) - VISIBLE_LINES))
    for row, (a, b) in enumerate(lines[first : first + VISIBLE_LINES]):
        x = left
        for i in range(a, b):
            word = test.words[i]
            typed = test.typed[i] if i < len(test.typed) else ""
            wrong_word = test._submitted(i) and typed != word
            for j in range(max(len(word), len(typed))):
                if j >= len(typed):
                    ch, attr = word[j], pair(P_DIM)
                elif j >= len(word):
                    ch, attr = typed[j], pair(P_WRONG) | curses.A_DIM
                elif typed[j] == word[j]:
                    ch, attr = word[j], curses.A_BOLD
                else:
                    # Show the letter that was wanted, in red, so the text stays readable.
                    ch, attr = word[j], pair(P_WRONG) | curses.A_BOLD
                if wrong_word:
                    attr |= curses.A_UNDERLINE
                if i == test.cur and j == len(typed) and test.state != "done":
                    attr |= curses.A_REVERSE
                put(win, top + row, x + j, ch, attr)
            if i == test.cur and len(typed) >= len(word) and test.state != "done":
                put(win, top + row, x + len(typed), " ", curses.A_REVERSE)
            x += max(len(word), len(typed)) + 1


def draw_test(win, test, h, left, width, now):
    top = max(0, (h - 7) // 2)
    if test.state == "ready":
        draw_menu(win, top, left, width, test.mode, test.amount)
    else:
        if test.mode == "time":
            status = str(max(0, math.ceil(test.amount - test.elapsed(now))))
        else:
            status = f"{test.cur}/{test.amount}"
        put(win, top, left, status, pair(P_ACCENT) | curses.A_BOLD)
        if test.elapsed(now) >= 1:
            live = f"{test.wpm(now):.0f} wpm"
            put(win, top, left + width - len(live), live, pair(P_DIM))
    draw_text(win, test, top + 2, left, width)
    if test.state == "ready":
        hint = first_fit(
            width,
            "type to start   ←→ length   ↑↓ time/words   tab new words   esc quit",
            "←→↑↓ change test   tab new words   esc quit",
        )
    else:
        hint = "tab restart"
    put_centered(win, top + 6, left, width, hint, pair(P_DIM))


def draw_chart(win, top, left, width, speeds):
    """Per-second speed as bars, with a red × under each second that had a mistake."""
    gutter = 4
    room = width - gutter
    group = math.ceil(len(speeds) / room)
    buckets = [speeds[i : i + group] for i in range(0, len(speeds), group)]
    vals = [sum(s for s, _ in b) / len(b) for b in buckets]
    errs = [sum(e for _, e in b) for b in buckets]
    col_w = max(1, min(3, room // len(buckets)))
    bar_w = col_w - 1 if col_w > 1 else 1
    peak = max(max(vals), 1)
    put(win, top, left, f"{peak:>3.0f}", pair(P_DIM))
    put(win, top + CHART_ROWS - 1, left, "  0", pair(P_DIM))
    put(win, top + CHART_ROWS, left + gutter, "─" * (len(buckets) * col_w), pair(P_DIM))
    for k, v in enumerate(vals):
        x = left + gutter + k * col_w
        eighths = round(v / peak * CHART_ROWS * 8)
        for r in range(CHART_ROWS):
            fill = min(8, max(0, eighths - r * 8))
            if fill:
                put(win, top + CHART_ROWS - 1 - r, x, BARS[fill] * bar_w, pair(P_ACCENT))
        if errs[k]:
            put(win, top + CHART_ROWS, x, "×" * bar_w, pair(P_WRONG) | curses.A_BOLD)


def draw_results(win, test, record, h, left, width):
    res, prev = record
    show_chart = h >= 17 and res["speeds"]
    top = max(0, (h - (17 if show_chart else 10)) // 2)
    x_acc, x_side = left + 15, left + 31
    put(win, top, left, "wpm", pair(P_DIM))
    put(win, top, x_acc, "acc", pair(P_DIM))
    put(win, top, x_side, mode_key(test.mode, test.amount), pair(P_DIM))
    for r, row in enumerate(big_number(res["wpm"])):
        put(win, top + 1 + r, left, row, pair(P_ACCENT) | curses.A_BOLD)
    acc_rows = big_number(res["acc"])
    for r, row in enumerate(acc_rows):
        put(win, top + 1 + r, x_acc, row, pair(P_ACCENT))
    put(win, top + 3, x_acc + len(acc_rows[2]) + 1, "%", pair(P_ACCENT))
    side = (
        f"raw {res['raw']:.0f}",
        f"consistency {res['consistency']:.0f}%",
        f"time {res['time']:.1f}s",
    )
    for r, text in enumerate(side):
        put(win, top + 1 + r, x_side, text)

    correct, incorrect, extra, missed = res["chars"]
    y = top + 5
    chars = f"{correct} correct · {incorrect} wrong · {extra} extra · {missed} missed"
    put(win, y, left, first_fit(width, "characters  " + chars, chars), pair(P_DIM))
    y += 2
    if show_chart:
        draw_chart(win, y, left, width, res["speeds"])
        y += CHART_ROWS + 2

    if res["wpm"] > MAX_HUMAN_WPM:
        put(win, y, left, "too fast to be typed: pasted? not saved", pair(P_WRONG))
    elif prev is None or res["wpm"] > prev.get("wpm", 0):
        if res["wpm"] > 0:
            put(win, y, left, "new personal best!", pair(P_GOOD) | curses.A_BOLD)
    else:
        line = f"personal best {prev.get('wpm', 0):.0f} wpm ({prev.get('date', '?')})"
        put(win, y, left, line, pair(P_DIM))
    hint = first_fit(
        width,
        "tab/enter next   shift-tab same words   ←→↑↓ change test   esc quit",
        "tab next   ⇧tab retry   arrows mode   esc quit",
    )
    put_centered(win, y + 2, left, width, hint, pair(P_DIM))


def draw(stdscr, test, record, now):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if w < MIN_COLS or h < MIN_ROWS:
        msg = f"Window too small: need {MIN_COLS}×{MIN_ROWS}, have {w}×{h}"
        put(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg)
    else:
        width = min(MAX_WIDTH, w - 4)
        left = (w - width) // 2
        if record:
            draw_results(stdscr, test, record, h, left, width)
        else:
            draw_test(stdscr, test, h, left, width, now)
    stdscr.refresh()


def read_escape(stdscr):
    """After an ESC: the rest of a sequence the terminal sent with it, or None for a bare ESC."""
    stdscr.timeout(0)
    rest = []
    try:
        while True:
            rest.append(stdscr.get_wch())
    except curses.error:
        pass
    stdscr.timeout(TICK_MS)
    return rest or None


def run(stdscr, args):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    curses.set_escdelay(25)
    init_colors()

    rng = random.Random(args.seed)
    best = load_best()
    mode, amount = args.mode, args.amount
    test = Test(mode, amount, rng)
    record = None
    while True:
        now = time.monotonic()
        test.tick(now)
        if test.state == "done" and record is None:
            record = record_result(test, best)
        draw(stdscr, test, record, now)
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        now = time.monotonic()

        if key == "\x1b":
            rest = read_escape(stdscr)
            if rest is None:
                if test.state != "running":  # a stray ESC mid-test must not end it
                    return
            elif rest[0] in BACKSPACE_KEYS:  # option-backspace on macOS
                test.backspace(now, whole_word=True)
            continue
        if key == "\t" or (key in ("\n", "\r", curses.KEY_ENTER) and record):
            test, record = Test(mode, amount, rng), None
        elif key == curses.KEY_BTAB:
            test, record = Test(mode, amount, rng, test.words), None
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            if test.state == "running":
                continue
            choices = amount_choices(mode, amount)
            idx = choices.index(amount)
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                idx += 1 if key == curses.KEY_RIGHT else -1
                amount = choices[min(max(idx, 0), len(choices) - 1)]
            else:
                pos = MODES[mode].index(amount) if amount in MODES[mode] else 1
                mode = "words" if mode == "time" else "time"
                amount = MODES[mode][pos]
            test, record = Test(mode, amount, rng), None
        elif key in BACKSPACE_KEYS:
            test.backspace(now)
        elif key in WORD_BACKSPACE_KEYS:
            test.backspace(now, whole_word=True)
        elif key == " ":
            test.space(now)
        elif isinstance(key, str) and key.isprintable():
            test.type_char(key, now)


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--time", type=int, metavar="SECONDS", help="timed test (default: 30)")
    group.add_argument("--words", type=int, metavar="N", help="type a fixed number of words")
    ap.add_argument("--seed", type=int, help="seed the word choice, for repeatable tests")
    args = ap.parse_args()
    if args.words is not None:
        if not 1 <= args.words <= 1000:
            ap.error("--words must be between 1 and 1000")
        args.mode, args.amount = "words", args.words
    else:
        args.mode, args.amount = "time", args.time or 30
        if not 1 <= args.amount <= 600:
            ap.error("--time must be between 1 and 600 seconds")
    return args


def main():
    args = parse_args()
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
