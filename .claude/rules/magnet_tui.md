---
paths:
  - "magnet_tui.py"
---

# magnet_tui.py

A text pane on top that anything can be pasted into, and below it the magnet links and info hashes
found in it, to copy later. Enter copies the selected magnet, ticks it and moves to the next, so a
batch goes out one at a time. Files and stdin given on the command line are taken in at the
start. `--print` (with `--hashes` for hashes) extracts without the window or saving; `--list`
prints the saved list.

- **`find()` is the core.** It recognises:
  - `magnet:?` links up to whitespace, quotes or `<>`, with `&amp;` undone and trailing sentence
    punctuation (`TRAILING`) dropped;
  - bare 40-hex hashes standing on their own;
  - base32 `urn:btih:` hashes;
  - all of the above inside percent-encoded text (redirect links), which is scanned a second
    time, unquoted.

  Entries are keyed by the v1 info hash in lower-case hex (base32 is converted). A v2-only
  magnet's key is `btmh:<sha256>`. The same torrent seen twice is one entry, and `richer()`
  keeps the link that says more: a magnet beats a bare hash, then the longer link wins. A bare
  hash gets a minimal `magnet:?xt=urn:btih:` link, with no trackers (DHT only), shown as
  "hash only".
- **Pastes are recognised by arriving at once.** `read_keys()` drains everything waiting after
  the first key, and `feed()` treats `PASTE_MIN`+ plain characters as a paste. That text goes
  into the text pane even when the list has the keys, so pasting works without pressing Tab
  first. Bracketed paste is not used.
- **Deleted entries stay deleted while typing.** `text_seen` holds the hashes the text pane has
  already yielded, and `retyped()` (after each typed character) only adds hashes it hasn't seen.
  An explicit paste, `p` or a file adds everything found, deleted ones included.
- `w` polls `pbpaste` every `CLIPBOARD_SECONDS` and adds magnets copied anywhere. It takes the
  clipboard at the moment it was switched on as the baseline, and ignores what this program
  copied itself (`own_clip`).
- The list is `~/.local/state/magnet_tui/magnets.json` (or `--file` / `$MAGNET_TUI_FILE`). It is
  written with temp file plus `os.replace` after every change and reloaded when its mtime changes,
  as in `day_tui`. The text pane is scratch and is not saved.

Testing: point `MAGNET_TUI_FILE` at a temp file, and save the clipboard first (`pbpaste >
saved`) and restore it after, since copying and `w` overwrite it. Build a sample with a full
magnet followed by a `.`, an `&amp;` one in HTML, a percent-encoded redirect, a base32
`urn:btih:`, the first hash again bare and upper-case, 42-hex and non-hex near-misses, and a v2
`btmh` magnet; `--print` must give 5. In tmux, paste with `load-buffer` plus `paste-buffer`
(that is a real burst), then Enter, `h`, `w` with a `pbcopy` from outside, `d`/`u`, typing a
hash after Tab, deleting it and typing more (must not come back), `D` `y`, and piped stdin. Check
100×26 and the 50×14 minimum.
