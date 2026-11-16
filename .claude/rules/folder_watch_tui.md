---
paths:
  - "folder_watch_tui.py"
---

# folder_watch_tui.py

Watches a folder tree and logs what is created, deleted, modified and renamed in it, newest first,
with an activity strip (changes per `BUCKET_SECONDS`) and a tally per kind. Default folder is the
current one; `--print` streams the same changes as plain lines until Ctrl-C (pipeable).

- **It polls, by design.** The stdlib has no FSEvents binding and kqueue needs a descriptor per
  file, so `Watcher` re-`lstat`s the whole tree every interval on a daemon thread (`snapshot()`)
  and `diff()` compares it with the last scan. That's why there is a `MAX_ENTRIES` cap (a bigger
  tree stops the watcher with a message pointing at `--depth`) and why the header shows the scan
  time. Anything that comes and goes within one interval is invisible.
- `diff()` is where the care is, and each rule is there because the naive diff got it wrong:
  - renames are matched by `(st_dev, st_ino)`; children of a renamed folder are dropped when
    they moved along with it, so `mv dir other` is one line;
  - a folder created or deleted with its contents is one line with an item count and the total
    size, not one per file;
  - a file whose inode changed at the same path is one "modified" (an editor's save-by-rename),
    and the temp file it was renamed from is not reported as deleted;
  - folders' own mtimes are ignored — their children's changes say it better;
  - a folder that turns unreadable keeps its old entries (`snapshot(old=…)`) so losing access
    doesn't look like everything inside was deleted.
- `IGNORED` (`.DS_Store`, `.git`, `__pycache__`, Spotlight litter) is skipped unless `--all`.
  Symlinks are never followed.
- Pausing (space) stops scanning but keeps the last snapshot, so the first scan after resuming
  reports everything that changed meanwhile. The selection is tracked by `Event` identity, and
  at the top of the list it follows the newest change.

Testing: `--print -n 0.3 DIR` in the background while a script touches, appends, `mv`s, `rm -rf`s,
saves by rename and `chmod 000`s things in a scratch dir covers every `diff()` rule. Stop it with
`pkill` — a backgrounded job in a non-interactive shell ignores SIGINT. The curses half works in a
tmux pane with `capture-pane -p` (`-e` to see the selection), at 80×24 and the 50×10 minimum.
