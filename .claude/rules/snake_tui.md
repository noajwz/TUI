---
paths:
  - "snake_tui.py"
---

# snake_tui.py

Snake on square cells (two characters wide), shaded from head to tail. Four foods:

- apple: grow by 1, 10 points;
- gold: grow by 2, 50 points;
- berry: shrink by 3, 5 points;
- pepper: faster for 60 steps, with points doubled.

Gold, berry and pepper only stay a while and blink before they go. Four modes:

- classic: the walls kill;
- wrap: the edges lead round;
- maze: `maze(level)` gives a new layout every 10 apples;
- zen: a bite cuts the tail off instead of ending the game.

An autopilot (`o`) plays on its own. The best ten per mode are kept in
`~/.local/state/snake_tui/scores.json`, but not games the autopilot played or games of 0
points.

- It is an original snake game in a long-public genre, not a copy of any one version. Its own
  foods, modes and look; keep it that way.
- `Game` has no screen: `steer(name)`, `step()`, `autopilot()`. Turns queue up (`TURN_QUEUE`),
  so two quick presses inside one step both count, and a turn straight back into the neck is
  dropped. The tail cell is free on a step where the snake doesn't grow; that is the
  `hits = body[:-1]` rule.
- Speed is `BASE_TICK`, faster by 2.5 ms per cell of length down to `FASTEST`, and divided by
  `PEPPER_SPEED` while a pepper lasts.
- **The autopilot**: a BFS to the nearest food (berries only once the snake is long). It takes
  that route only if a simulated snake that has eaten could still reach its own tail. Otherwise
  it picks the neighbour that keeps the tail reachable and is farthest from the food: the long
  way round. It plays well: 120 to 185 apples per game on a 30×18 field, and rarely dies.
- The field is sized from the window when a game starts (at most `MAX_W` × `MAX_H` cells, with
  room for the 24-column panel). Shrinking the window under it pauses with a message; `r`
  starts a new game that fits. The panel is left out when only the field fits.
- Maze layouts leave every open cell joined to every other. `start()` looks for a row near the
  middle with room for the snake and a little road ahead.

Testing: for each `maze(level)` check that the snake's start avoids the walls and that a flood
fill reaches every open cell. Check that turning back is ignored, that classic ends at the wall,
that wrap survives 30 steps into it, and that zen cuts the tail on a bite. Run the autopilot for
4,000 steps in every mode over a dozen seeds. In tmux with `HOME=<scratch>`, check the menu,
maze with `o`, classic into the wall (the banner), a resize to 50×16 (the pause message, then
`r`), and `wrap --auto` at 50×16 (dashed edges, the snake going round).
