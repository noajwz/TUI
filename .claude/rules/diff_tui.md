---
paths:
  - "diff_tui.py"
---

# diff_tui.py

Two text files side by side (or unified with `u`/`-u`), changed characters highlighted. Unchanged
stretches fold around `CONTEXT` lines. If one argument is a folder, the file of the same name in
it is used, as `diff` does. `--print` writes the same diff as text. Its exit status follows
`diff`: 0 same, 1 different, 2 trouble. Invisible differences count as different.

- **Alignment happens in two passes.** `compare()` runs `SequenceMatcher` over whole lines
  (with `autojunk=False`: by default it treats lines common in big files, such as blank ones, as
  junk, and the diff comes out odd). Each replaced block then goes to `pair_block()`, which
  pairs the most alike two lines (ratio > `PAIR_CUTOFF`) and recurses on each side of them, as
  `difflib.ndiff` does. That way an edited line sits beside its original, and a line that was
  only removed shows as a delete rather than as a change to whatever came next. A block over
  `PAIR_LIMIT` (lines × lines) is paired in order, so the cost doesn't explode.
- Paired lines get character spans from a second `SequenceMatcher` on the *displayed* strings,
  with tabs expanded and control characters replaced one-for-one, so spans index straight into
  what is drawn. Below `INTRALINE_CUTOFF` a pair is coloured whole instead, because spans over
  two unrelated lines are noise. A changed line is red on the old side and green on the new.
- `-w`/`-i` compare normalised keys (`key_of`: whitespace runs collapsed, casefolded) but show
  the real text. Lines that are equal only because of that are `similar`, marked `≈` in the
  middle, and don't count as changes.
- **`Side.read()` records what the text hides:**
  - the line endings (CRLF/LF/CR/mixed) and whether the file ends with a newline;
  - a UTF-8 BOM, and the encoding: UTF-8, else Windows-1252, for old Dutch Windows files;
  - a NUL byte in the first 8 kB, which means a binary file and is refused.

  `notes()` turns differences in these into the header's "also:" line. Lines are split on
  `\r\n|\r|\n` only: `str.splitlines()` would also split on form feeds and Unicode separators,
  which diff doesn't.
- The window polls both files' `(mtime_ns, size)` every `TICK_MS` and compares again when one
  changes, staying on the same line of the left file (`keep_place`). The same applies to
  toggling `w`, `i`, `f` and `u`.
- `self.lines` is what is on screen: rows after folding, and in unified mode a change takes two
  lines. `n`/`N` jump between change starts in it. `c` copies a real unified diff
  (`difflib.unified_diff`), not what is on screen.

Testing: two versions of a short list covering an edit, an insertion, a removal next to an edit
(it must pair with the edit, not shift everything), a tab and CJK text, and a line longer than
the pane. Then copies of the old file that differ only in CRLF, in the final newline, in
whitespace or case (`-w -i` must say the same and exit 0), and in cp1252. Also a binary file
(exit 2) and a folder as the second argument. `--print --width N` checks layout without a
terminal. In tmux check `n`, `u`, Enter on a fold, `→`, `s`, and appending to a file while it
is open (it says the file changed). Sizes: 110×24 and the 60×10 minimum.
