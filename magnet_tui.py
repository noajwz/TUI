#!/usr/bin/env python3
"""Magnet grabber — paste any text and the magnet links and torrent info hashes in it are picked
out into a list below it, to copy later one at a time: Enter copies a magnet and moves on (macOS).

The text can be anything: a web page's source, a chat, a forum post, a list. Magnet links are
recognised with &amp; escapes and inside percent-encoded redirect links; a bare 40-character hex
info hash (or a base32 urn:btih:) becomes a magnet link of its own. The same torrent found twice
is one entry, keeping the link that says the most (name, size, trackers). The list is saved
(~/.local/state/magnet_tui/magnets.json, or --file), so it is still there next time, and with
w on, every magnet copied anywhere else lands in it too.
"""

import argparse
import base64
import binascii
import curses
import json
import locale
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, unquote

DATA_PATH = os.path.expanduser("~/.local/state/magnet_tui/magnets.json")
TICK_MS = 250
CLIPBOARD_SECONDS = 1.0        # how often w looks at the clipboard
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 14
TEXT_TAIL = 20_000             # characters at the end of the text pane that are drawn
PASTE_MIN = 6                  # characters arriving at once that count as a paste, not keys

MAGNET = re.compile(r"magnet:\?[^\s\"'<>`]+", re.I)
BARE_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{40}(?![0-9A-Za-z])")
BTIH_B32 = re.compile(r"urn:btih:([A-Za-z2-7]{32})(?![0-9A-Za-z])", re.I)
XT_V1 = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})", re.I)
XT_V2 = re.compile(r"urn:btmh:1220([0-9a-f]{64})", re.I)     # BitTorrent v2: a sha-256 multihash
TRAILING = ".,;:!?)]}*"        # punctuation a sentence leaves after a link


# -- finding --

def to_hex(h):
    """A v1 info hash as 40 lower-case hex digits; base32 is the other way it is written."""
    if len(h) == 40:
        return h.lower()
    return base64.b32decode(h.upper()).hex()


def clean(text):
    text = unicodedata.normalize("NFC", text)
    return "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in text)


def parse_magnet(link):
    """An entry for a magnet link, or None when it names no torrent."""
    link = link.replace("&amp;", "&").replace("&#38;", "&").rstrip(TRAILING)
    params = parse_qs(link.partition("?")[2], keep_blank_values=True)
    v1 = v2 = None
    for xt in params.get("xt", []):
        if m := XT_V1.fullmatch(xt):
            try:
                v1 = to_hex(m[1])
            except binascii.Error:
                pass
        elif m := XT_V2.fullmatch(xt):
            v2 = m[1].lower()
    if not (v1 or v2):
        return None
    xl = params.get("xl", [""])[0]
    return {"hash": v1 or "btmh:" + v2, "magnet": link, "name": clean(params.get("dn", [""])[0]).strip(),
            "size": int(xl) if xl.isdigit() else None, "trackers": len(params.get("tr", [])), "bare": False}


def bare(h):
    return {"hash": h, "magnet": f"magnet:?xt=urn:btih:{h}", "name": "", "size": None, "trackers": 0, "bare": True}


def richer(new, old):
    """Whether `new` says more about the same torrent than `old` does."""
    if old["bare"] != new["bare"]:
        return old["bare"]
    return len(new["magnet"]) > len(old["magnet"])


def find(text):
    """The torrents in `text`, in the order they first appear, one entry per info hash."""
    found, seen = [], {}
    sources = [text]
    if "%3a" in text.lower():              # a magnet inside a redirect link's parameter
        sources.append(unquote(text))
    for src in sources:
        hits = []
        for m in MAGNET.finditer(src):
            e = parse_magnet(m[0])
            if e:
                hits.append((m.start(), e))
        for m in BARE_HEX.finditer(src):
            hits.append((m.start(), bare(m[0].lower())))
        for m in BTIH_B32.finditer(src):
            hits.append((m.start(), bare(to_hex(m[1]))))
        hits.sort(key=lambda hit: hit[0])
        for _, e in hits:
            old = seen.get(e["hash"])
            if old is None:
                seen[e["hash"]] = e
                found.append(e)
            elif richer(e, old):
                old.update(e)
    return found


def spans(text):
    """Character ranges of `text` that were picked out, for highlighting."""
    out = [m.span() for rx in (MAGNET, BARE_HEX, BTIH_B32) for m in rx.finditer(text)]
    return out


# -- the saved list --

class Store:
    def __init__(self, path):
        self.path = path
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            raise SystemExit(f"magnet_tui: can't read {self.path}: {e}")
        self.items = [e for e in data.get("magnets", []) if isinstance(e, dict) and e.get("hash")]
        self.stamp = self.file_stamp()

    def file_stamp(self):
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def changed_on_disk(self):
        return self.file_stamp() != self.stamp

    def save(self):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".magnets-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"magnets": self.items}, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)          # never a half-written file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.stamp = self.file_stamp()

    def add(self, entries, create=True):
        """Puts `entries` in the list; ones already there are improved if the new link says
        more. Returns (the new entries, how many known ones were found again)."""
        index = {e["hash"]: e for e in self.items}
        new, again = [], 0
        for e in entries:
            old = index.get(e["hash"])
            if old is not None:
                again += 1
                if richer(e, old):
                    old.update({k: e[k] for k in ("magnet", "name", "size", "trackers", "bare")})
            elif create:
                item = dict(e, added=time.time(), copied=None)
                self.items.append(item)
                index[item["hash"]] = item
                new.append(item)
        if new or again:
            self.save()
        return new, again


# -- formatting --

def fmt_size(n):
    """Binary units, as torrent clients show them."""
    for unit, limit in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= limit:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{n} B"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def chw(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cwidth(text):
    return sum(chw(ch) for ch in text)


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = chw(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def title(e):
    return e["name"] or ("info hash " + e["hash"] if e["bare"] else e["hash"])


def wrap_tail(text, width, height):
    """The last `height` screen lines of `text` wrapped at `width`, as [[(char, picked)]]."""
    start = max(0, len(text) - TEXT_TAIL)
    tail = text[start:]
    flags = bytearray(len(tail))
    for a, b in spans(tail):
        flags[a:b] = b"\1" * (b - a)
    rows, row, used = [], [], 0
    for i, ch in enumerate(tail):
        if ch == "\n":
            rows.append(row)
            row, used = [], 0
            continue
        ch = " " if ch == "\t" else "?" if ord(ch) < 32 or ord(ch) == 127 else ch
        w = chw(ch)
        if used + w > width:
            rows.append(row)
            row, used = [], 0
        row.append((ch, flags[i]))
        used += w
    rows.append(row)
    return rows[-height:]


def clipboard():
    try:
        return subprocess.run(["pbpaste"], capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


# -- the window --

class App:
    def __init__(self, stdscr, store, watch):
        self.stdscr, self.store = stdscr, store
        self.focus = "list"                # or "text"
        self.text = ""
        self.text_seen = set()             # hashes the text pane has yielded, so typing doesn't re-add deleted ones
        self.sel = None                    # hash of the selected entry
        self.top = 0
        self.needle = ""
        self.confirm = None                # (question, action) waiting for y
        self.undo = []                     # [(index, entry)] of deletions
        self.watch = False
        self.own_clip = None               # what we put on the clipboard ourselves
        self.last_clip, self.clip_at = None, 0.0
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        if watch:
            self.toggle_watch()

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def warn(self, text):
        self.say(text, curses.color_pair(4))

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    # -- the list --

    def rows(self):
        needle = self.needle.casefold()
        return [e for e in self.store.items
                if not needle or needle in e["name"].casefold() or needle in e["hash"]]

    def index(self, rows):
        for i, e in enumerate(rows):
            if e["hash"] == self.sel:
                return i
        return 0

    def selected(self):
        rows = self.rows()
        return rows[self.index(rows)] if rows else None

    def move(self, delta):
        rows = self.rows()
        if rows:
            self.sel = rows[max(0, min(len(rows) - 1, self.index(rows) + delta))]["hash"]

    # -- taking text in --

    def report(self, new, again, where):
        if new:
            self.sel = new[0]["hash"]
            extra = f", {again:,} already in the list" if again else ""
            self.say(f"{plural(len(new), 'new magnet')} from {where}{extra}", curses.color_pair(2))
        elif again:
            self.say(f"nothing new from {where} — {plural(again, 'magnet')} already in the list")
        else:
            self.warn(f"no magnet links or info hashes in {where}")

    def take(self, text, where):
        """Text pasted or read: everything found in it goes in, deleted ones too."""
        entries = find(text)
        self.text_seen |= {e["hash"] for e in entries}
        self.report(*self.store.add(entries), where)

    def paste(self, text, where="the paste"):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if self.text and not self.text.endswith("\n"):
            self.text += "\n"
        self.text += text
        self.take(text, where)

    def retyped(self):
        """After typing in the text pane: only torrents the text hadn't shown yet are added."""
        entries = find(self.text)
        fresh = [e for e in entries if e["hash"] not in self.text_seen]
        self.store.add([e for e in entries if e["hash"] in self.text_seen], create=False)
        if fresh:
            self.text_seen |= {e["hash"] for e in fresh}
            new, _ = self.store.add(fresh)
            if new:
                self.sel = new[0]["hash"]
                self.say(f"{plural(len(new), 'new magnet')} from the text", curses.color_pair(2))

    def toggle_watch(self):
        self.watch = not self.watch
        if self.watch:
            self.last_clip = clipboard()   # only what is copied from now on
            self.clip_at = time.monotonic()
            self.say("watching the clipboard — copy a magnet anywhere and it lands here", curses.color_pair(1))
        else:
            self.say("stopped watching the clipboard")

    def tick(self):
        if self.store.changed_on_disk():  # another window, or --file shared
            self.store.load()
        if not self.watch or time.monotonic() - self.clip_at < CLIPBOARD_SECONDS:
            return
        self.clip_at = time.monotonic()
        clip = clipboard()
        if clip is None or clip == self.last_clip:
            return
        self.last_clip = clip
        if clip == self.own_clip:
            return
        entries = find(clip)
        if entries:
            new, again = self.store.add(entries)
            if new:
                self.report(new, again, "the clipboard")

    # -- actions --

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        except (OSError, subprocess.CalledProcessError):
            self.warn("couldn't copy — pbcopy not found")
            return False
        self.own_clip = self.last_clip = text
        self.say(f"copied {what}", curses.color_pair(2))
        return True

    def copy_selected(self, field="magnet"):
        e = self.selected()
        if not e:
            self.warn("the list is empty — paste some text first")
            return
        if self.copy(e[field], ("the magnet for " if field == "magnet" else "the info hash of ") + title(e)):
            if field == "magnet":
                e["copied"] = time.time()
                self.store.save()
                self.move(1)               # on to the next, to copy a batch one after another

    def copy_all(self):
        rows = self.rows()
        if not rows:
            self.warn("nothing to copy")
            return
        if self.copy("\n".join(e["magnet"] for e in rows) + "\n", plural(len(rows), "magnet") + ", one per line"):
            now = time.time()
            for e in rows:
                e["copied"] = now
            self.store.save()

    def delete(self):
        e = self.selected()
        if not e:
            return
        rows = self.rows()
        i = self.index(rows)
        self.undo.append((self.store.items.index(e), e))
        self.store.items.remove(e)
        self.store.save()
        rest = [x for x in rows if x is not e]
        self.sel = rest[min(i, len(rest) - 1)]["hash"] if rest else None
        self.say(f"deleted {title(e)} — u brings it back")

    def undelete(self):
        if not self.undo:
            self.warn("nothing to undo")
            return
        i, e = self.undo.pop()
        if any(x["hash"] == e["hash"] for x in self.store.items):
            self.say("it is back in the list already")
            return
        self.store.items.insert(min(i, len(self.store.items)), e)
        self.store.save()
        self.sel = e["hash"]
        self.say(f"back: {title(e)}")

    def clear_list(self):
        self.undo = [(i, e) for i, e in enumerate(self.store.items)][::-1] + self.undo[-50:]
        n = len(self.store.items)
        self.store.items = []
        self.store.save()
        self.say(f"cleared {plural(n, 'magnet')} — u brings them back one at a time")

    # -- drawing --

    def draw(self, footer=True):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        items = self.store.items
        waiting = sum(1 for e in items if not e.get("copied"))
        self.put(0, 0, " Magnets ", curses.color_pair(1) | curses.A_BOLD)
        self.put(0, 10, f"{len(items):,} in the list" + (f" · {waiting:,} not copied yet" if waiting else ""),
                 curses.color_pair(5))
        if self.watch:
            self.put(0, w - 22, "◉ watching clipboard".rjust(21), curses.color_pair(2) | curses.A_BOLD)
        text_h = max(3, (h - 8) // 3)
        cursor = self.draw_text(1, text_h, w)
        self.draw_list(2 + text_h, h - 5 - text_h, w)
        self.draw_detail(h - 3, w)
        if footer:
            self.draw_footer(h - 1, w)
        if cursor and self.focus == "text":
            curses.curs_set(1)
            s.move(*cursor)
        else:
            curses.curs_set(0)
        s.refresh()

    def rule(self, y, w, label, hint, on):
        attr = (curses.color_pair(1) | curses.A_BOLD) if on else curses.color_pair(5)
        self.put(y, 0, "─" * w, attr)
        self.put(y, 1, f" {label} ", attr, cwidth(label) + 2)
        if hint:
            self.put(y, cwidth(label) + 4, f" {hint} ", curses.color_pair(5), cwidth(hint) + 2)

    def draw_text(self, y0, height, w):
        """The text pane; returns where its cursor goes."""
        on = self.focus == "text"
        hint = "typing here — ⇥ back to the list" if on else "paste anything — ⇥ to type, p pastes the clipboard"
        self.rule(y0, w, "text", hint, on)
        if not self.text:
            self.put(y0 + 1, 2, "a web page, a chat, a list: the magnet links and info hashes in it go below",
                     curses.color_pair(5))
            return y0 + 1, 2
        rows = wrap_tail(self.text, w - 3, height)
        for n, row in enumerate(rows):
            x = 2
            run, flag = [], None
            for ch, f in row + [("", 2)]:
                if f != flag and run:
                    text = "".join(run)
                    attr = curses.color_pair(2) | curses.A_BOLD if flag else curses.color_pair(5) if not on else 0
                    self.put(y0 + 1 + n, x, text, attr, cwidth(text))
                    x += cwidth(text)
                    run = []
                run.append(ch)
                flag = f
        last = rows[-1] if rows else []
        return y0 + len(rows), min(w - 1, 2 + sum(chw(c) for c, _ in last))

    def draw_list(self, y0, height, w):
        rows = self.rows()
        hint = f"/ {self.needle} · {len(rows):,} shown" if self.needle else "⏎ copies and moves on"
        self.rule(y0, w, "magnets", hint, self.focus == "list")
        if not rows:
            what = "nothing matches the filter" if self.needle else "none yet"
            self.put(y0 + 2, 2, what, curses.color_pair(5))
            return
        i = self.index(rows)
        if i < self.top:
            self.top = i
        elif i >= self.top + height:
            self.top = i - height + 1
        self.top = max(0, min(self.top, len(rows) - height))
        size_w, tr_w = 10, 13
        name_w = w - 4 - size_w - tr_w - 1
        for n, e in enumerate(rows[self.top:self.top + height]):
            y = y0 + 1 + n
            chosen = self.top + n == i and self.focus == "list"
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            done = bool(e.get("copied"))
            self.put(y, 1, "✓" if done else "•", base | curses.color_pair(2 if done else 1) | curses.A_BOLD, 1)
            attr = base | (curses.color_pair(5) if not e["name"] or done else curses.A_BOLD)
            self.put(y, 3, clean(title(e)), attr, name_w)
            size = fmt_size(e["size"]) if e.get("size") else ""
            self.put(y, 3 + name_w, size.rjust(size_w), base | curses.color_pair(5), size_w)
            tr = "hash only" if e["bare"] else plural(e["trackers"], "tracker") if e["trackers"] else "no trackers"
            self.put(y, 4 + name_w + size_w, tr.rjust(tr_w - 1), base | curses.color_pair(3 if e["bare"] else 5), tr_w - 1)
        if len(rows) > height:
            pos = f" {self.top + 1}–{min(len(rows), self.top + height)} of {len(rows):,} "
            self.put(y0, w - len(pos) - 2, pos, curses.color_pair(5))

    def draw_detail(self, y, w):
        e = self.selected()
        self.put(y - 1, 0, "─" * w, curses.color_pair(5))
        if not e:
            return
        bits = [e["hash"]]
        if e["bare"]:
            bits.append("no trackers: the client looks for peers through DHT")
        bits.append("added " + time.strftime("%d %b %H:%M", time.localtime(e["added"])))
        if e.get("copied"):
            bits.append("copied " + time.strftime("%d %b %H:%M", time.localtime(e["copied"])))
        self.put(y, 1, " · ".join(bits), curses.color_pair(1))
        self.put(y + 1, 1, clean(e["magnet"]), curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.confirm:
            self.put(y, 0, f" {self.confirm[0]}  y/n", curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD)
            return
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        if self.focus == "text":
            keys = ["type or paste", "⇥/esc the list", "⏎ new line", "^U clear the text"]
        else:
            keys = ["⏎ copy & next", "⇥ text", "p paste clipboard", "h copy hash", "a copy all",
                    "w watch clipboard", "d delete", "u undo", "o open", "x copied?", "/ filter",
                    "C clear text", "D clear list"]
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > w - 2:
            keys.pop()
        self.put(y, 0, " " + "  ".join(keys + ([] if self.focus == "text" else ["q quit"])), curses.color_pair(5))

    # -- input --

    def prompt(self, label, initial=""):
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                self.draw(footer=False)
                h, w = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(1) | curses.A_REVERSE)
                self.stdscr.move(h - 1, max(0, min(cwidth(text), w - 2)))
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf).strip()
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":
                        buf = []
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf).strip()
        finally:
            curses.curs_set(0)

    def feed(self, keys):
        """Everything that arrived at once. Many characters together are a paste, which goes
        into the text pane whichever pane has the keys; anything else is keys one by one."""
        chars = [k for k in keys if isinstance(k, str)]
        if len(chars) >= PASTE_MIN and len(chars) == len(keys) and self.focus == "list":
            self.paste("".join(chars))
            return True
        for k in keys:
            if not self.handle(k):
                return False
        return True

    def handle(self, key):
        if self.focus == "text":
            self.handle_text(key)
            return True
        ch = ord(key) if isinstance(key, str) and len(key) == 1 else key
        if self.confirm:
            action = self.confirm[1]
            self.confirm = None
            if ch in (ord("y"), ord("Y")):
                action()
            else:
                self.say("cancelled")
            return True
        if ch in (ord("q"), ord("Q")):
            return False
        page = max(1, self.stdscr.getmaxyx()[0] // 2)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in steps:
            self.move(steps[ch])
        elif ch in (10, 13, curses.KEY_ENTER, ord("c")):
            self.copy_selected()
        elif ch == ord("h"):
            self.copy_selected("hash")
        elif ch == ord("a"):
            self.copy_all()
        elif ch in (9, curses.KEY_BTAB, ord("i")):
            self.focus = "text"
        elif ch in (ord("p"), 22):         # p or ^V
            clip = clipboard()
            if clip:
                self.paste(clip, "the clipboard")
            else:
                self.warn("the clipboard is empty")
        elif ch == ord("w"):
            self.toggle_watch()
        elif ch in (ord("d"), curses.KEY_DC, 127, curses.KEY_BACKSPACE):
            self.delete()
        elif ch == ord("u"):
            self.undelete()
        elif ch == ord("x"):
            e = self.selected()
            if e:
                e["copied"] = None if e.get("copied") else time.time()
                self.store.save()
        elif ch == ord("o"):
            e = self.selected()
            if e:
                r = subprocess.run(["open", e["magnet"]], capture_output=True)
                if r.returncode == 0:
                    self.say(f"handed {title(e)} to the torrent app")
                else:
                    self.warn("no app opens magnet links here")
        elif ch == ord("/"):
            text = self.prompt(" filter by name or hash: ", self.needle)
            if text is not None:
                self.needle, self.top = text, 0
        elif ch == 27 and self.needle:
            self.needle = ""
        elif ch == ord("C"):
            self.text, self.text_seen = "", set()
            self.say("text cleared — the list stays")
        elif ch == ord("D"):
            if self.store.items:
                self.confirm = (f"Clear all {plural(len(self.store.items), 'magnet')} from the list?", self.clear_list)
        return True

    def handle_text(self, key):
        if key in ("\t", "\x1b", curses.KEY_BTAB):
            self.focus = "list"
        elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
            self.text = self.text[:-1]
        elif key == "\x15":                # ^U
            self.text, self.text_seen = "", set()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.text += "\n"
        elif isinstance(key, str) and (key.isprintable() or key == "\t"):
            self.text += key
            self.retyped()


def read_keys(stdscr):
    """The key that woke us and everything that arrived with it."""
    try:
        keys = [stdscr.get_wch()]
    except curses.error:
        return []
    stdscr.nodelay(True)
    try:
        while True:
            try:
                keys.append(stdscr.get_wch())
            except curses.error:
                break
    finally:
        stdscr.timeout(TICK_MS)
    return keys


def run(stdscr, store, texts, watch):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, focus, info
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # picked out, copied
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # messages, hash only
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, store, watch)
    for text, where in texts:
        app.paste(text, where)
    while True:
        app.draw()
        keys = read_keys(stdscr)
        if not keys:
            app.tick()
        elif not app.feed(keys):
            return


def main():
    parser = argparse.ArgumentParser(
        description="Pick the magnet links and torrent info hashes out of any text, into a list to copy from.",
        epilog="Paste into the window, press p for the clipboard, or give files (- for stdin). "
               "The list is kept in " + DATA_PATH.replace(os.path.expanduser("~"), "~") + ".")
    parser.add_argument("files", nargs="*", help="text files to take magnets from (- reads stdin)")
    parser.add_argument("-w", "--watch", action="store_true", help="start watching the clipboard")
    parser.add_argument("--file", default=os.environ.get("MAGNET_TUI_FILE", DATA_PATH),
                        help="where the list is kept (or $MAGNET_TUI_FILE)")
    parser.add_argument("--print", action="store_true",
                        help="print the magnets found in the files, stdin or the clipboard, one per line, "
                             "without opening the window or saving them")
    parser.add_argument("--hashes", action="store_true", help="with --print or --list, print info hashes instead")
    parser.add_argument("--list", action="store_true", help="print the saved list, one magnet per line")
    args = parser.parse_args()

    texts = []
    for name in args.files:
        try:
            if name == "-":
                texts.append((sys.stdin.buffer.read().decode("utf-8", "replace"), "stdin"))
            else:
                with open(name, "rb") as fh:
                    texts.append((fh.read().decode("utf-8", "replace"), os.path.basename(name)))
        except OSError as e:
            parser.error(f"can't read {name}: {e.strerror or e}")
    if not args.files and not sys.stdin.isatty() and (args.print or not args.list):
        texts.append((sys.stdin.buffer.read().decode("utf-8", "replace"), "stdin"))

    field = "hash" if args.hashes else "magnet"
    if args.print:
        if not texts:
            texts = [(clipboard() or "", "the clipboard")]
        found = find("\n".join(t for t, _ in texts))
        for e in found:
            print(e[field])
        raise SystemExit(0 if found else 1)
    store = Store(args.file)
    if args.list:
        for e in store.items:
            print(e[field])
        return

    if not sys.stdin.isatty():             # stdin was the text; the keys come from the terminal
        try:
            tty = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            parser.error("no terminal to read keys from — use --print")
        os.dup2(tty, 0)
        os.close(tty)
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, store, texts, args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
