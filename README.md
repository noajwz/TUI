# TUI

A collection of small terminal programs, written for fun with [Claude Code](https://claude.com/claude-code).

None of these are serious tools — they're hobby projects, built to see how far you get in a
terminal with nothing but the Python standard library. No dependencies and no build step: every
script runs on its own with `python3`.

| Script | What it does |
| --- | --- |
| `ascii_city.py` | Endless procedural city in the rain — neon streets, dark alleys, a roulette wheel |
| `wiki_tui.py` | Full-screen Wikipedia browser — followable links, search, contents, history |
| `termimage.py` | Shows images in the terminal, as real pixels or as coloured characters |
| `claude_usage_tui.py` | Live view of how much of your Claude usage limits is spent |
| `rooster_tui.py` | Work roster from an `.ics` subscription feed, re-fetched while you watch |
| `wiki_reader.py` | One-shot Wikipedia reader that pipes into `less` |
| `btc_tx_check.py` | Looks up a Bitcoin transaction by its hash |
| `bt_battery_tui.py` | Battery levels of connected Bluetooth devices |
| `pwgen_tui.py` | Password generator with a curses interface |
| `wordle_tui.py` | Wordle — six tries at a five-letter word, with hard mode and a shareable grid |
| `tictactoe.py` | Tic-tac-toe, two players or against an unbeatable AI |

`man ./termimage.1` documents the image viewer.

Written on macOS — a few of them lean on `system_profiler`, `pbcopy` and `sips`.
