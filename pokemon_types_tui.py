#!/usr/bin/env python3
"""Terminal UI Pokémon type calculator: how hard does each type hit each other type?"""

import argparse
import curses

TYPES = (
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting", "Poison", "Ground",
    "Flying", "Psychic", "Bug", "Rock", "Ghost", "Dragon", "Dark", "Steel", "Fairy",
)  # fmt: skip

# attacker -> {defender: multiplier}. Anything not listed is 1×. This is the chart
# from Generation 6 (X/Y) onwards, the one that added Fairy.
CHART = {
    "Normal": {"Rock": 0.5, "Ghost": 0, "Steel": 0.5},
    "Fire": {"Fire": 0.5, "Water": 0.5, "Grass": 2, "Ice": 2, "Bug": 2, "Rock": 0.5,
             "Dragon": 0.5, "Steel": 2},
    "Water": {"Fire": 2, "Water": 0.5, "Grass": 0.5, "Ground": 2, "Rock": 2, "Dragon": 0.5},
    "Electric": {"Water": 2, "Electric": 0.5, "Grass": 0.5, "Ground": 0, "Flying": 2,
                 "Dragon": 0.5},
    "Grass": {"Fire": 0.5, "Water": 2, "Grass": 0.5, "Poison": 0.5, "Ground": 2,
              "Flying": 0.5, "Bug": 0.5, "Rock": 2, "Dragon": 0.5, "Steel": 0.5},
    "Ice": {"Fire": 0.5, "Water": 0.5, "Grass": 2, "Ice": 0.5, "Ground": 2, "Flying": 2,
            "Dragon": 2, "Steel": 0.5},
    "Fighting": {"Normal": 2, "Ice": 2, "Poison": 0.5, "Flying": 0.5, "Psychic": 0.5,
                 "Bug": 0.5, "Rock": 2, "Ghost": 0, "Dark": 2, "Steel": 2, "Fairy": 0.5},
    "Poison": {"Grass": 2, "Poison": 0.5, "Ground": 0.5, "Rock": 0.5, "Ghost": 0.5,
               "Steel": 0, "Fairy": 2},
    "Ground": {"Fire": 2, "Electric": 2, "Grass": 0.5, "Poison": 2, "Flying": 0, "Bug": 0.5,
               "Rock": 2, "Steel": 2},
    "Flying": {"Electric": 0.5, "Grass": 2, "Fighting": 2, "Bug": 2, "Rock": 0.5,
               "Steel": 0.5},
    "Psychic": {"Fighting": 2, "Poison": 2, "Psychic": 0.5, "Dark": 0, "Steel": 0.5},
    "Bug": {"Fire": 0.5, "Grass": 2, "Fighting": 0.5, "Poison": 0.5, "Flying": 0.5,
            "Psychic": 2, "Ghost": 0.5, "Dark": 2, "Steel": 0.5, "Fairy": 0.5},
    "Rock": {"Fire": 2, "Ice": 2, "Fighting": 0.5, "Ground": 0.5, "Flying": 2, "Bug": 2,
             "Steel": 0.5},
    "Ghost": {"Normal": 0, "Psychic": 2, "Ghost": 2, "Dark": 0.5},
    "Dragon": {"Dragon": 2, "Steel": 0.5, "Fairy": 0},
    "Dark": {"Fighting": 0.5, "Psychic": 2, "Ghost": 2, "Dark": 0.5, "Fairy": 0.5},
    "Steel": {"Fire": 0.5, "Water": 0.5, "Electric": 0.5, "Ice": 2, "Rock": 2, "Steel": 0.5,
              "Fairy": 2},
    "Fairy": {"Fire": 0.5, "Fighting": 2, "Poison": 0.5, "Dragon": 2, "Dark": 2, "Steel": 0.5},
}  # fmt: skip

# Badge colours: (256-colour background, text on it, 8-colour fallback foreground).
# The 256-colour values are the nearest xterm matches to the games' type colours.
LOOKS = {
    "Normal": (144, 16, curses.COLOR_WHITE),
    "Fire": (208, 16, curses.COLOR_RED),
    "Water": (69, 16, curses.COLOR_BLUE),
    "Electric": (220, 16, curses.COLOR_YELLOW),
    "Grass": (113, 16, curses.COLOR_GREEN),
    "Ice": (116, 16, curses.COLOR_CYAN),
    "Fighting": (124, 231, curses.COLOR_RED),
    "Poison": (127, 231, curses.COLOR_MAGENTA),
    "Ground": (179, 16, curses.COLOR_YELLOW),
    "Flying": (141, 16, curses.COLOR_CYAN),
    "Psychic": (204, 16, curses.COLOR_MAGENTA),
    "Bug": (142, 16, curses.COLOR_GREEN),
    "Rock": (137, 16, curses.COLOR_YELLOW),
    "Ghost": (60, 231, curses.COLOR_MAGENTA),
    "Dragon": (57, 231, curses.COLOR_BLUE),
    "Dark": (95, 231, curses.COLOR_WHITE),
    "Steel": (146, 16, curses.COLOR_WHITE),
    "Fairy": (175, 16, curses.COLOR_MAGENTA),
}

MULT_TEXT = {4: "4×", 2: "2×", 1: "1×", 0.5: "½×", 0.25: "¼×", 0: "0×"}
VERDICT = {
    4: "super effective",
    2: "super effective",
    1: "normal damage",
    0.5: "not very effective",
    0.25: "not very effective",
    0: "no effect",
}
# Chart cells leave 1× blank so the interesting cells stand out.
CHART_GLYPH = {2: "2", 1: "", 0.5: "½", 0: "0"}

MODES = ("Defend", "Attack", "Chart")
MAX_DEFENDERS = 2  # a Pokémon has at most two types

BADGE_W = 10  # " Electric " is the longest name plus a space either side
LIST_W = BADGE_W + 4  # the type list: marker, badge, selection mark
CHART_CELL = 4  # " NOR" per defending type in the chart
# Chart row labels: full names when the window is wide enough, else "NOR" like the columns.
CHART_LABELS = (BADGE_W, 5)

# Colour pairs. 1..18 are badges, 19..36 the same colours as plain text.
P_TEXT = len(TYPES) + 1
P_GOOD, P_BAD, P_NONE, P_TITLE = range(2 * len(TYPES) + 1, 2 * len(TYPES) + 5)

UP = {curses.KEY_UP, ord("k")}
DOWN = {curses.KEY_DOWN, ord("j")}
LEFT = {curses.KEY_LEFT, ord("h")}
RIGHT = {curses.KEY_RIGHT, ord("l")}
PICK_KEYS = {ord(" "), ord("\n"), curses.KEY_ENTER}
CLEAR_KEYS = {ord("x"), curses.KEY_BACKSPACE, 127, 8}
MODE_KEYS = {ord("d"): 0, ord("a"): 1, ord("c"): 2}

HELP = {
    "Defend": "↑↓ move  space pick (up to 2)  x clear  tab/d/a/c mode  q quit",
    "Attack": "↑↓ move  space pick moves  x clear  tab/d/a/c mode  q quit",
    "Chart": "←↑↓→ move  tab/d/a/c mode  q quit",
}


def multiplier(attacker, defenders):
    """Damage multiplier of one attacking type against a (one- or two-type) defender."""
    m = 1
    for d in defenders:
        m *= CHART[attacker].get(d, 1)
    return m


def defend_groups(defenders):
    """{multiplier: [attacking types]} for everything that can hit these defenders."""
    groups = {}
    for a in TYPES:
        groups.setdefault(multiplier(a, defenders), []).append(a)
    return groups


def attack_groups(attackers):
    """{multiplier: [defending types]}, each defender scored by the best of the attackers.

    One attacker answers "what does this type hit?"; several answer "what do these moves
    cover between them?", which is what matters when choosing a moveset.
    """
    groups = {}
    for d in TYPES:
        best = max(multiplier(a, [d]) for a in attackers)
        groups.setdefault(best, []).append(d)
    return groups


def parse_type(name):
    """Resolve a type name or unambiguous prefix ("psy", "ele"). Returns (type, error)."""
    name = name.strip().lower()
    exact = [t for t in TYPES if t.lower() == name]
    hits = exact or [t for t in TYPES if t.lower().startswith(name)]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"unknown type {name!r}"
    return None, f"{name!r} could be {', '.join(hits)}"


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    rich = curses.COLORS >= 256
    for i, t in enumerate(TYPES, 1):
        bg, fg, basic = LOOKS[t]
        if rich:
            curses.init_pair(i, fg, bg)
            curses.init_pair(P_TEXT - 1 + i, bg, -1)
        else:
            curses.init_pair(i, curses.COLOR_BLACK, basic)
            curses.init_pair(P_TEXT - 1 + i, basic, -1)
    curses.init_pair(P_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(P_BAD, curses.COLOR_RED, -1)
    curses.init_pair(P_NONE, 8 if curses.COLORS >= 16 else curses.COLOR_WHITE, -1)
    curses.init_pair(P_TITLE, curses.COLOR_CYAN, -1)


def pair(n):
    return curses.color_pair(n) if curses.has_colors() else 0


def badge_attr(t):
    return pair(TYPES.index(t) + 1) | curses.A_BOLD


def text_attr(t):
    return pair(P_TEXT + TYPES.index(t)) | curses.A_BOLD


def mult_attr(m):
    if m > 1:
        return pair(P_GOOD) | curses.A_BOLD
    if m == 0:
        return pair(P_NONE) | curses.A_BOLD
    if m < 1:
        return pair(P_BAD) | curses.A_BOLD
    return curses.A_DIM


def put(win, y, x, text, attr=0):
    """addstr that clips at the window edge instead of raising."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        text = text[: max(0, w - x - (y == h - 1))]
        if text:
            win.addstr(y, max(0, x), text, attr)


def put_badge(win, y, x, t):
    put(win, y, x, f" {t} ".ljust(BADGE_W), badge_attr(t))


def badge_lines(w):
    return max(1, (w - 5) // (BADGE_W + 1))


def badge_rows_height(w, rows, gap):
    per_line = badge_lines(w)
    return sum(max(1, -(-len(types) // per_line)) + gap for _, types in rows)


def draw_badge_rows(win, y, x, w, rows, gap=1):
    """Paint [(multiplier, [types])] as labelled, wrapped rows of badges. Returns next y."""
    per_line = badge_lines(w)
    for m, types in rows:
        put(win, y, x, MULT_TEXT[m].rjust(3), mult_attr(m))
        if not types:
            put(win, y, x + 6, "—", curses.A_DIM)
            y += 1
        for i in range(0, len(types), per_line):
            for j, t in enumerate(types[i : i + per_line]):
                put_badge(win, y, x + 5 + j * (BADGE_W + 1), t)
            y += 1
        y += gap
    return y


def fit_gap(win, top, w, rows, reserve):
    """Blank lines between groups if they fit above the footer, else none."""
    room = win.getmaxyx()[0] - 1 - top - reserve
    return 1 if badge_rows_height(w, rows, 1) <= room else 0


def draw_type_list(win, top, cursor, picked):
    for i, t in enumerate(TYPES):
        y = top + i
        if i == cursor:
            put(win, y, 0, "▸", curses.A_BOLD)
        put_badge(win, y, 2, t)
        if t in picked:
            put(win, y, 2 + BADGE_W, f" {picked.index(t) + 1}", curses.A_BOLD)


def draw_defend(win, top, x, w, cursor, picked):
    defenders = picked or [TYPES[cursor]]
    label = " / ".join(defenders)
    note = "" if picked else "   (preview — space to pick)"
    put(win, top, x, "Defending as ", curses.A_BOLD)
    put(win, top, x + 13, label, text_attr(defenders[0]))
    put(win, top, x + 13 + len(label), note, curses.A_DIM)

    groups = defend_groups(defenders)
    order = (4, 2, 1, 0.5, 0.25, 0) if len(defenders) == 2 else (2, 1, 0.5, 0)
    rows = [(m, groups.get(m, [])) for m in order]
    y = draw_badge_rows(win, top + 2, x, w, rows, fit_gap(win, top + 2, w, rows, 1))

    if picked:
        a = TYPES[cursor]
        m = multiplier(a, picked)
        put(win, y, x, a, text_attr(a))
        line = f" attacking {label}: "
        put(win, y, x + len(a), line)
        put(win, y, x + len(a) + len(line), f"{MULT_TEXT[m]} {VERDICT[m]}", mult_attr(m))


def draw_attack(win, top, x, w, cursor, picked):
    attackers = picked or [TYPES[cursor]]
    label = " + ".join(attackers)
    note = "" if picked else "   (preview — space to pick)"
    put(win, top, x, "Attacking with ", curses.A_BOLD)
    put(win, top, x + 15, label[: max(0, w - 15)], text_attr(attackers[0]))
    put(win, top, x + 15 + len(label), note, curses.A_DIM)

    groups = attack_groups(attackers)
    rows = [(m, groups.get(m, [])) for m in (2, 1, 0.5, 0)]
    y = draw_badge_rows(win, top + 2, x, w, rows, fit_gap(win, top + 2, w, rows, 1))
    if len(attackers) > 1:
        put(win, y, x, "Each defending type is scored by the best of these moves.", curses.A_DIM)


def chart_width(label_w):
    return label_w + len(TYPES) * CHART_CELL


def draw_chart(win, top, left, cell):
    w = win.getmaxyx()[1]
    label_w = next((lw for lw in CHART_LABELS if left + chart_width(lw) < w), CHART_LABELS[-1])
    r0, c0 = cell
    put(win, top, left, "ATK↓ DEF→"[: label_w + CHART_CELL], curses.A_DIM)
    for c, t in enumerate(TYPES):
        attr = text_attr(t) | (curses.A_REVERSE if c == c0 else 0)
        put(win, top + 1, left + label_w + c * CHART_CELL, f" {t[:3].upper()}", attr)
    for r, a in enumerate(TYPES):
        y = top + 2 + r
        name = a if label_w == BADGE_W else a[:3].upper()
        attr = text_attr(a) | (curses.A_REVERSE if r == r0 else 0)
        put(win, y, left, f" {name} ".ljust(label_w), attr)
        for c, d in enumerate(TYPES):
            m = CHART[a].get(d, 1)
            glyph = CHART_GLYPH[m]
            attr = mult_attr(m)
            if r == r0 or c == c0:
                glyph = glyph or "·"  # dots trace the crosshair through 1× cells
            if (r, c) == (r0, c0):
                attr |= curses.A_REVERSE
            put(win, y, left + label_w + c * CHART_CELL + 1, glyph.center(CHART_CELL - 1), attr)

    a, d = TYPES[r0], TYPES[c0]
    m = CHART[a].get(d, 1)
    y = top + 2 + len(TYPES)
    put(win, y, left, a, text_attr(a))
    put(win, y, left + len(a), " → ")
    put(win, y, left + len(a) + 3, d, text_attr(d))
    put(win, y, left + len(a) + 3 + len(d), f"  {MULT_TEXT[m]} {VERDICT[m]}", mult_attr(m))


def draw(stdscr, mode, cursor, picked, cell):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    need_w = 2 + max(chart_width(CHART_LABELS[-1]), LIST_W + 4 * (BADGE_W + 1) + 5)
    need_h = len(TYPES) + 6  # title, gap, two chart headers, 18 rows, status, footer
    if w < need_w or h < need_h:
        msg = f"Window too small: need {need_w}×{need_h}, have {w}×{h}"
        put(stdscr, h // 2, max(0, (w - len(msg)) // 2), msg)
        stdscr.refresh()
        return

    put(stdscr, 0, 1, "Pokémon Type Calculator", pair(P_TITLE) | curses.A_BOLD)
    tabs_x = w - 1 - sum(len(m) + 3 for m in MODES)
    for m in MODES:
        attr = curses.A_REVERSE | curses.A_BOLD if m == MODES[mode] else curses.A_DIM
        put(stdscr, 0, tabs_x, f" {m} ", attr)
        tabs_x += len(m) + 3

    top = 2
    name = MODES[mode]
    if name == "Chart":
        draw_chart(stdscr, top, 1, cell)
    else:
        draw_type_list(stdscr, top, cursor, picked)
        x = LIST_W + 2
        panel_w = w - x - 1
        if name == "Defend":
            draw_defend(stdscr, top, x, panel_w, cursor, picked)
        else:
            draw_attack(stdscr, top, x, panel_w, cursor, picked)

    put(stdscr, h - 1, 1, HELP[name], curses.A_DIM)
    stdscr.refresh()


def toggle(picked, t, limit):
    """Add or remove t. Past the limit the newest pick is replaced, so the first stays."""
    if t in picked:
        return [p for p in picked if p != t]
    if limit and len(picked) >= limit:
        return picked[: limit - 1] + [t]
    return picked + [t]


def run(stdscr, args):
    curses.curs_set(0)
    stdscr.keypad(True)
    init_colors()

    mode = 0
    cursor = TYPES.index(args.types[0]) if args.types else 0
    picked = {"Defend": list(args.types), "Attack": []}
    cell = (cursor, cursor)  # chart cursor: (attacker row, defender column)
    n = len(TYPES)
    while True:
        draw(stdscr, mode, cursor, picked.get(MODES[mode], []), cell)
        key = stdscr.getch()
        name = MODES[mode]
        if key == ord("q"):
            return
        if key == ord("\t"):
            mode = (mode + 1) % len(MODES)
        elif key == curses.KEY_BTAB:
            mode = (mode - 1) % len(MODES)
        elif key in MODE_KEYS:
            mode = MODE_KEYS[key]
        elif name == "Chart":
            r, c = cell
            if key in UP:
                r = (r - 1) % n
            elif key in DOWN:
                r = (r + 1) % n
            elif key in LEFT:
                c = (c - 1) % n
            elif key in RIGHT:
                c = (c + 1) % n
            cell = (r, c)
        elif key in UP:
            cursor = (cursor - 1) % n
        elif key in DOWN:
            cursor = (cursor + 1) % n
        elif key in PICK_KEYS:
            limit = MAX_DEFENDERS if name == "Defend" else None
            picked[name] = toggle(picked[name], TYPES[cursor], limit)
        elif key in CLEAR_KEYS:
            picked[name] = []


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "types",
        nargs="*",
        metavar="TYPE",
        help="start defending as these one or two types; prefixes work (psy, ele)",
    )
    args = ap.parse_args()
    if len(args.types) > MAX_DEFENDERS:
        ap.error(f"a Pokémon has at most {MAX_DEFENDERS} types")
    resolved = []
    for name in args.types:
        t, err = parse_type(name)
        if err:
            ap.error(err)
        if t not in resolved:
            resolved.append(t)
    args.types = resolved
    return args


def main():
    args = parse_args()
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
