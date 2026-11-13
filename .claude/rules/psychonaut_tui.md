---
paths:
  - "psychonaut_tui.py"
  - "archwiki_tui.py"
---

# psychonaut_tui.py

A fork of `wiki_tui.py` for [PsychonautWiki](https://psychonautwiki.org), which is a stock MediaWiki
with an open `api.php`, so the network layer is the same three calls minus the language ones
(`--lang`, the `L` key and `translated_title()` are gone; the cache and history are keyed by title
alone). It is a **separate self-contained script on purpose**, following the `wiki_reader`/`wiki_tui`
precedent: the two share no code at runtime, so a fix to the parser or layout of one has to be
applied to the other by hand.

The reason it is not just `wiki_tui --site`: tables. `wiki_tui` drops every `<table>`, which is
right for Wikipedia and wrong here, because the substance box at the top of every drug page
(`#InfoTable` with nested `.ROATable`s) is the thing people open the site for — dose tiers
(Threshold/Light/Common/Strong/Heavy) and duration per route, plus the interactions list. So:

- Tables are rendered **cell by cell** (`<tr>`, `<td>`, `<th>` are block boundaries), which also
  handles the unclassed tables the wiki uses as multi-column layout containers. Only `wikitable`
  data grids are omitted (`SKIP_TABLE_CLASSES`), with the same `[ table omitted ]` marker.
- The substance box is class-labelled per cell, and `CELL_KINDS` maps those classes to block kinds.
  A `RowHeader`/`ROARowHeader` cell becomes a `label` that is held back in `ArticleParser.label`
  and attached as the `bullet` of the `RowValues` cell that follows; the pair is one `row` block.
  `layout()` pads the labels of a run of consecutive rows to the same width (`row_label_widths()`),
  which is what makes a dosage table line up as a column.
- Interaction cells (`SBInteractionDangerous` / `Unsafe` / `Uncertain`) carry their severity only
  as a class — the text is just the substance name — so `INTERACTION_LEVELS` supplies the word.
- `ROACaption` cells (the WARNING and DISCLAIMER prose) render as dim quotes; the collapse arrow
  the site puts in front of route names (`⇣ Sublingual`) is stripped in `_flush()`.

Every substance also has an `X/Summary` page, reachable from the "Summary sheet" link at the top.
It is built around a 2-D dose-scale table that does not reduce to text, and the main page's box
already holds the same numbers, so there is deliberately no shortcut key for it.

Testing is the same as for `wiki_tui`: `parse_article()` + `layout()` on a saved
`action=parse` response for the pure stages (`LSD` has three routes and an interactions list,
`Cannabis` has collapsed `mw-collapsed` route tables, `Psilocybin mushrooms` has the layout
tables, `Psychedelic` has no substance box at all), and a pty for the rest.
