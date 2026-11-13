---
paths:
  - "bbc_tui.py"
---

# bbc_tui.py

A reader for BBC News: the World RSS feed and its six regional feeds as tabs (`SECTIONS`), each
story opening in the terminal. The list is `parse_feed()` → `build_rows()`; a story is
`parse_article()` → `article_blocks()` → `layout()`. All of those are pure.

- **RSS carries only a title and a one-line summary**, so the story text comes from scraping the
  linked page, and that is the part that can break. `ArticleParser` reads the page's `<article>`,
  a flat run of `data-block` sections. `BLOCK_TAGS` is an allow-list: `text`, `subheadline`, and the
  captions of `image` and `media`, with `byline` and `metadata` read separately. Anything else
  (`topicList`, `promoList`, `links`, `flourish`, ...) is navigation or an embed and is dropped.
  If the BBC redesigns and the blocks disappear, stories degrade to the feed's summary with a note.
- **Video pages have no blocks**, only loose `<p>`s, which are used only if no block gave any text.
  A page with under `THIN_WORDS` of text also gets the feed's summary. `story_tag()` classifies
  video, live and audio links, which have nothing to read in full and say to press `o`.
- **Text the page holds that a reader never sees has to be removed by hand**: the `, external` span
  that ends outbound links (only inside `<a>`, so a sentence that really says that survives), the
  `Image caption, ` prefixes, bullets that are one whole link (promos dropped into the body), and
  the desk sign-offs in `BOILERPLATE`. A new sign-off is a one-word addition there.
- A bold-only paragraph becomes `lede`. Links are kept as plain text and can't be followed.
- `Line.block`/`Line.offset` (non-space characters into the block) are what keep the reader on the
  same sentence across a resize or the `w` toggle; `line_at()` finds that spot after re-wrapping.
- Feeds are refetched after `REFRESH_SECONDS` (the feed's `<ttl>`), and only while the list is on
  screen; the selection follows its story, not its row, because new stories land on top. A failed
  refresh keeps what is on screen. Read stories dim for this session only; nothing is saved.
- Feed order is the BBC's editorial order, not chronological, and is kept that way.

Testing: run `parse_article()` + `layout()` over every story in the live feeds (about 140 pages,
go gently) and assert no line is wider than the width and that no `, external` or `BOILERPLATE`
text survives. For the curses half, a tmux pane plus `capture-pane -p` (`-e` to see dim and bold),
at 40×10 (the minimum) and 80×24. Replace `bbc_tui.fetch` with a function that can fail on demand
to test the failure paths, and lower `REFRESH_SECONDS` for auto-refresh. Replace `bbc_tui.subprocess`
too, or `o` opens a real browser.
