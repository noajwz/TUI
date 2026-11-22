---
paths:
  - "clock_tui.py"
---

# clock_tui.py

An analog clock with the date beside it:

- the date, ISO week and day of the year;
- the quarter and week of the month;
- UTC, the offset and DST, and Unix time;
- the moon, and sunrise and sunset;
- how far through the day, week, month and year it is;
- the month's calendar with ISO week numbers, and world clocks.

`--once` prints a small face and the facts as text.

- **The face is braille** (`Canvas`): each character is 2×4 dots, and a terminal cell is about
  twice as tall as wide, so a dot is about square and the circle comes out round. Each dot
  belongs to a layer (`LAYERS`), and the highest layer in a cell picks its colour, so the
  second hand is red wherever it crosses. Numerals are text laid over the dots, except where a
  hand passes. `draw_face()` returns `{(col, row): (text, layer)}` and knows nothing of curses,
  which is what lets `--once` print it.
- Ticking (the default) sleeps until the next whole second (`wait_ms`), so the hand moves on the
  second. `s` sweeps it with a redraw every `SMOOTH_MS`.
- The layout keeps `PANEL_W` (36) columns beside the clock. The clock shrinks to make room, and
  only below 24 columns does the clock show alone. Panel sections are dropped whole when they
  don't fit, the later ones first: head, sky, progress bars, calendar, world clocks.
- **The sky is computed, offline.** The moon is the age since a known new moon (2000-01-06
  18:14 UTC) modulo the synodic month; the illuminated share is (1 − cos)/2. Sunrise and sunset
  use the almanac algorithm (`sun_event`), which is good to a minute or two and returns "up" or
  "down" in polar summer and winter. The place comes from `--lat/--lon`, else from the Mac's
  time zone (`/etc/localtime` → `CITIES`, a few dozen zones); without either there is no sun
  line.
- `z` moves the face through the world clocks and back to local time. The panel always shows
  local time.

Testing: `--once`. Then call `sun_times` for Amsterdam on 21 June (≈05:17/22:06) and 21 December
(≈08:48/16:28), and for Tromsø at both solstices (`up`/`down`). Call `moon()` at known full moons
(2024-04-23 23:49 UTC, 2025-01-13 22:27 UTC) and the new moon of 2024-04-08 18:21 UTC: the lit
share should be ≈1 and ≈0. In tmux, check 120×34 and 120×40 (the calendar appears), 80×24 (the
clock shrinks to keep the panel), and `n`, `z`, `c`, `?`.
