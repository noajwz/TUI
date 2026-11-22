---
paths:
  - "spiro_tui.py"
---

# spiro_tui.py

A spirograph. A wheel of `r` teeth rolls inside (hypotrochoid) or outside (epitrochoid) a ring of
`R` teeth, with the pen at `hole × r` from the wheel's centre. More than 1 puts the pen past the
rim, which the real toy can't do. The pen draws the curve live, with the ring, the wheel and its
pen arm drawn over the paper. Designs stack in layers, coloured by layer, by a rainbow along the
curve, or plain. `--once` prints a finished design as braille, in 24-bit colour on a terminal.

- **Teeth are integers on purpose.** `Design.turns = r / gcd(R, r)` is how often the wheel goes
  round the ring before the curve closes, and `loops = R / gcd(R, r)`. So the drawing has an
  exact end (`period`), and the panel can say what a design will make before it is drawn.
  `random_numbers()` keeps choosing until a design goes round 2 to 15 times. Two numbers sharing
  no factor give `turns = r`, a solid disc of ~100 loops, which is fun once but not as the usual
  random pick. Presets 5 and 9 are dense like that on purpose.
- **`Paper` is braille**, 2×4 dots a character as in `clock_tui`, plus a colour per cell: the
  last ink to touch it. The wheel overlay is drawn each frame into separate bit and colour
  dicts and OR'd over the paper. It is never inked, so it disappears when the drawing is done
  or `w` hides it.
- Points are `DT = 2π / STEPS_PER_TURN` apart along the curve, joined by short lines, so fast
  speeds leave no gaps. Auto speed draws any design in about `AUTO_SECONDS`.
- The scale fits the biggest design on the paper (`extent()`). Adding a layer or resizing redraws
  every layer from scratch at the new scale (`fit`, `redraw`), since ink is pixels and can't be
  rescaled. Changing the numbers (`g r p o`, Enter, presets) restarts only the current design;
  `a` keeps it as a layer and starts another; `x` clears them all.

Testing: `--once` with the default (96/36/0.8: 3 times round, 8 loops) and with `--outside --ring
60 --wheel 24` (5 lobes). Check that every preset's pen is back at its start after `period`
(`math.dist(pen(0), pen(period)) < 1e-6`), and that `random_numbers()` over 500 draws stays
within 2 to 15 turns and never puts an inside wheel at or above the ring. In tmux: 110×34 (watch
the wheel roll), `f`, `a`, `7`, a resize to the 60×16 minimum (the panel hides, the layers are
redrawn), and `-d` for a dozen seconds with `TERM=xterm` (8 colours).
