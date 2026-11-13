---
paths:
  - "wiki_tui.py"
  - "psychonaut_tui.py"
  - "archwiki_tui.py"
  - "chan_tui.py"
---

# wiki_tui.py

A full-screen Wikipedia browser. Unlike the other scripts it renders MediaWiki's **HTML** rather than
the plaintext extract, which is what makes links followable. The pipeline is three stages, and each is
pure and testable without a terminal — import the module and call them directly:

1. `fetch_article(lang, title)` → `action=parse&prop=text` HTML (returns `None` for a missing page).
2. `parse_article(html)` → flat `[Block]`. `ArticleParser` (an `HTMLParser`) drops whole subtrees by tag
   (`SKIP_TAGS`) or by CSS class (`SKIP_CLASSES` — references, navboxes, editsection links, infoboxes),
   tracking `skip_at` against an element-depth counter. Link offsets are recorded as `(start, end, kind,
   target)` character positions *into the block's own text*; `kind` is `page`, `anchor` or `external`.
3. `layout(blocks, width)` → a `Doc` of display lines. `wrap_block()` word-wraps while mapping those
   character offsets onto `(col0, col1)` screen columns, so a wrapped link stays one selectable `Link`.
   `prune_empty_sections()` drops headings whose bodies were skipped (References, Citations).

Re-`layout()` is what happens on resize; parsed blocks are cached per `(lang, title)` so history
navigation and re-wrapping never re-fetch. `Browser` holds all UI state; every network call goes through
`Browser.call()`, which paints a "Loading…" bar and converts exceptions into a message string (HTTP 429
from the API is common when hammering it, and is reported in plain words).

Text is capped at `MAX_WIDTH` columns for readability; `w` toggles between that and the full window
(`full_width`, honoured by `text_width()`). `toggle_width()` keeps the reader on the same text
because `Doc` has no line → block map: `text_offset()` counts the characters above the top line that
don't depend on the width, and `line_at_offset()` finds that character again after re-wrapping.
`text_size()` must exclude anything `layout()` draws per line or to the width — spaces, `rule`
lines and the `│` bar on quotes — or the position drifts; check by laying a long article out at
two widths and asserting the totals match. `psychonaut_tui` has the same three functions.

Failures must stay on screen and recoverable: `show_notice()` renders an error or the welcome text as if
it were a page, so a missing article or a dead network leaves a readable screen rather than exiting.

Testing it without a human: drive it through a pty (`pty.fork`, set the size with `TIOCSWINSZ`, write
keystrokes to the fd, strip ANSI from what comes back). That covers resize, link following and history.
The parse/layout stages need no pty at all — assert on `doc.lines`, `doc.links` and `doc.headings`.
