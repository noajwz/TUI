---
paths:
  - "csv_tui.py"
---

# csv_tui.py

A spreadsheet-style reader for one CSV/TSV file (or `-` for stdin): a grid with the header row
fixed, a cell cursor and a line above the grid with the full value of the current cell, Enter for
the row as a record, `c` for statistics of a column, sort, search, filters, pinned and hidden
columns, copy, and export of what's on screen to a new file. It never writes to the file it reads.
`--once` prints the columns with type, filled, distinct, min and max.

- **Guessing the format.** The delimiter is whichever of `, ; tab |` splits the first
  `SNIFF_LINES` lines into the most consistent field count (then the most fields); `csv.Sniffer`
  isn't used, it's unreliable on semicolon files. `.tsv`/`.tab` are always tab. Encodings in order:
  UTF-16 by BOM (Excel "Unicode text"), UTF-8 with or without BOM, Windows-1252, Latin-1 (never
  fails). The first row is a header unless most of it reads as numbers or dates; `H` flips it.
- **Dutch Excel exports are the case to keep working:** `;`-delimited, `1.234,56`, `dd-mm-yyyy`.
  A column uses a decimal comma when the delimiter isn't `,` and commas between digits are at least
  as common as dot decimals that can't be thousands (`1.5`). Dates are day-first unless a second
  part over 12 proves otherwise.
- **Column types** (`integer`, `number`, `date`, `text`, `empty`) come from the first
  `SAMPLE_ROWS` rows: 90 % of the filled cells must parse, and placeholders in `NULLS` (`n/a`,
  `-`, …) don't count. Numbers are right-aligned. `Column.key()` is the one sort/compare key:
  `None` for blank, `(0, value)` when the cell parses as the column's type, `(1, natural text)`
  otherwise, so `>`/`<` filters only compare like with like and stray words sort after numbers.
- **Rows are never reordered.** `Table.rows` is a list built once (it was a slicing property once
  and that made every loop quadratic); `App.view` is the list of row indices after filters then a
  stable sort. Blanks sort last in both directions. Ragged rows are padded to the widest and
  counted in the title.
- `App.cols` is the visible columns in order (`x` hides, `X` restores); `pinned` is how many of
  them stay at the left; `left` is the first scrolling one on screen (`fix_left`).
- `csv.field_size_limit` is raised at import; the default raises on cells over 128 K characters.
- With `-`, stdin is read whole and fd 0 is swapped for `/dev/tty` before curses starts.

Testing: make fixtures in the job's tmp folder, not in the repo — UTF-8 with quoted commas and
newlines, `;` + decimal comma in cp1252, UTF-16 TSV, ragged, header-less numbers, duplicate and
blank header names, empty, header-only, and ~200 k rows (`--once` takes about 1 s, sorting in the
TUI well under that). `--once` covers detection; the grid works in tmux with `capture-pane -p`
at 100×30 and the 50×10 minimum. In zsh, quote `'?'` and `'$'` for `tmux send-keys`.
