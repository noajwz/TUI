---
paths:
  - "compound_tui.py"
---

# compound_tui.py

A compound interest calculator: five inputs, a results panel, and a stacked area graph of deposits
(blue) with interest on top (green). No network and nothing saved. It recalculates on every repaint,
which is cheap (at most 1200 months), and that is why the graph can follow a number as it is typed:
`App.current()` applies the unfinished `draft` to a copy of the values, and Esc just drops the draft.

- **Growth is at the equivalent monthly rate**, `(1 + r/n)^(n/12)`, whatever the compounding. Lump
  sums therefore match `P(1 + r/n)^(nt)` exactly, and monthly deposits at monthly compounding match
  the ordinary annuity formula; those are the checks to run after touching `project()`. Deposits
  land at month end. A real account on yearly compounding credits interest once a year, so its
  balance would climb in steps between those dates where this curve is smooth.
- **The graph is eighth-blocks, not braille**, because it needs two filled colours. A cell holding
  the top of the deposits is drawn with the deposits as foreground and the interest as background
  (`BOTH`). When the top of the balance falls in that same cell there is empty space above too, and
  a cell cannot show three things, so `area_cell()` paints it in whichever colour has the larger
  share. That leaves a kink under a cell tall where the interest band is still thin; it can't be
  removed, only moved.
- Rates can't go below 0, because the stack assumes the balance is never under the deposits.
- Amounts default to euros; `c` switches to dollars and back (`SYMBOLS`), and `--symbol` takes any
  other symbol, which then joins the cycle. It only changes the symbol: there is no exchange rate.
- ←→ change a field and also move the graph cursor, so Tab switches between the two. The graph is
  read a year at a time, or by the month up to `MONTH_STEP_YEARS`.

`project()`, `nudge()`, `nice_top()`, `area_cell()`, `tick_rows()` and the `fmt_*` helpers are pure.
Testing the curses half: a tmux pane plus `capture-pane -p`, at 66×20 (the minimum) and 80×24; run it
with `--start 1000000000000 --monthly 1000000000 --rate 100 --years 100 --compounding daily` for the
widest numbers. Text capture can't tell a `BOTH` cell from a filled one, so check colours with `-e`.
