---
paths:
  - "pokemon_types_tui.py"
---

# pokemon_types_tui.py

`CHART` is the Generation 6+ chart (the one with Fairy), stored sparsely: attacker → only the
non-1× defenders. It should hold exactly 51 super-effective, 61 not-very-effective and 8 no-effect
entries; counting those is the quickest check after editing it. `multiplier()`, `defend_groups()`,
`attack_groups()` and `parse_type()` are pure. Attack mode with several types picked scores each
defender by the **best** of them (move coverage), not the product — multiplying only makes sense
for a defender's two types. Testing the curses half: a tmux pane plus `capture-pane -p`, at 80×24,
the smallest size it draws at.
