---
paths:
  - "json_peek_tui.py"
---

# json_peek_tui.py

Explores a JSON document (a file, a URL with `-H` headers, or a pipe) as a folding tree, with a
table view for arrays and a reader for long strings. `--print` writes the tree as text, opened
`--depth` levels (default 2). `--path .a[0].b` prints that value as JSON, or roots `--print` there.

- **Piped input:** `Source.read()` takes all of stdin first, then `main()` `dup2`s `/dev/tty`
  onto fd 0, so curses reads keys from the terminal. Without a tty, or with stdout not a
  terminal, it points at `--print`/`--path`. `r` reads a file or URL again, keeping the
  selection and any opened strings; a pipe can't be read twice.
- **Numbers are parsed as `Decimal`** (`parse_float`), so `1.10` stays `1.10` and long floats
  keep their digits. That's why `dump()` exists: `json.dumps` can't write a Decimal as a
  number. Everything copied or shown as JSON goes through `dump()`/`scalar()`.
- `parse()` tries plain JSON, then strips the `)]}'`-style anti-XSSI prefixes, then JSONP, then
  JSON Lines (to a list). A failure is `BadJSON` with the position: the screen shows the line
  with a caret, windowed around the column so minified JSON still points at the spot.
- **The tree is never built as nodes.** `build()` walks only the expanded paths (tuples of
  keys and indexes) into a flat `rows` list, and rebuilds when `dirty`. It stops at `MAX_ROWS`,
  as do `expand_all`/`expand_to`. `Doc.decoded` maps a path to the value decoded out of the
  string there (JSON text, or a JWT as header/payload/signature, unverified). `Doc.at()` swaps
  it in wherever a walk passes that path, and `←` on it puts the string back.
- **Readable hints** (`hint()`, `as_time()`):
  - a number under a time-like key (`TIME_KEY`: `created_at`, `updatedAt`, `exp`, `iat`, …)
    within 1e9–4e9 is read as Unix seconds, 1e12–4e12 as milliseconds;
  - an ISO 8601 string is a date and gets "3 days ago";
  - the key is required for numbers, or every id near 1.7e9 would turn into a date.

  The table shows those numbers as local times; the tree keeps the raw number with the time
  beside it.
- **The table** (`t`, on an array or anything inside one) flattens nested objects into dotted
  columns `FLATTEN` keys deep (`commit.author.name`), since API items are rarely flat. Columns
  are key-path tuples, and `()` is the column for items that aren't objects. `Table.shown` lists
  the columns not hidden with `x`; `t.col`/`t.left` are positions in it, while `t.sort` holds a
  `cols` index. Sorting keeps empty cells at the bottom in both directions. Enter goes back to the
  tree at that item's cell.
- Search covers keys and scalar values across the whole document, not only the open rows. Matches
  come in document order, so `find()` goes to the first one after the selection without having
  to compare positions.
- Paths are shown jq-style (`fmt_path`: `.data[0].name`, `.["odd key"]`). `parse_path` also takes
  `data.0.name` and resolves while it reads, so an error says how far it got.

Testing: build a sample response with an epoch `created_at`, an ISO date, a `1.10`, JSON inside
a string, a JWT, a multi-line string, objects with uneven keys, an empty key and a key with
dots. Check it with `--print -d 3` and a few `--path` lookups, including bracket paths and
round-trips through `fmt_path`/`parse_path`. A bad file, a minified bad file, JSON Lines and a
`)]}'` prefix cover `parse()`. The curses half works in tmux with `capture-pane -p`, piped
(`cat f | …`) as well as from a file; check 100×30 and the 50×10 minimum. A generated 20 MB
array (≈1.4M values) parses in 0.2 s and is the test for `MAX_ROWS` and search speed.
`https://api.github.com/repos/python/cpython/commits` makes a good table.
