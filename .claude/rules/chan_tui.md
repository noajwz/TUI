---
paths:
  - "chan_tui.py"
---

# chan_tui.py

A read-only 4chan reader on the public JSON API (`a.4cdn.org`: `boards.json`, `/{b}/catalog.json`,
`/{b}/thread/{no}.json`; files from `i.4cdn.org`). There is no posting, and there won't be: posting
needs a captcha and the API is read-only. Three screens sit on a stack in `App.screens` —
`BoardsScreen` → `CatalogScreen` → `ThreadScreen` — and Backspace pops one. A thread screen first
unwinds its own `jumps` (positions saved when following a `>>quote`), and a list screen first
clears its filter, before the screen itself is popped.

- **The API's rules are enforced in `api_get()`**: at most one request a second (it sleeps), and
  thread refreshes send `If-Modified-Since`, so an unchanged thread costs a 304 with no body. Watch
  mode (`a`) refetches every `THREAD_REFRESH` s; the API asks for no less than 10.
- **Comments are HTML** and `CommentParser` turns them into `(text, style, link)` runs: greentext
  (`span.quote`), quotelinks, dead links, spoilers (`<s>`), code (`<pre>`), red ban text. Runs are
  merged *before* `linkify()` looks for bare URLs, because `<wbr>` splits long URLs into separate
  data chunks. `link_target()` classifies an href as `post` (`#p123`), `thread` (same or another
  board), `board` (`>>>/g/`, including `catalog#s=` searches, which open the catalog filtered) or `url`.
- **Layout is display-column aware** (`cwidth()`): threads are full of CJK and emoji, and wrapping
  by `len()` overflows the window. `layout_thread()` → `Doc` records each link's row and each post's
  first row; re-layout on resize, refresh or the `w` width toggle (`App.full_width`, capped
  otherwise at `MAX_WIDTH`) keeps the reader on the same post via `anchor()`.
- Spoilers are hidden at *draw* time by swapping each character for `░` of the same width, so `s`
  needs no re-layout. "Replies:" back-links are computed from the parsed quotes (`backlinks()`).
- `v` shows a post's image in the terminal by running `termimage.py` as a **subprocess** if it is
  next to the script (videos get their thumbnail); `chan_tui` must keep working without it.

Testing: `parse_comment()`, `wrap_runs()`, `layout_thread()` and `catalog_entry()` are pure — run
them on a saved `thread.json`/`catalog.json` and assert no `doc.plain(row)` is wider than the width.
For the curses half, a tmux pane plus `capture-pane -p` against the live API. New posts arriving
can be faked by replacing `chan_tui.fetch_thread` with a function that returns a thread minus its
last posts first, then the whole thread, and lowering `THREAD_REFRESH`.
