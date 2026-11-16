---
paths:
  - "git_glance_tui.py"
---

# git_glance_tui.py

Recent commits of GitHub repos from the REST API, with `urllib` (no `gh`, which isn't installed).
Targets are owners (their `MAX_REPOS` most recently pushed repos) or `owner/repo`, as names or
github.com URLs; `DEFAULT_TARGETS` is `noajwz`. Three views: the feed (every repo's commits merged,
newest first, under day headings), repos (last push, a 14-day sparkline, commits in the window) and
a commit's detail (message, files, `+/−` bars), which is the only view that costs a call of its own.

- **The 60-requests-an-hour unauthenticated limit shapes it.** Startup costs one call per owner
  plus one per repo; a refresh (`REFRESH_SECONDS`, or `r`) re-lists the repos and refetches commits
  only where `pushed_at` moved since (`Glance.fetched`), so a quiet refresh is one call per owner.
  A failed listing keeps that target's previous repos. Commit details are cached by sha, failures
  included until `r` in the detail view retries. Test sparingly against the live API, or
  monkeypatch `api_get_once` (an offline run is just a lambda returning an error).
- `GITHUB_TOKEN` or `GH_TOKEN` is optional: with one, the token owner's own listing goes through
  `/user/repos` so private repos appear, and the limit is 5000.
- A fork's commits page sometimes answers 422 and is fine a second later, so `api_get` retries a
  422 or 5xx once. `note_rate` keeps the lowest remaining count per window because parallel replies
  land out of order.
- Fetching runs on threads (`Glance.load`, a `ThreadPoolExecutor`) and swaps the dicts in under a
  lock; the curses loop never blocks on the network and redraws every 125 ms for the spinner.
- `wrap()` reflows commit bodies: hard-wrapped paragraphs and list items are joined and wrapped
  again under a hanging indent when the window is narrower than the message, and kept as written
  otherwise. Trailers (`Co-Authored-By:` …) are never joined and are drawn dim.
- The selected commit is tracked by `(repo, sha)` so a refresh that adds commits doesn't move it.

Testing the curses half: a tmux pane plus `capture-pane -p`, at 50×12 (the minimum; author and sha
columns drop below 72 and 60 columns) and 100×30. `--once N` prints the feed without curses.
