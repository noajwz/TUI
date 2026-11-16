---
paths:
  - "file_tool_tui.py"
---

# file_tool_tui.py

Two tabs over one folder: **Duplicates** (everything below it) and **Rename** (its own files).
`--dupes` prints the duplicate sets without curses (pipeable); `-r` opens on the Rename tab.

## Duplicates

- **`Scan` narrows in three steps**: group by `st_size`, then hash the first `PARTIAL_BYTES` of
  every candidate, then hash whole only the files that still match. A worker thread does the walk,
  a `ThreadPoolExecutor` of `HASH_THREADS` does the reading. Sets are published under `lock` as
  they are confirmed, biggest files first. The hash is blake2b; files aren't compared byte by
  byte.
- What it won't count, and why each one is there:
  - **hard links** are one `File` (keyed by `(st_dev, st_ino)`) with the other paths in
    `links`, never a duplicate of themselves; trashing one trashes all its paths, or no space
    would be freed;
  - **iCloud-only files** (`SF_DATALESS` in `st_flags`) are counted and skipped, because opening
    one to hash it downloads it;
  - **empty files** fall under `MIN_SIZE` (every empty file matches every other);
  - **other volumes** are not entered. `~/Library/CloudStorage` holds File Provider mounts, and
    one (ShellFish) times out on listing. A listing that fails partway counts as unreadable
    rather than aborting the scan; only the root folder failing is fatal;
  - **hidden entries, `IGNORED`, `~/Library` (`SKIPPED_PATHS`) and the insides of `BUNDLES`**
    (`.app`, `.photoslibrary`, …) are skipped unless `--all`. Trashing one file inside a
    library package corrupts the library. Naming `~/Library` as the folder still scans it.
- **One copy of every set always stays.** `toggle_mark()` won't mark the last unmarked copy.
  `trash_marked()` checks again at the moment of trashing: each marked file and at least one kept
  copy must still match the scan's size and mtime, or those are skipped. `a` marks all but one
  per set by `KEEP_RULES`. With a `/` search active it marks the matches instead, keeping a
  non-matching copy where there is one: that is the "clear the duplicates out of this folder"
  workflow.
- Trashing is `disk_usage_tui`'s way (`/usr/bin/trash`, Finder through `osascript` as the
  fallback), batched 100 paths per call, and it checks with `lexists` afterwards.
- Quitting during a scan sets `stop`, and `work()` shuts the pool down with `cancel_futures`;
  without that, exiting waited for every queued file to be opened.

## Rename

- `new_name()` runs a fixed pipeline: find/replace on the **whole name** (so `.jpeg` → `.jpg`
  works), then the template on the stem (`{name}` `{n}` `{n:3}` `{date}` `{date:%Y%m%d}`
  `{folder}`), then case on the stem, then the extension rule (`keep`/`lower`/`upper`, or a
  fixed `.xyz` set with `E`). Rules work on the NFC form of the name (APFS hands out NFD, and what
  you type is NFC). `os.rename` gets the raw name.
- **`make_plan()` decides conflicts** by comparing names the way the volume does (`name_key`):
  normalisation-insensitive, and case-insensitive unless `case_insensitive()` finds otherwise.
  `pathconf` has no case-sensitivity name on macOS, so it looks up a swapped-case name and
  compares inodes. A file's own old name doesn't count against it, so `Foo` → `foo` and swaps
  are allowed. A target that matches a folder, a hidden file or a left-out file is "already
  exists". Any conflict blocks the whole batch.
- **`apply_moves()` is all-or-nothing**: everything goes to a temporary `.file_tool-…` name
  first (which is what lets `a`↔`b` swaps and chains work), then to its target, with a `lexists`
  check before each, since `rename()` would replace a file without a word. On any error it
  puts everything back and reports what it couldn't. Undo (`u`) is the same call reversed, and
  it brings the rule back. After a successful batch the rule resets, so pressing Enter twice
  can't add a prefix twice.
- The counter follows the sort order (`natural()`: `IMG_9` before `IMG_10`) over the included,
  filtered files only. Find, replace and name prompts preview live as you type; ESC puts the
  old value back.
- Renaming in the folder updates the Duplicates list (`remap()`), and switching to the Rename
  tab rereads the folder (`R` rereads it in place; `o` shows the selected file in Finder).

Testing: build a scratch tree with copies, a hard link, a symlink, two
empty files, same-size files with different contents, and two 170 kB files that share their first
64 kB, plus copies in a `.hidden` folder and an `X.app`, then run `--dupes` with and without
`--all`. Exercise rename by importing the module and calling `Renamer`, `make_plan` and
`apply_moves` on a scratch folder: a swap, a case-only rename, a blocked target that must roll
back. The curses half works in tmux with `capture-pane -p`, at 80×24 and the 60×12 minimum.
`--all ~` makes a heavy run (≈190k files, ≈20 s here) for watching progress and quitting
mid-hash. Only trash scratch files.
