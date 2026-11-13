---
paths:
  - "typing_tui.py"
---

# typing_tui.py

A typing speed test in the standard curses shape: time mode (15/30/60/120 s) or words mode
(10/25/50/100 words), picked with the arrow keys before the first keystroke, which is also what
starts the clock. `Test` holds every rule and all the maths and imports nothing from curses; every
method takes `now` explicitly, so a run can be replayed at exact timestamps from a test.

- **Three numbers, kept distinct.** WPM counts only words typed exactly right plus the space after
  each, at five characters to the word; raw counts every character as the text stands; accuracy is
  per *keystroke* (`keys`/`wrong_keys`), so a typo fixed with backspace costs accuracy but not
  WPM. Folding any two of these together is the easy mistake.
- **Time mode ends in `tick()`**, at exactly `started + amount`, and `tick()` runs before every key
  and every repaint, so a key that arrives after the deadline is dropped rather than counted.
- Backspace from the start of a word steps back into the previous word only if that word has a
  mistake in it; a correct word is locked in.
- **A pasted run takes no time at all**, which made WPM run into six figures. Runs are timed as at
  least a second, and anything over `MAX_HUMAN_WPM` is shown but never saved as a best.
- Personal bests live per `"time 30"`-style key in `~/.local/state/typing_tui/best.json`,
  written best-effort; they are loaded once at startup.
- ESC quits only outside a running test. ESC followed by DEL is how macOS sends option-backspace,
  and deletes the word.

Testing the curses half: a tmux pane plus `capture-pane -p`, at 50×10 (the minimum) and 80×24.
Run it with `HOME` pointed at a temp directory so test runs don't write to the real bests, and
send keys in chunks with sleeps between: one `send-keys` of the whole text arrives in a single
instant and trips the paste guard.
