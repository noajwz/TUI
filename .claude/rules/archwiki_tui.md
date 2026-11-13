---
paths:
  - "archwiki_tui.py"
---

# archwiki_tui.py

A second fork of `wiki_tui.py`, via `psychonaut_tui.py`, for the [Arch Wiki](https://wiki.archlinux.org)
(stock MediaWiki, `api.php` at the site root, articles under `/title/`). Self-contained on purpose,
like `psychonaut_tui`: a parser or layout fix in one of the three has to be copied by hand. With no
topic it opens the Main page; `h` goes back to it. What differs is what Arch pages are made of:

- **Commands are the content.** `<pre>` blocks are code (green), `<i>` inside code or a `<pre>` is a
  placeholder (`ph`, yellow underline: `pacman -S package_name`), and a code block is one entry in
  `doc.links` (kind `code`) spanning every line of the block, so Tab and `c` select it and Enter
  copies it. `code_text()` drops the `$ `/`# ` prompts only when *every* non-blank line has one and
  the block is not a file body — a config file of `#` comments must be copied as is.
  `{{hc|file|contents}}` arrives as two `<pre>`s told apart only by inline `margin-bottom:0` /
  `margin-top:0` styles (`prehead` / `prebody`); the header is selectable only if it is a command.
  Code is cut to the width with a `↪` continuation, never word-wrapped.
- **Copying** tries `pbcopy`, `wl-copy`, `xclip`, `xsel`, then OSC 52 — people read this wiki on
  the Arch machine, often over SSH or on a bare console, so it is not macOS-only like the rest.
  `man(8)` links run `man` locally on Linux only: macOS has BSD pages under the same names, so
  there they open man.archlinux.org.
- **Boxes** (`BOX_CLASSES`): Note/Tip/Warning, the "out of date" banners (which are `noprint`, so
  boxes are matched before `SKIP_CLASSES`), and Related articles, drawn as one line of links. Each
  block in a box carries `box` and a `box_id`, and layout draws a `┃` bar in the box's colour on
  every line, blank lines between its blocks included; `box_id` keeps two adjacent notes apart.
  The box's first `<strong>` is its label and gets a colon.
- **Tables are kept**, because many are command references (systemd's) or hardware matrices.
  `Grid` places cells honouring colspan/rowspan; `widths()` never cuts a body word and returns
  None when they can't fit, and the table is then stacked, one record per row labelled by the header
  row (`stacked_rows()`). Rules between rows are joined per column boundary (`┬ ┴ ┼`), so colspan
  section rows close their columns properly. Yes/No cells colour by class (`CELL_STYLES`).
- **Lines are 4-tuples** `(text, kind, spans, size)`; a span's last item is a link index (int) or a
  style name (str), drawn styles first, then links. `size` is the width-independent character count
  that `text_offset()` sums. It is stored rather than recomputed because a stacked table repeats its
  header labels on every record: those are subtracted and the header row is counted once, so the
  totals match the grid. Resize and `w` both keep the reader on the same text through it.
- Page links keep their `#section` (`Title#Anchor`), so following one lands on the section. Search
  sorts translations (`Title (Español)`, `TRANSLATED_RE`) after English, keeping the API's order
  otherwise; random skips them.

Testing: `parse_article()` + `layout()` on saved `action=parse` responses, at several widths, asserting
no line is wider than the width, every span lies inside its line, and `text_offset(doc, len(doc.lines))`
is the same at every width. Good fixtures: `Pacman` (boxes, placeholders, out-of-date banner),
`Systemd` (colspan command tables), `Installation guide` (`hc` file blocks), `Laptop/Lenovo`
(10-column tables that stack below 132 columns), `Main page` (dl lists). For the curses half,
a tmux pane plus `capture-pane -p` (`-e` for colours), with a fake `pbcopy` first on `PATH` that writes
to a file, so copy tests don't clobber the real clipboard.
