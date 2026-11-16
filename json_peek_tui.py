#!/usr/bin/env python3
"""JSON peek — explore a JSON document, such as an API response, as a tree you fold open (macOS).

Reads a file, a URL (with -H for headers) or a pipe: `curl -s … | json_peek_tui.py`. Objects
and arrays fold, collapsed ones show a preview of what is inside, and the path of the selection
is shown jq-style. To make a response readable it reads epoch timestamps and ISO dates as local
times ("3 days ago"), decodes JSON that hides inside a string and JWT tokens, shows an array of
objects as a sortable table, and opens long strings in a reader. Numbers keep the exact digits
they were written with. JSON Lines and the )]}' prefix some APIs put first are understood.
"""

import argparse
import base64
import curses
import functools
import json
import locale
import os
import re
import subprocess
import sys
import textwrap
import time
import unicodedata
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime
from decimal import Decimal

MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 10
FETCH_TIMEOUT = 20
MAX_ROWS = 200_000             # expanding stops here, so the tree stays quick to rebuild
MAX_MATCHES = 100_000
TABLE_SAMPLE = 500             # rows measured to size the table's columns
MAX_COL = 32
FLATTEN = 3                    # nested objects become dotted columns this many keys deep
INDENT = 2
# what some APIs put in front of their JSON so a <script> tag can't read it
XSSI_PREFIXES = (")]}'", "while(1);", "for(;;);")
JSONP = re.compile(r"[\w$.]+\s*\((.*)\)\s*;?\s*", re.S)
# a number under one of these keys is read as a Unix time when it is in range
TIME_KEY = re.compile(r"(?i:time|date|stamp|created|updated|modified|expire|since|until)|_at$|[a-z]At$|^(ts|exp|iat|nbf|auth_time)$")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")
JWT = re.compile(r"eyJ[\w-]*\.eyJ[\w-]*\.[\w-]*")
URL = re.compile(r"https?://\S+")
NAME = re.compile(r"[A-Za-z_$][\w$]*")
PATH_TOKEN = re.compile(r'\.?(?P<name>[A-Za-z_$][\w$-]*)|\.?\[(?P<index>-?\d+)\]|\.?\[(?P<quoted>"(?:[^"\\]|\\.)*")\]|\.(?P<num>\d+)')
MISSING = object()

# value -> how to colour it, for the tree, the table and --print alike
STYLES = ("key", "index", "dim", "string", "number", "bool", "null", "decoded", "match")

Row = namedtuple("Row", "path depth key value")


class BadJSON(Exception):
    def __init__(self, msg, text, pos):
        super().__init__(msg)
        self.text, self.pos = text, pos


# -- reading --

def decode_bytes(data):
    return data.decode("utf-8-sig", errors="replace")


def parse(text):
    """(document, what it was). JSON Lines becomes a list. Raises BadJSON."""
    body = text.strip()
    for prefix in XSSI_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):].lstrip()
            break
    try:
        return json.loads(body, parse_float=Decimal), "JSON"
    except json.JSONDecodeError as e:
        first = e
    m = JSONP.fullmatch(body)
    if m and body[:1] not in "{[":
        try:
            return json.loads(m[1], parse_float=Decimal), "JSONP"
        except json.JSONDecodeError:
            pass
    lines = [line for line in body.splitlines() if line.strip()]
    if len(lines) > 1:
        try:
            return [json.loads(line, parse_float=Decimal) for line in lines], "JSON Lines"
        except json.JSONDecodeError:
            pass
    raise BadJSON(f"{first.msg} at line {first.lineno}, column {first.colno}", body, first.pos)


def fetch(url, headers):
    """(body, status line). An HTTP error still has a body worth reading: APIs explain in JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": "json_peek_tui",
                                               "Accept": "application/json, */*;q=0.5", **headers})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            body, status, reason = r.read(), r.status, r.reason
    except urllib.error.HTTPError as e:
        body, status, reason = e.read(), e.code, e.reason
    return body, f"HTTP {status} {reason} · {time.monotonic() - started:.1f} s"


class Source:
    """Where the document comes from, so r can read it again."""

    def __init__(self, arg, headers):
        self.arg, self.headers = arg, headers
        self.kind = "stdin" if arg in (None, "-") else "url" if re.match(r"https?://", arg) else "file"
        self.data = None

    def label(self):
        if self.kind == "stdin":
            return "stdin"
        if self.kind == "url":
            return self.arg
        path = os.path.abspath(self.arg)
        home = os.path.expanduser("~")
        return "~" + path[len(home):] if path.startswith(home + "/") else path

    def read(self):
        """(bytes, status or ""). Raises OSError / URLError."""
        if self.kind == "stdin":
            if self.data is None:
                self.data = sys.stdin.buffer.read()
            return self.data, ""
        if self.kind == "url":
            return fetch(self.arg, self.headers)
        with open(self.arg, "rb") as fh:
            return fh.read(), ""


# -- the document --

def kids(value, sort_keys=False):
    if isinstance(value, dict):
        return sorted(value.items(), key=lambda kv: kv[0].casefold()) if sort_keys else value.items()
    return enumerate(value)


def scalar(v):
    """A scalar as JSON text, numbers with their original digits."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def dump(value, indent=2):
    """Pretty JSON that keeps Decimal numbers as written (json.dumps can't)."""
    out = []

    def emit(v, level):
        if isinstance(v, (dict, list)) and v:
            opening, closing = ("{", "}") if isinstance(v, dict) else ("[", "]")
            out.append(opening + "\n")
            items = list(v.items()) if isinstance(v, dict) else list(enumerate(v))
            for i, (k, x) in enumerate(items):
                out.append(" " * indent * (level + 1))
                if isinstance(v, dict):
                    out.append(json.dumps(k, ensure_ascii=False) + ": ")
                emit(x, level + 1)
                out.append(",\n" if i < len(items) - 1 else "\n")
            out.append(" " * indent * level + closing)
        elif isinstance(v, (dict, list)):
            out.append("{}" if isinstance(v, dict) else "[]")
        else:
            out.append(scalar(v))
    emit(value, 0)
    return "".join(out)


def b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)), parse_float=Decimal)


@functools.lru_cache(maxsize=2048)
def decode_string(s):
    """(value, what) for a JSON object or array written into a string, or a JWT; else (None, None).
    A JWT's signature is shown, not checked."""
    t = s.strip()
    if t[:1] in ("{", "[") and t[-1:] in ("}", "]"):
        try:
            v = json.loads(t, parse_float=Decimal)
            if isinstance(v, (dict, list)):
                return v, "JSON text"
        except ValueError:
            pass
    if JWT.fullmatch(t):
        head, payload, signature = t.split(".")
        try:
            return {"header": b64json(head), "payload": b64json(payload), "signature": signature}, "JWT"
        except ValueError:
            pass
    return None, None


class Doc:
    """The parsed document, plus the strings that were decoded into values, by path."""

    def __init__(self, root):
        self.root = root
        self.decoded = {}                  # path -> (value, "JSON text" | "JWT")

    def at(self, path, value):
        hit = self.decoded.get(path)
        return hit[0] if hit else value

    def get(self, path):
        """The value at `path`; raises LookupError (or TypeError) when it isn't there."""
        v = self.at((), self.root)
        for i, k in enumerate(path):
            v = self.at(path[:i + 1], v[k])
        return v

    def walk(self, start=(), sort_keys=False):
        """(path, key, value) for everything from `start` down, in document order."""
        stack = [(start, start[-1] if start else None, self.get(start))]
        while stack:
            path, key, v = stack.pop()
            yield path, key, v
            if isinstance(v, (dict, list)):
                stack.extend(reversed([(path + (k,), k, self.at(path + (k,), x)) for k, x in kids(v, sort_keys)]))

    def stats(self):
        """(values, deepest level)."""
        count, deepest = 0, 0
        for path, _, _ in self.walk():
            count += 1
            deepest = max(deepest, len(path))
        return count, deepest


def fmt_path(path):
    out = []
    for k in path:
        if isinstance(k, int):
            out.append(f"[{k}]")
        elif NAME.fullmatch(k):
            out.append("." + k)
        else:
            out.append("[" + json.dumps(k, ensure_ascii=False) + "]")
    text = "".join(out)
    return "." + text if text.startswith("[") or not text else text


def parse_path(text, doc):
    """A jq-like path — .data[3].name, data.3.name, .["odd key"] — resolved against the document.
    Raises ValueError saying how far it got."""
    text = text.strip()
    if text in ("", "."):
        return ()
    pos, path = 0, ()
    if not text.startswith((".", "[")):
        text = "." + text
    while pos < len(text):
        m = PATH_TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"can't read the path from “{text[pos:]}”")
        pos = m.end()
        here = doc.get(path)
        if m["index"] is not None or m["num"] is not None:
            n = int(m["index"] if m["index"] is not None else m["num"])
            if isinstance(here, list):
                if not -len(here) <= n < len(here):
                    raise ValueError(f"{fmt_path(path)} has {len(here)} items, no [{n}]")
                path += (n % len(here),)
                continue
            key = str(n)
        else:
            key = m["name"] if m["name"] is not None else json.loads(m["quoted"])
        if not isinstance(here, dict) or key not in here:
            raise ValueError(f"{fmt_path(path)} has no “{key}”")
        path += (key,)
    return path


# -- making values readable --

def fmt_size(n):
    for unit, limit in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


def plural(n, word, many=None):
    return f"{n:,} {word if n == 1 else many or word + 's'}"


def relative(seconds):
    """'3 days ago' / 'in 2 h' for an offset from now."""
    s = abs(seconds)
    for unit, size in (("year", 365.25 * 86400), ("month", 30.44 * 86400), ("day", 86400), ("h", 3600), ("min", 60)):
        if s >= size:
            n = int(s // size)
            text = f"{n} {unit}" + ("s" if n != 1 and unit in ("year", "month", "day") else "")
            break
    else:
        return "just now"
    return f"{text} ago" if seconds < 0 else f"in {text}"


def as_time(key, v):
    """The Unix time a value stands for, or None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, Decimal)) and isinstance(key, str) and TIME_KEY.search(key):
        n = float(v)
        if 1e9 <= n < 4e9:
            return n
        if 1e12 <= n < 4e12:
            return n / 1000                # milliseconds
        return None
    if isinstance(v, str) and ISO_DATE.fullmatch(v):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def hint(key, v, now, full=False):
    """A short note on what a value means; `full` adds the local time for numbers."""
    t = as_time(key, v)
    if t is not None:
        local = time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        return f"{local} · {relative(t - now)}" if isinstance(v, (int, Decimal)) or full else relative(t - now)
    if isinstance(v, str):
        value, what = decode_string(v)
        if what:
            return f"{what} · ⏎ opens it"
        if "\n" in v:
            return f"{v.count(chr(10)) + 1} lines · ⏎ reads"
        if len(v) > 80:
            return f"{len(v):,} chars · ⏎ reads"
    if isinstance(v, list) and len(v) > 1 and all(isinstance(x, dict) for x in v[:50]):
        return "t table"
    return ""


def short(v):
    """A child inside a collapsed preview."""
    if isinstance(v, dict):
        return "{…}" if v else "{}"
    if isinstance(v, list):
        return "[…]" if v else "[]"
    return scalar(v)


def preview(v, room):
    """{id: 1, name: "Ann", …} — what a collapsed container holds, within `room` columns."""
    if room <= 3:
        return ""
    parts, used = [], 2
    items = kids(v)
    for k, x in items:
        text = f"{k}: {short(x)}" if isinstance(v, dict) else short(x)
        if used + cwidth(text) + 2 > room:
            parts.append("…")
            break
        parts.append(text)
        used += cwidth(text) + 2
    opening, closing = ("{", "}") if isinstance(v, dict) else ("[", "]")
    return opening + ", ".join(parts) + closing


def style_of(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (int, Decimal)):
        return "number"
    return "dim"


def row_pieces(row, is_open, decoded, label, needle=""):
    """[(text, style)] for one line of the tree, before the hint."""
    pieces = []
    v, key = row.value, row.key
    container = isinstance(v, (dict, list))
    pieces.append(("▾ " if is_open and container and v else "▸ " if container and v else "  ", "dim"))
    if key is None:
        pieces.append((label, "dim"))
    elif isinstance(key, int):
        pieces.append((f"[{key}]", "index"))
    else:
        k = clean(key)
        pieces.append((k or '""', "match" if needle and needle in key.casefold() else "key"))
    pieces.append((": " if isinstance(key, str) else " ", "dim"))
    if decoded:
        pieces.append((f"⟨{decoded}⟩ ", "decoded"))
    if container:
        n = len(v)
        count = (f"{{{n}}}" if isinstance(v, dict) else f"[{n}]") if n else ("{}" if isinstance(v, dict) else "[]")
        pieces.append((count, "dim"))
        if n and not is_open:
            pieces.append((" ", "dim"))
            pieces.append(("\0preview", "dim"))      # filled in by whoever knows the width
    else:
        text = scalar(v)
        hit = needle and isinstance(v, (str, int, Decimal)) and not isinstance(v, bool) and needle in str(v).casefold()
        pieces.append((text, "match" if hit else style_of(v)))
    return pieces


def clean(text):
    text = unicodedata.normalize("NFC", text)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        text = "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in text)
    return text


def cwidth(text):
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = cwidth(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def layout(pieces, v, width):
    """Clips pieces to `width`, and fills the preview placeholder with what is left."""
    out, used = [], 0
    for text, style in pieces:
        if text == "\0preview":
            text = preview(v, width - used)
        w = cwidth(text)
        if used + w > width:
            if width - used > 0:
                out.append((fit(text, width - used), style))
            used = width
            break
        out.append((text, style))
        used += w
    return out, used


# -- table --

def columns(items):
    """Key paths of the objects in the order first seen, nested objects flattened FLATTEN deep
    (commit.author.name); () is the column for items that aren't objects."""
    cols, seen = [], set()
    if any(not isinstance(x, dict) for x in items):
        cols.append(())

    def add(d, prefix):
        for k, v in d.items():
            p = prefix + (k,)
            if isinstance(v, dict) and v and len(p) < FLATTEN:
                add(v, p)
            elif p not in seen:
                seen.add(p)
                cols.append(p)
    for x in items:
        if isinstance(x, dict):
            add(x, ())
    return cols


def cell(item, col):
    if not col:
        return MISSING if isinstance(item, dict) else item
    v = item
    for k in col:
        if not isinstance(v, dict) or k not in v:
            return MISSING
        v = v[k]
    return v


def col_name(col):
    return ".".join(clean(k) or '""' for k in col) if col else "value"


def cell_text(v, col=()):
    """What a table cell shows; Unix times under a time-like key show as local times."""
    if v is MISSING:
        return ""
    key = col[-1] if col else None
    if isinstance(v, (int, Decimal)) and not isinstance(v, bool) and as_time(key, v) is not None:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(as_time(key, v)))
    if isinstance(v, dict):
        return f"{{{len(v)}}}"
    if isinstance(v, list):
        return f"[{len(v)}]"
    if isinstance(v, str):
        return clean(v)
    return scalar(v)


def sort_key(v):
    if isinstance(v, bool):
        return (1, int(v), "")
    if isinstance(v, (int, Decimal)):
        return (0, v, "")
    if isinstance(v, str):
        return (2, 0, v.casefold())
    if isinstance(v, (dict, list)):
        return (3, len(v), "")
    return (4, 0, "")                      # null and missing last


class Table:
    def __init__(self, path, items):
        self.path, self.items = path, items
        self.cols = columns(items) or [()]
        self.shown = list(range(len(self.cols)))   # indexes into cols; x hides one
        self.order = list(range(len(items)))
        self.row = self.top = 0
        self.col = self.left = 0           # positions in `shown`
        self.sort = None                   # (index into cols, descending)
        self.widths = []
        for c in self.cols:
            head = col_name(c)
            sample = (cwidth(cell_text(cell(x, c), c)) for x in items[:TABLE_SAMPLE])
            self.widths.append(max(3, min(MAX_COL, max([cwidth(head) + 2, *sample]))))   # + room for ↑

    def resort(self):
        if self.sort is None:
            self.order = list(range(len(self.items)))
            return
        c, desc = self.sort
        col = self.cols[c]
        present = [i for i in range(len(self.items)) if cell(self.items[i], col) not in (MISSING, None)]
        absent = [i for i in range(len(self.items)) if cell(self.items[i], col) in (MISSING, None)]
        present.sort(key=lambda i: sort_key(cell(self.items[i], col)), reverse=desc)
        self.order = present + absent      # empty cells stay at the bottom either way


# -- the window --

class App:
    def __init__(self, stdscr, source, args):
        self.stdscr, self.source, self.args = stdscr, source, args
        self.mode = "tree"                 # or "table", "text"
        self.doc = None
        self.bad = None                    # BadJSON when the document didn't parse
        self.error = None
        self.status = ""
        self.size = 0
        self.kind = ""
        self.stats = (0, 0)
        self.expanded = {()}
        self.sort_keys = False
        self.sel, self.top = (), 0
        self.rows, self.index, self.dirty = [], {}, True
        self.truncated = False
        self.needle, self.matches, self.match_i = "", [], 0
        self.table = None
        self.text = None                   # (title, lines, top) in the text view
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.load()
        if self.doc is not None:
            self.expand_to(args.depth)

    # -- loading --

    def load(self):
        self.bad = self.error = None
        try:
            data, self.status = self.source.read()
        except (OSError, urllib.error.URLError) as e:
            self.error = f"can't read {self.source.label()}: {getattr(e, 'reason', None) or e.strerror or e}"
            return
        self.size = len(data)
        try:
            root, self.kind = parse(decode_bytes(data))
        except BadJSON as e:
            self.bad, self.doc = e, None
            return
        except RecursionError:
            self.error = "nested too deep for Python's JSON reader"
            return
        old = self.doc
        self.doc = Doc(root)
        if old:                            # a reload keeps the strings that were opened
            for path, hit in old.decoded.items():
                try:
                    v = self.doc.get(path)
                except (LookupError, TypeError):
                    continue
                if isinstance(v, str) and decode_string(v)[1]:
                    self.doc.decoded[path] = decode_string(v)
        self.stats = self.doc.stats()
        self.dirty = True
        if self.needle:
            self.find(self.needle, keep=True)

    def reload(self):
        if self.source.kind == "stdin":
            self.warn("read from a pipe — there is nothing to read again")
            return
        self.load()
        if self.doc is not None and not self.exists(self.sel):
            self.sel = ()
        self.mode = "tree"
        self.say("read again" + (f" · {self.status}" if self.status else ""))

    def exists(self, path):
        try:
            self.doc.get(path)
            return True
        except (LookupError, TypeError):
            return False

    # -- the tree --

    def build(self):
        if not self.dirty:
            return
        rows, self.truncated = [], False
        stack = [((), 0, None, self.doc.get(()))]
        while stack:
            path, depth, key, v = stack.pop()
            rows.append(Row(path, depth, key, v))
            if len(rows) >= MAX_ROWS:
                self.truncated = True
                break
            if path in self.expanded and isinstance(v, (dict, list)):
                stack.extend(reversed([(path + (k,), depth + 1, k, self.doc.at(path + (k,), x))
                                       for k, x in kids(v, self.sort_keys)]))
        self.rows = rows
        self.index = {r.path: i for i, r in enumerate(rows)}
        self.dirty = False
        if self.sel not in self.index:     # hidden by a collapse: select the nearest open ancestor
            p = self.sel
            while p and p not in self.index:
                p = p[:-1]
            self.sel = p

    def reveal(self, path):
        for i in range(len(path)):
            self.expanded.add(path[:i])
        self.sel = path
        self.dirty = True

    def expand_to(self, depth):
        """Everything above `depth` open, everything else shut."""
        self.expanded = set()
        count = 0
        stack = [((), self.doc.get(()))]
        while stack and count <= MAX_ROWS:
            path, v = stack.pop()
            if len(path) < depth and isinstance(v, (dict, list)):
                self.expanded.add(path)
                count += len(v)
                stack.extend((path + (k,), self.doc.at(path + (k,), x)) for k, x in kids(v))
        self.expanded.add(())
        self.dirty = True

    def expand_all(self, start):
        count = 0
        for path, _, v in self.doc.walk(start, self.sort_keys):
            if isinstance(v, (dict, list)):
                self.expanded.add(path)
                count += len(v)
                if count > MAX_ROWS:
                    self.warn(f"stopped after {MAX_ROWS:,} rows — open the parts you need")
                    break
        self.dirty = True

    def selected(self):
        self.build()
        return self.rows[self.index.get(self.sel, 0)]

    def move(self, delta):
        self.build()
        i = max(0, min(len(self.rows) - 1, self.index.get(self.sel, 0) + delta))
        self.sel = self.rows[i].path

    def open_string(self, row, decode=True):
        """Enter on a string: decode what is inside it, or read it whole."""
        if decode:
            value, what = decode_string(row.value)
            if what:
                self.doc.decoded[row.path] = (value, what)
                self.expanded.add(row.path)
                self.dirty = True
                self.say(f"decoded the {what}" + (" — the signature is not checked" if what == "JWT" else ""))
                return
        self.show_text(fmt_path(row.path), row.value)

    def right(self):
        row = self.selected()
        v = row.value
        if isinstance(v, (dict, list)) and v:
            if row.path in self.expanded:
                self.move(1)
            else:
                self.expanded.add(row.path)
                self.dirty = True
        elif isinstance(v, str) and decode_string(v)[1]:
            self.open_string(row)

    def left(self):
        row = self.selected()
        if row.path in self.doc.decoded and row.path not in self.expanded:
            del self.doc.decoded[row.path]      # back to the string it was
            self.dirty = True
        elif row.path in self.expanded and row.path and isinstance(row.value, (dict, list)) and row.value:
            self.expanded.discard(row.path)
            self.dirty = True
        elif row.path:
            self.sel = row.path[:-1]

    def toggle(self):
        row = self.selected()
        v = row.value
        if isinstance(v, (dict, list)) and v:
            self.expanded ^= {row.path}
            self.dirty = True
        elif isinstance(v, str):
            if decode_string(v)[1] or "\n" in v or len(v) > 80:
                self.open_string(row)
            else:
                self.say(f"{len(v):,} characters — y copies it")

    # -- search and paths --

    def find(self, needle, keep=False):
        self.needle = needle.casefold()
        self.matches = []
        if not self.needle:
            return
        first_after, passed = None, False   # the first match below the selection is where to go
        for path, key, v in self.doc.walk(sort_keys=self.sort_keys):
            text = v if isinstance(v, str) else None if isinstance(v, (dict, list, bool)) or v is None else str(v)
            if (isinstance(key, str) and self.needle in key.casefold()) or (text is not None and self.needle in text.casefold()):
                if passed and first_after is None:
                    first_after = len(self.matches)
                self.matches.append(path)
                if len(self.matches) >= MAX_MATCHES:
                    break
            passed = passed or path == self.sel
        if keep:
            return
        if not self.matches:
            self.warn(f"no “{needle}” anywhere")
            return
        self.match_i = first_after or 0
        self.reveal(self.matches[self.match_i])

    def next_match(self, step):
        if not self.matches:
            self.warn("/ searches keys and values")
            return
        self.match_i = (self.match_i + step) % len(self.matches)
        self.reveal(self.matches[self.match_i])

    def go_to(self):
        text = self.prompt(" go to path (.data[0].id): ", fmt_path(self.sel))
        if text is None:
            return
        try:
            self.reveal(parse_path(text, self.doc))
        except (ValueError, LookupError, TypeError) as e:
            self.warn(str(e))

    # -- table and text --

    def open_table(self):
        row = self.selected()
        path = row.path
        v = row.value
        while not (isinstance(v, list) and v) and path:
            path = path[:-1]               # the array the selection is in
            v = self.doc.get(path)
        if not (isinstance(v, list) and v):
            self.warn("t shows an array as a table — select one, or something inside one")
            return
        if self.table is None or self.table.path != path or self.table.items is not v:
            self.table = Table(path, v)
            under = self.sel[len(path):]
            if under and isinstance(under[0], int):
                self.table.row = self.table.order.index(under[0])
        self.mode = "table"

    def show_text(self, title, text):
        self.text = [title, text, 0]
        self.wrap_cache = (None, [])
        self.mode = "text"

    def wrapped(self, width):
        if self.wrap_cache[0] == width:
            return self.wrap_cache[1]
        out = []
        for line in self.text[1].split("\n"):
            parts = textwrap.wrap(line, width, replace_whitespace=False, drop_whitespace=False,
                                  break_on_hyphens=False) or [""]
            out.extend(parts)
        self.wrap_cache = (width, out)
        return out

    # -- drawing --

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

    def attr(self, style):
        return {"key": curses.A_BOLD, "index": curses.color_pair(1), "dim": curses.color_pair(5),
                "string": curses.color_pair(2), "number": curses.color_pair(1) | curses.A_BOLD,
                "bool": curses.color_pair(3), "null": curses.color_pair(7),
                "decoded": curses.color_pair(7) | curses.A_BOLD,
                "match": curses.color_pair(3) | curses.A_REVERSE}[style]

    def draw(self, footer=True):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        title = " JSON peek "
        self.put(0, 0, title, curses.color_pair(1) | curses.A_BOLD)
        label = self.source.label()
        self.put(0, len(title) + 1, label, curses.A_BOLD, w - len(title) - 2)
        info = [self.status, self.kind, fmt_size(self.size) if self.size else ""]
        if self.doc is not None:
            info.append(f"{plural(self.stats[0], 'value')} · depth {self.stats[1]}")
        line = " · ".join(i for i in info if i)
        bad_status = re.match(r"HTTP [45]", self.status or "")
        self.put(1, 1, line, curses.color_pair(4) if bad_status else curses.color_pair(5))
        if self.error:
            self.put(3, 2, self.error, curses.color_pair(4) | curses.A_BOLD)
        elif self.bad:
            self.draw_bad(3, w)
        elif self.mode == "table":
            self.draw_table(2, h - 4, w)
        elif self.mode == "text":
            self.draw_text(2, h - 3, w)
        else:
            self.draw_tree(2, h - 4, w)
        if footer:
            self.draw_footer(h - 1, w)
        s.refresh()

    def draw_bad(self, y, w):
        e = self.bad
        self.put(y, 2, f"not JSON: {e}", curses.color_pair(4) | curses.A_BOLD)
        start = e.text.rfind("\n", 0, e.pos) + 1
        end = e.text.find("\n", e.pos)
        line = e.text[start:end if end >= 0 else len(e.text)]
        col = e.pos - start
        room = w - 6
        lo = max(0, min(col - room // 2, len(line) - room))   # a window around the spot, for minified JSON
        snippet = line[lo:lo + room].replace("\t", " ")
        self.put(y + 2, 4, clean(snippet), curses.A_BOLD)
        self.put(y + 3, 4 + cwidth(snippet[:col - lo]), "^ here", curses.color_pair(4) | curses.A_BOLD)
        before = e.text[:start].rstrip("\n").split("\n")[-3:] if start else []
        if before:
            self.put(y + 5, 4, "the lines before it:", curses.color_pair(5))
            for i, b in enumerate(before):
                self.put(y + 6 + i, 4, clean(b[:room]), curses.color_pair(5))

    def draw_tree(self, y0, height, w):
        self.build()
        rows = self.rows
        i = self.index.get(self.sel, 0)
        if i < self.top:
            self.top = i
        elif i >= self.top + height:
            self.top = i - height + 1
        self.top = max(0, min(self.top, len(rows) - height))
        now = time.time()
        for n, row in enumerate(rows[self.top:self.top + height]):
            y = y0 + n
            chosen = self.top + n == i
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            indent = min(row.depth * INDENT, w // 3)
            decoded = self.doc.decoded.get(row.path, (None, None))[1]
            pieces = row_pieces(row, row.path in self.expanded, decoded, "root", self.needle)
            note = hint(row.key, row.value, now)
            room = w - 2 - indent
            if note and room > 40:
                room -= min(cwidth(note), room // 3) + 2
            laid, used = layout(pieces, row.value, room)
            x = 1 + indent
            for text, style in laid:
                attr = self.attr(style)
                self.put(y, x, text, (attr & ~curses.A_REVERSE if chosen else attr) | base, cwidth(text))
                x += cwidth(text)
            if note and w - 2 - indent > 40:
                nw = min(cwidth(note), (w - 2 - indent) // 3)
                self.put(y, w - nw - 1, fit(note, nw), base | curses.color_pair(5), nw)
        # the path of the selection, and what it is
        row = rows[i] if rows else None
        if row:
            what = self.describe(row, now)
            p = fmt_path(row.path)
            self.put(y0 + height, 1, p, curses.color_pair(1) | curses.A_BOLD, w - 2)
            self.put(y0 + height, 3 + cwidth(p), what, curses.color_pair(5))

    def describe(self, row, now):
        v = row.value
        if isinstance(v, dict):
            what = plural(len(v), "key")
        elif isinstance(v, list):
            what = plural(len(v), "item")
        elif isinstance(v, str):
            what = plural(len(v), "char")
        else:
            what = style_of(v)
        note = hint(row.key, v, now, full=True)
        decoded = self.doc.decoded.get(row.path)
        extra = []
        if decoded:
            extra.append(f"decoded from a string ({decoded[1]}) — ← puts it back")
        if self.needle and self.matches:
            extra.append(f"match {self.match_i + 1:,} of {len(self.matches):,}")
        if self.truncated:
            extra.append(f"showing the first {MAX_ROWS:,} rows")
        return " · ".join([what] + ([note] if note else []) + extra)

    def draw_table(self, y0, height, w):
        t = self.table
        n = len(t.items)
        t.row = max(0, min(t.row, n - 1))
        t.col = max(0, min(t.col, len(t.shown) - 1))
        idx_w = len(str(n - 1)) + 1
        # columns that fit from `left`, moving left so the cursor column is in view
        if t.col < t.left:
            t.left = t.col
        while True:
            x, last = idx_w + 2, t.left - 1
            for c in range(t.left, len(t.shown)):
                if x + t.widths[t.shown[c]] > w - 1 and c > t.left:
                    break
                x += t.widths[t.shown[c]] + 2
                last = c
            if t.col <= last or t.left >= t.col:
                break
            t.left += 1
        body = height - 2
        if t.row < t.top:
            t.top = t.row
        elif t.row >= t.top + body:
            t.top = t.row - body + 1
        t.top = max(0, min(t.top, n - body))
        x = idx_w + 2
        self.put(y0, 1, "#".rjust(idx_w), curses.color_pair(5))
        for c in range(t.left, last + 1):
            ci = t.shown[c]
            arrow = ""
            if t.sort and t.sort[0] == ci:
                arrow = " ↓" if t.sort[1] else " ↑"
            attr = curses.A_BOLD | (curses.A_REVERSE if c == t.col else 0)
            self.put(y0, x, fit(col_name(t.cols[ci]), t.widths[ci] - len(arrow)) + arrow, attr, t.widths[ci])
            x += t.widths[ci] + 2
        self.put(y0 + 1, 1, "─" * (w - 2), curses.color_pair(5))
        for r in range(t.top, min(n, t.top + body)):
            y = y0 + 2 + r - t.top
            i = t.order[r]
            item = t.items[i]
            chosen = r == t.row
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 1, str(i).rjust(idx_w), base | curses.color_pair(5), idx_w)
            x = idx_w + 2
            for c in range(t.left, last + 1):
                ci = t.shown[c]
                v = cell(item, t.cols[ci])
                style = "dim" if v is MISSING or isinstance(v, (dict, list)) else style_of(v)
                text = cell_text(v, t.cols[ci])
                if isinstance(v, (int, Decimal)) and not isinstance(v, bool) and text == scalar(v):
                    text = text.rjust(t.widths[ci])
                self.put(y, x, text, base | self.attr(style), t.widths[ci])
                x += t.widths[ci] + 2
        info = f"{fmt_path(t.path)} · {plural(n, 'row')} · {plural(len(t.shown), 'column')}"
        if len(t.shown) < len(t.cols):
            info += f" ({len(t.cols) - len(t.shown):,} hidden)"
        if last < len(t.shown) - 1 or t.left:
            info += f" · showing {t.left + 1}–{last + 1}"
        col = t.cols[t.shown[t.col]]
        v = cell(t.items[t.order[t.row]], col)
        note = hint(col[-1] if col else None, v, time.time(), full=True) if v is not MISSING else ""
        self.put(y0 + height, 1, info + (f" · {note}" if note else ""), curses.color_pair(1))

    def draw_text(self, y0, height, w):
        title, _, top = self.text
        lines = self.wrapped(w - 4)
        top = max(0, min(top, len(lines) - height))
        self.text[2] = top
        self.put(y0, 1, title, curses.color_pair(1) | curses.A_BOLD)
        for n, line in enumerate(lines[top:top + height - 1]):
            self.put(y0 + 1 + n, 2, clean(line))
        if len(lines) > height - 1:
            self.put(y0, w - 20, f"{top + 1}–{min(len(lines), top + height - 1)} of {len(lines):,}".rjust(18),
                     curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        if self.mode == "table":
            keys = ["↑↓ rows", "←→ columns", "s sort", "⏎ in tree", "x hide column", "X all columns",
                    "y copy row", "esc tree"]
        elif self.mode == "text":
            keys = ["↑↓ scroll", "y copy", "esc back"]
        elif self.bad or self.error:
            keys = ["r read again"]
        else:
            keys = ["←→ fold", "⏎ open", "/ find", "n next", "t table", ": go to", "c copy path",
                    "y copy value", "v as JSON", "0-9 depth", "e expand all", "s sort keys", "o open URL", "r reload"]
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > w - 2:
            keys.pop()
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))

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

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            n = text.count("\n") + 1
            self.say(f"copied {what}" + (f" ({plural(n, 'line')})" if n > 1 else f": {clean(text)[:60]}"))
        except (OSError, subprocess.CalledProcessError):
            self.warn("couldn't copy — pbcopy not found")

    def handle(self, ch):
        if ch in (ord("q"), ord("Q")):
            return False
        if ch == ord("r"):
            self.reload()
            return True
        if self.doc is None:
            return True
        h = self.stdscr.getmaxyx()[0]
        page = max(1, h - 6)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        back = (27, curses.KEY_BACKSPACE, 127, 8)
        if self.mode == "text":
            if ch in steps:
                self.text[2] = max(0, self.text[2] + steps[ch])
            elif ch in back or ch in (curses.KEY_LEFT, ord("h")):
                self.mode = "tree"
            elif ch == ord("y"):
                self.copy(self.text[1], "the text")
            return True
        if self.mode == "table":
            self.handle_table(ch, steps, back)
            return True
        self.handle_tree(ch, steps)
        return True

    def handle_table(self, ch, steps, back):
        t = self.table
        if ch in steps:
            t.row = max(0, min(len(t.items) - 1, t.row + steps[ch]))
        elif ch in (curses.KEY_RIGHT, ord("l")):
            t.col = min(len(t.shown) - 1, t.col + 1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            t.col = max(0, t.col - 1)
        elif ch == ord("s"):
            i, ci = t.order[t.row], t.shown[t.col]
            if t.sort is None or t.sort[0] != ci:
                t.sort = (ci, False)
            elif not t.sort[1]:
                t.sort = (ci, True)
            else:
                t.sort = None
            t.resort()
            t.row = t.order.index(i)       # stay on the same item
        elif ch == ord("x"):
            if len(t.shown) == 1:
                self.warn("that is the last column")
            else:
                self.say(f"hid {col_name(t.cols[t.shown.pop(t.col)])} — X shows every column")
        elif ch == ord("X"):
            t.shown = list(range(len(t.cols)))
        elif ch in (curses.KEY_ENTER, 10, 13):
            i = t.order[t.row]
            col = t.cols[t.shown[t.col]]
            path = t.path + (i,)
            if cell(t.items[i], col) is not MISSING:
                path += col
            self.reveal(path)
            self.mode = "tree"
        elif ch == ord("y"):
            self.copy(dump(t.items[t.order[t.row]]), f"{fmt_path(t.path + (t.order[t.row],))} as JSON")
        elif ch in back or ch == ord("t"):
            self.mode = "tree"

    def handle_tree(self, ch, steps):
        if ch in steps:
            self.move(steps[ch])
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.right()
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.left()
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self.toggle()
        elif ch == ord("/"):
            text = self.prompt(" find in keys and values: ", self.needle)
            if text is not None:
                self.find(text)
        elif ch == ord("n"):
            self.next_match(1)
        elif ch == ord("N"):
            self.next_match(-1)
        elif ch == 27 and self.needle:
            self.needle, self.matches = "", []
        elif ch == ord(":"):
            self.go_to()
        elif ch == ord("t"):
            self.open_table()
        elif ch == ord("c"):
            self.copy(fmt_path(self.sel), "the path")
        elif ch == ord("y"):
            v = self.selected().value
            self.copy(v if isinstance(v, str) else dump(v), "the value")
        elif ch == ord("v"):
            row = self.selected()
            self.show_text(fmt_path(row.path) + " as JSON", dump(row.value))
        elif ord("0") <= ch <= ord("9"):
            self.expand_to(ch - ord("0") or 1)
            self.say(f"open {ch - ord('0') or 1} level{'s' if ch > ord('1') else ''} deep")
        elif ch == ord("e"):
            self.expand_all(self.sel)
        elif ch == ord("s"):
            self.sort_keys = not self.sort_keys
            self.dirty = True
            self.say("keys sorted A–Z" if self.sort_keys else "keys in document order")
        elif ch == ord("o"):
            v = self.selected().value
            m = URL.search(v) if isinstance(v, str) else None
            if m:
                subprocess.run(["open", m[0]], capture_output=True)
                self.say(f"opened {m[0]}")
            else:
                self.warn("o opens a URL — this value has none")


def run(stdscr, source, args):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, numbers, indexes, path
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # strings
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # booleans, messages, matches
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)  # null, decoded strings
    stdscr.keypad(True)
    app = App(stdscr, source, args)
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            return


ANSI = {"key": "\033[1m", "index": "\033[36m", "dim": "\033[2m", "string": "\033[32m", "number": "\033[1;36m",
        "bool": "\033[33m", "null": "\033[35m", "decoded": "\033[1;35m", "match": "\033[7;33m"}


def print_outline(doc, start, depth, label, width):
    """The tree as text, open `depth` levels below `start`; pipeable."""
    color = sys.stdout.isatty()
    now = time.time()
    stack = [(start, start[-1] if start else None, doc.get(start))]
    while stack:
        path, key, v = stack.pop()
        row = Row(path, len(path) - len(start), key, v)
        is_open = row.depth < depth
        if is_open and isinstance(v, (dict, list)):
            stack.extend(reversed([(path + (k,), k, x) for k, x in kids(v)]))
        indent = row.depth * INDENT
        note = hint(key, v, now)
        room = width - indent - (cwidth(note) + 2 if note else 0)
        laid, used = layout(row_pieces(row, is_open, None, label), v, max(10, room))
        text = "".join((ANSI[s] + t + "\033[0m") if color else t for t, s in laid)
        if note:
            text += " " * max(2, room - used + 2) + ((ANSI["dim"] + note + "\033[0m") if color else note)
        print(" " * indent + text)


def main():
    parser = argparse.ArgumentParser(
        description="Explore JSON — a file, a URL or a pipe — as a tree you fold open, with a table "
                    "for arrays of objects and dates, embedded JSON and JWTs made readable.",
        epilog="examples:  curl -s https://api.github.com/repos/python/cpython | %(prog)s\n"
               "           %(prog)s https://api.example.com/v1/items -H 'Authorization: Bearer …'\n"
               "           %(prog)s response.json --path .data[0] --print",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", help="a file, an http(s) URL, or - for stdin (the default when piped)")
    parser.add_argument("-H", "--header", action="append", default=[], metavar="'NAME: VALUE'",
                        help="a request header for a URL; may be repeated")
    parser.add_argument("-d", "--depth", type=int, default=2, metavar="N",
                        help="levels open at the start, and in --print (default 2)")
    parser.add_argument("--path", metavar="PATH", help="print the value at PATH (.data[0].name) as JSON")
    parser.add_argument("--print", action="store_true", help="print the tree as text instead of opening the window")
    args = parser.parse_args()
    if args.source is None and sys.stdin.isatty():
        parser.error("give a file or a URL, or pipe JSON in")
    headers = {}
    for h in args.header:
        name, sep, value = h.partition(":")
        if not sep or not name.strip():
            parser.error(f"a header looks like 'Name: value', not {h!r}")
        headers[name.strip()] = value.strip()
    source = Source(args.source, headers)
    if source.kind == "file" and not os.path.isfile(args.source):
        parser.error(f"no such file: {args.source}")

    if args.print or args.path:
        try:
            data, status = source.read()
            root, kind = parse(decode_bytes(data))
        except BadJSON as e:
            print(f"not JSON: {e}", file=sys.stderr)
            raise SystemExit(1)
        except (OSError, urllib.error.URLError) as e:
            print(f"can't read {source.label()}: {getattr(e, 'reason', None) or e}", file=sys.stderr)
            raise SystemExit(1)
        if status:
            print(status, file=sys.stderr)
        doc = Doc(root)
        try:
            start = parse_path(args.path, doc) if args.path else ()
        except (ValueError, LookupError, TypeError) as e:
            print(f"--path: {e}", file=sys.stderr)
            raise SystemExit(1)
        if args.print:
            width = os.get_terminal_size().columns if sys.stdout.isatty() else 120
            print_outline(doc, start, args.depth, fmt_path(start) if start else source.label(), width)
        else:
            print(dump(doc.get(start)))
        return

    if source.kind == "stdin":
        source.read()                      # all of it, before the keyboard takes stdin's place
        try:
            tty = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            parser.error("no terminal to read keys from — use --print or --path")
        os.dup2(tty, 0)
        os.close(tty)
    if not sys.stdout.isatty():
        parser.error("the output is not a terminal — use --print or --path")
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, source, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
