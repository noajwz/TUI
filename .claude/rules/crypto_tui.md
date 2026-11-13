---
paths:
  - "crypto_tui.py"
---

# crypto_tui.py

BTC, ETH and XMR only (`COINS`), from CoinGecko's keyless public API. One `/coins/markets` call
covers the whole table for all three coins — prices, 1h/24h/7d/30d/1y changes, ATH, supply and an
hourly 7-day sparkline — and is refetched every `REFRESH_SECONDS`. The big chart is
`/coins/{id}/market_chart`, cached per `(coin, currency, days)` for the TTL in `RANGES`.

- **The rate limit is the constraint that shapes it.** The free tier gives 429s after a handful of
  calls in quick succession. A failed chart fetch is not retried for `RETRY_SECONDS` (retrying
  every tick of the 1 s event loop is what keeps a 429 going), and 7D falls back to the
  markets sparkline (`sparkline_chart()`), so the default view costs no call of its own. Test
  sparingly against the live API, or monkeypatch `fetch_markets`/`fetch_chart`.
- **`total_volumes` is a rolling 24h figure**, not volume per interval, so the bars are averaged
  per column (`bucket_means()`, never summed) and scaled from just under the window's minimum —
  from zero they would be a flat block. The strip is labelled `24h vol` for that reason.
- The chart is braille, two data points per cell column and four per row (`braille_chart()`); a
  jump is split between two columns so the line stays joined. `resample()`, `braille_chart()`,
  `bar_rows()` and the `fmt_*` helpers are pure.

Testing the curses half: a tmux pane plus `capture-pane -p`, at 60×20 (the minimum, where the 1h
column is dropped) and 80×24.
