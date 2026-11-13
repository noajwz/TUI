---
paths:
  - "election_tui.py"
---

# election_tui.py

All 60 elections from 1788 to 2024 on three screens: map, state-by-state table, and one line per
election. Nothing is fetched at runtime. The two data blocks at the bottom (`SHAPES`, `ELECTIONS`)
are most of the file's 160 KB, and the code above them is about 900 lines.

- **The data is plain text, meant to be edited by hand**, and the format is described above the
  block. `check_data(ELECTIONS)` asserts every candidate's state votes add up to their national
  figure; run it after any edit. It was generated once from Wikipedia's per-election state tables
  plus a hand-kept list of candidates, parties, running mates and notes, by throwaway scripts that
  aren't in the repo.
- **Where sources disagree, the numbers are what the tables say.** National popular votes come from
  a table's Total row, which can differ slightly from the sum of its states: under 1% in 1828 and
  3% for Clay in 1832. In 1872 the gap is the rejected Arkansas and Louisiana returns, which are
  in the national figure but not in `states`. 1960 is the one override: Byrd's 610,409 is the state
  sum, because the Total row counts only some of the unpledged electors. Popular votes before 1824
  are left out on purpose.
- **The electoral-vote oddities are real, not bugs.** 1788-1800 electors cast two votes, so the
  totals are double and `MAP_RIVALS` picks who the map shows for 1796 and 1800. A state's `ev` can
  exceed the votes it cast (absent electors, D.C. in 2000), and the difference is deliberate.
  1788 and 1792 have per-state figures only for Washington. 1872's 66 votes are those of the
  electors pledged to Greeley, who died before they met; the note explains where they went.
- **`no_vote` is different from not existing.** A state missing from an election is drawn as not yet
  a state unless it is in `no_vote` (1788, 1864, 1868, 1872), or `PARENT_UNTIL` says its land still
  belonged to another state. Shapes are modern borders throughout.
- **The map is half-block pixels, two per cell**, filled from the polygons by `rasterise()` for the
  exact window size. `picture()` then colours them, darkens the pixels along borders between states,
  and stripes split states. A label goes inside its state only if it fits (`place_label()`, widest
  form first), and otherwise becomes a callout beside the map, so a bigger window moves labels onto
  the map on its own. The shapes were simplified per shared arc, so neighbouring states still meet.

`parse_elections()`, `rasterise()`, `picture()`, `state_parts()` and `check_data()` are pure.
Testing the curses half: a tmux pane plus `capture-pane -p`, at 300x90 and 120x40, at 80x24 (where
the panel stacks under the map) and at 50x16 (the minimum). Check an old map too, 1788 or 1796, plus
`TERM=xterm` for the 8-colour path and `TERM=vt100`, where the map gives way to a line of text.
