---
paths:
  - "git_glance_tui.py"
---

# git_glance_tui.py

Recent commits of GitHub repos from the REST API, with `urllib` (no `gh`, which isn't installed).
Targets are owners (their `MAX_REPOS` most recently pushed repos) or `owner/repo`, as names or
github.com URLs; `DEFAULT_TARGETS` is `noajwz`. Four views: the feed (every repo's commits merged,
newest first, under day headings), repos (last push, a 14-day sparkline, commits in the window),
branches (each branch against the default one: behind/ahead with a two-sided bar, last commit,
ahead/merged/diverged) and a commit's detail (message, files, `+/−` bars). Enter on a branch opens
its own commits (the ones the default branch lacks) in the feed, and from there the detail view.

- **The 60-requests-an-hour unauthenticated limit shapes it.** Startup costs one call per owner
  plus one per repo; a refresh (`REFRESH_SECONDS`, or `r`) re-lists the repos and refetches commits
  only where `pushed_at` moved since (`Glance.fetched`), so a quiet refresh is one call per owner.
  A failed listing keeps that target's previous repos. Commit details are cached by sha, failures
  included until `r` in the detail view retries. Test sparingly against the live API, or
  monkeypatch `api_get_once` (an offline run is just a lambda returning an error).
- **Branches cost the most, so they are lazy.** Nothing is fetched until the tab is opened
  (`Glance.want_branches`); then one `/branches` call per repo and one `/compare/{base}...{head}`
  per non-default branch, by sha. Compares are cached by `(repo, base sha, head sha)`, so a
  refresh only lists the branches of repos whose `pushed_at` moved, and only compares a branch
  whose head or default head moved; a branch at the default's sha costs nothing. At most
  `MAX_COMPARES` per repo, and they stop `RATE_RESERVE` calls short of the limit, with a message.
  The compare reply carries the commits ahead (up to 250), which is what the branch feed shows,
  at no extra call. `branch_tried` keeps a failed listing from being retried every frame; `r`
  retries it, and failed compares are retried on the next branch load.
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
columns drop below 72 and 60 columns; the branch bar below 70) and 100×30. `--once N` prints the
feed and `--branches` the branches without curses. For the branches tab, an offline fake of
`api_get_once` serving `/users/x/repos`, `/commits`, `/branches` and `/compare/` (with ahead,
merged, diverged, same-sha, a failing compare and a repo over `MAX_COMPARES`) is cheaper than the
live API; count its calls to check a quiet refresh re-asks for nothing but the repo list.
