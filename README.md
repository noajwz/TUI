# TUI

A collection of small terminal programs, written for fun with [Claude Code](https://claude.com/claude-code).

None of these are serious tools — they're hobby projects, built to see how far you get in a
terminal with nothing but the Python standard library. No dependencies and no build step: every
script runs on its own with `python3`.

| Script | What it does |
| --- | --- |
| `wiki_tui.py` | Full-screen Wikipedia browser — followable links, search, contents, history |
| `psychonaut_tui.py` | The same browser for PsychonautWiki, with the dosage and duration tables rendered |
| `archwiki_tui.py` | The same browser for the Arch Wiki — commands you can copy with one key, note and warning boxes, tables drawn as grids |
| `chan_tui.py` | 4chan reader — boards, catalogs and threads with greentext, quote previews, back-links and a watch mode |
| `bbc_tui.py` | BBC News world headlines from the RSS feeds, by region, with the full text of each story |
| `termimage.py` | Shows images in the terminal, as real pixels or as coloured characters |
| `claude_usage_tui.py` | Live view of how much of your Claude usage limits is spent |
| `rooster_tui.py` | Work roster from an `.ics` subscription feed, re-fetched while you watch |
| `reminders_tui.py` | Apple Reminders in the terminal: tick off, add, reschedule, edit notes |
| `calendar_tui.py` | Apple Calendar in the terminal — month, week and agenda views of every account, read-only |
| `wiki_reader.py` | One-shot Wikipedia reader that pipes into `less` |
| `crypto_tui.py` | Live BTC, ETH and XMR prices with braille price charts, volume, and a crosshair to read past prices |
| `compound_tui.py` | Compound interest calculator — a live stacked graph of deposits against interest, readable year by year |
| `spotify_chart_tui.py` | The top 100 songs on Spotify as a bar chart — daily, weekly and all time, with rank movement |
| `election_tui.py` | Every US presidential election since 1788, with the electoral map, results and state-by-state table of each |
| `git_glance_tui.py` | Recent commits of GitHub repos at a glance — one feed across every repo, 14-day activity per repo, and each commit's message and changed files |
| `btc_tx_check.py` | Looks up a Bitcoin transaction by its hash |
| `bt_battery_tui.py` | Battery levels of connected Bluetooth devices |
| `disk_usage_tui.py` | What is filling the disk, in the manner of WinDirStat — folders by size, a shaded treemap, file types and the largest files, with reveal in Finder and move to Trash |
| `usb_speed_tui.py` | Read and write speed of USB drives, sequential and 4K random, with a live graph |
| `folder_watch_tui.py` | Watches a folder for changes — files created, deleted, modified and renamed below it, as a live log with an activity strip |
| `test_watch_tui.py` | Reruns a Python project's tests on every save (unittest or pytest) — failures first, tracebacks a key away, rerun only what failed, jump to the line in your editor |
| `text_stats_tui.py` | Lines of code, comments and blanks per programming language, word frequency, and text metrics — readability, line endings, indentation, encodings, TODOs |
| `pwgen_tui.py` | Password generator with a curses interface |
| `typing_tui.py` | Typing speed test — timed or word-count runs, WPM, accuracy, a per-second chart and personal bests |
| `wordle_tui.py` | Wordle — six tries at a five-letter word, with hard mode and a shareable grid |
| `minesweeper_tui.py` | Minesweeper — three levels or a custom board, safe first dig, chording, mouse support |
| `pokemon_types_tui.py` | Pokémon type calculator — weaknesses of any type pair, move coverage, and the full chart |
| `tictactoe.py` | Tic-tac-toe, two players or against an unbeatable AI |

`man ./termimage.1` documents the image viewer.

`ascii_city.py` grew up and moved out — it now lives in
[noajwz/ascii_city](https://github.com/noajwz/ascii_city).

Written on macOS — a few of them lean on `system_profiler`, `pbcopy` and `sips`, and
`reminders_tui.py` and `calendar_tui.py` compile a small Swift helper with `swiftc` on first run (Command Line Tools).
