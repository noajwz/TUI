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
python3 reminders_tui.py [--once] [--rebuild]   # curses; Apple Reminders, read and write
python3 btc_tx_check.py <64-hex-txid> [--testnet]     # Blockstream API
python3 wiki_reader.py <topic> [--lang nl]            # Wikipedia API, pipes to `less -R`
python3 wiki_tui.py [topic] [--lang nl]               # full-screen Wikipedia browser
python3 psychonaut_tui.py [topic]                     # the same, for psychonautwiki.org
python3 termimage.py IMAGE|--wiki TOPIC [--mode ...]  # images in the terminal
python3 termimage.py --probe                          # what does this terminal support?
```

Ruff is the linter (a `.ruff_cache/` from ruff 0.16.1 is present but gitignored). Ruff is **not
installed on this machine and there is no pyproject/ruff.toml** — it runs with defaults, e.g.
`ruff check . && ruff format .` once available.

Three shapes exist and each new script should follow one of them:

- **curses TUIs** (`bt_battery_tui`, `pwgen_tui`, `claude_usage_tui`, `rooster_tui`, `reminders_tui`, `wordle_tui`): `main(stdscr)` launched via `curses.wrapper(main)`,
  a `stdscr.timeout(...)` + `stdscr.getch()` event loop, module-level constants for tunables
  (`REFRESH_SECONDS`, `MIN_LEN`/`MAX_LEN`, option tables).
- **CLI scripts** (`btc_tx_check`, `wiki_reader`): `argparse` in `main()`, raw ANSI escape constants
  (`BOLD`, `CYAN`, `RESET`, …) defined at module top rather than a color library, and network helpers
  that return `(data, error)` tuples instead of raising.
- **`wiki_tui`** and **`termimage`**, the two larger programs, described below. `psychonaut_tui` is a
  fork of `wiki_tui`, also described below.

`ascii_city.py` used to live here and has moved to its own repository,
[`noajwz/ascii_city`](https://github.com/noajwz/ascii_city) — it outgrew being one script among
several. Its history went with it, so `git log` here no longer covers it.

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

## psychonaut_tui.py

A fork of `wiki_tui.py` for [PsychonautWiki](https://psychonautwiki.org), which is a stock MediaWiki
with an open `api.php`, so the network layer is the same three calls minus the language ones
(`--lang`, the `L` key and `translated_title()` are gone; the cache and history are keyed by title
alone). It is a **separate self-contained script on purpose**, following the `wiki_reader`/`wiki_tui`
precedent: the two share no code at runtime, so a fix to the parser or layout of one has to be
applied to the other by hand.

The reason it is not just `wiki_tui --site`: tables. `wiki_tui` drops every `<table>`, which is
right for Wikipedia and wrong here, because the substance box at the top of every drug page
(`#InfoTable` with nested `.ROATable`s) is the thing people open the site for — dose tiers
(Threshold/Light/Common/Strong/Heavy) and duration per route, plus the interactions list. So:

- Tables are rendered **cell by cell** (`<tr>`, `<td>`, `<th>` are block boundaries), which also
  handles the unclassed tables the wiki uses as multi-column layout containers. Only `wikitable`
  data grids are omitted (`SKIP_TABLE_CLASSES`), with the same `[ table omitted ]` marker.
- The substance box is class-labelled per cell, and `CELL_KINDS` maps those classes to block kinds.
  A `RowHeader`/`ROARowHeader` cell becomes a `label` that is held back in `ArticleParser.label`
  and attached as the `bullet` of the `RowValues` cell that follows; the pair is one `row` block.
  `layout()` pads the labels of a run of consecutive rows to the same width (`row_label_widths()`),
  which is what makes a dosage table line up as a column.
- Interaction cells (`SBInteractionDangerous` / `Unsafe` / `Uncertain`) carry their severity only
  as a class — the text is just the substance name — so `INTERACTION_LEVELS` supplies the word.
- `ROACaption` cells (the WARNING and DISCLAIMER prose) render as dim quotes; the collapse arrow
  the site puts in front of route names (`⇣ Sublingual`) is stripped in `_flush()`.

Every substance also has an `X/Summary` page, reachable from the "Summary sheet" link at the top.
It is built around a 2-D dose-scale table that does not reduce to text, and the main page's box
already holds the same numbers, so there is deliberately no shortcut key for it.

Testing is the same as for `wiki_tui`: `parse_article()` + `layout()` on a saved
`action=parse` response for the pure stages (`LSD` has three routes and an interactions list,
`Cannabis` has collapsed `mw-collapsed` route tables, `Psilocybin mushrooms` has the layout
tables, `Psychedelic` has no substance box at all), and a pty for the rest.

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

## reminders_tui.py

Apple Reminders, read and write: lists on the left, the selected list's reminders on the right,
plus Today / Scheduled / All views. Changes go straight into the Reminders store, so they sync to
the app and the phone like any other edit.

**It is not stdlib-only in the way the rest are**: the talking is done by a small Swift program
against EventKit, embedded as `SWIFT_SOURCE` and compiled once with `swiftc` into
`~/Library/Caches/reminders_tui/ekhelper-<hash>` (the hash is of the source, so editing it forces a
rebuild; `--rebuild` does so explicitly). That was not a style choice. Reminders' AppleScript/JXA
dictionary, measured on 236 reminders: 30 s to read every list's properties, and over 70 s with a
`whose completed is false` filter. EventKit reads the same in 0.3 s. Don't move it back to
`osascript`. The helper takes one operation per run (`dump`, `add`, `update`, `delete`) with a JSON
request on stdin and answers JSON on stdout or one line of error on stderr; `call_helper()` turns
that into the usual `(data, error)` tuple. Due dates cross the wire as `[y, m, d]` or
`[y, m, d, H, M]` in local time; a timed due date also gets one alarm at that moment, the way the
Reminders app does it.

Permission is TCC's "Reminders" (full access), granted to the terminal app that runs it, not to
Python. The first run shows the system dialog; a denial ends up as the helper's stderr line.

**Subtasks show up flat**, beside their parent. EventKit returns them as ordinary reminders and has
no public parent link; `EKReminder` has a private `parentID`, but it was nil on every one of 235
reminders, so there is nothing to nest by. AppleScript hides subtasks altogether, which is why its
counts are lower than this program's.

List order is alphabetical: EventKit does not expose the order the Reminders app shows.

`parse_due(text, now)`, `format_due(r, now)`, `visible(...)` and `next_priority()` are pure. For
the curses half without touching real reminders, import the module, replace `rt.call_helper` with a
fake backed by a saved `dump` (run the helper with `dump` once and keep its stdout), and call
`curses.wrapper(rt.run_curses, "fake", data)` in a tmux pane: `tmux capture-pane -p` gives the
screen as text. Please don't test writes against the real store; they sync to the phone.
