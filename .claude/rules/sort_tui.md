---
paths:
  - "sort_tui.py"
---

# sort_tui.py

Sorting algorithms animated as bars, like the videos: 37 of them, a race mode (nine side by side
on the same input), demo mode, input shapes, bars or dots, plain or rainbow colours.

- **Every algorithm is a generator over `Arr`.** It yields after each comparison or write, so a
  step is one operation. That makes the step counts comparable between algorithms and the races
  fair. `Arr` counts comparisons, swaps, writes and reads, and fills `hot` with the indices each
  step touched (`cmp` red, `write` green, `pivot` yellow, `read` blue). `Runner.advance(k)` merges
  `hot` over the k steps of a frame, with comparisons winning.
- Recursive sorts are nested generators with `yield from`; `circle` even returns a value that way.
  Values held aside (merge's left half, a pivot value) are compared with `cmp_v(x, i)`.
- Pacing: `SPEEDS` is steps per frame. Fractions step every few frames (`Runner.credit`).
  `auto` divides `estimate()`, a rough step count from the algorithm's average complexity, by
  the frames in `AUTO_SECONDS`, so each sort takes about 9 s whatever its speed. A race paces by
  its first entry. Demo mode moves on `DEMO_PAUSE` after the green check, skipping joke sorts
  whose `limit` (the bar count above which they won't finish) is too low.
- When a sort ends, `Runner.ok` checks the result, and the green sweep (`sweep`) runs across it.
  If it's wrong it turns red and says "that's a bug", so a broken new algorithm shows itself.
- Bars use eighth blocks (`BLOCKS`) for the top cell; dots use half blocks, for twice the height
  resolution. Rainbow colours are 36 pairs from 16 up, along the hue circle in the 256-colour
  cube, or the 6 basic colours otherwise. Resizing below the bar count drops to a size that fits
  and starts over.

Testing: import the module and run every algorithm through `Runner` to the end on every shape in
`SHAPES`, at sizes 1, 2, 3, 8, 13, 64, 100 and 257 (smaller for joke sorts with a `limit`). The
result must be sorted and a permutation of the input. All 37 do this in about a second. A table of
steps at 100 random bars is a good sanity check: counting ≈ 2n, quicksorts ≈ 6n, bubble ≈ n²/2,
cycle sort writing fewer than n times. The window works in tmux at 120×32 (watch `r` race to
the end), at the 60×14 minimum, and with `TERM=xterm` for 8 colours.
