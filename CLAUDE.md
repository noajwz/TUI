# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Small terminal programs written for fun. **No package, no dependencies, no tests, no config files** —
every script is stdlib-only (`curses`, `urllib.request`, `json`, `subprocess`) and self-contained.
Adding a third-party import would be a departure from the project's premise; prefer stdlib.

Ruff is the linter (a `.ruff_cache/` from ruff 0.16.1 is present but gitignored). Ruff is **not
installed on this machine and there is no pyproject/ruff.toml** — it runs with defaults, e.g.
`ruff check . && ruff format .` once available.

Three shapes exist and each new script should follow one of them:

- **curses TUIs** (`bbc_tui`, `bt_battery_tui`, `compound_tui`, `crypto_tui`, `election_tui`, `disk_usage_tui`, `spotify_chart_tui`, `pwgen_tui`, `claude_usage_tui`, `rooster_tui`, `reminders_tui`, `calendar_tui`, `wordle_tui`, `minesweeper_tui`, `pokemon_types_tui`, `typing_tui`): `main(stdscr)` launched via `curses.wrapper(main)`,
  a `stdscr.timeout(...)` + `stdscr.getch()` event loop, module-level constants for tunables
  (`REFRESH_SECONDS`, `MIN_LEN`/`MAX_LEN`, option tables).
- **CLI scripts** (`btc_tx_check`, `wiki_reader`): `argparse` in `main()`, raw ANSI escape constants
  (`BOLD`, `CYAN`, `RESET`, …) defined at module top rather than a color library, and network helpers
  that return `(data, error)` tuples instead of raising.
- **`wiki_tui`** and **`termimage`**, the two larger programs, described below. `psychonaut_tui` is a
  fork of `wiki_tui` and `archwiki_tui` a fork of that, both described below. `chan_tui` borrows `wiki_tui`'s pieces (`call()`,
  `prompt()`, Tab-selected links) but shares no code with it; also described below.

`ascii_city.py` used to live here and has moved to its own repository,
[`noajwz/ascii_city`](https://github.com/noajwz/ascii_city) — it outgrew being one script among
several. Its history went with it, so `git log` here no longer covers it.

`wiki_reader.py` and `wiki_tui.py` are deliberately separate takes on the same idea and neither
supersedes the other: `wiki_reader` is the one-shot pager version (pipeable, scriptable), `wiki_tui`
is the interactive browser. Keep `wiki_reader.py` as it is.

macOS-specific by design: `system_profiler` for Bluetooth, `pbcopy` for the clipboard, `sips` for
image conversion.

`claude_usage_tui.py` reads the utilization block Claude Code caches in `~/.claude.json`. It is
strictly read-only and makes no network calls — any running Claude Code session refreshes that cache,
which is what makes it live rather than a snapshot. Keep it read-only.

Per-script notes live in **`.claude/rules/<script>.md`** and load automatically when you
work on that script (the `wiki_tui` notes also load for its forks and `chan_tui`). When you
add a script or change one's design, put its notes there, not here.
