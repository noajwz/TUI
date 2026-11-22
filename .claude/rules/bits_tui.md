---
paths:
  - "bits_tui.py"
---

# bits_tui.py

A number as its bits, at 8, 16, 32 or 64 bits wide: a grid to move along and flip, every way of
reading the bits, and a key for each bit operation. `--print` writes the same readings without
the window.

- **The value is the raw bits.** `Bits.v` is an unsigned int masked to `Bits.w`. Everything else
  is computed from it: signed, the float, the bytes, the character. Whole-number results go in as
  two's complement and are truncated to the width, with a message when they didn't fit. Floats
  go in as their IEEE-754 bits (`struct` with `e`/`f`/`d`, so 16 bits is half precision), and
  8 bits refuses them. Changing width keeps the low bits; undo (`u`, the stack of
  `(value, width)`) brings back the rest.
- **`evaluate()` is a whitelist walk over `ast`**: number and string constants (one character is
  its code point, longer strings are their UTF-8 bytes read big-endian), `$` (rewritten to `_v`),
  `pi e tau inf nan`, and arithmetic and bit operators. Nothing else is evaluated: no calls,
  attributes, names or comprehensions. `MAX_EXPR_BITS` refuses `2**100000` and `1 << 10**9`
  before they are computed.
- `readings()`, `float_readings()` and `facts()` are plain functions returning
  `(label, value, colour)`, where the colour is a pair number or `"bold"`. That's so `--print`
  works without curses: they used to call `curses.color_pair` and crashed outside the window.
- The grid (`layout_bits`) picks the widest cell, 5 down to 1, with gaps between nibbles and
  bytes, that fits the window: 64 bits at 80 columns is 1 a bit. Bit numbers go on every bit
  when cells are 3 or wider, on each nibble's top bit at 2, and each byte's at 1, since narrower
  numbers run together. Left is the top bit; the cursor is a bit index (0 the lowest).
- The float view colours sign, exponent and mantissa. It shows the raw and unbiased exponent,
  the formula, the class (normal, subnormal, zero, infinity, quiet or signalling NaN), the exact
  decimal value (`Decimal(float)`), and the next float toward +∞. That's the bits minus one below
  zero, plus one above.

Testing: `--print` with `0xDEADBEEF` (−559,038,737 signed; −6.2598534e18 as a float), `3.14`
(exactly 3.1400001049041748046875 as single), `-w 8 -- -1`, and `"'😀'"` (U+1F600, F0 9F 98 80).
Call `evaluate` on the forbidden forms (`__import__('os')`, `(1).bit_length()`, `lambda`,
`2**100000`, `1/0`, `3.5 & 1`) and round-trip `from_float`/`to_float` at 16, 32 and 64 bits
through zero, −0, a subnormal, the largest finite value and infinity. In tmux, at 100×28 and
80×24: type `1 0 1 1` into 8 bits (176), then `< < ~ n` (193), `u` four times, `^` `0xF0`
(64), `w` through the widths, Enter `-2.5` (pass `-l --` to tmux so it isn't read as a flag).
