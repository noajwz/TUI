---
paths:
  - "rooster_tui.py"
---

# rooster_tui.py

A work roster published as an `.ics` subscription feed, shown month by month and re-fetched every
15 minutes (the feed advertises `X-PUBLISHED-TTL:PT1H`). The feed genuinely rewrites itself: two
fetches two days apart went from 39 shifts to 60, gaining a whole month and two shift codes that
had never appeared before. Three things follow from that and are easy to undo by accident.

- **The feed URL is a bearer token.** It needs no login, so anyone holding it can read the roster.
  It lives in `~/.ess_calendar_url`, `$ESS_ICS_URL`, or `.ess_url` beside the script — the last is
  gitignored — and must **never** be written into a file that gets committed. This repo is public.
- **Times are converted to the local zone and never shown as the UTC in the file.** The publisher
  moves its UTC stamps across the DST boundary precisely so that local times stay put: the same
  `DvT` shift is `05:00Z` in September and `06:00Z` on 25 October, and both are 07:00 locally.
  Print the raw UTC and the whole roster appears to drift an hour every autumn. The invariant worth
  asserting on is that each shift code has exactly one local start time across the entire feed.
- **Shift codes are printed verbatim and there is no lookup table.** `DICO` and `V4` turned up
  between those two fetches, so any hardcoded legend is wrong the moment the employer invents a
  code. If meanings are ever wanted, add a dict with a fall-through to the raw code, the way
  `claude_usage_tui.py` handles limit kinds — never a bare dict lookup.

`parse_events(text)` → `[Shift]` and `build_rows(shifts, today)` → display rows are pure, so
month grouping, free-day gaps and the scroll target need no terminal to test. A `VEVENT` that
fails to parse is skipped rather than fatal, for the same reason: one bad block must not blank the
roster. `fetch_ics()` accepts `file://` URLs, which is what lets the whole program — TUI included —
run against a saved fixture with no network: `rooster_tui.py --url file:///tmp/roster.ics`.

For the curses half, fork a pty and set the size with `TIOCSWINSZ` as with `wiki_tui`. Note that
curses only transmits the lines that *changed*, so asserting on a scroll needs a forced full
repaint — nudge the window size by a column and back — or you read a partial screen and conclude a
working key is broken. A failed refresh keeps the roster already on screen and reports in the
footer; the error shares that line with the totals, so it is trimmed to fit rather than allowed to
run back over them.
