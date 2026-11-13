---
paths:
  - "wordle_tui.py"
---

# wordle_tui.py

Standard curses shape, but three things are worth knowing before changing it.

**Two vocabularies, not one.** `ANSWERS` is a hand-kept list of ~1200 common words and is the only
thing that can *be* the answer; guesses are checked against `ANSWERS | /usr/share/dict/words`. That
split is the point: `web2` is Webster's 1913, so it happily supplies `aalii` and `rybat` — fine to
guess, never fair to have to guess — while *missing* `pixel`, `pasta` and `women`. Neither list
works alone. A missing dictionary degrades to `ANSWERS` rather than failing.

**`score_guess()` spends each answer letter once.** The position pass runs first and the near pass
draws only from what it left behind, so `llama` against `hello` marks two near `L`s and no more, and
`eerie` against `opera` marks only the first `E`. Scoring each letter independently is the classic
bug here, and it looks correct until a guess repeats a letter.

**The layout is a ladder, not a breakpoint.** `choose_layout()` walks `LAYOUTS` roomiest-first and
takes the first that fits, so a short window loses the gaps between grid rows before it loses the
boxed tiles. `rows_needed()` has to stay in step with what `draw()` actually consumes — the on-screen
keyboard is the piece that silently falls off the bottom when it doesn't.

Testing it without a human: fork a pty as with `wiki_tui`, set the size with `TIOCSWINSZ` *before*
the child paints its first frame, and read the fd **non-blocking** — a blocking `os.read` just hangs
once the program is idle in `getch`. Replaying the escapes onto a grid to assert on a frame needs
`CHA`/`VPA` (`ESC[nG`, `ESC[nd`) handled, since that is how ncurses moves the cursor absolutely;
ignore them and every row lands in column 0. The pure halves need no pty at all — call
`score_guess()`, `hard_mode_error()` and `choose_layout()` directly.
