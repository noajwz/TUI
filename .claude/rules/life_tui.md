---
paths:
  - "life_tui.py"
---

# life_tui.py

Conway's Game of Life, and nine other B/S rules (HighLife, Seeds, Day & Night, …), filling the
window:

- three zooms: braille (2×4 cells a character), half blocks (1×2, so square cells), or full;
- a cursor, mouse drawing, and a pattern library stamped as a movable, turnable ghost;
- RLE and plain-text files, with the rule taken from the RLE header;
- run, step and rewind, with green for cells just born;
- it notices when the world has died or settled into a still life or an oscillator.

- **The engine is bit-parallel.** `World.rows[y]` is one int, bit x being the cell (x, y).
  `step()` makes the eight neighbour planes from shifted copies of the rows above, at and below:
  rotations when the edges wrap, plain shifts (with the edge falling off) when they don't. It
  adds them with full adders into a 4-bit count: `ones twos fours eights`. Conway is
  `twos & ~fours & ~eights & (ones | alive)`; other rules OR together a mask equal to each
  birth count on dead cells and each survival count on live ones. A random 400×400 world takes
  well under a millisecond a generation. Remember `~x` on a Python int is negative: mask it.
- The world is the window's size at the current zoom. A resize or zoom change makes a new world
  with the same cells, centred (`World.resized`), and drops the rewind history.
- `fresh[y]` holds the cells born in the last step, for the green; an edit clears it.
- Cycle spotting hashes `tuple(rows)` for the last `CYCLE_WINDOW` generations. A repeat is
  "a still life", "oscillating with period p" or "everything died". A spaceship never exactly
  repeats on a finite world that doesn't wrap, and on one that does only after crossing it. Edits
  and rule changes forget what was seen.
- `u` rewinds from `HISTORY` saved copies of the rows: each step and each edit saves one, so it
  undoes edits too.
- `parse_cells` takes plain text (`O`/`*` alive, `!`/`#` comments) or RLE, judged by its ending
  `!` and run characters. `load_file` also reads `rule = B…/S…`.
- The mouse: a click toggles, and a drag paints with the state the first click made, so dragging
  over live cells erases. Drags need `\033[?1002h` (button-held motion), which `run` turns on
  and off.

Testing: compare `World.step` with a naive neighbour count on ~60 random worlds of random sizes,
across all of `RULES`, wrapping and not. It must match exactly. Also check that the diehard
reaches 0 cells at generation 130 on a 200×200 world without wrapping, that a glider moves
(1, 1) in 4 generations, that blinker, pulsar, pentadecathlon and block repeat after 2, 3, 15
and 1, and that the Gosper gun's RLE is 36 cells. In tmux at 100×30:

- `-p gosper` fires gliders;
- `-p pulsar -z full` reports "period 3";
- `p`, then a pattern, then `r` and Enter stamps it;
- `z` goes to braille, and `R` runs random;
- `-f x.rle` with a rule in the header shows that rule;
- `n n n n u u` gives generation 2.

The mouse was not tested under tmux.
