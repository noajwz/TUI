---
paths:
  - "sql_tui.py"
---

# sql_tui.py

A SQL scratchpad on `sqlite3`: the schema on the left, an editor top right, the results grid
below. `F5`/`Ctrl-R` runs the statement the cursor is in, `F6` runs all of them, `Shift-Tab` or
`F2`–`F4` move between panes. Arguments: at most one database, plus any number of CSV/TSV files to
load as tables. With no database it is an in-memory one.

- **Safety first.** A database file opens with `mode=ro` (a `file:` URI) unless `--write`, and a
  write attempt's error says how to get write access. With `--write` the connection is in
  autocommit (`isolation_level=None`), so each statement commits on its own unless you `BEGIN`.
  An open transaction shows in the header, and quitting asks to commit or roll it back.
  `.db`/`.sqlite` files that don't exist are only created with `--write`.
- **CSV loading** (`import_csv`):
  - the delimiter comes from `csv.Sniffer`, the encoding from UTF-8 with a fallback to cp1252;
  - blank or duplicate header names become `col3` and `b_2`;
  - columns are typed INTEGER, REAL or TEXT from their values, and with `;` as the delimiter
    `3,50` reads as 3.5 (Dutch Excel exports);
  - next to a real database the tables are `TEMP`, which a read-only connection still accepts.

  `.import` uses the same function.
- **Splitting** (`split_units`) relies on `sqlite3.complete_statement` at each `;`, so semicolons
  in strings, comments and `CREATE TRIGGER … BEGIN … END` don't split. A line starting with `.`
  where no statement is under way is a dot command (`.tables`, `.schema`, `.import`, `.export`,
  `.help`), run in the main thread. Between statements, the cursor's statement is the one just
  before it.
- **Running** (`Runner`) happens on a thread with `check_same_thread=False`. Only one runs at a
  time, and the schema isn't reread until it has finished. `Ctrl-C` calls `conn.interrupt()`.
  The window is in `curses.raw()`, so Ctrl-C, Ctrl-Q and Ctrl-S arrive as keys rather than
  signals. A result set stops at `MAX_ROWS`. After a failure the cursor jumps to the statement
  that failed.
- The screen ticks every `TICK_MS`, for the spinner and to notice when a query finishes. So every
  modal loop must ignore `getch() == -1`: the pager once closed itself within 100 ms.
- **The editor**:
  - highlighting comes from `tokenize()`, whose state carries `/* … */` and quoted strings
    across lines;
  - undo snapshots each run of the same kind of edit;
  - a paste (several characters arriving at once, `feed()`) goes in raw, without the auto-indent
    of a typed Enter;
  - Tab completes table, column (after `table.`), function and keyword names, or inserts 4
    spaces.

  The text is kept per database in `~/.local/state/sql_tui/buffers/`, and every statement run in
  `history.json` (`Ctrl-P`).
- Results sort on the client (`s`: up, down, off), with NULLs last either way. `c`/`y`/`Y` copy a
  cell, a row, or everything as tab-separated text. `e` writes the rows in their current order to
  CSV. Enter shows the row as a record.

Testing:

- Use `HOME=<scratch>` so buffers and history stay out of the real state folder.
- The pure parts can be imported and called directly: `split_units`, `tokenize`, `import_csv`
  (on a `;`/decimal-comma cp1252 file, and on a TSV with blank and duplicate headers), and
  `Runner` on a read-only `Db`. On the runner, check a SELECT, a refused UPDATE, a syntax error,
  a recursive CTE stopped with `stop()`, and a 25,000-row result cut off at `MAX_ROWS`.
- In tmux, paste with `load-buffer` and `paste-buffer`. A `;` at the end of a `send-keys`
  argument is tmux's command separator: escape it as `\;`, and only there.
- Check `--write` with `BEGIN; INSERT …` then `Ctrl-Q`, `r`: the row must not stay. Sizes: 110×30
  and the 70×16 minimum.
