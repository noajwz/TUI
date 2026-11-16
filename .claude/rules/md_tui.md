---
paths:
  - "md_tui.py"
---

# md_tui.py

A Markdown reader: renders one file (a folder opens its README, `-` reads stdin) with headings,
emphasis, lists and task lists, quotes and GitHub alerts (`> [!WARNING]`), fenced and indented
code, tables as box-drawn grids, footnotes, reference links and inline/block HTML reduced to its
text, images and links. Tab selects links *and* code blocks; Enter follows a link (Markdown files
open in place, with Backspace to come back; `#anchors` jump; anything else goes to `open`) or
copies a code block. `t` is the contents, `/` search, `e` opens `$EDITOR`. It checks the file's
mtime every `CHECK_SECONDS` and redraws when it changes, so it doubles as a live preview.
`--once` prints the rendering (ANSI on a tty, plain when piped).

Three stages, each pure and testable without a terminal — the same split as `wiki_tui`, but no
code is shared with it:

1. `parse(text)` → `[Block]` plus a `Context` of reference links and footnotes. A small
   CommonMark-ish block parser: containers (quotes, list items) collect their lines, strip their
   marker or indent, and recurse into `parse_blocks`. Lazy continuation lines are followed;
   setext headings, `starts_block()` deciding what interrupts a paragraph, and list items whose
   content column comes from the marker width are the parts most worth keeping right.
2. `inline(text, ctx)` → spans of `(text, flags, Link)`. Emphasis isn't the full CommonMark
   delimiter algorithm: `emphasis()` looks ahead for a closer, refuses intraword `_` (so
   `snake_case` survives) and spaced `*` (`2 * 3`), and lets a single `*` skip over `**` runs.
3. `layout(blocks, ctx, width)` → `Doc`. `render()` returns `[Line]` without knowing where they
   land; containers prepend their bar/bullet segments with `prefixed()`. `Doc.__init__` then walks
   the lines once to find heading lines, anchors (GitHub slugs, `-1` for duplicates) and each
   link's rectangles — computed after the fact, which is why links inside tables and quotes work
   without extra bookkeeping. Footnotes are numbered in order of first reference, so
   `ctx.note_order` is reset before each layout.

Segments carry a semantic `kind` (`h1`, `code`, `rule`, `bullet`, …) and inline `flags`;
`App.seg_attr()` and `ANSI` turn them into curses attributes or escapes, so layout never touches
curses. Every `Line` remembers its top-level block, which is how a resize, `w` or a live reload
keeps the reader at the same place (`position()` → `relayout(keep)`).

Testing: `--once --width N` on a torture file (front matter, setext, lazy lists, loose lists,
nested quotes, alerts, tables with `\|` and alignment, HTML blocks, footnotes, reference links)
and on big real files — Homebrew's `/opt/homebrew/docs/Manpage.md` (5 k lines) renders in well
under a second. `parse`/`layout` can be called directly to assert on `doc.links`,
`doc.anchors`, `doc.headings`. The curses half works in tmux at 90×24 and the 30×6 minimum;
quote `'?'` and `']'` for `tmux send-keys` in zsh.
