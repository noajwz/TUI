#!/usr/bin/env python3
"""Terminal UI showing how much of your Claude usage limits is spent.

Reads the utilization block Claude Code caches in ~/.claude.json, so it needs
no credentials and makes no network calls.  Any running Claude Code session
refreshes that cache, which is what makes this live rather than a snapshot.
"""

import argparse
import curses
import json
import os
import sys
import time
from datetime import datetime

CONFIG_PATH = os.path.expanduser("~/.claude.json")
REFRESH_SECONDS = 2       # how often to stat the cache file
STALE_SECONDS = 15 * 60   # older than this and the numbers get a warning
BAR_WIDTH = 32
BAR_FILLED = "█"
BAR_EMPTY = "░"

# Labels for the limit kinds the cache reports.  Anything unlisted falls back
# to a prettified version of its own key, so a new limit still shows up.
LIMIT_LABELS = {
    "session": "Session (5h)",
    "weekly_all": "Weekly (all models)",
    "weekly_opus": "Weekly (Opus)",
    "weekly_sonnet": "Weekly (Sonnet)",
    "weekly_cowork": "Weekly (Cowork)",
    "weekly_oauth_apps": "Weekly (OAuth apps)",
}

# Older caches have no "limits" array, only these top-level buckets.
LEGACY_BUCKETS = [
    ("five_hour", "Session (5h)"),
    ("seven_day", "Weekly (all models)"),
    ("seven_day_opus", "Weekly (Opus)"),
    ("seven_day_sonnet", "Weekly (Sonnet)"),
    ("seven_day_cowork", "Weekly (Cowork)"),
    ("seven_day_oauth_apps", "Weekly (OAuth apps)"),
]

PLAN_NAMES = {
    "claude_pro": "Pro",
    "claude_max": "Max",
    "claude_team": "Team",
    "claude_enterprise": "Enterprise",
}


# ---------------------------------------------------------------- reading


def read_config(path):
    """Return (config dict, error). Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"{path} not found - is Claude Code installed?"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"cannot read {os.path.basename(path)}: {e}"


def parse_ts(text):
    """ISO-8601 (with Z or an offset) to an aware local datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def label_for(kind, scope=None):
    label = LIMIT_LABELS.get(kind) or kind.replace("_", " ").capitalize()
    if scope and str(scope).lower() not in label.lower():
        label = f"{label} - {scope}"
    return label


def limits_from(util):
    """Flatten the utilization block into [{label, percent, severity, ...}]."""
    limits = []
    entries = util.get("limits")
    if isinstance(entries, list) and entries:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            percent = entry.get("percent")
            if not isinstance(percent, (int, float)):
                continue
            kind = entry.get("kind") or entry.get("group") or "limit"
            limits.append({
                "label": label_for(str(kind), entry.get("scope")),
                "percent": float(percent),
                "severity": entry.get("severity"),
                "resets_at": parse_ts(entry.get("resets_at")),
                "active": bool(entry.get("is_active", True)),
            })
        return limits

    for key, label in LEGACY_BUCKETS:
        bucket = util.get(key)
        if not isinstance(bucket, dict):
            continue
        percent = bucket.get("utilization")
        if not isinstance(percent, (int, float)):
            continue
        limits.append({
            "label": label,
            "percent": float(percent),
            "severity": None,
            "resets_at": parse_ts(bucket.get("resets_at")),
            "active": True,
        })
    return limits


def spend_line(util):
    """One line about paid extra usage, or None when it is off."""
    spend = util.get("spend")
    if not isinstance(spend, dict) or not spend.get("enabled"):
        return None
    used = spend.get("used") or {}
    amount = used.get("amount_minor")
    exponent = used.get("exponent", 2)
    currency = used.get("currency", "")
    if not isinstance(amount, (int, float)):
        return None
    text = f"Extra usage: {amount / (10 ** exponent):.2f} {currency}".strip()
    percent = spend.get("percent")
    if isinstance(percent, (int, float)):
        text += f" ({percent:.0f}% of cap)"
    return text


def read_usage(path=CONFIG_PATH):
    """Return (snapshot, error). Snapshot is None when there is nothing to show."""
    config, error = read_config(path)
    if error:
        return None, error

    cached = config.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None, "no usage data cached yet - open a Claude Code session once"
    util = cached.get("utilization")
    if not isinstance(util, dict):
        return None, "cached usage data is empty"

    account = config.get("oauthAccount") or {}
    fetched_ms = cached.get("fetchedAtMs")
    org_type = account.get("organizationType") or ""
    return {
        "limits": limits_from(util),
        "fetched_at": fetched_ms / 1000 if isinstance(fetched_ms, (int, float)) else None,
        "name": account.get("displayName") or account.get("emailAddress") or "",
        "plan": PLAN_NAMES.get(org_type, org_type),
        "spend": spend_line(util),
    }, None


class Cache:
    """Re-reads the config only when it changes on disk."""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.snapshot = None
        self.error = None
        self._mtime = None
        self._checked = 0.0

    def poll(self, force=False):
        now = time.monotonic()
        if self.snapshot is None and self.error is None:
            force = True
        if force or now - self._checked >= REFRESH_SECONDS:
            self._checked = now
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = None
            if force or mtime != self._mtime:
                self._mtime = mtime
                self.snapshot, self.error = read_usage(self.path)
        return self.snapshot, self.error


# ---------------------------------------------------------------- formatting


def fmt_delta(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def fmt_reset(when, now=None):
    """'resets in 4h 12m  (02:00)' for a reset time, '' when unknown."""
    if when is None:
        return ""
    now = now or datetime.now().astimezone()
    delta = (when - now).total_seconds()
    clock = when.strftime("%H:%M") if when.date() == now.date() else when.strftime("%a %d %b %H:%M")
    if delta <= 0:
        return f"reset due  ({clock})"
    return f"resets in {fmt_delta(delta)}  ({clock})"


def fmt_age(fetched_at, now=None):
    if fetched_at is None:
        return "age unknown"
    age = max(0.0, (now or time.time()) - fetched_at)
    return f"updated {fmt_delta(age)} ago" if age >= 1 else "updated just now"


def is_stale(fetched_at, now=None):
    return fetched_at is not None and (now or time.time()) - fetched_at > STALE_SECONDS


def draw_bar(percent, width=BAR_WIDTH):
    filled = max(0, min(width, round(width * percent / 100)))
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def level(limit):
    """0 fine, 1 getting close, 2 nearly out - severity first, percent as backup."""
    severity = str(limit.get("severity") or "").lower()
    if severity in ("critical", "exceeded", "blocked"):
        return 2
    if severity in ("warning", "warn", "elevated"):
        return 1
    percent = limit["percent"]
    if percent >= 90:
        return 2
    if percent >= 70:
        return 1
    return 0


# ---------------------------------------------------------------- curses UI

TITLE_PAIR, GOOD_PAIR, WARN_PAIR, ALERT_PAIR, DIM_PAIR = 1, 2, 3, 4, 5
LEVEL_PAIRS = (GOOD_PAIR, WARN_PAIR, ALERT_PAIR)


def main(stdscr, path=CONFIG_PATH):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(TITLE_PAIR, curses.COLOR_CYAN, -1)    # title
    curses.init_pair(GOOD_PAIR, curses.COLOR_GREEN, -1)    # plenty left
    curses.init_pair(WARN_PAIR, curses.COLOR_YELLOW, -1)   # getting close
    curses.init_pair(ALERT_PAIR, curses.COLOR_RED, -1)     # nearly out
    curses.init_pair(DIM_PAIR, curses.COLOR_WHITE, -1)     # labels / chrome

    stdscr.timeout(1000)
    cache = Cache(path)
    force = False

    while True:
        snapshot, error = cache.poll(force)
        force = False
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, text, attr=0, h=h, w=w):
            if 0 <= y < h and 0 <= x < w - 1:
                stdscr.addnstr(y, x, text, w - x - 1, attr)

        title = "Claude usage limits"
        put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(TITLE_PAIR) | curses.A_BOLD)

        y = 2
        if error:
            put(y, 2, error, curses.color_pair(ALERT_PAIR))
        elif not snapshot["limits"]:
            put(y, 2, "No limits reported.", curses.color_pair(DIM_PAIR))
        else:
            now = datetime.now().astimezone()
            limits = snapshot["limits"]
            label_w = max(len(lim["label"]) for lim in limits)
            bar_w = max(8, min(BAR_WIDTH, w - label_w - 16))
            for lim in limits:
                pair = curses.color_pair(LEVEL_PAIRS[level(lim)])
                put(y, 2, lim["label"].ljust(label_w), curses.A_BOLD)
                put(y, 4 + label_w, draw_bar(lim["percent"], bar_w), pair)
                put(y, 5 + label_w + bar_w, f"{lim['percent']:>3.0f}%", pair | curses.A_BOLD)
                y += 1
                reset = fmt_reset(lim["resets_at"], now)
                if reset:
                    suffix = "" if lim["active"] else "   not the binding limit"
                    put(y, 4, reset + suffix, curses.color_pair(DIM_PAIR))
                    y += 1
                y += 1
                if y >= h - 3:
                    break

            if snapshot["spend"] and y < h - 3:
                put(y, 2, snapshot["spend"], curses.color_pair(DIM_PAIR))

        if snapshot:
            info_y = h - 2   # sits just above the footer, whatever the height
            who = " - ".join(part for part in (snapshot["name"], snapshot["plan"]) if part)
            if who:
                put(info_y, 2, who, curses.color_pair(DIM_PAIR))
            age = fmt_age(snapshot["fetched_at"])
            attr = curses.color_pair(WARN_PAIR if is_stale(snapshot["fetched_at"]) else DIM_PAIR)
            put(info_y, max(0, w - len(age) - 3), age, attr)

        put(h - 1, 2, "q quit    r refresh", curses.color_pair(DIM_PAIR))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            break
        if key in (ord("r"), ord("R")):
            force = True


# ---------------------------------------------------------------- one-shot

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
LEVEL_COLORS = (GREEN, YELLOW, RED)


def print_once(path=CONFIG_PATH, color=True):
    """Plain-text dump for scripts, prompts and 'just tell me the number'."""
    def paint(text, code):
        return f"{code}{text}{RESET}" if color else text

    snapshot, error = read_usage(path)
    if error:
        print(paint(f"Claude usage: {error}", RED), file=sys.stderr)
        return 1

    limits = snapshot["limits"]
    if not limits:
        print("Claude usage: no limits reported.")
        return 0

    now = datetime.now().astimezone()
    label_w = max(len(lim["label"]) for lim in limits)
    print(paint("Claude usage limits", CYAN + BOLD))
    for lim in limits:
        code = LEVEL_COLORS[level(lim)]
        bar = paint(draw_bar(lim["percent"]), code)
        percent = paint(f"{lim['percent']:>3.0f}%", code + BOLD)
        print(f"  {lim['label'].ljust(label_w)}  {bar} {percent}")
        reset = fmt_reset(lim["resets_at"], now)
        if reset:
            print(f"  {' ' * label_w}  {paint(reset, DIM)}")
    if snapshot["spend"]:
        print(f"  {snapshot['spend']}")
    print(f"  {paint(fmt_age(snapshot['fetched_at']), YELLOW if is_stale(snapshot['fetched_at']) else DIM)}")
    return 0


def cli():
    parser = argparse.ArgumentParser(description="Show Claude usage limits in the terminal.")
    parser.add_argument("--once", action="store_true",
                        help="print the limits once and exit instead of running the TUI")
    parser.add_argument("--no-color", action="store_true", help="plain text output with --once")
    parser.add_argument("--config", default=CONFIG_PATH,
                        help=f"path to Claude Code's config (default: {CONFIG_PATH})")
    args = parser.parse_args()

    if args.once:
        return print_once(args.config, color=not args.no_color and sys.stdout.isatty())
    curses.wrapper(main, args.config)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
