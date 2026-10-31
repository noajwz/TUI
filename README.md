# TUI

A collection of small terminal programs, written for fun with [Claude Code](https://claude.com/claude-code).

None of these are serious tools — they're hobby projects, built to see how far you get in a
terminal with nothing but the Python standard library. No dependencies and no build step: every
script runs on its own with `python3`.

| Script | What it does |
| --- | --- |
| `wiki_tui.py` | Full-screen Wikipedia browser — followable links, search, contents, history |
| `psychonaut_tui.py` | The same browser for PsychonautWiki, with the dosage and duration tables rendered |
| `chan_tui.py` | 4chan reader — boards, catalogs and threads with greentext, quote previews, back-links and a watch mode |
| `termimage.py` | Shows images in the terminal, as real pixels or as coloured characters |
| `claude_usage_tui.py` | Live view of how much of your Claude usage limits is spent |
| `rooster_tui.py` | Work roster from an `.ics` subscription feed, re-fetched while you watch |
| `reminders_tui.py` | Apple Reminders in the terminal: tick off, add, reschedule, edit notes |
| `wiki_reader.py` | One-shot Wikipedia reader that pipes into `less` |
| `btc_tx_check.py` | Looks up a Bitcoin transaction by its hash |
| `bt_battery_tui.py` | Battery levels of connected Bluetooth devices |
| `usb_speed_tui.py` | Read and write speed of USB drives, sequential and 4K random, with a live graph |
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
`reminders_tui.py` compiles a small Swift helper with `swiftc` on first run (Command Line Tools).
