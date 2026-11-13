---
paths:
  - "minesweeper_tui.py"
---

# minesweeper_tui.py

`Board` holds every rule and imports nothing from curses, so a game can be played from a test:
`Board(rows, cols, mines, random.Random(seed))`, then `dig()` / `toggle_flag()` and assert on
`state`, `revealed`, `mines`. Mines are laid on the **first dig**, never before, keeping that cell
and its ring clear (just the cell, if the board is too dense for a ring), so the opening always
flood-fills. `dig()` on a revealed number chords: it opens the neighbours once the flags around it
match the count, and a wrong flag there loses the game, as it should.

ESC is deliberately not a quit key. A mouse or key sequence ncurses doesn't recognise arrives as a
bare ESC followed by junk, and quitting on it ends the game out from under the player.

Testing the curses half: a tmux pane plus `capture-pane -p`. Under `TERM=tmux-256color` ncurses
expects **X10** mouse reports, not SGR — a left press on 1-based column X, row Y is
`send-keys -H 1b 5b 4d 20 <32+X> <32+Y>` (`22` for the right button, `23` for the release).
