#!/usr/bin/env python3
"""Bit explorer — a number as its bits: flip them, shift them, and see what they mean.

The bits fill the top of the window, 8, 16, 32 or 64 of them, grouped in nibbles and bytes, with
the hex digit under each nibble. Move along them and flip one with space, or type 0 and 1 to
write a pattern in. Enter takes an expression — 0xDEADBEEF, -1, 'A', "ABCD", 3.14, $ << 4 | 0b1010
— where $ is the value you have.

Below, the same bits read every way: unsigned and signed (two's complement, ones' complement,
sign and magnitude), hex, octal, the bytes in both byte orders, ASCII, the Unicode character, and
as an IEEE-754 float (half, single or double) broken into sign, exponent and mantissa, down to
the exact decimal value the bits hold. Shifts, rotates, NOT, negation, AND/OR/XOR, bit and byte
reversal are a key each, and u undoes.
"""

import argparse
import ast
import curses
import locale
import math
import operator
import struct
import subprocess
import time
import unicodedata
from decimal import Decimal

WIDTHS = (8, 16, 32, 64)
FLOATS = {16: ("e", 5, 10, "half"), 32: ("f", 8, 23, "single"), 64: ("d", 11, 52, "double")}
MIN_W, MIN_H = 80, 24
MESSAGE_SECONDS = 4
UNDO_MAX = 500
MAX_EXPR_BITS = 4096           # a result bigger than this is refused rather than computed
NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf, "nan": math.nan}
BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
          ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
          ast.LShift: operator.lshift, ast.RShift: operator.rshift, ast.BitOr: operator.or_,
          ast.BitXor: operator.xor, ast.BitAnd: operator.and_}
UNOPS = {ast.Invert: operator.invert, ast.USub: operator.neg, ast.UAdd: operator.pos}

HELP = """\
Bits          ← → or h l    move along the bits        Home/End  the top / the bottom bit
              space         flip the bit               0 1       write a bit and move right
              w W           8 → 16 → 32 → 64 bits (and back); the low bits are kept

Values        Enter         type an expression: 0xFF, 0b1010, 0o17, 1_000, -1, 'A', "ABCD",
                            3.14, 1e-3, inf, nan, pi — with + - * / // % ** << >> & | ^ ~
                            and $ for the value you have now ($ << 4, $ ^ 0xFF, ~$)
                            A float is stored as IEEE-754 bits of the current width.
              + -           add or take away one     x  all zeros     X  all ones

Operations    < >           shift left / right (logical)   a  shift right, keeping the sign
              , .           rotate left / right             ~  NOT (flip every bit)
              n             negate (two's complement)       r  reverse the bits
              b             swap the byte order             & | ^  with a value you type
              u             undo

Views         f             the float view: sign, exponent and mantissa coloured and explained
              c  C  y       copy as hex / decimal / binary

              ? this help   q quit"""


# -- reading expressions --

class Bad(Exception):
    pass


def evaluate(text, current):
    """The value of an expression: an int, or a float. Only arithmetic and bit operations on
    numbers, characters and $; nothing else of Python is reachable."""
    src = text.replace("$", " _v ")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as e:
        raise Bad(f"can't read that: {e.msg}") from None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or v is None:
                raise Bad("only numbers and characters")
            if isinstance(v, (int, float)):
                return v
            if isinstance(v, str):
                if not v:
                    raise Bad("an empty string has no bits")
                if len(v) == 1:
                    return ord(v)
                return int.from_bytes(v.encode("utf-8"), "big")
            if isinstance(v, bytes):
                return int.from_bytes(v, "big")
            raise Bad("only numbers and characters")
        if isinstance(node, ast.Name):
            if node.id == "_v":
                return current
            if node.id in NAMES:
                return NAMES[node.id]
            raise Bad(f"unknown name {node.id} — $ is the value you have")
        if isinstance(node, ast.UnaryOp) and type(node.op) in UNOPS:
            x = ev(node.operand)
            if isinstance(node.op, ast.Invert) and isinstance(x, float):
                raise Bad("~ needs a whole number")
            return UNOPS[type(node.op)](x)
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            a, b = ev(node.left), ev(node.right)
            op = type(node.op)
            if op in (ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd) and (
                    isinstance(a, float) or isinstance(b, float)):
                raise Bad("bit operations need whole numbers")
            if op is ast.LShift and b > MAX_EXPR_BITS or op is ast.Pow and isinstance(a, int) and isinstance(
                    b, int) and abs(a) > 1 and b * max(1, abs(a).bit_length()) > MAX_EXPR_BITS:
                raise Bad("that number is far too big")
            try:
                return BINOPS[op](a, b)
            except ZeroDivisionError:
                raise Bad("division by zero") from None
            except (OverflowError, ValueError) as e:
                raise Bad(str(e)) from None
        raise Bad("only numbers, characters, $ and + - * / // % ** << >> & | ^ ~")
    return ev(tree)


# -- the bits --

def to_float(v, w):
    """The float these bits are, for 16, 32 or 64 bits."""
    code = FLOATS[w][0]
    return struct.unpack(">" + code, v.to_bytes(w // 8, "big"))[0]


def from_float(x, w):
    code = FLOATS[w][0]
    return int.from_bytes(struct.pack(">" + code, x), "big")


def fmt_group(digits, n):
    """Underscores every n digits from the right, as Python writes them."""
    out = []
    while digits:
        out.append(digits[-n:])
        digits = digits[:-n]
    return "_".join(reversed(out)) or "0"


def float_parts(v, w):
    _, ebits, mbits, _ = FLOATS[w]
    sign = v >> (w - 1)
    exp = (v >> mbits) & ((1 << ebits) - 1)
    man = v & ((1 << mbits) - 1)
    return sign, exp, man, ebits, mbits, (1 << (ebits - 1)) - 1


def float_kind(v, w):
    sign, exp, man, ebits, mbits, bias = float_parts(v, w)
    if exp == (1 << ebits) - 1:
        if man == 0:
            return "infinity"
        return "NaN (quiet)" if man >> (mbits - 1) else "NaN (signalling)"
    if exp == 0:
        return "zero" if man == 0 else "subnormal"
    return "normal"


def exact(x, digits=40):
    """The exact decimal value of a float, shortened if very long."""
    if math.isnan(x) or math.isinf(x):
        return str(x)
    s = format(Decimal(x), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if len(s) > digits:
        d = Decimal(x)
        s = f"{d:.{digits - 8}e}"
    return s


def show_float(x):
    if math.isnan(x):
        return "nan"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    return repr(x)


class Bits:
    def __init__(self, v=0, w=32):
        self.v, self.w = v, w

    @property
    def mask(self):
        return (1 << self.w) - 1

    def signed(self, v=None):
        v = self.v if v is None else v
        return v - (1 << self.w) if v >> (self.w - 1) else v

    def reverse(self):
        return int(format(self.v, f"0{self.w}b")[::-1], 2)

    def byteswap(self):
        return int.from_bytes(self.v.to_bytes(self.w // 8, "big"), "little")

    def rotate(self, k):
        k %= self.w
        return ((self.v << k) | (self.v >> (self.w - k))) & self.mask


def readings(b):
    """The bits read as numbers, bytes and a character: [(label, value, colour)], the colour a
    curses pair number, 0 for none, or "bold"."""
    v, w = b.v, b.w
    sv = b.signed()
    raw = v.to_bytes(w // 8, "big")
    rows = [("unsigned", f"{v:,}", 0), ("signed", f"{sv:,}", 0),
            ("hex", "0x" + fmt_group(format(v, f"0{w // 4}X"), 4), 3),
            ("octal", "0o" + format(v, "o"), 0),
            ("bytes, big end", " ".join(f"{x:02X}" for x in raw), 0),
            ("bytes, little end", " ".join(f"{x:02X}" for x in raw[::-1]), 0),
            ("as ASCII", "".join(chr(x) if 32 <= x < 127 else "·" for x in raw), 0)]
    if v <= 0x10FFFF and not 0xD800 <= v <= 0xDFFF:
        ch = chr(v)
        name = unicodedata.name(ch, "") or {0: "NULL", 9: "TAB", 10: "LINE FEED", 13: "CARRIAGE RETURN",
                                              32: "SPACE", 127: "DELETE"}.get(v, "")
        shown = ch if ch.isprintable() and not unicodedata.combining(ch) else " "
        utf8 = " ".join(f"{x:02X}" for x in ch.encode("utf-8", "surrogatepass"))
        rows.append(("character", f"U+{v:04X} {shown}  {name.lower()}  (UTF-8 {utf8})", 5))
    else:
        rows.append(("character", "beyond Unicode", 8))
    if w in FLOATS:
        x = to_float(v, w)
        rows.append((f"as {FLOATS[w][3]} float", f"{show_float(x)}  ({float_kind(v, w)})", 6))
    return rows

def float_readings(b):
    """The bits taken apart as an IEEE-754 float."""
    v, w = b.v, b.w
    sign, exp, man, ebits, mbits, bias = float_parts(v, w)
    x = to_float(v, w)
    kind = float_kind(v, w)
    rows = [("sign", f"{sign}  ({'negative' if sign else 'positive'})", 1),
            ("exponent", f"{exp} raw", 3),
            ("mantissa", f"0x{man:0{(mbits + 3) // 4}X} / 2^{mbits}", 6)]
    if kind == "normal":
        frac = Decimal(man) / Decimal(1 << mbits)
        rows[1] = ("exponent", f"{exp} raw − bias {bias} = {exp - bias}", 3)
        rows.append(("formula", f"{'−' if sign else '+'}1.{str(frac)[2:12] or '0'}… × 2^{exp - bias}", 0))
    elif kind == "subnormal":
        rows[1] = ("exponent", f"0: subnormal, so 2^{1 - bias} and no hidden 1", 3)
        rows.append(("formula", f"{'−' if sign else '+'}0.{man / (1 << mbits):.10f}… × 2^{1 - bias}"[0:60], 0))
    else:
        rows.append(("formula", {"zero": "all exponent and mantissa bits clear: zero",
                                  "infinity": "exponent all ones, mantissa zero: infinity"}.get(
            kind, "exponent all ones, mantissa not zero: not a number"), 0))
    rows += [("value", f"{show_float(x)}  ({kind})", "bold"),
             ("exactly", exact(x), 0)]
    if kind in ("normal", "subnormal") and x:
        nxt = v - 1 if sign else v + 1          # toward +∞: smaller in size below zero, bigger above
        up = to_float(nxt, w)
        gap = abs(up - x) if not math.isinf(up) else 0
        rows.append(("next float up", f"{show_float(up)}  (+{gap:.2g})", 8))
    return rows

def facts(b):
    """Counts and transformations of the bits."""
    v, w = b.v, b.w
    pop = bin(v).count("1")
    lead = w - v.bit_length()
    trail = (v & -v).bit_length() - 1 if v else w
    sv = b.signed()
    ones = sv if sv >= 0 else -(~v & b.mask)
    signmag = (v & (b.mask >> 1)) * (-1 if v >> (w - 1) else 1)
    power = f"yes, 2^{v.bit_length() - 1}" if v and not v & (v - 1) else "no"
    nxt = 1 << (v - 1).bit_length() if v > 1 else 1
    width = len(format(b.mask, "X"))
    rows = [("set bits", f"{pop} of {w}", 2),
            ("leading zeros", str(lead), 0), ("trailing zeros", str(trail), 0),
            ("highest bit", str(v.bit_length() - 1) if v else "none", 0),
            ("parity", "even" if pop % 2 == 0 else "odd", 0),
            ("power of two", power, 0),
            ("next power of 2", f"{nxt:,}" if nxt <= b.mask else "beyond this width", 0),
            ("multiple of", f"{1 << trail:,}" if v else "everything", 0),
            ("ones' complement", f"{ones:,}", 8),
            ("sign & magnitude", f"{signmag:,}", 8),
            ("Gray code", f"0x{v ^ (v >> 1):0{width}X}", 8),
            ("bits reversed", f"0x{b.reverse():0{width}X}", 8),
            ("bytes swapped", f"0x{b.byteswap():0{width}X}", 8)]
    return rows


# -- the window --

class App:
    def __init__(self, stdscr, bits):
        self.s = stdscr
        self.b = bits
        self.cur = bits.w - 1                 # the bit the cursor is on: 0 is the lowest
        self.float_view = False
        self.undo = []
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.last_expr = ""

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def warn(self, text):
        self.say(text, curses.color_pair(1))

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        room = w - x - (1 if y == h - 1 else 0)
        if width is not None:
            room = min(room, width)
            text = text[:room].ljust(room)
        try:
            self.s.addstr(y, x, text[:max(0, room)], attr)
        except curses.error:
            pass

    # -- changing the value --

    def set(self, v, why=None):
        b = self.b
        if v == b.v:
            return
        self.undo.append((b.v, b.w))
        del self.undo[:-UNDO_MAX]
        b.v = v & b.mask
        if why:
            self.say(why)

    def store(self, x, label):
        """Puts an expression's result in: whole numbers in two's complement, floats as IEEE bits."""
        b = self.b
        if isinstance(x, float):
            if b.w not in FLOATS:
                self.warn("a float needs 16, 32 or 64 bits — w changes the width")
                return
            try:
                v = from_float(x, b.w)
            except (OverflowError, struct.error):
                self.warn(f"{x} is too big for a {FLOATS[b.w][3]}-precision float")
                return
            self.set(v)
            self.float_view = True
            back = to_float(v, b.w)
            note = "" if back == x or math.isnan(x) else f" — the nearest {FLOATS[b.w][3]} float is {show_float(back)}"
            self.say(f"{label} = {show_float(x)} as {b.w}-bit float{note}")
            return
        fits = -(1 << (b.w - 1)) <= x <= b.mask
        self.set(x & b.mask)
        if not fits:
            self.warn(f"{label} = {x:,} doesn't fit in {b.w} bits — the lowest {b.w} are kept")
        else:
            self.say(f"{label} = {x:,}")

    def ask(self, label, initial=""):
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                self.draw(footer=False)
                h, w = self.s.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(6) | curses.A_REVERSE, w - 1)
                self.s.move(h - 1, min(len(text), w - 2))
                self.s.refresh()
                try:
                    key = self.s.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf).strip()
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":
                        buf = []
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf).strip()
        finally:
            curses.curs_set(0)

    def enter(self):
        text = self.ask(" value or expression ($ is the current one): ", self.last_expr)
        if not text:
            return
        self.last_expr = text
        try:
            x = evaluate(text, self.b.v)
        except Bad as e:
            self.warn(str(e))
            return
        self.store(x, text)

    def with_operand(self, symbol):
        text = self.ask(f" $ {symbol} ")
        if not text:
            return
        try:
            y = evaluate(text, self.b.v)
        except Bad as e:
            self.warn(str(e))
            return
        if isinstance(y, float):
            self.warn(f"{symbol} needs a whole number")
            return
        op = {"&": operator.and_, "|": operator.or_, "^": operator.xor}[symbol]
        self.set(op(self.b.v, y & self.b.mask), f"$ {symbol} {text}")

    # -- drawing --

    def layout_bits(self, avail):
        """(cell width, x of each bit from the left): wide cells when there's room, and gaps
        between nibbles and bytes."""
        w = self.b.w
        for cw in (5, 4, 3, 2, 1):
            nib, byte = (1, 2) if cw > 1 else (0, 1)
            total = w * cw + (w // 4 - 1) * nib + (w // 8 - 1) * (byte - nib)
            if total <= avail:
                break
        xs = []
        for k in range(w):                    # k = 0 is the top bit, drawn leftmost
            xs.append(k * cw + (k // 4) * nib + (k // 8) * (byte - nib))
        return cw, xs, total

    def field(self, i):
        """Which part of a float bit i is: 'sign', 'exponent' or 'mantissa'."""
        w = self.b.w
        if i == w - 1:
            return "sign"
        return "exponent" if i >= FLOATS[w][2] else "mantissa"

    def draw(self, footer=True):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        b = self.b
        floaty = self.float_view and b.w in FLOATS
        view = f"{b.w} bits · " + (f"{FLOATS[b.w][3]}-precision float view" if floaty else "integer view")
        self.put(0, 1, "Bit explorer", curses.color_pair(6) | curses.A_BOLD)
        self.put(0, w - len(view) - 2, view, curses.color_pair(8))
        y = self.draw_grid(2, w, floaty)
        self.draw_info(y + 1, w, floaty)
        if footer:
            self.draw_footer(h - 1, w)
        s.refresh()

    def draw_grid(self, y0, w, floaty):
        b = self.b
        cw, xs, total = self.layout_bits(w - 4)
        x0 = (w - total) // 2
        colours = {"sign": 1, "exponent": 3, "mantissa": 6}
        if floaty:
            spans = {}
            for k in range(b.w):
                spans.setdefault(self.field(b.w - 1 - k), []).append(xs[k])
            for name, pos in spans.items():
                label = {"sign": "±", "exponent": "exponent", "mantissa": "mantissa (fraction)"}[name]
                left, right = pos[0], pos[-1] + cw
                label = label if len(label) <= right - left else label[:max(1, right - left)]
                self.put(y0, x0 + left + (right - left - len(label)) // 2, label,
                         curses.color_pair(colours[name]) | curses.A_BOLD)
        for k in range(b.w):                  # bit numbers
            i = b.w - 1 - k
            if cw >= 3:
                self.put(y0 + 1, x0 + xs[k], str(i).center(cw), curses.color_pair(8))
            elif cw == 2 and i % 4 == 3:          # too narrow to number each: the top of each nibble
                self.put(y0 + 1, x0 + xs[k], str(i), curses.color_pair(8))
            elif i % 8 == 7:
                self.put(y0 + 1, x0 + xs[k], str(i), curses.color_pair(8))
        for k in range(b.w):                  # the bits themselves
            i = b.w - 1 - k
            bit = (b.v >> i) & 1
            text = ("1" if bit else "0").center(cw)
            if bit:
                attr = curses.A_REVERSE | curses.A_BOLD | (curses.color_pair(colours[self.field(i)]) if floaty else 0)
            else:
                attr = curses.color_pair(colours[self.field(i)]) if floaty else curses.color_pair(8)
            if i == self.cur:
                attr = curses.color_pair(9) | curses.A_BOLD
            self.put(y0 + 2, x0 + xs[k], text, attr)
            if cw >= 3:
                self.put(y0 + 3, x0 + xs[k], ("▀" if i == self.cur else " ") * cw, curses.color_pair(3))
        hy = y0 + (4 if cw >= 3 else 3)
        for k in range(0, b.w, 4):            # the hex digit under each nibble
            i = b.w - 1 - k
            nib = (b.v >> (i - 3)) & 0xF
            left, right = xs[k], xs[k + 3] + cw
            self.put(hy, x0 + left + (right - left - 1) // 2, "0123456789ABCDEF"[nib], curses.color_pair(3) | curses.A_BOLD)
        i = self.cur
        info = f"bit {i}  ·  2^{i} = {1 << i:,}  ·  byte {i // 8}, bit {i % 8} in it  ·  nibble {i // 4}"
        if floaty:
            sign, exp, man, ebits, mbits, bias = float_parts(b.v, b.w)
            f = self.field(i)
            if f == "sign":
                info = f"bit {i}: the sign — 1 is negative"
            elif f == "exponent":
                info = f"bit {i}: exponent bit {i - mbits}, worth 2^{i - mbits} = {1 << (i - mbits)} in the exponent"
            else:
                info = f"bit {i}: mantissa bit, worth 2^-{mbits - i} of the significand"
        self.put(hy + 1, max(1, (w - len(info)) // 2), info, curses.color_pair(3))
        return hy + 2

    def draw_info(self, y0, w, floaty):
        h = self.s.getmaxyx()[0]
        rows_l = float_readings(self.b) + [("", "", 0)] + readings(self.b)[:4] if floaty else readings(self.b)
        rows_r = facts(self.b)
        right_w = 38 if w >= 100 else 34
        left_w = w - right_w - 3
        label_l = 18
        def attr(colour):
            return curses.A_BOLD if colour == "bold" else curses.color_pair(colour)
        for n, (label, value, colour) in enumerate(rows_l):
            y = y0 + n
            if y >= h - 1:
                break
            self.put(y, 1, label, curses.color_pair(8), label_l)
            self.put(y, 1 + label_l, value, attr(colour), left_w - label_l)
        x = w - right_w - 1
        for n, (label, value, colour) in enumerate(rows_r):
            y = y0 + n
            if y >= h - 1:
                break
            self.put(y, x, label, curses.color_pair(8), 17)
            self.put(y, x + 17, value, attr(colour), right_w - 17)

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 1, self.message, self.message_attr, w - 2)
            return
        keys = ["⏎ value", "␣ flip", "0 1 write", "w width", "< > shift", ", . rotate", "~ not", "n negate",
                "& | ^", "f float", "u undo", "? help", "q quit"]
        while len(keys) > 1 and len("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(y, 1, "  ".join(keys), curses.color_pair(8))

    def help(self):
        self.s.erase()
        for n, line in enumerate(HELP.split("\n")):
            self.put(1 + n, 2, line)
        self.put(len(HELP.split("\n")) + 2, 2, "any key to close", curses.color_pair(8))
        self.s.refresh()
        self.s.getch()

    def copy(self, what):
        b = self.b
        text = {"hex": f"0x{b.v:0{b.w // 4}X}", "decimal": str(b.v), "binary": f"0b{b.v:0{b.w}b}"}[what]
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {text}")
        except (OSError, subprocess.CalledProcessError):
            self.warn("couldn't copy — pbcopy not found")

    # -- keys --

    def handle(self, ch):
        b = self.b
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in (curses.KEY_LEFT, ord("h")):
            self.cur = min(b.w - 1, self.cur + 1)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.cur = max(0, self.cur - 1)
        elif ch in (curses.KEY_UP, ord("k")):
            self.cur = min(b.w - 1, self.cur + 8)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cur = max(0, self.cur - 8)
        elif ch == curses.KEY_HOME:
            self.cur = b.w - 1
        elif ch == curses.KEY_END:
            self.cur = 0
        elif ch == ord(" "):
            self.set(b.v ^ (1 << self.cur))
        elif ch in (ord("0"), ord("1")):
            bit = 1 << self.cur
            self.set(b.v | bit if ch == ord("1") else b.v & ~bit)
            self.cur = max(0, self.cur - 1)
        elif ch in (10, 13, curses.KEY_ENTER, ord("=")):
            self.enter()
        elif ch in (ord("+"),):
            self.set(b.v + 1)
        elif ch in (ord("-"),):
            self.set(b.v - 1)
        elif ch == ord("<"):
            self.set(b.v << 1, "shifted left")
        elif ch == ord(">"):
            self.set(b.v >> 1, "shifted right")
        elif ch == ord("a"):
            self.set(b.signed() >> 1, "shifted right, keeping the sign")
        elif ch == ord(","):
            self.set(b.rotate(1), "rotated left")
        elif ch == ord("."):
            self.set(b.rotate(-1), "rotated right")
        elif ch == ord("~"):
            self.set(~b.v, "every bit flipped")
        elif ch == ord("n"):
            self.set(-b.v, "negated: flip every bit and add one")
        elif ch == ord("r"):
            self.set(b.reverse(), "bits reversed")
        elif ch == ord("b"):
            self.set(b.byteswap(), "bytes swapped — the other byte order")
        elif ch == ord("x"):
            self.set(0)
        elif ch == ord("X"):
            self.set(b.mask)
        elif ch in (ord("&"), ord("|"), ord("^")):
            self.with_operand(chr(ch))
        elif ch in (ord("w"), ord("W")):
            i = WIDTHS.index(b.w)
            nw = WIDTHS[(i + (1 if ch == ord("w") else -1)) % len(WIDTHS)]
            if b.v > (1 << nw) - 1:
                self.warn(f"only the lowest {nw} bits fit — u brings the rest back")
            self.undo.append((b.v, b.w))
            b.w, b.v = nw, b.v & ((1 << nw) - 1)
            self.cur = min(self.cur, nw - 1)
        elif ch == ord("u"):
            if self.undo:
                b.v, b.w = self.undo.pop()
                self.cur = min(self.cur, b.w - 1)
                self.say("undone")
            else:
                self.say("nothing to undo")
        elif ch == ord("f"):
            if b.w not in FLOATS:
                self.warn("floats need 16, 32 or 64 bits — w changes the width")
            else:
                self.float_view = not self.float_view
        elif ch == ord("c"):
            self.copy("hex")
        elif ch == ord("C"):
            self.copy("decimal")
        elif ch == ord("y"):
            self.copy("binary")
        elif ch == ord("?"):
            self.help()
        return True


def run(stdscr, bits, start, args):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW)     # the cursor
    stdscr.keypad(True)
    app = App(stdscr, bits)
    app.float_view = args.float
    if start is not None:
        app.store(start, args.value)
        app.undo.clear()
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            continue
        if not app.handle(ch):
            return


def main():
    parser = argparse.ArgumentParser(
        description="Explore a number as bits: flip, shift and rotate them, and read them as integers, "
                    "characters and IEEE-754 floats.")
    parser.add_argument("value", nargs="?", help="a value or expression to start with: 0xFF, -1, 'A', 3.14 …")
    parser.add_argument("-w", "--width", type=int, choices=WIDTHS, default=32, help="bits wide (default 32)")
    parser.add_argument("-f", "--float", action="store_true", help="start in the float view")
    parser.add_argument("--print", action="store_true", help="print the readings and exit, without the window")
    args = parser.parse_args()
    bits = Bits(0, args.width)
    start = None
    if args.value:
        try:
            start = evaluate(args.value, 0)
        except Bad as e:
            parser.error(str(e))
    if args.print:
        if isinstance(start, float):
            if args.width not in FLOATS:
                parser.error("a float needs 16, 32 or 64 bits")
            bits.v = from_float(start, args.width)
        elif start is not None:
            bits.v = start & bits.mask
        print(f"0b{fmt_group(format(bits.v, f'0{bits.w}b'), 4)}")
        rows = readings(bits) + facts(bits)
        if bits.w in FLOATS and (args.float or isinstance(start, float)):
            rows += float_readings(bits)
        for label, value, _ in rows:
            print(f"{label:<18}{value}")
        return
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, bits, start, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
