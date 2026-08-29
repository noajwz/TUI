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
python3 claude_usage_tui.py [--once]   # Claude usage limits, read from ~/.claude.json
python3 btc_tx_check.py <64-hex-txid> [--testnet]     # Blockstream API
python3 wiki_reader.py <topic> [--lang nl]            # Wikipedia API, pipes to `less -R`
python3 wiki_tui.py [topic] [--lang nl]               # full-screen Wikipedia browser
python3 termimage.py IMAGE|--wiki TOPIC [--mode ...]  # images in the terminal
python3 termimage.py --probe                          # what does this terminal support?
python3 ascii_city.py        # curses; endless procedural city, skyline and street views
```

Ruff is the linter (a `.ruff_cache/` from ruff 0.16.1 is present but gitignored). Ruff is **not
installed on this machine and there is no pyproject/ruff.toml** — it runs with defaults, e.g.
`ruff check . && ruff format .` once available.

Three shapes exist and each new script should follow one of them:

- **curses TUIs** (`bt_battery_tui`, `pwgen_tui`, `claude_usage_tui`): `main(stdscr)` launched via `curses.wrapper(main)`,
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
`render_street()` return the same `(ch, co)` pair of character/colour grids, so the blit loop, the
HUD and the input handling in `main()` serve either; `tab` switches, and each view keeps its own
camera so switching back and forth loses neither position.

Nothing about the city is stored. The world is cut into chunks seeded by name (`f"city|{layer}|{i}"`,
`f"street|{side}|{i}"`), so the block you walked past regenerates identically when you walk back —
`_cache` and `_street_cache` are throughput, not state, and clearing them changes nothing you can
see. Generation stores a **`tone` float rather than a curses colour**: the palettes differ between an
8-colour and a 256-colour terminal, and a building has to be the same building in both.

The street view is one-point perspective with the vanishing point pinned — you translate but never
turn — and cells are twice as tall as wide, so `View.fy` is half `View.fx`.

- The facades are **two planes at fixed x**, so no ray casting is needed: a column with slope `t`
  meets the plane at `X` at distance `(X - cam_x) / t`. Both sides are solved, and whichever has an
  actual building nearer wins. That per-side `building_at()` returning `None` is what makes an alley
  see-through rather than a wall.
- There is **no depth buffer**. Three per-column arrays — distance, top row, base row — are the whole
  of the depth information, because a facade is a vertical plane. That is why the sky is drawn
  *after* the walls: an unlit window is a blank cell, so `wall_top` is the only thing that knows a
  building is in the way, and `hidden()` is the test everything else uses.
- A horizontal line on a facade is a **slope on screen**, steep when you are close, so roofs and
  awnings go through `facade_line()`, which fills from the previous column's row to this one's. Draw
  one cell per column instead and the line breaks into dashes.
- A bay is many columns wide up close. Windows get the **middle** of their bay or the facade smears
  into horizontal stripes; a sign letter gets **exactly one** column, the first of its bay
  (`new_bay`), or the word stutters as `CCCLLLUUUBBB`.
- `draw_hung_sign()` lays its letters out **in rows, not world units**. Spacing them by a true height
  makes two of them round onto the same row as the sign recedes, and a HOTEL with the T missing is
  worse than one slightly the wrong size.
- The smoker's cigarette is drawn as its own point rather than left to the sprite art, because
  nearest-neighbour downsampling drops single cells — and the ember is the thing still legible when
  he is a smudge at the end of the street. His smoke keeps no state: a puff's position is a function
  of how long ago the drag was.

Testing it without a terminal: stub the module globals `init_colors()` would have set (`PALETTES`,
`NEON`, `STAR`/`STREET`/`CURB`/…) with plain integers and call `render_street()` directly — it
returns a grid of characters you can print or assert on. That covers geometry, determinism (render a
spot, evict the cache by walking, render it again, compare) and sign/prop placement. For the curses
half, fork a pty and set the size with `TIOCSWINSZ` as with `wiki_tui`. A complete frame — render,
`addch` loop and `refresh` — costs about 4.4 ms at 240x70, comfortably inside the 30 FPS budget.
