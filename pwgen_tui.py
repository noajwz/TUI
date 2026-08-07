#!/usr/bin/env python3
"""Terminal UI random password generator."""

import curses
import secrets
import string
import subprocess

AMBIGUOUS = "Il1O0"

OPTIONS = [
    ("Uppercase (A-Z)", "upper", True),
    ("Lowercase (a-z)", "lower", True),
    ("Digits (0-9)", "digits", True),
    ("Symbols (!@#$...)", "symbols", False),
    ("Exclude ambiguous (Il1O0)", "no_ambiguous", False),
]

MIN_LEN, MAX_LEN = 4, 128


def build_charset(state):
    charset = ""
    if state["upper"]:
        charset += string.ascii_uppercase
    if state["lower"]:
        charset += string.ascii_lowercase
    if state["digits"]:
        charset += string.digits
    if state["symbols"]:
        charset += "!@#$%^&*()-_=+[]{};:,.<>?/|~"
    if state["no_ambiguous"]:
        charset = "".join(c for c in charset if c not in AMBIGUOUS)
    return charset


def generate(length, charset):
    if not charset:
        return ""
    return "".join(secrets.choice(charset) for _ in range(length))


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def strength_label(length, charset):
    pool = len(charset)
    if pool == 0 or length == 0:
        return "N/A", 0
    import math
    bits = length * math.log2(pool)
    if bits < 40:
        return "Weak", 1
    if bits < 60:
        return "Fair", 2
    if bits < 80:
        return "Good", 3
    return "Strong", 4


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title / highlights
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # password / good
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # selection
    curses.init_pair(4, curses.COLOR_RED, -1)      # weak
    curses.init_pair(5, curses.COLOR_WHITE, -1)    # dim

    state = {key: default for _, key, default in OPTIONS}
    length = 16
    selected = 0  # 0 = length row, 1..len(OPTIONS) = option rows, last = generate action
    n_rows = 1 + len(OPTIONS)
    password = generate(length, build_charset(state))
    message = ""

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, text, attr=0, h=h, w=w):
            if 0 <= y < h and x < w:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)

        title = " Password Generator "
        put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(1) | curses.A_BOLD)

        # Password display box
        box_y = 2
        put(box_y, 2, "Password:", curses.color_pair(5))
        pw_display = password if password else "(no character set selected)"
        pw_attr = curses.color_pair(2) | curses.A_BOLD if password else curses.color_pair(4)
        put(box_y + 1, 2, pw_display, pw_attr)

        label, level = strength_label(length, build_charset(state))
        bar_colors = [curses.color_pair(4), curses.color_pair(4), curses.color_pair(3),
                      curses.color_pair(2), curses.color_pair(2)]
        bar = "".join("#" if i < level else "-" for i in range(4))
        put(box_y + 2, 2, f"Strength: {label:8s} [{bar}]",
            bar_colors[level] if level < len(bar_colors) else curses.color_pair(2))

        # Controls
        y = box_y + 4
        marker = "> " if selected == 0 else "  "
        attr = curses.color_pair(3) | curses.A_BOLD if selected == 0 else 0
        put(y, 2, f"{marker}Length: < {length:3d} >", attr)
        y += 2

        for i, (label_text, key, _default) in enumerate(OPTIONS):
            row_idx = 1 + i
            marker = "> " if selected == row_idx else "  "
            box = "[x]" if state[key] else "[ ]"
            attr = curses.color_pair(3) | curses.A_BOLD if selected == row_idx else 0
            put(y, 2, f"{marker}{box} {label_text}", attr)
            y += 1

        y += 1
        gen_row = n_rows
        marker = "> " if selected == gen_row else "  "
        attr = curses.color_pair(1) | curses.A_BOLD if selected == gen_row else curses.color_pair(1)
        put(y, 2, f"{marker}[ Generate new password ]", attr)

        if message:
            put(y + 2, 2, message, curses.color_pair(2))

        footer = "up/down move  left/right adjust length  space toggle  enter generate  c copy  q quit"
        put(h - 1, 2, footer, curses.color_pair(5))

        stdscr.refresh()
        key = stdscr.getch()
        message = ""

        if key in (ord("q"), ord("Q")):
            break
        elif key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % (n_rows + 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % (n_rows + 1)
        elif key == curses.KEY_LEFT:
            if selected == 0:
                length = max(MIN_LEN, length - 1)
                password = generate(length, build_charset(state))
        elif key == curses.KEY_RIGHT:
            if selected == 0:
                length = min(MAX_LEN, length + 1)
                password = generate(length, build_charset(state))
        elif key == ord(" "):
            if 1 <= selected <= len(OPTIONS):
                opt_key = OPTIONS[selected - 1][1]
                state[opt_key] = not state[opt_key]
                password = generate(length, build_charset(state))
        elif key in (10, 13, curses.KEY_ENTER):
            password = generate(length, build_charset(state))
        elif key in (ord("c"), ord("C")):
            if password and copy_to_clipboard(password):
                message = "Copied to clipboard!"
            elif password:
                message = "Copy failed (pbcopy unavailable)."
        elif key in (ord("r"), ord("R")):
            password = generate(length, build_charset(state))


if __name__ == "__main__":
    curses.wrapper(main)
