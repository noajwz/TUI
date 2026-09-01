# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Small terminal programs written for fun. **No package, no dependencies, no tests, no config files** —
every script is stdlib-only (`curses`, `urllib.request`, `json`, `subprocess`) and self-contained.
Adding a third-party import would be a departure from the project's premise; prefer stdlib.

Run any of them directly:

```bash
python3 bt_battery_tui.py    # curses; Bluetooth battery levels, polls every 5s
python3 pwgen_tui.py         # curses; password generator, copies via pbcopy
python3 tictactoe.py         # plain stdin/stdout; 2-player or minimax AI
python3 wordle_tui.py [--daily] [--hard] [--seed N] [--word W]   # curses; Wordle
python3 claude_usage_tui.py [--once]   # Claude usage limits, read from ~/.claude.json
python3 rooster_tui.py [--once]        # curses; work roster from an .ics feed, refetched every 15m
python3 btc_tx_check.py <64-hex-txid> [--testnet]     # Blockstream API
python3 wiki_reader.py <topic> [--lang nl]            # Wikipedia API, pipes to `less -R`
python3 wiki_tui.py [topic] [--lang nl]               # full-screen Wikipedia browser
python3 termimage.py IMAGE|--wiki TOPIC [--mode ...]  # images in the terminal
python3 termimage.py --probe                          # what does this terminal support?
python3 ascii_city.py        # curses; endless procedural city in the rain, two views
```

Ruff is the linter (a `.ruff_cache/` from ruff 0.16.1 is present but gitignored). Ruff is **not
installed on this machine and there is no pyproject/ruff.toml** — it runs with defaults, e.g.
`ruff check . && ruff format .` once available.

Three shapes exist and each new script should follow one of them:

- **curses TUIs** (`bt_battery_tui`, `pwgen_tui`, `claude_usage_tui`, `rooster_tui`, `wordle_tui`): `main(stdscr)` launched via `curses.wrapper(main)`,
  a `stdscr.timeout(...)` + `stdscr.getch()` event loop, module-level constants for tunables
  (`REFRESH_SECONDS`, `MIN_LEN`/`MAX_LEN`, option tables).
- **CLI scripts** (`btc_tx_check`, `wiki_reader`): `argparse` in `main()`, raw ANSI escape constants
  (`BOLD`, `CYAN`, `RESET`, …) defined at module top rather than a color library, and network helpers
  that return `(data, error)` tuples instead of raising.
- **`wiki_tui`**, **`termimage`** and **`ascii_city`**, the larger programs, described below.

`wiki_reader.py` and `wiki_tui.py` are deliberately separate takes on the same idea and neither
supersedes the other: `wiki_reader` is the one-shot pager version (pipeable, scriptable), `wiki_tui`
is the interactive browser. Keep `wiki_reader.py` as it is.

macOS-specific by design: `system_profiler` for Bluetooth, `pbcopy` for the clipboard, `sips` for
image conversion.

`claude_usage_tui.py` reads the utilization block Claude Code caches in `~/.claude.json`. It is
strictly read-only and makes no network calls — any running Claude Code session refreshes that cache,
which is what makes it live rather than a snapshot. Keep it read-only.

## wiki_tui.py

A full-screen Wikipedia browser. Unlike the other scripts it renders MediaWiki's **HTML** rather than
the plaintext extract, which is what makes links followable. The pipeline is three stages, and each is
pure and testable without a terminal — import the module and call them directly:

1. `fetch_article(lang, title)` → `action=parse&prop=text` HTML (returns `None` for a missing page).
2. `parse_article(html)` → flat `[Block]`. `ArticleParser` (an `HTMLParser`) drops whole subtrees by tag
   (`SKIP_TAGS`) or by CSS class (`SKIP_CLASSES` — references, navboxes, editsection links, infoboxes),
   tracking `skip_at` against an element-depth counter. Link offsets are recorded as `(start, end, kind,
   target)` character positions *into the block's own text*; `kind` is `page`, `anchor` or `external`.
3. `layout(blocks, width)` → a `Doc` of display lines. `wrap_block()` word-wraps while mapping those
   character offsets onto `(col0, col1)` screen columns, so a wrapped link stays one selectable `Link`.
   `prune_empty_sections()` drops headings whose bodies were skipped (References, Citations).

Re-`layout()` is what happens on resize; parsed blocks are cached per `(lang, title)` so history
navigation and re-wrapping never re-fetch. `Browser` holds all UI state; every network call goes through
`Browser.call()`, which paints a "Loading…" bar and converts exceptions into a message string (HTTP 429
from the API is common when hammering it, and is reported in plain words).

Failures must stay on screen and recoverable: `show_notice()` renders an error or the welcome text as if
it were a page, so a missing article or a dead network leaves a readable screen rather than exiting.

Testing it without a human: drive it through a pty (`pty.fork`, set the size with `TIOCSWINSZ`, write
keystrokes to the fd, strip ANSI from what comes back). That covers resize, link following and history.
The parse/layout stages need no pty at all — assert on `doc.lines`, `doc.links` and `doc.headings`.

## termimage.py

Displays an image in the terminal two different ways, and is importable so `wiki_tui` could use it
later — but `wiki_tui.py` does not depend on it and must stay that way unless asked.

Usage is documented in `termimage.1`, a roff man page (`man ./termimage.1`, or `mandoc -T lint`
after editing it — it currently lints clean). Keep it in step with the argparse options.

- **Approach A, characters** (works anywhere): decode to RGBA, box-filter resample, then print `▀`
  per cell with the foreground set to the top pixel and the background to the bottom — two pixels per
  cell. `render_ascii()` is the further fallback for terminals without 24-bit colour.
- **Approach B, real pixels** (kitty graphics protocol, which Ghostty and kitty both speak): send the
  PNG itself and let the terminal draw it. `t=t` hands over a *temp file path* instead of the pixels
  (~100 byte escape instead of megabytes; the terminal deletes the file), and `t=d` base64s the image
  inline in 4096-byte chunks for when the terminal is on the far end of an SSH connection.

`kitty_supported()` asks the terminal rather than guessing from `$TERM`: it sends a graphics query with
a DA1 query (`ESC[c`) chasing it, so there is always a reply to wait for — if DA1 answers and no `_G`
reply arrived, the protocol is unsupported. Read the reply with `os.read` on the raw fd;
`sys.stdin.read(1)` blocks filling its text buffer and the probe hangs.

The PNG decoder is complete for still images and deliberately does not lean on external tools: all five
colour types, bit depths 1/2/4/8/16, all five scanline filters, and Adam7 interlacing. That matters
because **`sips` preserves interlacing and bit depth on a PNG→PNG conversion**, so "convert it with
sips first" does *not* rescue those two cases — it returns a file that fails the same way. `sips` is
used only to transcode non-PNG formats (JPEG, WebP) and to downscale before transmission.

Testing without a graphical terminal: fork a pty, answer the graphics query with `ESC_Gi=31;OK ESC\`,
and assert on the escapes the program emits (one `a=T` placement header, `m=1` on every chunk but the
last, base64 payload reassembling into a valid PNG). Both the answering and silent cases must be
tested, since the silent one is what exercises the fallback.

## ascii_city.py

An endless procedural city with **two renderers** over one screen loop. Both `render_skyline()` and
`render_street()` fill the same `(ch, co)` pair of character/colour grids, so the blit loop, the HUD
and the input handling in `main()` serve either; `tab` switches, and each view keeps its own camera
so switching back and forth loses neither position.

They also share one weather clock. Both call `rain_intensity(now)` and `lightning(now, wet)`
themselves rather than being handed a number, so the downpour you tab out of is the downpour falling
on the skyline, and a strike lights both. This is worth a test, and has one: tabbing used to land you
in a dry, still postcard, which is exactly the kind of break that no amount of looking at one view
will show you.

`wander()`, behind the spacebar, blends every direction it probes by score instead of picking the
best one. Picking the winner outright makes the walk twitch, because the winner changes from one
frame to the next: measured over eighty seconds that was 395 hard snaps, some of them a full
3.8-radian flip. Blending gives a heading that moves continuously and sits between two equally good
options rather than flapping between them; `main()` then eases towards it and slows down through
the turn, which takes the frame-to-frame change from 0.28 rad to 0.01.

Nothing about the city is stored. The skyline is cut into chunks seeded by name
(`f"city|{layer}|{i}"`); the street's every question is answered by a hash of the cell's own
coordinates. Caches (`_cache`, `_open_cache`, `_lots`, `_road_cache`) are throughput, not state —
clearing them changes nothing you can see, and there is a test that proves it. They evict through
`_evict()`, which drops the oldest third rather than wiping the lot: a full wipe makes the next
frame rebuild every cell in sight at once, and on a long enough walk that is a visible hitch. Dicts
keep insertion order, and while you are moving the oldest entries are the ones behind you. Generation stores a
**`tone` float rather than a curses colour**, because the palettes differ between an 8-colour and a
256-colour terminal and a building has to be the same building in both.

### The skyline is a waterfront

Three parallax layers of chunk-generated buildings standing on the far bank, and the water in front
of them. `dens` falling away with distance is most of what separates the layers — the far bank is a
dim shape, the near one is a lit building — and `gap` widening as it comes forward is what keeps the
near layer a row of towers with sky between them instead of a wall across the bottom. Both were the
fix for a view that had become an unreadable mash of glyphs with no silhouette in it.

`reflect()` mirrors everything above the waterline into the water. An ASCII reflection lives or dies
on being broken up: same glyphs, displaced sideways by a swell that changes with depth and time, and
thinned out with depth until it is gone in eight rows or so. Let it survive further down and it
stops reading as water and starts reading as the picture having been printed twice. Rain roughens
the surface, so the harder it comes down the less of the city you can read in it.

### The street view is a grid raycaster

It did not start that way: when you could only walk up and down one avenue, the facades were two
planes at fixed `x` and a column could solve directly for what it saw. Free turning killed that.
`cast()` now runs a DDA over cells of `CELL = 5.0` world units, in cell units so the arithmetic
stays the standard form, and returns the walls a column meets nearest-first. Because the ray is
built as `dir + plane * cam`, the distance that falls out is already measured along `dir`, so
there is no fisheye to correct at the edges.

- **No depth buffer.** Three per-column arrays — distance, top row, base row — are the whole of the
  depth information, because a facade is a vertical plane. That is why the sky is drawn *after* the
  walls: an unlit window is a blank cell, so `wall_top` is the only thing that knows a building is
  in the way, and `hidden()` is the test every prop, drop and reflection uses.
- **Casting does not stop at the first wall**, because a tall building behind a short one still
  shows over its roof. Hits are drawn near-first, each clipped to the rows above everything already
  drawn, and the loop breaks once the cover reaches row 0.
- **A ray that hits nothing** is looking out of the city down a cross street. `draw_sky()` gives
  those columns a taller haze silhouette; without it the vanishing point is a hole.
- A horizontal line on a facade is a **slope on screen**, so roofs and awnings go through
  `facade_line()`, which fills from the previous column's row to this one's. One cell per column
  instead and the line breaks into dashes.
- **Corner bars are only for real corners.** Every cell is its own building, so a naive "the face
  changed, draw a bar" test puts a picket fence down every street. `draw_walls()` first checks
  whether the column to the left was the building *next door on the same plane*; that is a terrace,
  and the step in the roof line is the only edge actually there.
- Windows get the middle of their bay or a near facade smears into horizontal stripes; a flat sign
  letter gets exactly one column, the first of its bay, or the word stutters as `CCCLLLUUUBBB`.
  `draw_hung_sign()` lays its letters out **in rows, not world units** — spacing them by a true
  height makes two of them round onto the same row as the sign recedes, and a HOTEL with the T
  missing is worse than one slightly the wrong size.

### Getting lost is the feature

The layout is deliberately irregular, because the point is to be able to lose yourself in it.
`_is_road()` puts avenues on a period but varies their width and offset and drops roughly one in
thirteen entirely, so two blocks merge into a long one — **but never two in a row**. Left to chance
they clump, and three missing avenues in a row leaves a 180-unit stretch with no cross street
anywhere in it. That is rare enough that you only meet one after wandering for a while, which is
exactly what made it hard to pin down when it was reported as "the streets change when walking for a
long time": it stops reading as a long block and starts reading as the city having quietly turned
into somewhere else. `_built_over()` gating on its own predecessor caps the run at one and holds the
worst gap to 90 units, while still merging as many blocks as before. `_is_alley()` cuts a one-cell passage
through a block and then breaks it into stretches, one in five missing — that is what makes a dead
end you have to back out of.

`road_at()` is the switch everything hangs off: a face on a proper street gets neon, the lit
palette and someone smoking outside it; a face on an alley gets the `dark` palette at any range,
a third of the lit windows, a fire escape, a dumpster and a bare bulb over the door. No awning
either — an alley has no shopfronts to put one over. That single boolean is why an alley reads as
somewhere you should not go.

### Rain and lightning

`rain_intensity()` is three slow sines that never line up, so the weather swells to a downpour,
eases and occasionally stops; the HUD says which. Everything else scales off that one number —
drop count, the `~` standing on the road, how far neon spills, whether stars are out at all.

`lightning()` is deliberately, testably **rare**: about one strike every five or six minutes, and
only while `wet >= STRIKE_WET`, so it belongs to the storm rather than happening at random. Time is
cut into slots and only one slot in seven has a strike in it, which is the same
answer-from-a-hash trick the layout uses — no state, and the same strike at the same moment however
you got there. One clean flash reads as a rendering bug, so a strike is two or three sub-flashes a
tenth of a second apart with an afterglow decaying behind them.

A flash is the only thing in the program that reaches across every pass at once, through `v.flash`:
the sky fills pale so everything with a roof turns into a silhouette against it, every corner and
roof line goes white, the rain lights up, the road takes a wash — and `wall_colour()` promotes alley
faces from the `dark` palette to `far`, which for half a second shows you what is down there.

Drops sit on a **world-locked lattice** so they hold still relative to the city while you walk and
turn through them, and each is drawn as the segment it fell through since the last frame, which is
what gives the streak its slant — the wind's and the perspective's — without faking anything in
screen space. Reflections are exact rather than approximated: a mirror plane at `y = 0` puts the
image of a thing at height `h` in the same column, which is all `View.reflect_row()` is.

### The club

One building in `CLUB_ODDS` is not offices — roughly one every 265 world units, so you come across
one every few minutes of wandering. It is decided last in `lot()`, after the faces are made, so that
being a club changes nothing about the building it was otherwise going to be.

It carries **no sign of any kind**, which is the point: `draw_club_column()` replaces the whole
normal facade path, so there is no neon, no hung sign, no awning and no window grid — just a squat
windowless slab, speckled rather than filled so it reads as a mass instead of a hole in the street.
What gives it away is `club_light()`: four to the floor at 134 BPM, white on the kick, holding a
laser colour for a fifth of a beat after it, and the strobe let off the leash for the whole of every
eighth bar. That one function drives the slit windows under the roof, the door, how far the
light spills across the wet pavement — `collect_glow()` calls it too, so the puddles pulse in time —
and the five people outside, who go white together on the kick.

Three of those five are ravers and the rest are smoking about it. `draw_raver()` puts them on the
same clock as the sound system, offset by a fraction of a beat each so the pavement is in time
without being in lockstep, and gives them a glow stick in each hand. A stick is drawn as a short
trail of its own past positions: one moving cell reads as a speck, three read as a light. Keep the
swing tight — at arm's length the trails of five dancers overlap into noise.

### The casino, and the one door in the city that opens

Same odds as the club (`CLUB_ODDS`, different salt) and mutually exclusive with it, so a building is
never both. `draw_casino_column()` is the club's opposite in every respect: bulbs chasing round the
roof and the door canopy, its name across the front in neon, and every window in the place lit.
`chase()` is the running-bulb pattern — a function of where the bulb is and what time it is, so the
whole building agrees on it without anything being stored.

**It is the only building you can enter.** Everything else in the city is a solid cell that
`can_stand()` keeps you out of; the casino is a proximity trigger instead of an interior.
`casino_at()` scans the 5x5 cells around you for a door within `CASINO_REACH`, and while you are
standing in one, `main()` renders the wheel instead of the street. Walk back out and it is gone.
Stand there and it deals you another after `CASINO_AGAIN` seconds, which is the joke and also
correct. Movement keys stay live throughout — `s` is how you leave.

`Spin` draws the result first and then solves the flight to end on it: the ball eases to a stop over
a whole number of turns plus exactly the offset that lands it in the right pocket. So the wheel is
honest — a uniform `randrange(37)` — but the animation never has to guess where it is going, and
there is a test that casts 2000 spins back from the final angle and checks they agree.

`WHEEL` is the real single-zero pocket order, which is not decorative: it is what puts high next to
low and red next to black the whole way round, and you can watch it turn. `_spin_rng` is the one
random source in this file that is deliberately **not** seeded from position — everything else is a
hash of where you are so the city is the same city every time, but a wheel you could predict is not
a wheel.

### The cheat menu

**Anything new in the city gets an entry here, in the same commit that adds it.** This is a standing
instruction, not a nicety: everything worth looking at is deliberately rare, so a feature with no way
to reach it cannot be checked by the person who asked for it, and will not be. If what you added is a
place, give it a row in `CHEAT_PLACES` and a predicate for `find_place()`; if it is a condition —
weather, time, a mode — give it a key that sets it, the way `w` and `L` do. Both are a few lines.
Adding the feature and leaving the menu alone is an unfinished job.

`` ` `` opens a panel over the live view — over, not instead of, so that weather you change happens
in front of you. Letters belong to the panel while it is up; the arrow keys do not, so you can still
walk about with it open.

**It generates nothing.** The city is a pure function of its coordinates, so `find_place()` searches
for what is already out there and moves the camera to it. `CLUB_ODDS` and the rest are only ever
read. A jump is exactly equivalent to having walked there, and the test for it censuses every club
and casino cell in a patch, uses every cheat, and asserts the two sets are identical — the point
being that a debug tool that quietly made the rare things common would be worse than no tool.

The search rings outward from where you stand and skips anything within `CHEAT_SKIP`, so pressing
the same key twice hops to the next one rather than landing you back where you are. Order the
predicates by cost: `_door_spot()` tests the hash **before** `is_open()`, because it throws out 1199
cells in 1200 for one multiply and means `lot()` — which builds a whole building — is only reached
on a real hit. That is the difference between 7 ms and something you would notice. `_long_street()`
is the one that cannot be cheap, since it has to `probe()`, so it stops as soon as it finds a run
worth looking down.

Two traps worth remembering. Stand a teleport **back** from what it found — three units from a
facade is a nose against a wall — and make the alley search insist on being able to see somewhere,
or it happily drops you in a one-cell walled courtyard that is technically an alley and no use at
all. Both were caught by asserting on `probe()` distance at the landing spot rather than by looking.

Weather is two module-level overrides beside the weather functions: `WEATHER_STEPS` with an index
that `rain_intensity()` consults, and `_forced_strike`, which `lightning()` checks before its own
schedule so a strike can be had on demand even when dry. The flash envelope came out into
`_strike_flash()` so the one you asked for is the same strike the storm would have had. A test
compares 400,000 samples of both functions on `auto` against the formulas they replaced and requires
zero drift.

### Testing it

Stub the module globals `init_colors()` would have set (`PALETTES`, `NEON`, `STAR`, `CURB`, `RAIN`,
`BULB`, …) with plain integers and call `render_street(View(x, z, yaw, w, h), now)` directly — it
returns a grid of characters to print or assert on, no terminal needed. That covers geometry,
determinism (render a spot, walk far enough to evict every cache, render it again, compare), and
navigation: 4000 `wander()` steps that never end up inside a wall. A quick ASCII dump of
`is_open()` over a few hundred cells is the fastest way to judge whether the street plan is
interesting. For the curses half, fork a pty and set the size with `TIOCSWINSZ` as with `wiki_tui`.

Rarity is worth asserting on rather than eyeballing, because these are rare enough that you will
not see one by accident in a test run: measure the gap between strikes over a simulated few hundred
minutes, and the mean spacing of clubs and casinos over a few hundred cells. Watch out for one trap
— the colour stub gives every colour the same id, so a test that counts "cells lit white" has to
give `FLASH` a distinct sentinel first or it matches the whole screen.

The casino is the one thing here that headless rendering cannot check, because it lives in
`main()`'s loop rather than in a render function. Drive it through a pty with `start_position()`
monkeypatched to a spot outside a casino whose door faces -z, walk in with `w`, and look for
`FAITES VOS JEUX` then a result then a second spin then `tab view` on the way out. Slice the **raw**
bytes and strip ANSI per segment — stripping first and then slicing by byte offset silently reads
the wrong window and will tell you a working feature is broken. That test earned its keep
immediately: `spin` was already the yaw rate in `main()`, and the wheel shadowing it was invisible
to every headless check.

A complete frame — render, `addch` loop and `refresh` — costs about 11 ms at 240x70 in a downpour
against a 33 ms budget. If that ever slips, the levers in order are `near_lots()` reach,
`RAIN_REACH` and `MAX_VIEW`.

## wordle_tui.py

Standard curses shape, but three things are worth knowing before changing it.

**Two vocabularies, not one.** `ANSWERS` is a hand-kept list of ~1200 common words and is the only
thing that can *be* the answer; guesses are checked against `ANSWERS | /usr/share/dict/words`. That
split is the point: `web2` is Webster's 1913, so it happily supplies `aalii` and `rybat` — fine to
guess, never fair to have to guess — while *missing* `pixel`, `pasta` and `women`. Neither list
works alone. A missing dictionary degrades to `ANSWERS` rather than failing.

**`score_guess()` spends each answer letter once.** The position pass runs first and the near pass
draws only from what it left behind, so `llama` against `hello` marks two near `L`s and no more, and
`eerie` against `opera` marks only the first `E`. Scoring each letter independently is the classic
bug here, and it looks correct until a guess repeats a letter.

**The layout is a ladder, not a breakpoint.** `choose_layout()` walks `LAYOUTS` roomiest-first and
takes the first that fits, so a short window loses the gaps between grid rows before it loses the
boxed tiles. `rows_needed()` has to stay in step with what `draw()` actually consumes — the on-screen
keyboard is the piece that silently falls off the bottom when it doesn't.

Testing it without a human: fork a pty as with `wiki_tui`, set the size with `TIOCSWINSZ` *before*
the child paints its first frame, and read the fd **non-blocking** — a blocking `os.read` just hangs
once the program is idle in `getch`. Replaying the escapes onto a grid to assert on a frame needs
`CHA`/`VPA` (`ESC[nG`, `ESC[nd`) handled, since that is how ncurses moves the cursor absolutely;
ignore them and every row lands in column 0. The pure halves need no pty at all — call
`score_guess()`, `hard_mode_error()` and `choose_layout()` directly.

## rooster_tui.py

A work roster published as an `.ics` subscription feed, shown month by month and re-fetched every
15 minutes (the feed advertises `X-PUBLISHED-TTL:PT1H`). The feed genuinely rewrites itself: two
fetches two days apart went from 39 shifts to 60, gaining a whole month and two shift codes that
had never appeared before. Three things follow from that and are easy to undo by accident.

- **The feed URL is a bearer token.** It needs no login, so anyone holding it can read the roster.
  It lives in `~/.ess_calendar_url`, `$ESS_ICS_URL`, or `.ess_url` beside the script — the last is
  gitignored — and must **never** be written into a file that gets committed. This repo is public.
- **Times are converted to the local zone and never shown as the UTC in the file.** The publisher
  moves its UTC stamps across the DST boundary precisely so that local times stay put: the same
  `DvT` shift is `05:00Z` in September and `06:00Z` on 25 October, and both are 07:00 locally.
  Print the raw UTC and the whole roster appears to drift an hour every autumn. The invariant worth
  asserting on is that each shift code has exactly one local start time across the entire feed.
- **Shift codes are printed verbatim and there is no lookup table.** `DICO` and `V4` turned up
  between those two fetches, so any hardcoded legend is wrong the moment the employer invents a
  code. If meanings are ever wanted, add a dict with a fall-through to the raw code, the way
  `claude_usage_tui.py` handles limit kinds — never a bare dict lookup.

`parse_events(text)` → `[Shift]` and `build_rows(shifts, today)` → display rows are pure, so
month grouping, free-day gaps and the scroll target need no terminal to test. A `VEVENT` that
fails to parse is skipped rather than fatal, for the same reason: one bad block must not blank the
roster. `fetch_ics()` accepts `file://` URLs, which is what lets the whole program — TUI included —
run against a saved fixture with no network: `rooster_tui.py --url file:///tmp/roster.ics`.

For the curses half, fork a pty and set the size with `TIOCSWINSZ` as with `wiki_tui`. Note that
curses only transmits the lines that *changed*, so asserting on a scroll needs a forced full
repaint — nudge the window size by a column and back — or you read a partial screen and conclude a
working key is broken. A failed refresh keeps the roster already on screen and reports in the
footer; the error shares that line with the totals, so it is trimmed to fit rather than allowed to
run back over them.
