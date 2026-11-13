---
paths:
  - "spotify_chart_tui.py"
---

# spotify_chart_tui.py

The global top 100 as a bar chart, in three views (`CHARTS`): daily, weekly and all time. The data is
scraped from kworb.net's HTML, which needs no key but is a third-party summary of Spotify's charts.
A chart is fetched once when first opened and again only on `r`.

- **The three pages are not the same table.** Daily and weekly have rank movement (`P+`), peak and
  time on chart and link each track; all time has only streams and a daily figure, no track links and
  no date. `parse_chart()` finds columns by heading and `detail()` shows whichever exist, so `o` falls
  back to a Spotify search when a row has no track id.
- **The date in the title is the chart's own**, read off the page, and it lags: on 6 Nov 2026 the daily
  chart was dated 3 Nov. Don't replace it with today's date.
- **Bars start at zero and share one scale across the whole chart**, set by the #1 song, so they don't
  change length on scrolling. Cyan is for the bars; green and red are kept for the movement arrows.
- `columns()` hands spare width to the bars. Under `ARTIST_MIN_LABEL` the artist column goes and the
  artist appears only in the detail lines. Widths go through `cwidth()`, since titles include CJK and emoji.

`parse_chart()`, `columns()`, `row_runs()`, `detail()`, `bar()` and the formatters are pure, so run them
on a saved page and assert no row is wider than the window. For the curses half use a tmux pane plus
`capture-pane -p`, at 50×12 (the minimum) and 80×24. To test without the network, replace
`fetch_chart`. Put a fake `open` first on `PATH` to test `o` without starting a browser.
