---
paths:
  - "cascade_tui.py"
---

# cascade_tui.py

Cascade, a falling-block puzzle: four-cell pieces in a 10×20 well, with a ghost, hold (once per
piece), the next three, and lock delay. Three modes:

- marathon: faster every 10 rows;
- sprint: 40 rows against the clock;
- cascade: after a clear, loose clumps fall and can chain.

The best ten per mode are kept in `~/.local/state/cascade_tui/scores.json`; `--scores` prints
them.

- **It is its own game, not a copy of Tetris**, which the request named. The name, the piece
  names (`bar`, `square`, `tee`, `step`, `stair`, `hook`, `crook`), the colours, the rotation
  (turn in a box, then try `KICKS`), the scoring and the cascade mode are this game's own. Keep
  it that way: don't add another game's name, rotation tables, colours or music.
- `Game` has no screen. The window calls `shift`, `turn`, `soft_drop`, `hard_drop`,
  `hold_piece` and `toggle_pause`, plus `update(now)` every tick, and draws `grid`, `piece`,
  `queue` and `hold`. That's what lets a bot play it headless.
- Rows 0–1 of `grid` are above the well (`HIDDEN`), where pieces appear. A piece that can't
  appear, or locks entirely up there, ends the game.
- **Lock delay**: resting on something starts `LOCK_DELAY`, and each move or turn restarts it, at
  most `LOCK_RESETS` times (`touched()`). Pieces fall one row every `interval()`: 0.8 s × 0.84^(level − 1).
- **Clearing**: full rows flash for `FLASH` (`flashing`), then go. In cascade mode `settle()`
  then drops every connected clump of blocks (by side or by top), lowest first, as a solid
  piece, until nothing moves. New full rows clear as the next link of the chain, each worth
  `× chain`. `combo` counts consecutive pieces that cleared something.
- Pieces come from a shuffled bag of the seven, so each appears once in every seven.
- The play loop waits a tick for a key, then takes every key that arrived at once, so held-down
  keys keep up.

Testing: import it and check that:

- each shape has four rotations of four cells, and four turns come back;
- a fresh bag (`g.bag = []` first, since the preview already drew from it) gives each shape once
  per seven;
- hold works once per piece, and again for the next;
- an upright bar into a one-cell gap clears a row.

For a cascade chain: a bottom row missing column 4, the row above missing columns 0, 1 and 4,
and two blocks hanging at columns 0–1 two rows up; an upright bar in column 4 must give rows 2,
best chain 2. A random bot through every mode must finish or top out without errors. In tmux
with `HOME=<scratch>`: the menu, a few moves, hold, slamming until the well fills (the banner
and "a new best!"), the scores file, `m` for the menu, sprint, `p`, and the 56×24 minimum, where
the keys move to the pause screen.
