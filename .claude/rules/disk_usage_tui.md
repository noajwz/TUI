---
paths:
  - "disk_usage_tui.py"
---

# disk_usage_tui.py

What is filling the disk, WinDirStat-style: a size-sorted folder tree, a cushion treemap under it,
a list of file types and a list of the largest files. It scans `/` unless given a folder.

- **The scan is `Scan`, eight worker threads on one queue of folders.** `scandir` and `lstat`
  release the GIL, so the threads overlap the syscalls: the whole disk (≈730k files) took 10–18 s
  here, a home folder of 176k files 2–4 s. Each folder's totals are added to all its ancestors
  under `lock` once its listing is done, never per file, and `Node.todo` counts the listings still
  outstanding below a node, so `todo == 0` means its numbers are final and `root.todo` is "busy".
  The UI reads the tree without the lock while it fills in. `gc` is off during a scan (millions
  of nodes and nothing to collect) and the tree is `gc.freeze()`d afterwards.
- **Sizes are `st_blocks * 512`, allocated bytes**, not `st_size`: that is what a file costs
  the disk, so sparse and compressed files count for what they use and iCloud files that aren't
  downloaded count as 0. Hard-linked files are counted once (`Scan.links`, by inode). Decimal
  units, as Finder shows them, so totals are ~7% above `du -h`'s GiB.
- **It never leaves the starting device**, so other volumes (`/dev`, the VM and Preboot APFS
  volumes, external drives) are shown as `other volume` and not entered. On macOS that is not
  enough for `/`: the system volume and the Data volume report the same `st_dev`, and the Data
  volume is reachable twice — through the firmlinks (`/Users`, `/Applications`, `/private`, …)
  and again at `/System/Volumes/Data`. `firmlinked()` reads `/usr/share/firmlinks` and skips
  only the firmlinked entries under `/System/Volumes/Data` (shown as `counted at /`), so what
  lives only on the Data volume's root (`.Spotlight-V100`, `MobileSoftwareUpdate`) still counts.
- **Unreadable folders are the big unknown, and are surfaced rather than hidden.** Without Full
  Disk Access for the terminal, TCC refuses `~/Library/Containers` (Docker's disk image lives
  there), Group Containers, the Photos library, Mail and `~/.Trash`; on the machine this was
  written on that was over 200 GB of a 394 GB Data volume. `Node.denied` counts them per subtree,
  the tree shows `(N unreadable inside)`, the footer says how to grant access, and the header
  splits the disk into *here*, *not seen* (or *elsewhere* when scanning a folder) and *free*.
  Capacity and free come from `statvfs`, which on APFS is the whole container.
- **The treemap bypasses curses.** Python's curses passes a colour pair inside the attribute,
  which only has room for pairs up to 255; higher pairs spill into the attribute bits and come
  out as underlines and wrong colours. A shaded map of half-block pixels needs thousands of
  fg/bg combinations, so `write_map()` sends SGR escapes itself after `stdscr.refresh()`, inside
  a cursor save/restore (`ESC 7`/`ESC 8`), and only for the cells that changed. Curses believes
  the map's cells are blank and so never touches them — which is also why anything that moves or
  removes the map (resize, `m`, a too-small window) must go through `set_map_geom()`, which calls
  `clearok(True)` and forgets what was sent, or old pixels are left behind. Colour is 24-bit when
  `COLORTERM` says so (or `TERM_PROGRAM` is Ghostty, iTerm or WezTerm), otherwise the 256-colour
  cube, otherwise 8 flat colours; `Inks` hides which. Shades of a colour must stay off the grey
  ramp (`xterm()`), or every cushion gets a grey rim in 256 colours.
- **Layout is squarified (`squarify()`), shading is van Wijk's cushions** (`ridge()`, `paint()`),
  on half-cell pixels, which are about square. Items under `LUMP_PX` are merged into one grey lump
  per folder, which bounds the work at roughly one rectangle per pixel: the whole disk at 300×90
  lays out in ~60 ms. While scanning the map is re-laid at most every `MAP_REFRESH` s.
- The map shows the **parent of the selection** (in the Tree view) with the selection outlined,
  so moving among siblings only moves the outline and stepping into a folder zooms. A click
  selects the item under the pointer one level below the current selection, so repeated clicks
  drill down. Types view dims everything but the chosen extension; the biggest extensions get a
  `PALETTE` colour each, assigned stickily so they don't swap while the scan runs.
- `q` is the only quit key; ESC is not, for the reason given under `minesweeper_tui`.
- `t` moves to the Trash with `/usr/bin/trash` (Finder via `osascript` if it is missing), only
  after `y`, and only once the scan is idle, since `detach()` edits the tree. The space is not
  free until the Trash is emptied, and the footer says so. `r` rescans the selected folder in
  place (or everything, on the root); a rescanned hard link that was first seen elsewhere in the
  tree counts as 0, a known edge.

`squarify()`, `ridge()`, `ext_of()`, `firmlinked()`, `xterm()` and the formatters are pure, and
`Scan` runs without a terminal: `--once` prints the top of the tree, the largest files and the
types. For the curses half use tmux with `capture-pane -p -e`; the map is real escapes, so a
capture shows it, and `COLORTERM=truecolor` in the pane's command gets 24-bit. Check 300x90 and
160x48, 80x24 (map just fits), 60x12 (minimum, no map) and `TERM=xterm` for 8 colours. Mouse
clicks under `TERM=tmux-256color` are X10 reports, as for `minesweeper_tui`. Test `t` on a scratch
file of your own, never on the real tree.
