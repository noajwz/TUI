---
paths:
  - "sunken_tui.py"
---

# sunken_tui.py

An original roguelike set in a drowned city: twelve depths, then the Tide-Heart on the altar in the
palace at depth 12. Taking it floods the palace and opens the upward current (`EXIT`) at the
depth's start, and stepping onto that with the Heart wins. Permadeath; runs are scored into a hall
of records. At ~4,000 lines it is the largest script here. It follows the curses-TUI shape, with
`run(stdscr, …)` under `curses.wrapper` and a blocking `getch()`.

**`Game` knows nothing about the screen.** Actions (`player_move`, `drink`, `throw`, `zap`,
`tame`, …) return the time they took, 0 for none, and `UI.spend()` passes that to
`Game.spend()`. Anything that needs a menu mid-action (a shrine underfoot, bumping the hermit, a
shell of lore) sets `game.pending`, which `UI.settle()` handles. That separation is what lets the
headless bot below play whole runs.

- **Time** is energy-like: every actor has `nxt`. `run_world()` lets creatures act until the
  player's `nxt` comes round, and `tick_to()` runs `world_tick()` once per 100 units. Each world
  tick moves the tide, ice, clouds, statuses, breath, oil, regeneration and relic cooldowns.
  Speeds are 100-based (`speed()`, `mspeed()`).
- **Water is elevation against a level, not painted tiles.** `make_elevation()` gives every tile
  a height: noise, whole rooms sunk or raised, canals low, bridges high. `Level.update_water()`
  floods the lowest share of the floor, `0.08 + 0.032 × (depth − 1)` plus the tide's shift
  (`TIDE_SHIFT`), with the lower 48% of that deep. So the tide is only a number changing, and the
  share of water stays the same whatever the layout. The start, the stairs and the altar (3×3)
  are set to elevation 5, so they stay dry, even through the flood after the Heart. Vaults sit at
  −2, so they stay deep at most tides.
- **Sight is light.**
  - `compute_light()` unions shadowcasts from the lit lamps and coral (cached in
    `static_light` until `light_dirty`), glowing algae, the lantern (only when lit, oiled and not
    under water), glowing or lantern-carrying creatures, and anglers' lures. Ink removes light.
  - You see a tile in your field of view if it is lit, adjacent, or within dark-sight.
  - Creatures see you (`monster_sees()`) at their `sight` range when your tile is lit, and at
    their much shorter `dark` range when it isn't.
  - Kelp hides you completely. Deep water hides you from anything that doesn't swim.
  - Noticing is a chance per turn (`notice_chance()`), lowered by stealth, sneaking and hush.
  - `blind` creatures (drowned sailors) only find you by noise; moths and wraiths go for a lit
    lantern regardless.
- **Noise** (`noise()`) is a BFS through non-solid tiles, with doors costing 3. It wakes sleepers
  by chance and sends wanderers to search. `step_noise()` is what each step costs, depending on
  the water you're in.
- **AI**: states are sleep, wander, search, hunt, eat (bait), ambush, flee and idle. Movement
  uses BFS flow fields per (target, movement class) (`field()`), cached for one player turn.
  The classes are walk (never into deep water), swim, aquatic (water only), deep, and fly. An
  aquatic creature left on dry ground by the tide flops toward water and takes damage.
- **Sneak attacks**: anything not hunting you (`unaware()`) is hit automatically, for damage ×
  the weapon's sneak multiplier.
- **Identification**: what vials, shells, rods and charms look like is shuffled per run into
  `game.looks`, and `game.known` holds identified keys. Keys must be unique across
  `VIALS`/`SHELLS`/`RODS`/`CHARMS`: the charm `guard` was once `warding`, which collided with the
  shell.
- **Growth**: the five `SKILLS` gain experience from use (`gain()`, `SKILL_STEPS`). Maximum
  health comes from depth reached (`HP_PER_DEPTH`) and vials of vigor. Relics (`RELICS`, one pool
  per run), shrine pacts (`PACTS`, a boon and a bane each) and gear do the rest.
- **Companions**: tame (`T`) a beast when it is hurt, asleep or eating thrown bait. They follow,
  fight, grow every second kill, and come down the stairs when within 3 and following. Aquatic
  ones can't leave their water. The Siren's Conch charms anything but the King for 40 turns
  (`charm_turns`).
- **Saving**: `save_game()` pickles the whole `Game` with the RNG state, on `S`, on every descent
  and on Ctrl-C. The save is deleted when the run ends (`record()`), not when it is loaded, so a
  crash doesn't lose a run. The classes pickle as `__main__.*`, so a save made by importing the
  module under another name won't load in the game.
- `explore_step()` (`o`) walks to the nearest remembered item or frontier, counting closed doors
  as frontier (without that it stopped at the first room with doors). It swims only when nothing
  else is left, and only with full breath and no heavy armour. "Half your breath is gone" and
  "lungs burn" are coloured messages that interrupt it. `>` travels to seen stairs, with deep
  water weighted heavily.

Testing: never against the real `~/.local/state/sunken_tui`. Point `STATE_DIR`, `SAVE_PATH`
and `RECORDS_PATH` at a scratch folder when importing, or run the window with `HOME=<scratch>`.

- Generate every depth for 40 seeds and check that the start is dry and the stairs exist.
- Run a headless bot that explores, fights, tames, uses random items and relics, takes pacts,
  trades, sneaks, toggles the lantern, pickles and unpickles mid-run and descends. With full
  health every turn it should reach depth 12 and win some runs; with normal health its deaths
  show the balance.
- For the window, build a save by exec'ing the source into `__main__` (so the classes pickle
  under that name), descending to any depth, and `save_game()`. Then launch under tmux, at
  120×34 and the 80×24 minimum, with `TERM=xterm` for 8 colours. A save placed next to a shrine
  or the hermit tests those menus.
