---
paths:
  - "calendar_tui.py"
---

# calendar_tui.py

Apple Calendar, **read-only**: a month grid, a week, or an agenda, from every account the Calendar
app shows. It talks to EventKit through the same embedded-Swift-helper arrangement as
`reminders_tui.py` (compiled into `~/Library/Caches/calendar_tui/ekhelper-<hash>`, `--rebuild` to
force it), but the helper does one thing only: it reads `{"from": [y, m, d], "to": [y, m, d]}` on
stdin and answers every calendar plus each event occurrence overlapping that range. EventKit expands
recurring events into occurrences itself, which is the main reason not to read the store any other
way. Times cross the wire as `[y, m, d, H, M]` in local time. Permission is TCC's "Calendars" (full
access), granted to the terminal app; a session with no GUI to show the dialog in gets a denial.

- **Fetches are by range, not everything.** `load_range()` widens what the view needs to whole
  months (one before, three after); `ensure()` refetches only when the view moves outside what is
  loaded, and the timed refresh re-reads the loaded range. A failed fetch still marks its range as
  loaded, so the next draw doesn't hammer the helper; the refresh retries.
- **An event's last day is `(end - 1 minute).date()`**, so one ending at midnight doesn't spill into
  the next day. EventKit ends a one-day all-day event at 23:59:59 of that day; that works too.
  `index_by_day()` puts multi-day events on every day they cover.
- Hidden calendars (`c`) and the search filter (`/`) apply through `reindex()`, which rebuilds
  `by_day`; everything draws from that. Both last for the session only.

`index_by_day()`, `month_weeks()`, `agenda_rows()`, `parse_goto()`, `add_months()`, `load_range()`
and the `*_text`/`format_when` helpers are pure. For the curses half, import the module, replace
`call_helper` with a fake `(path, start, end) -> (data, None)` and call
`curses.wrapper(ct.run_curses, "fake", data, loaded)` in a tmux pane. Check 56×14 (the minimum),
80×24, and something wide like 300×90, where month cells start showing times (`TIMES_MIN`) and the
agenda adds a place column.
