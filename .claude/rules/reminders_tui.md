---
paths:
  - "reminders_tui.py"
---

# reminders_tui.py

Apple Reminders, read and write: lists on the left, the selected list's reminders on the right,
plus Today / Scheduled / All views. Changes go straight into the Reminders store, so they sync to
the app and the phone like any other edit.

**It is not stdlib-only in the way the rest are**: the talking is done by a small Swift program
against EventKit, embedded as `SWIFT_SOURCE` and compiled once with `swiftc` into
`~/Library/Caches/reminders_tui/ekhelper-<hash>` (the hash is of the source, so editing it forces a
rebuild; `--rebuild` does so explicitly). That was not a style choice. Reminders' AppleScript/JXA
dictionary, measured on 236 reminders: 30 s to read every list's properties, and over 70 s with a
`whose completed is false` filter. EventKit reads the same in 0.3 s. Don't move it back to
`osascript`. The helper takes one operation per run (`dump`, `add`, `update`, `delete`) with a JSON
request on stdin and answers JSON on stdout or one line of error on stderr; `call_helper()` turns
that into the usual `(data, error)` tuple. Due dates cross the wire as `[y, m, d]` or
`[y, m, d, H, M]` in local time; a timed due date also gets one alarm at that moment, the way the
Reminders app does it.

Permission is TCC's "Reminders" (full access), granted to the terminal app that runs it, not to
Python. The first run shows the system dialog; a denial ends up as the helper's stderr line.

**Subtasks show up flat**, beside their parent. EventKit returns them as ordinary reminders and has
no public parent link; `EKReminder` has a private `parentID`, but it was nil on every one of 235
reminders, so there is nothing to nest by. AppleScript hides subtasks altogether, which is why its
counts are lower than this program's.

List order is alphabetical: EventKit does not expose the order the Reminders app shows.

`parse_due(text, now)`, `format_due(r, now)`, `visible(...)` and `next_priority()` are pure. For
the curses half without touching real reminders, import the module, replace `rt.call_helper` with a
fake backed by a saved `dump` (run the helper with `dump` once and keep its stdout), and call
`curses.wrapper(rt.run_curses, "fake", data)` in a tmux pane: `tmux capture-pane -p` gives the
screen as text. Please don't test writes against the real store; they sync to the phone.
