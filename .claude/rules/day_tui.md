---
paths:
  - "day_tui.py"
---

# day_tui.py

A day planner and time tracker in three views. **Plan** is a timeline of the day (15, 30 or 60
minutes a line, `z`) with planned blocks on the left and tracked sessions on the right, a red line
at now. **Track** has the running timer, the activities with today's totals, and the day's log.
**Summary** sets tracked time against planned time per activity for a day, a week or a month
(`p`), with a per-day chart. `[` `]` move between days in every view; `T` is today.

- **One JSON file**, `~/.local/state/day_tui/day.json` (the `typing_tui` convention), or
  `--file` / `$DAY_TUI_FILE`: `plans` (day → blocks with start and end in minutes of the day),
  `sessions` (activity, start and end in epoch seconds), `running` (activity and start, or null),
  `activities` (most recently used first) and `colors` (activity, lower-cased → palette index).
  Writes go to a temp file and `os.replace`, after every change. The loop reloads when the file's
  mtime changes, so `--start`/`--stop` from a shell or a second window show up within a second.
- **A running timer is only a start time**, so it survives quitting, sleep and reboots, and
  `--status` can print it for a status line. One runs at a time: `Store.start` stops the previous
  one. Sessions under `MIN_SESSION` seconds are dropped as mis-presses.
- **Days are bounded by `day_bounds`** (local midnight to the next, via datetime, so DST days
  are 23 or 25 hours). `Store.sessions(lo, hi)` clips sessions to the range, so one running past
  midnight counts on both days; it includes the running timer with index −1.
- **Blocks never overlap**: adding, editing, `J`/`K` (move) and `L`/`H` (length) refuse with the
  name of the block in the way. A new block without a length runs `DEFAULT_BLOCK` minutes or up
  to the next block. `parse_spec` reads `Deep work 1h30`, `Lunch 12-13`, `Call 14:30 45m`,
  `at 18:00 1.5h`; the same parser is used for sessions added or edited by hand.
- Plan and track are linked by **name**: a block's title is an activity (Space on a block starts
  its timer), colours are per lower-cased name, and the summary matches plan to tracking
  case-insensitively.
- `u` undoes from a stack of JSON snapshots taken in `Store.change()` before each edit.

Testing: always point `DAY_TUI_FILE` at a temp file — never the real one. Seed a day through
`Store` (blocks plus a handful of sessions with `epoch_at`), then drive tmux at 100×30 and the
60×16 minimum: add (`Enter`, then text with `send-keys -l`), `J`/`L` into a neighbour (must
refuse), `x`, `d` then `u`, Space on a block, `2` then `n`, `a`, `s`, `]` then `y`, `3` then `p`.
`--start`/`--status`/`--stop`/`--once` cover the CLI; `parse_spec` is easy to check directly.
