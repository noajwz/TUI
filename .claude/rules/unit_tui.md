---
paths:
  - "unit_tui.py"
---

# unit_tui.py

A unit converter driven by one free-text line: `5 ft 11 in to cm`, `72 F`, `1 cup flour to g`,
`8 L/100km to mpg`, `3 h 20 min / 4`, `0xff to bin`, `100 usd to eur`. The result updates as
you type; below it, a list of every kind of unit and a table of the value in each unit of the
current kind (Tab steps through kinds). All printable keys go to the line, so commands are
control keys (^S swap, ^T make the selected row the target, ^P precision, ^E an example, ^Y
copy with unit, F1/^G help) and Esc clears, then quits. `curses.raw()` is on so ^S and ^Y reach
it instead of the terminal. `--once "expr"` prints the result, `--table` the whole kind.

- **Dimensions, not lists.** Every unit is a factor times a vector of exponents over `DIMS`
  (m kg s A K mol cd rad bit EUR). `UNITS` defines them per category, each as an expression in
  units defined above it (`"mi/h"`, `"5/9 K"`), parsed by the same parser as the input, so
  anything that multiplies out converts — `J/(kg K) to cal/(g K)` needs no entry.
- **The grammar.** sum → product (`*` `/`) → juxtaposition → power → unary → atom. Writing units
  side by side multiplies and binds *tighter* than `/` (GNU units' rule): `J/kg K` is J/(kg K),
  `L/100km` is L/(100 km). A number after a unit inside a juxtaposition starts a new term to add,
  which is how `5 ft 11 in` and `1 h 30 min` work. The catch for definitions: `K 5/9` would be
  K + 5/9, so write `5/9 K`, and `pi/180 rad` would divide by radians, so `pi rad/180`. The lexer
  folds `1/2`, `3 1/2` and `3 ½` into one number when a unit follows, reads `m2`/`cm³` as powers
  (unless `g0`/`cmH2O` is itself a unit), and `5'11"` as feet and inches — or arcminutes and
  arcseconds after a `°`.
- **Lookups** (`lookup`): exact symbol, lower-cased name or plural, SI prefix (binary prefixes
  only for bit and byte), spelled-out prefix (`kilometres`), then lower case (`KM`). Symbols of
  fewer than three letters aren't added lower-cased, or `ps` would be metric horsepower.
  `PHRASES` rewrite multi-word names first (fluid ounce, miles per hour, square/cubic X, per).
- **Bridges** in `convert_value`: equal dimensions; reciprocal (L/100 km ↔ mpg); a difference
  only in the angle exponent, counting a cycle as a revolution (Hz ↔ rpm); volume ↔ mass through
  an `INGREDIENTS` density (water when none is named, with a note).
- **Temperature** keeps its offset only when the input is exactly `[-]number unit` with a
  temperature unit (`single_affine`), where a lone `C`/`F` means Celsius/Fahrenheit; anywhere
  else the scales are differences and C/F are coulomb/farad.
- **Which table** (`pick_category`): Typography, Radioactivity, Radiation dose and Angular speed
  win when you wrote one of their own units; Torque (whose units belong to Force and Length) when
  the unit text matches one of its rows; otherwise the kind of the units written, then the first
  kind with those dimensions. With no target the line shows `COUNTERPART`, else the kind's SI unit.
- **Currency** comes from the ECB's daily XML, fetched on a thread the first time an ISO code or
  money sign appears (`wants_rates`); nothing touches the network otherwise. EUR is the base.

Testing: `--once` covers almost everything — temperatures both ways, `5 ft 11 in`, `5'11"`,
`12°30'15"`, fractions, decimal commas, `8 L/100km to mpg`, `60 Hz to rpm`, `1 cup flour to g`,
`6.2 ft to ft in`, `100000 s to d h min s`, `50 lbf ft to N m`, bases, and errors (`5 kg to m`,
`1/0 kg`, an unknown word). Every table row must parse: loop `table_units()` over
`CATEGORY_ORDER`. The curses half works in tmux at 100×28 and the 60×16 minimum; send text with
`send-keys -l`.
