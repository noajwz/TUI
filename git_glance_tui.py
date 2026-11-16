#!/usr/bin/env python3
"""git glance — the recent commits of GitHub repos at a glance: one feed across every repo, the repos
with their activity, and any commit's message and files (GitHub REST API)."""

import argparse
import curses
import json
import locale
import os
import re
import subprocess
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

API = "https://api.github.com"
USER_AGENT = "git-glance/1.0 (terminal commit viewer)"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

DEFAULT_TARGETS = ["noajwz"]       # owners (their repos) or owner/repo; the command line replaces these
MAX_REPOS = 12                     # most recently pushed repos taken per owner
COMMITS_PER_REPO = 30              # one page per repo; the feed is these merged
REFRESH_SECONDS = 300              # a refresh re-lists the repos, refetching only those pushed to since
SPARK_DAYS = 14
WORKERS = 6
MAX_COMPARES = 10                  # branches per repo compared with the default branch, one call each
RATE_RESERVE = 5                   # calls the branch comparisons leave unspent, for the next refresh
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 12

BARS = "▁▂▃▄▅▆▇█"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
REPO_PAIRS = [1, 2, 3, 7, 6]       # colour pairs handed out to repos in name order
# file status in a commit -> (mark, colour pair)
STATUS_MARK = {"added": ("A", 2), "removed": ("D", 4), "modified": ("M", 3), "changed": ("M", 3),
               "renamed": ("R", 6), "copied": ("C", 6)}
LIST_ITEM = re.compile(r"^\s*([-*•]|\d+[.)])\s+")
TRAILER = re.compile(r"^(co-authored-by|signed-off-by|reviewed-by|reported-by):", re.I)

RATE = {}                          # the X-RateLimit-* headers of the latest reply
RETRY_ERRORS = {"HTTP 422", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"}


def note_rate(headers):
    """Keeps the lowest count seen in the current window: parallel replies land out of order."""
    try:
        remaining, limit, reset = (int(headers[f"X-RateLimit-{k}"]) for k in ("Remaining", "Limit", "Reset"))
    except (KeyError, TypeError, ValueError):
        return
    if RATE.get("reset") == reset:
        remaining = min(remaining, RATE["remaining"])
    RATE.update(remaining=remaining, limit=limit, reset=reset)


def api_get(path, params=None):
    """GET from the API as (data, error). A 422 or 5xx is retried once: GitHub hands those out now and
    then for a fork's commits and they clear within a second."""
    data, err = api_get_once(path, params)
    if err in RETRY_ERRORS:
        time.sleep(1)
        data, err = api_get_once(path, params)
    return data, err


def api_get_once(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            note_rate(resp.headers)
            return json.load(resp), None
    except urllib.error.HTTPError as e:
        note_rate(e.headers)
        if e.code == 429 or (e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0"):
            reset = e.headers.get("X-RateLimit-Reset")
            until = f" until {time.strftime('%H:%M', time.localtime(int(reset)))}" if reset else ""
            return None, f"rate limited{until}" + ("" if TOKEN else " (GITHUB_TOKEN raises it to 5000/h)")
        if e.code == 401:
            return None, "GitHub refused the token (HTTP 401)"
        if e.code == 404:
            return None, "not found" + ("" if TOKEN else " (private? set GITHUB_TOKEN)")
        if e.code == 409:
            return [], None        # an empty repository: no commits
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"network error: {getattr(e, 'reason', e)}"
    except json.JSONDecodeError:
        return None, "bad JSON from GitHub"


_login = {}


def token_login():
    """Who the token belongs to, so their private repos can be listed too; None without a token."""
    if TOKEN and "login" not in _login:
        data, _ = api_get("/user")
        if isinstance(data, dict) and data.get("login"):
            _login["login"] = data["login"]
    return _login.get("login")


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def repo_record(r):
    return {"name": r["full_name"], "short": r["name"], "pushed": r.get("pushed_at") or "",
            "url": r["html_url"], "description": r.get("description") or "",
            "fork": bool(r.get("fork")), "private": bool(r.get("private")),
            "default": r.get("default_branch") or "main"}


def fetch_repos(target, limit=MAX_REPOS):
    """The repos a target names: the one for owner/repo, an owner's `limit` most recently pushed otherwise."""
    try:
        if "/" in target:
            data, err = api_get(f"/repos/{target}")
            return (None, f"{target}: {err}") if err else ([repo_record(data)], None)
        if TOKEN and target.lower() == (token_login() or "").lower():
            data, err = api_get("/user/repos", {"affiliation": "owner", "sort": "pushed", "per_page": 100})
        else:
            data, err = api_get(f"/users/{target}/repos", {"type": "owner", "sort": "pushed", "per_page": 100})
        if err:
            return None, f"{target}: {err}"
        repos = sorted((repo_record(r) for r in data), key=lambda r: r["pushed"], reverse=True)
        return repos[:limit], None
    except (KeyError, TypeError, AttributeError):
        return None, f"{target}: unexpected reply from GitHub"


def commit_record(repo, c):
    info = c["commit"]
    message = (info.get("message") or "").strip()
    return {"repo": repo, "sha": c["sha"], "title": message.split("\n", 1)[0], "message": message,
            "author": (c.get("author") or {}).get("login") or info["author"].get("name") or "?",
            "when": parse_time(info["committer"]["date"]), "url": c["html_url"],
            "merge": len(c.get("parents") or []) > 1}


def fetch_commits(repo):
    """The newest COMMITS_PER_REPO commits on the repo's default branch, newest first."""
    data, err = api_get(f"/repos/{repo}/commits", {"per_page": COMMITS_PER_REPO})
    if err:
        return None, f"{repo}: {err}"
    try:
        return [commit_record(repo, c) for c in data], None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, f"{repo}: unexpected commit data"


def fetch_branches(repo):
    """The repo's branches, the default one first: one call, the first 100."""
    data, err = api_get(f"/repos/{repo['name']}/branches", {"per_page": 100})
    if err:
        return None, f"{repo['name']} branches: {err}"
    try:
        found = [{"name": b["name"], "sha": b["commit"]["sha"], "protected": bool(b.get("protected")),
                  "default": b["name"] == repo["default"]} for b in data]
    except (KeyError, TypeError):
        return None, f"{repo['name']}: unexpected branch data"
    return sorted(found, key=lambda b: (not b["default"], b["name"].lower())), None


def fetch_compare(key):
    """How far head is ahead of and behind base, keyed (repo, base sha, head sha); the commits ahead come
    along (up to 250), newest first, so a branch's own commits need no call of their own."""
    repo, base, head = key
    data, err = api_get(f"/repos/{repo}/compare/{base}...{head}")
    if err:
        return None, err
    try:
        ahead = [commit_record(repo, c) for c in data.get("commits") or []][::-1]
        tip = ahead[0] if ahead and len(ahead) == data["ahead_by"] else None
        if not data["ahead_by"] and data.get("merge_base_commit"):
            tip = commit_record(repo, data["merge_base_commit"])    # nothing ahead: the head is the merge base
        return {"ahead": data["ahead_by"], "behind": data["behind_by"], "commits": ahead,
                "when": tip["when"] if tip else None, "author": tip["author"] if tip else None}, None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "unexpected compare data"


def person(p):
    return {"name": p.get("name") or "?", "email": p.get("email") or "", "when": parse_time(p["date"])}


def fetch_detail(repo, sha):
    """Author, committer and the changed files of one commit — one call, made when it is opened."""
    data, err = api_get(f"/repos/{repo}/commits/{sha}")
    if err:
        return None, err
    try:
        info, stats = data["commit"], data.get("stats") or {}
        files = [{"name": f["filename"], "old": f.get("previous_filename"), "status": f.get("status", ""),
                  "add": f.get("additions", 0), "del": f.get("deletions", 0)} for f in data.get("files") or []]
        return {"author": person(info["author"]), "committer": person(info["committer"]),
                "login": (data.get("author") or {}).get("login"),
                "add": stats.get("additions", 0), "del": stats.get("deletions", 0), "files": files}, None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "unexpected commit data"


def normalize_target(text):
    """Accepts owner, owner/repo, or a github.com URL of either."""
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text.strip()).strip("/")
    text = text.removesuffix(".git")
    return "/".join(text.split("/")[:2])


# ---------------------------------------------------------------- model


class Glance:
    """Repos and their commits, fetched on a background thread so the screen never waits on the network."""

    def __init__(self, targets, limit=MAX_REPOS):
        self.targets = targets
        self.limit = limit
        self.by_target = {}        # target -> [repo name], kept from the last good listing when one fails
        self.repos = {}            # name -> repo record
        self.commits = {}          # name -> [commit], newest first
        self.fetched = {}          # name -> the pushed_at its commits were fetched at
        self.labels = {}           # name -> the short name, or owner/name where short names clash
        self.pairs = {}            # name -> colour pair
        self.details = {}          # sha -> detail record, error string, or None while on its way
        self.errors = []           # problems from the last refresh
        self.branches = {}         # name -> [branch], default first; only fetched once the tab is opened
        self.branch_pushed = {}    # name -> the pushed_at its branches were listed at
        self.branch_tried = {}     # name -> the pushed_at last attempted, so a failure isn't retried every frame
        self.compares = {}         # (repo, base sha, head sha) -> compare record, or an error string
        self.branch_errors = []
        self.branches_wanted = False
        self.branches_loading = False
        self.loading = False
        self.loaded = False        # a refresh has finished at least once
        self.refreshed_at = 0.0    # when the last refresh started
        self.lock = threading.Lock()

    def refresh(self, force=False):
        if self.loading or self.branches_loading or (not force and time.time() - self.refreshed_at < REFRESH_SECONDS):
            return
        self.loading = True
        self.refreshed_at = time.time()
        threading.Thread(target=self.load, daemon=True).start()

    def load(self):
        """One refresh: list the repos, then fetch commits only for those pushed to since last time —
        a quiet refresh costs one call per owner, which is what makes 60 calls an hour enough."""
        errors, repos, by_target = [], {}, {}
        with ThreadPoolExecutor(WORKERS) as pool:
            listings = list(pool.map(lambda t: fetch_repos(t, self.limit), self.targets))
            for target, (found, err) in zip(self.targets, listings):
                if err:
                    errors.append(err)
                    found = [self.repos[n] for n in self.by_target.get(target, []) if n in self.repos]
                by_target[target] = [r["name"] for r in found]
                repos.update((r["name"], r) for r in found)
            stale = [n for n, r in repos.items() if self.fetched.get(n) != r["pushed"]]
            results = list(pool.map(fetch_commits, stale))
        commits = {n: self.commits[n] for n in repos if n in self.commits}
        fetched = {n: self.fetched[n] for n in repos if n in self.fetched}
        for name, (found, err) in zip(stale, results):
            if err:
                errors.append(err)
            else:
                commits[name], fetched[name] = found, repos[name]["pushed"]
        shorts = [r["short"] for r in repos.values()]
        labels = {n: r["short"] if shorts.count(r["short"]) == 1 else n for n, r in repos.items()}
        pairs = {n: REPO_PAIRS[i % len(REPO_PAIRS)] for i, n in enumerate(sorted(repos))}
        with self.lock:
            self.by_target, self.repos, self.commits, self.fetched = by_target, repos, commits, fetched
            self.labels, self.pairs, self.errors = labels, pairs, errors
            branches = self.branches_wanted and not self.branches_loading
            self.branches_loading = self.branches_loading or branches
            self.loaded, self.loading = True, False
        if branches:
            self.load_branches()

    def want_branches(self):
        """Called while the branches tab is open: fetches what hasn't been tried at the current pushed_at."""
        self.branches_wanted = True
        with self.lock:
            if self.loading or self.branches_loading or not self.loaded:
                return
            if not any(self.branch_tried.get(n) != r["pushed"] for n, r in self.repos.items()):
                return
            self.branches_loading = True
        threading.Thread(target=self.load_branches, daemon=True).start()

    def load_branches(self):
        """Lists the branches of repos pushed to since they were last listed, then compares every branch
        whose head or default head moved. Comparisons stop RATE_RESERVE calls short of the limit. The caller
        sets branches_loading, under the lock, so only one of these runs at a time."""
        try:
            repos = self.repo_list()
            stale = [r for r in repos if self.branch_pushed.get(r["name"]) != r["pushed"]]
            for r in stale:
                self.branch_tried[r["name"]] = r["pushed"]
            errors = []
            with ThreadPoolExecutor(WORKERS) as pool:
                listings = list(pool.map(fetch_branches, stale))
                branches = {r["name"]: self.branches[r["name"]] for r in repos if r["name"] in self.branches}
                pushed = {n: self.branch_pushed[n] for n in branches if n in self.branch_pushed}
                for r, (found, err) in zip(stale, listings):
                    if err:
                        errors.append(err)
                    else:
                        branches[r["name"]], pushed[r["name"]] = found, r["pushed"]
                jobs, compares = [], {}
                for name, bs in branches.items():
                    default = next((b for b in bs if b["default"]), None)
                    for b in [b for b in bs if not b["default"]][:MAX_COMPARES] if default else []:
                        key = (name, default["sha"], b["sha"])
                        if b["sha"] == default["sha"]:
                            compares[key] = {"ahead": 0, "behind": 0, "commits": [], "when": None, "author": None}
                        elif isinstance(self.compares.get(key), dict):
                            compares[key] = self.compares[key]
                        else:
                            jobs.append(key)
                if RATE and RATE["remaining"] - RATE_RESERVE < len(jobs):
                    spare = max(0, RATE["remaining"] - RATE_RESERVE)
                    errors.append(f"{len(jobs) - spare} branches not compared: the API limit is nearly spent")
                    jobs = jobs[:spare]
                for key, (found, err) in zip(jobs, pool.map(fetch_compare, jobs)):
                    compares[key] = found or err
            with self.lock:
                self.branches, self.branch_pushed, self.compares = branches, pushed, compares
                self.branch_errors = errors
        finally:
            self.branches_loading = False

    def branch_commits(self, repo, branch):
        """The commits a branch has that the default branch doesn't, newest first."""
        found = self.compare(repo, branch)
        return list(found["commits"]) if isinstance(found, dict) else []

    def compare(self, repo, branch):
        """The branch's compare record, an error string, or None (not compared, or on its way)."""
        default = next((b for b in self.branches.get(repo, []) if b["default"]), None)
        return self.compares.get((repo, default["sha"], branch["sha"])) if default else None
    def repo_list(self):
        with self.lock:
            return sorted(self.repos.values(), key=lambda r: r["pushed"], reverse=True)

    def feed(self, repo=None):
        with self.lock:
            if repo:
                return list(self.commits.get(repo, []))
            return sorted((c for cs in self.commits.values() for c in cs), key=lambda c: c["when"], reverse=True)

    def detail(self, commit):
        """The commit's detail record, an error string, or None while it is on its way."""
        sha = commit["sha"]
        if sha not in self.details:
            self.details[sha] = None
            threading.Thread(target=self.load_detail, args=(commit,), daemon=True).start()
        return self.details[sha]

    def load_detail(self, commit):
        data, err = fetch_detail(commit["repo"], commit["sha"])
        self.details[commit["sha"]] = data or err

    def retry_detail(self, commit):
        if isinstance(self.details.get(commit["sha"]), str):
            del self.details[commit["sha"]]


# ---------------------------------------------------------------- formatting


def ago(ts, now=None):
    s = max(0, (now or time.time()) - ts)
    for size, unit in ((31_536_000, "y"), (2_592_000, "mo"), (604_800, "w"), (86_400, "d"), (3600, "h"), (60, "m")):
        if s >= size:
            return f"{int(s // size)}{unit}"
    return "now"


def day_label(ts):
    day, today = datetime.fromtimestamp(ts).date(), datetime.now().date()
    days = (today - day).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if 1 < days < 7:
        return day.strftime("%A")
    return day.strftime("%a %-d %b" + (" %Y" if day.year != today.year else ""))


def fit(text, n):
    """Cuts text to n columns with an ellipsis, padding it out to n."""
    if n <= 0:
        return ""
    return text[: n - 1] + "…" if len(text) > n else text.ljust(n)


def fit_left(text, n):
    """Like fit, but cuts from the left — for paths, whose end is the part worth keeping."""
    if n <= 0:
        return ""
    return "…" + text[-(n - 1):] if len(text) > n else text.ljust(n)


def day_counts(commits, days=SPARK_DAYS):
    """Commits per local day, oldest first, ending today."""
    today, counts = datetime.now().date(), [0] * days
    for c in commits:
        back = (today - datetime.fromtimestamp(c["when"]).date()).days
        if 0 <= back < days:
            counts[days - 1 - back] += 1
    return counts


def window_count(commits, days=SPARK_DAYS):
    """Commits in the sparkline window as text, with a + when the fetched page may not reach back that far."""
    n = sum(day_counts(commits, days))
    return f"{n}+" if n and n == len(commits) == COMMITS_PER_REPO else str(n)


def wrap(text, width):
    """A commit message at `width` columns. If it fits it is kept as written; otherwise its paragraphs
    and list items, hard-wrapped for a wider screen, are joined up and wrapped again under a hanging
    indent — wrapping line by line would leave a stub of a word on every other line."""
    width = max(10, width)
    lines = text.split("\n")
    if all(len(line) <= width for line in lines):
        return lines
    out, blocks = [], []           # blocks: [first-line prefix, hanging indent, [text]] or None for a blank
    for line in lines:
        bullet = LIST_ITEM.match(line)
        indent = len(line) - len(line.lstrip())
        block = blocks[-1] if blocks else None
        if not line.strip():
            blocks.append(None)
        elif bullet:
            blocks.append([bullet.group(0), " " * len(bullet.group(0)), [line[bullet.end():]]])
        elif block and indent == len(block[1]) and not TRAILER.match(line.strip()) and not TRAILER.match(block[2][0]):
            block[2].append(line.strip())
        else:
            blocks.append([line[:indent], line[:indent], [line.strip()]])
    for block in blocks:
        if block is None:
            out.append("")
        else:
            out.extend(textwrap.wrap(" ".join(block[2]), width, initial_indent=block[0],
                                     subsequent_indent=block[1], break_on_hyphens=False) or [block[0]])
    return out


# ---------------------------------------------------------------- drawing


def put(scr, y, x, text, w, attr=0):
    h, _ = scr.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0:
        return
    text = text[: w - x - (1 if y == h - 1 else 0)]
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


def put_runs(scr, y, x, runs, w):
    """Draws runs left to right, cutting the one that reaches the edge."""
    for text, attr in runs:
        if x >= w:
            return
        put(scr, y, x, text, w, attr)
        x += len(text)


def dim():
    return curses.color_pair(5)


class UI:
    def __init__(self, glance):
        self.g = glance
        self.view = "feed"         # feed, repos, branches or detail
        self.repo = None           # the repo the feed is narrowed to, from the repos view
        self.branch = None         # (repo, branch) whose own commits the feed shows, from the branches view
        self.sel = {"feed": 0, "repos": 0, "branches": 0}
        self.top = {"feed": 0, "repos": 0, "branches": 0}
        self.sel_branch = None     # (repo, branch name) of the selected branch, kept across refreshes
        self.sel_key = None        # (repo, sha) of the selected commit, so a refresh doesn't move it
        self.commit = None         # the commit the detail view shows
        self.scroll = 0
        self.message, self.message_at, self.message_attr = "", 0.0, 0

    def say(self, text, attr=0):
        self.message, self.message_at, self.message_attr = text, time.time(), attr

    def repo_attr(self, name):
        return curses.color_pair(self.g.pairs.get(name, 1))

    def scope(self):
        """The commits the feed lists: a branch's own, one repo's, or everyone's."""
        if self.branch:
            return self.g.branch_commits(*self.branch)
        return self.g.feed(self.repo)

    def feed_commits(self):
        commits = self.scope()
        keys = [(c["repo"], c["sha"]) for c in commits]
        if self.sel_key in keys:
            self.sel["feed"] = keys.index(self.sel_key)
        self.sel["feed"] = max(0, min(self.sel["feed"], len(commits) - 1))
        self.sel_key = keys[self.sel["feed"]] if commits else None
        return commits

    def selected_commit(self):
        commits = self.feed_commits()
        return commits[self.sel["feed"]] if commits else None

    def move(self, step, total, view=None):
        view = view or self.view
        self.sel[view] = max(0, min(self.sel[view] + step, total - 1))
        if view == "branches":
            rows = [row for row in self.branch_rows()[0] if row[1]]
            self.sel_branch = (rows[self.sel[view]][0]["name"], rows[self.sel[view]][1]["name"]) if rows else None
        if view == "feed":
            commits = self.scope()
            self.sel_key = (commits[self.sel[view]]["repo"], commits[self.sel[view]]["sha"]) if commits else None

    # -------------------------------------------------------- frame

    def draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            put(scr, 0, 0, f"window too small — needs {MIN_W}×{MIN_H}", w)
            return
        self.draw_header(scr, w)
        body_h = h - 3
        if self.view == "repos":
            self.draw_repos(scr, 2, body_h, w)
        elif self.view == "branches":
            self.draw_branches(scr, 2, body_h, w)
        elif self.view == "detail" and self.commit:
            self.draw_detail(scr, 2, body_h, w)
        else:
            self.draw_feed(scr, 2, body_h, w)
        self.draw_footer(scr, h - 1, w)

    def draw_header(self, scr, w):
        runs = [(" git glance ", curses.color_pair(1) | curses.A_BOLD), ("  ", 0)]
        for name, label in (("feed", "Feed"), ("repos", "Repos"), ("branches", "Branches")):
            active = self.view == name or (name == "feed" and self.view == "detail")
            runs += [(f" {label} ", curses.A_REVERSE if active else dim()), (" ", 0)]
        if self.branch and self.view in ("feed", "detail"):
            repo, b = self.branch
            runs += [("› ", dim()), (self.g.labels.get(repo, repo), self.repo_attr(repo) | curses.A_BOLD),
                     (" › ", dim()), (b["name"], curses.A_BOLD)]
        elif self.repo and self.view not in ("repos", "branches"):
            runs += [("› ", dim()), (self.g.labels.get(self.repo, self.repo), self.repo_attr(self.repo) | curses.A_BOLD)]
        put_runs(scr, 0, 0, runs, w)
        if self.g.loading or self.g.branches_loading:
            what = "fetching" if self.g.loading else "comparing branches"
            status = f"{SPINNER[int(time.time() * 8) % len(SPINNER)]} {what}"
        elif self.g.loaded and self.g.errors and not self.g.repos:
            status = "fetch failed"
        elif self.g.loaded:
            status = f"updated {ago(self.g.refreshed_at)} ago" if time.time() - self.g.refreshed_at >= 60 else "up to date"
        else:
            status = ""
        put(scr, 0, w - len(status) - 1, status, w, dim())
        put(scr, 1, 0, "─" * w, w, dim())

    def draw_footer(self, scr, y, w):
        rate = f"API {RATE['remaining']}/{RATE['limit']}" if RATE else ""
        low = RATE and RATE["remaining"] < 10
        put(scr, y, w - len(rate) - 1, rate, w, curses.color_pair(4) if low else dim())
        room = w - len(rate) - 3
        if self.message and time.time() - self.message_at < MESSAGE_SECONDS:
            put(scr, y, 1, fit(self.message, room), w, self.message_attr)
        elif self.g.errors or (self.view == "branches" and self.g.branch_errors):
            errors = self.g.errors + (self.g.branch_errors if self.view == "branches" else [])
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            put(scr, y, 1, fit(f"! {errors[0]}{more}", room), w, curses.color_pair(4))
        else:
            hints = {
                "feed": "↑↓ move  ⏎ open  o browser  c copy sha  "
                        + ("esc back  " if self.repo or self.branch else "tab repos  ") + "r refresh  q quit",
                "repos": "↑↓ move  ⏎ commits  o browser  tab branches  r refresh  q quit",
                "branches": "↑↓ move  ⏎ its commits  o compare  c copy name  tab feed  r refresh  q quit",
                "detail": "↑↓ scroll  n/p next/prev  o browser  c copy sha  esc back  q quit",
            }[self.view]
            put(scr, y, 1, fit(hints, room), w, dim())

    def centre(self, scr, y, w, text, attr=0):
        put(scr, y, max(0, (w - len(text)) // 2), text, w, attr)

    def placeholder(self, scr, top, height, w, empty):
        """What an empty list shows: progress before the first load, then the problem or `empty`."""
        y = top + height // 2 - 1
        if not self.g.loaded:
            self.centre(scr, y, w, f"fetching the recent commits of {', '.join(self.g.targets)} …", dim())
        elif self.g.errors and not self.g.repos:
            for i, err in enumerate(self.g.errors[: height - 1]):
                self.centre(scr, y + i, w, fit(err, w - 4).rstrip(), curses.color_pair(4))
        else:
            self.centre(scr, y, w, empty, dim())

    # -------------------------------------------------------- feed

    def draw_feed(self, scr, top, height, w):
        commits = self.feed_commits()
        if not commits:
            self.placeholder(scr, top, height, w, "no commits")
            return
        lines, last = [], None             # (commit, None) or (None, day heading)
        for c in commits:
            day = day_label(c["when"])
            if day != last:
                lines.append((None, day))
                last = day
            lines.append((c, None))
        commit_lines = [i for i, (c, _) in enumerate(lines) if c]
        at = commit_lines[self.sel["feed"]]
        want = at - 1 if at and lines[at - 1][0] is None else at    # show the selection's day heading too
        t = self.top["feed"]
        t = min(t, want)
        t = max(t, at - height + 1)
        t = max(0, min(t, len(lines) - height))
        self.top["feed"] = t

        show_repo = self.repo is None and self.branch is None
        rw = min(18, max(len(self.g.labels.get(c["repo"], c["repo"])) for c in commits))
        aw = min(14, max(len(c["author"]) for c in commits)) if w >= 72 else 0
        show_sha = w >= 60
        for row, (c, day) in enumerate(lines[t: t + height]):
            y = top + row
            if c is None:
                put_runs(scr, y, 1, [(day + " ", curses.A_BOLD), ("─" * w, dim())], w - 1)
                continue
            selected = t + row == at
            runs = [(f"{ago(c['when']):>4}  ", dim())]
            if show_repo:
                runs.append((fit(self.g.labels.get(c["repo"], c["repo"]), rw) + "  ", self.repo_attr(c["repo"])))
            if aw:
                runs.append((fit(c["author"], aw) + "  ", curses.color_pair(6)))
            used = 1 + sum(len(text) for text, _ in runs)
            title_w = w - used - (9 if show_sha else 0) - 1
            runs.append((fit(c["title"], title_w), dim() if c["merge"] else 0))
            if show_sha:
                runs.append(("  " + c["sha"][:7], curses.color_pair(3)))
            if selected:
                put(scr, y, 0, " " * w, w, curses.A_REVERSE)
                runs = [(text, attr | curses.A_REVERSE) for text, attr in runs]
            put_runs(scr, y, 1, runs, w)

    # -------------------------------------------------------- repos

    def draw_repos(self, scr, top, height, w):
        repos = self.g.repo_list()
        if not repos:
            self.placeholder(scr, top, height, w, "no repos")
            return
        commits = {r["name"]: self.g.commits.get(r["name"]) for r in repos}
        counts = {n: day_counts(cs) if cs is not None else None for n, cs in commits.items()}
        peak = max([max(cs) for cs in counts.values() if cs] + [1])
        rw = min(24, max(len(self.g.labels.get(r["name"], r["name"])) for r in repos))
        head = f"{fit('repo', rw + 2)}{'pushed':>6}  {fit(f'last {SPARK_DAYS} days', SPARK_DAYS)}  {'commits':>7}  about"
        put(scr, top, 1, head, w, dim())
        height -= 1
        sel = self.sel["repos"] = max(0, min(self.sel["repos"], len(repos) - 1))
        t = max(min(self.top["repos"], sel), sel - height + 1, 0)
        self.top["repos"] = t
        for row, r in enumerate(repos[t: t + height]):
            y, name = top + 1 + row, r["name"]
            mark = "⑂" if r["fork"] else " "
            runs = [(fit(self.g.labels.get(name, name), rw), self.repo_attr(name) | curses.A_BOLD),
                    (f" {mark}", dim()),
                    (f"{ago(parse_time(r['pushed'])) if r['pushed'] else '—':>6}  ", dim())]
            if counts[name] is None:
                runs += [("·" * SPARK_DAYS, dim()), (f"  {'?':>7}  ", dim())]
            else:
                for n in counts[name]:
                    level = -(-n * len(BARS) // peak) - 1 if n else None     # ceil, so one commit shows
                    runs.append((BARS[level] if n else "·", self.repo_attr(name) if n else dim()))
                runs.append((f"  {window_count(commits[name]):>7}  ", 0))
            about = ("private · " if r["private"] else "") + r["description"]
            used = 1 + sum(len(text) for text, _ in runs)
            runs.append((fit(about, w - used - 1), dim()))
            if t + row == sel:
                put(scr, y, 0, " " * w, w, curses.A_REVERSE)
                runs = [(text, attr | curses.A_REVERSE) for text, attr in runs]
            put_runs(scr, y, 1, runs, w)

    # -------------------------------------------------------- branches

    def branch_rows(self):
        """([(repo, None, None) heading or (repo, branch, compare)], repos with only their default branch).
        A repo's branches are newest first; ones not compared yet go last."""
        rows, singles = [], 0
        for r in self.g.repo_list():
            branches = self.g.branches.get(r["name"])
            if branches is None:
                continue
            others = [b for b in branches if not b["default"]]
            if not others:
                singles += 1
                continue
            found = [(b, self.g.compare(r["name"], b)) for b in others]
            found.sort(key=lambda bf: -(bf[1]["when"] or 0) if isinstance(bf[1], dict) else 1)
            rows.append((r, None, None))
            rows += [(r, b, f) for b, f in found]
        return rows, singles

    def draw_branches(self, scr, top, height, w):
        self.g.want_branches()
        rows, singles = self.branch_rows()
        if not rows:
            if not self.g.loaded or self.g.branches_loading or not self.g.branches:
                n = len(self.g.repos)
                self.centre(scr, top + height // 2 - 1, w,
                            f"listing the branches of {n} repos — a call each, and one per branch to compare" if n
                            else "fetching the repos …", dim())
            else:
                self.centre(scr, top + height // 2 - 1, w,
                            f"every repo has only its default branch ({singles})" if singles else "no branches", dim())
            return
        picks = [i for i, row in enumerate(rows) if row[1]]
        keys = [(rows[i][0]["name"], rows[i][1]["name"]) for i in picks]
        if self.sel_branch in keys:
            self.sel["branches"] = keys.index(self.sel_branch)
        sel = self.sel["branches"] = max(0, min(self.sel["branches"], len(picks) - 1))
        self.sel_branch = keys[sel]
        lines = rows + ([(None, None, singles)] if singles else [])
        at = picks[sel]
        want = at - 1 if rows[at - 1][1] is None else at       # keep the repo's heading in view
        t = max(min(self.top["branches"], want), at - height + 1, 0)
        t = self.top["branches"] = max(0, min(t, len(lines) - height))

        found = [f for _, b, f in rows if b and isinstance(f, dict)]
        peak = max([max(f["ahead"], f["behind"]) for f in found] + [1])
        nw = min(28, max(len(b["name"]) for _, b, _ in rows if b) + 2)
        half = max(0, min(12, (w - nw - 34) // 2))   # each side of the ahead/behind bar
        for row, (r, b, f) in enumerate(lines[t: t + height]):
            y = top + row
            if r is None:
                put(scr, y, 3, f"{f} more repo{'s' * (f != 1)} with only a default branch", w, dim())
                continue
            if b is None:
                n = len(self.g.branches.get(r["name"], [])) - 1
                more = f" (the first {MAX_COMPARES} compared)" if n > MAX_COMPARES else ""
                put_runs(scr, y, 1, [(self.g.labels.get(r["name"], r["name"]), self.repo_attr(r["name"]) | curses.A_BOLD),
                                     (f"  {n} branch{'es' * (n != 1)} besides {r['default']}{more} ", dim()),
                                     ("─" * w, dim())], w - 1)
                continue
            runs = [("  ", 0), (fit(b["name"], nw), curses.A_BOLD)]
            if isinstance(f, dict):
                state, attr = (("same as " + r["default"], dim()) if not f["ahead"] and not f["behind"]
                               else ("merged", dim()) if not f["ahead"]
                               else ("ahead", curses.color_pair(2)) if not f["behind"]
                               else ("diverged", curses.color_pair(3)))
                behind = -(-f["behind"] * half // peak) if f["behind"] else 0
                ahead = -(-f["ahead"] * half // peak) if f["ahead"] else 0
                if w < 70:                 # no room for the bar: −behind +ahead
                    runs += [(f"{'−' + str(f['behind']):>6} ", curses.color_pair(4) if f["behind"] else dim()),
                             (f"{'+' + str(f['ahead']):<6}", curses.color_pair(2) if f["ahead"] else dim()),
                             (f"{ago(f['when']) if f['when'] else '—':>4}  ", dim()), (state, attr)]
                else:
                    runs += [(f"{f['behind']:>5} behind ", curses.color_pair(4) if f["behind"] else dim()),
                             (" " * (half - behind), 0), ("█" * behind, curses.color_pair(4)), ("│", dim()),
                             ("█" * ahead, curses.color_pair(2)), (" " * (half - ahead), 0),
                             (f" {str(f['ahead']) + ' ahead':<10}", curses.color_pair(2) if f["ahead"] else dim()),
                             (f"{ago(f['when']) if f['when'] else '—':>4}  ", dim()), (state, attr)]
            elif isinstance(f, str):
                runs.append((f"! {f}", curses.color_pair(4)))
            else:
                runs.append(("comparing …" if self.g.branches_loading else "not compared", dim()))
            if t + row == at:
                put(scr, y, 0, " " * w, w, curses.A_REVERSE)
                runs = [(text, attr | curses.A_REVERSE) for text, attr in runs]
            put_runs(scr, y, 1, runs, w)

    # -------------------------------------------------------- detail

    def detail_lines(self, c, w):
        """The detail view as lines of runs."""
        detail = self.g.detail(c)
        text_w = w - 4
        lines = [[(self.g.labels.get(c["repo"], c["repo"]), self.repo_attr(c["repo"]) | curses.A_BOLD),
                  ("  ", 0), (c["sha"], curses.color_pair(3))]]
        if isinstance(detail, dict):
            a, k = detail["author"], detail["committer"]
            login = [(f"  @{detail['login']}", curses.color_pair(6))] if detail["login"] else []
            lines.append([("Author  ", dim()), (f"{a['name']} <{a['email']}>", 0)] + login)
            lines.append([("Date    ", dim()), (self.when(a["when"]), 0)])
            if (k["name"], k["email"]) != (a["name"], a["email"]) or abs(k["when"] - a["when"]) > 60:
                lines.append([("Landed  ", dim()), (f"{self.when(k['when'])}  by {k['name']}", dim())])
        else:
            lines.append([("Author  ", dim()), (c["author"], 0)])
            lines.append([("Date    ", dim()), (self.when(c["when"]), 0)])
        lines.append([])
        title, _, body = c["message"].partition("\n")
        lines += [[(line, curses.A_BOLD)] for line in wrap(title, text_w)]
        body = body.strip("\n")
        if body:
            lines.append([])
            lines += [[(line, dim() if TRAILER.match(line) else 0)] for line in wrap(body, text_w)]
        lines.append([])
        if detail is None:
            lines.append([("loading the changed files …", dim())])
        elif isinstance(detail, str):
            lines.append([(f"! {detail} — r to retry", curses.color_pair(4))])
        else:
            lines += self.file_lines(detail, w)
        return lines

    def when(self, ts):
        return f"{datetime.fromtimestamp(ts).strftime('%a %-d %b %Y %H:%M')}  ({ago(ts)} ago)"

    def file_lines(self, detail, w):
        files = detail["files"]
        n = len(files)
        lines = [[(f"{n} file{'s' * (n != 1)} changed{' (the first 300)' if n >= 300 else ''}  ", 0),
                  (f"+{detail['add']}", curses.color_pair(2)), (f" −{detail['del']}", curses.color_pair(4))]]
        if not files:
            return lines
        peak = max(f["add"] + f["del"] for f in files) or 1
        num_w = max(len(f"+{f['add']} −{f['del']}") for f in files)
        bar_w = min(24, max(0, w - 60))
        name_w = w - 4 - 2 - num_w - 2 - bar_w - 2
        for f in files:
            mark, pair = STATUS_MARK.get(f["status"], ("?", 5))
            name = f"{f['old']} → {f['name']}" if f["old"] else f["name"]
            plus = -(-f["add"] * bar_w // peak) if f["add"] else 0
            minus = -(-f["del"] * bar_w // peak) if f["del"] else 0
            over = plus + minus - bar_w    # rounding both up can overshoot
            if over > 0:
                plus, minus = (plus - over, minus) if plus >= minus else (plus, minus - over)
            lines.append([(f"  {mark} ", curses.color_pair(pair)), (fit_left(name, name_w) + "  ", 0),
                          (f"+{f['add']} −{f['del']}".rjust(num_w) + "  ", dim()),
                          ("+" * plus, curses.color_pair(2)), ("-" * minus, curses.color_pair(4))])
        return lines

    def draw_detail(self, scr, top, height, w):
        lines = self.detail_lines(self.commit, w)
        self.scroll = max(0, min(self.scroll, len(lines) - height))
        for row, runs in enumerate(lines[self.scroll: self.scroll + height]):
            put_runs(scr, top + row, 2, runs, w - 1)
        if len(lines) > height:
            pos = f" {self.scroll + 1}–{min(len(lines), self.scroll + height)}/{len(lines)} "
            put(scr, 1, w - len(pos) - 2, pos, w, dim())

    # -------------------------------------------------------- keys

    def key(self, scr, key):
        """Handles one key; returns False to quit."""
        h, _ = scr.getmaxyx()
        page = max(1, h - 5)
        if key in (ord("q"), ord("Q")):
            return False
        if key == ord("r"):
            if self.view == "detail" and isinstance(self.g.details.get(self.commit["sha"]), str):
                self.g.retry_detail(self.commit)
            else:
                self.g.refresh(force=True)
                if self.g.loading:
                    self.say("fetching …", dim())
        elif key == ord("\t"):
            self.view = {"feed": "repos", "repos": "branches", "branches": "feed", "detail": "repos"}[self.view]
            if self.view == "feed":
                self.repo = self.branch = None
        elif self.view == "branches":
            self.branches_key(key, page)
        elif self.view == "feed":
            self.feed_key(key, page)
        elif self.view == "repos":
            self.repos_key(key, page)
        elif self.view == "detail":
            self.detail_key(key, page)
        return True

    def nav(self, key, page, total):
        """The movement keys every list shares; True when the key was one of them."""
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page,
                 curses.KEY_HOME: -total, ord("g"): -total, curses.KEY_END: total, ord("G"): total}
        if key in steps and total:
            self.move(steps[key], total)
        return key in steps

    def feed_key(self, key, page):
        commits = self.feed_commits()
        if self.nav(key, page, len(commits)) or not commits and key not in BACK_KEYS:
            return
        c = commits[self.sel["feed"]] if commits else None
        if key in OPEN_KEYS:
            self.view, self.commit, self.scroll = "detail", c, 0
        elif key in BACK_KEYS and self.branch:
            self.view, self.branch = "branches", None
        elif key in BACK_KEYS and self.repo:
            self.view, self.repo = "repos", None
        elif key == ord("o"):
            self.browse(c["url"])
        elif key == ord("c"):
            self.copy(c["sha"])

    def repos_key(self, key, page):
        repos = self.g.repo_list()
        if self.nav(key, page, len(repos)) or not repos:
            return
        r = repos[self.sel["repos"]]
        if key in OPEN_KEYS:
            self.view, self.repo, self.branch = "feed", r["name"], None
            self.sel["feed"], self.top["feed"], self.sel_key = 0, 0, None
        elif key == ord("o"):
            self.browse(r["url"])

    def branches_key(self, key, page):
        rows = [row for row in self.branch_rows()[0] if row[1]]
        if self.nav(key, page, len(rows)) or not rows:
            return
        repo, b, found = rows[self.sel["branches"]]
        if key in OPEN_KEYS:
            if isinstance(found, dict) and found["commits"]:
                self.view, self.branch, self.repo = "feed", (repo["name"], b), None
                self.sel["feed"], self.top["feed"], self.sel_key = 0, 0, None
            elif isinstance(found, dict):
                self.say(f"{b['name']} has nothing that {repo['default']} doesn't", dim())
            else:
                self.say(f"{b['name']} hasn't been compared", dim())
        elif key == ord("o"):
            q = urllib.parse.quote
            self.browse(f"{repo['url']}/compare/{q(repo['default'], safe='')}...{q(b['name'], safe='')}")
        elif key == ord("c"):
            self.copy(b["name"])

    def detail_key(self, key, page):
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page, ord(" "): page,
                 curses.KEY_HOME: -10**6, ord("g"): -10**6, curses.KEY_END: 10**6, ord("G"): 10**6}
        if key in steps:
            self.scroll = max(0, self.scroll + steps[key])
        elif key in BACK_KEYS:
            self.view = "feed"
        elif key in (ord("n"), ord("p")):
            commits = self.feed_commits()
            if commits:
                self.move(1 if key == ord("n") else -1, len(commits), "feed")
                self.commit, self.scroll = self.selected_commit(), 0
        elif key == ord("o"):
            self.browse(self.commit["url"])
        elif key == ord("c"):
            self.copy(self.commit["sha"])

    def browse(self, url):
        try:
            opened = webbrowser.open(url)
        except webbrowser.Error:
            opened = False
        self.say(f"opened {url}" if opened else f"couldn't open a browser — {url}", 0 if opened else curses.color_pair(4))

    def copy(self, text):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {text}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))


OPEN_KEYS = (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l"))
BACK_KEYS = (27, curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127, 8)


def run(stdscr, args):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, a repo colour
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # additions, a repo colour
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # shas, a repo colour
    curses.init_pair(4, curses.COLOR_RED, -1)      # deletions, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # authors, a repo colour
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)  # a repo colour
    stdscr.timeout(125)

    glance = Glance(args.targets, args.repos)
    ui = UI(glance)
    while True:
        glance.refresh()
        ui.draw(stdscr)
        stdscr.refresh()
        key = stdscr.getch()
        if key != -1 and not ui.key(stdscr, key):
            return


def print_once(args):
    bold, dim_, cyan, yellow, red, reset = "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"
    glance = Glance(args.targets, args.repos)
    glance.load()
    for err in glance.errors:
        print(f"{red}error:{reset} {err}")
    commits = glance.feed()[: args.once]
    rw = max([len(glance.labels[c["repo"]]) for c in commits] + [0])
    aw = min(14, max([len(c["author"]) for c in commits] + [0]))
    last = None
    for c in commits:
        day = day_label(c["when"])
        if day != last:
            print(f"{bold}{day}{reset}")
            last = day
        print(f"{dim_}{ago(c['when']):>4}{reset}  {cyan}{glance.labels[c['repo']]:<{rw}}{reset}  "
              f"{dim_}{fit(c['author'], aw)}{reset}  {yellow}{c['sha'][:7]}{reset}  {c['title']}")
    return 1 if glance.errors and not commits else 0


def print_branches(args):
    bold, dim_, green, red, reset = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"
    glance = Glance(args.targets, args.repos)
    glance.load()
    glance.branches_loading = True
    glance.load_branches()
    for err in glance.errors + glance.branch_errors:
        print(f"{red}error:{reset} {err}")
    singles = 0
    for r in glance.repo_list():
        branches = [b for b in glance.branches.get(r["name"], []) if not b["default"]]
        if not branches:
            singles += r["name"] in glance.branches
            continue
        print(f"{bold}{glance.labels[r['name']]}{reset}  {dim_}besides {r['default']}{reset}")
        nw = max(len(b["name"]) for b in branches)
        for b in branches:
            f = glance.compare(r["name"], b)
            if isinstance(f, dict):
                print(f"  {b['name']:<{nw}}  {red if f['behind'] else dim_}{f['behind']:>5} behind{reset}  "
                      f"{green if f['ahead'] else dim_}{f['ahead']:>5} ahead{reset}  "
                      f"{dim_}{ago(f['when']) + ' ago' if f['when'] else ''}{reset}")
            else:
                print(f"  {b['name']:<{nw}}  {dim_}{f or 'not compared'}{reset}")
    if singles:
        print(f"{dim_}{singles} more repo{'s' * (singles != 1)} with only a default branch{reset}")
    return 1 if (glance.errors or glance.branch_errors) and not glance.branches else 0


def main():
    parser = argparse.ArgumentParser(
        description="The recent commits of GitHub repos at a glance.",
        epilog="Set GITHUB_TOKEN (or GH_TOKEN) to include private repos and lift the limit of 60 requests an hour.")
    parser.add_argument("targets", nargs="*", metavar="OWNER|OWNER/REPO",
                        help=f"whose repos to show, as names or github.com URLs (default {' '.join(DEFAULT_TARGETS)})")
    parser.add_argument("--repos", type=int, default=MAX_REPOS, metavar="N",
                        help=f"most recently pushed repos to show per owner (default {MAX_REPOS})")
    parser.add_argument("--once", nargs="?", const=20, type=int, metavar="N",
                        help="print the N newest commits (default 20) and exit")
    parser.add_argument("--branches", action="store_true",
                        help="print each repo's branches, how far ahead of and behind the default branch, and exit")
    args = parser.parse_args()
    args.targets = [t for t in map(normalize_target, args.targets or DEFAULT_TARGETS) if t]
    if not args.targets:
        parser.error("no owner or repo given")
    if args.once:
        raise SystemExit(print_once(args))
    if args.branches:
        raise SystemExit(print_branches(args))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
