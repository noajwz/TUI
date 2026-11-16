---
paths:
  - "text_stats_tui.py"
---

# text_stats_tui.py

Statistics of a folder (or one file) of text, in four views: Languages (files, lines, code,
comment, blank and size per language, like tokei/cloc), Files, Words (frequency with stop words,
programming keywords and short words filtered out) and Metrics (comment density, lexical
diversity, hapax legomena, line lengths, trailing whitespace, line endings, encodings,
indentation, TODO/FIXME markers, Flesch readability of the prose files). Enter on a language,
or `l`, narrows the other views to it; Esc clears that. `--once` prints all of it.

- **Which files:** in a git work tree, `git ls-files --cached --others --exclude-standard`, so
  .gitignore is respected. When git lists nothing, the folder is walked instead (it isn't a
  repository, or it's ignored inside one, like Homebrew's Cellar under /opt/homebrew). Walking
  skips hidden folders and `SKIP_DIRS`. NUL bytes in the first 8 KB mean binary and the file is
  skipped, as is anything over `MAX_FILE_BYTES`. UTF-8 is tried first, then Latin-1.
- **Lines are counted like tokei:** blank, comment (a comment and no code) or code. `LANGUAGES`
  gives each language its line comments, block comments and string quotes. `Lang` compiles them
  into one regex for the next interesting token, and `classify()` follows strings so a `#` in
  quotes isn't a comment. Single quotes end at the line end; triple quotes and backticks carry
  over. A block marked `True` only counts as a comment at the start of a line, which is how
  Python docstrings are comments while a `"""` mid-expression is a string. Nested block comments
  aren't tracked.
- Language comes from the file name, then the extension, then a `#!` line (`SHEBANGS`). A name
  without an extension falls back to Text (LICENSE); an unknown extension is Other.
- Words are runs of letters (`WORD`), lower-cased, kept as one Counter per language in `Scan`
  so the language filter costs nothing. `KEYWORDS` is `keyword.kwlist` plus common keywords of
  other languages; `c` toggles it. `k` is taken by vim-style movement.
- Readability uses the English Flesch formulas with a vowel-group syllable guess, only over files
  of kind `prose` (Markdown, Text, reST, TeX, Org). It's a rough figure and the page says so.
- The scan runs on a thread; the views recompute from it at most once a second while it runs
  (`App.cached`). The Python stdlib (2,600 files, 1.1 M lines) takes about 3 s.

Testing: `--once` on this repo, `../win_scripts` (CRLF, PowerShell block comments, Batch `REM`)
and the Python stdlib for speed. `classify()` is easy to check directly with small samples per
language. Don't point it at `~/src` itself: that's not a repo, so it walks into `KP/`. The curses
half works in tmux with `capture-pane -p` at 100×30 and the 60×12 minimum (Size column and bars
drop below 80).
