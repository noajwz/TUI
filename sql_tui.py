#!/usr/bin/env python3
"""SQL scratchpad — write SQL against a SQLite database and see the answers as a table (macOS).

Three panes: the schema (tables and views, their columns, indexes and row counts), an editor with
highlighting and completion of table and column names, and the results as a grid you can sort,
copy from and export. F5 or Ctrl-R runs the statement the cursor is in, F6 runs them all; a query
runs in the background and Ctrl-C stops it.

A database file opens read-only unless --write is given, so a scratchpad can't damage it by
accident. CSV files given on the command line are loaded as tables (temporary ones when a real
database is open). With no file at all it is an empty database in memory. The editor's text is
kept per database, and every query run goes into a history (Ctrl-P).
"""

import argparse
import csv
import curses
import hashlib
import io
import json
import locale
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

STATE_DIR = os.path.expanduser("~/.local/state/sql_tui")
MAX_ROWS = 10_000              # a result set is cut off here
MAX_COL = 40                   # widest a results column is drawn
SAMPLE_ROWS = 500              # rows measured to size the columns
HISTORY_MAX = 500
TICK_MS = 100
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 70, 16
SCHEMA_W = 28
PASTE_MIN = 4                  # characters arriving at once that are a paste, not typing
UNDO_MAX = 300
TAB = "    "
DB_EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".db3")

KEYWORDS = frozenset("""
ABORT ACTION ADD AFTER ALL ALTER ALWAYS ANALYZE AND AS ASC ATTACH AUTOINCREMENT BEFORE BEGIN BETWEEN BY
CASCADE CASE CAST CHECK COLLATE COLUMN COMMIT CONFLICT CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP DATABASE DEFAULT DEFERRED DELETE DESC DETACH DISTINCT DO DROP EACH ELSE END
ESCAPE EXCEPT EXCLUDE EXCLUSIVE EXISTS EXPLAIN FAIL FILTER FIRST FOLLOWING FOR FOREIGN FROM FULL GENERATED
GLOB GROUP GROUPS HAVING IF IGNORE IMMEDIATE IN INDEX INDEXED INITIALLY INNER INSERT INSTEAD INTERSECT INTO
IS ISNULL JOIN KEY LAST LEFT LIKE LIMIT MATCH MATERIALIZED NATURAL NO NOT NOTHING NOTNULL NULL NULLS OF
OFFSET ON OR ORDER OTHERS OUTER OVER PARTITION PLAN PRAGMA PRECEDING PRIMARY QUERY RAISE RANGE RECURSIVE
REFERENCES REGEXP REINDEX RELEASE RENAME REPLACE RESTRICT RETURNING RIGHT ROLLBACK ROW ROWS SAVEPOINT SELECT
SET TABLE TEMP TEMPORARY THEN TIES TO TRANSACTION TRIGGER UNBOUNDED UNION UNIQUE UPDATE USING VACUUM VALUES
VIEW VIRTUAL WHEN WHERE WINDOW WITH WITHOUT
INTEGER TEXT REAL BLOB NUMERIC
""".split())
FUNCTIONS = ("abs", "avg", "char", "coalesce", "count", "date", "datetime", "group_concat", "hex", "ifnull",
             "iif", "instr", "json", "json_extract", "julianday", "length", "lower", "ltrim", "max", "min",
             "nullif", "printf", "random", "replace", "round", "rtrim", "strftime", "substr", "sum", "time",
             "total", "trim", "typeof", "unixepoch", "upper")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
NUMBER = re.compile(r"(?:0[xX][0-9A-Fa-f]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
IDENT_CHARS = re.compile(r"[A-Za-z0-9_$.]*$")

INITIAL = """\
-- SQL scratchpad. F5 or Ctrl-R runs the statement the cursor is in; F6 runs them all.
-- Shift-Tab moves between the schema, this editor and the results; Tab completes names.
-- Lines starting with a dot are commands: .tables  .schema [name]  .import file.csv [table]
--   .export file.csv (the results)  .help
SELECT sqlite_version() AS version, datetime('now', 'localtime') AS now;
"""

HELP = """\
Anywhere      F5  Ctrl-R   run the statement the cursor is in
              F6           run every statement in the editor
              Ctrl-C       stop a query that is running
              Shift-Tab    next pane (schema → editor → results); F2 F3 F4 go straight there
              Ctrl-P       history of queries you've run
              Ctrl-Q  F10  quit (it asks about an open transaction first)

Editor        typing, arrows, Home/End or Ctrl-A/Ctrl-E, PgUp/PgDn
              Tab          complete a table, column, function or keyword (4 spaces otherwise)
              Ctrl-K/U     delete to the end / start of the line   Ctrl-D  delete a character
              Ctrl-Z/Y     undo / redo        Esc  to the results
              The text is kept for this database for next time.

Results       arrows or hjkl, PgUp/PgDn, g/G first/last row, 0/$ first/last column
              Enter        the row as a record (every column, values in full)
              s            sort by this column (again to reverse, again for the query's order)
              c            copy the cell      y  copy the row      Y  copy every row (tab-separated)
              e            export the results to a CSV file
              i  Esc       back to the editor

Schema        ↑↓, → or Enter opens a table (columns, indexes, row count), ← closes it
              Enter on an open table runs SELECT * on it
              s            the CREATE statement     i  insert the name at the cursor   c  copy it
              r            read the schema again

Commands      .tables  .schema [name]  .import FILE [TABLE]  .export FILE  .help
              (each on a line of its own, outside any statement)

Safety        A database file opens read-only unless sql_tui was started with --write.
              With --write each statement commits on its own, unless you BEGIN a transaction."""


# -- text helpers --

def cwidth(text):
    w = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def fit(text, width, right=False):
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
    pad = " " * (width - cwidth(text))
    return pad + text if right else text + pad


def clean(text):
    text = unicodedata.normalize("NFC", text)
    return "".join("↵" if c == "\n" else " " if c == "\t" else "?" if ord(c) < 32 or ord(c) == 127 else c
                   for c in text)


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def fmt_ms(seconds):
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def show(v):
    """A value as the grid shows it."""
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<{plural(len(v), 'byte')}>"
    if isinstance(v, float):
        return repr(v)
    return clean(str(v))


def common_prefix(words):
    first, last = min(words), max(words)
    n = 0
    while n < len(first) and first[n] == last[n]:
        n += 1
    return first[:n]


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def copy(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# -- SQL text --

def tokenize(line, state):
    """([(start, end, style)], state at the end of the line). The state carries a /* comment or
    a quoted string over into the next line: None, "block", "'" or '"'."""
    out, i, n = [], 0, len(line)
    while i < n:
        if state == "block":
            j = line.find("*/", i)
            if j < 0:
                out.append((i, n, "comment"))
                return out, "block"
            out.append((i, j + 2, "comment"))
            i, state = j + 2, None
            continue
        if state in ("'", '"'):
            j = i
            while j < n:
                if line[j] == state:
                    if j + 1 < n and line[j + 1] == state:
                        j += 2
                        continue
                    break
                j += 1
            style = "string" if state == "'" else "ident"
            if j >= n:
                out.append((i, n, style))
                return out, state
            out.append((i, j + 1, style))
            i, state = j + 1, None
            continue
        ch = line[i]
        if line.startswith("--", i):
            out.append((i, n, "comment"))
            return out, None
        if line.startswith("/*", i):
            state = "block"
            out.append((i, i + 2, "comment"))
            i += 2
            continue
        if ch in "'\"":
            out.append((i, i + 1, "string" if ch == "'" else "ident"))
            state, i = ch, i + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and line[i + 1].isdigit()):
            m = NUMBER.match(line, i)
            if m:
                out.append((i, m.end(), "number"))
                i = m.end()
                continue
        if ch.isalpha() or ch == "_":
            m = WORD.match(line, i)
            word = m.group()
            if word.upper() in KEYWORDS:
                out.append((i, m.end(), "keyword"))
            elif m.end() < n and line[m.end()] == "(":
                out.append((i, m.end(), "function"))
            i = m.end()
            continue
        if ch == "?" or ch == ":" and i + 1 < n and line[i + 1].isalpha():
            out.append((i, i + 1, "param"))
        i += 1
    return out, state


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def split_units(text):
    """The statements and dot commands in `text`: [(kind, text, start, end)] with offsets into it.
    A dot command is a line starting with '.' where no statement is under way."""
    units, pos, pending = [], 0, 0          # pending: where the statement being collected began
    for line in text.split("\n"):
        end = pos + len(line)
        if line.lstrip().startswith(".") and not strip_comments(text[pending:pos]).strip():
            units.append(("command", line.strip(), pos, end))
            pending = end + 1
        else:
            j = pos
            while True:
                k = text.find(";", j, end)
                if k < 0:
                    break
                chunk = text[pending:k + 1]
                if sqlite3.complete_statement(chunk):
                    units.append(("sql", chunk, pending, k + 1))
                    pending = k + 1
                j = k + 1
        pos = end + 1
    rest = text[pending:]
    if strip_comments(rest).strip():
        units.append(("sql", rest, pending, len(text)))
    return [(kind, t, s, e) for kind, t, s, e in units if kind == "command" or strip_comments(t).strip(" \n\t;")]


# -- the database --

class Db:
    def __init__(self, path, write):
        self.path, self.memory = path, path is None
        self.readonly = not write and not self.memory
        if self.memory:
            self.conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        elif self.readonly:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        else:
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.objects = []                   # [(schema, type, name, table, sql)]
        self.columns = {}                   # name -> [(name, type, notnull, default, pk)]
        self.counts = {}
        self.load()

    @property
    def label(self):
        return "scratch database — gone when you quit" if self.memory else short(os.path.abspath(self.path))

    def load(self):
        objs, cols = [], {}
        for schema, master in (("main", "sqlite_master"), ("temp", "sqlite_temp_master")):
            try:
                rows = self.conn.execute(f"SELECT type, name, tbl_name, sql FROM {master} "
                                         "WHERE name NOT LIKE 'sqlite_%' ORDER BY name COLLATE NOCASE").fetchall()
            except sqlite3.Error:
                continue
            for kind, name, table, sql in rows:
                objs.append((schema, kind, name, table, sql or ""))
                if kind in ("table", "view"):
                    try:
                        info = self.conn.execute(f"PRAGMA {schema}.table_info({quote_ident(name)})").fetchall()
                    except sqlite3.Error:
                        info = []
                    cols[name] = [(c[1], c[2], c[3], c[4], c[5]) for c in info]
        self.objects, self.columns = objs, cols
        self.counts = {}

    def tables(self):
        return [o for o in self.objects if o[1] in ("table", "view")]

    def count(self, schema, name):
        key = (schema, name)
        if key not in self.counts:
            try:
                self.counts[key] = self.conn.execute(
                    f"SELECT count(*) FROM {schema}.{quote_ident(name)}").fetchone()[0]
            except sqlite3.Error:
                self.counts[key] = None
        return self.counts[key]


def unique_name(db, base):
    taken = {o[2].lower() for o in db.objects}
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def import_csv(db, path, table=None):
    """Loads a CSV or TSV file as a table, with the column types guessed. Returns (name, rows)."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.lower().endswith(".tsv") else csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if r]
    if not rows:
        raise ValueError("the file is empty")
    header, data = rows[0], rows[1:]
    width = max(len(r) for r in rows)
    names, seen = [], set()
    for i in range(width):
        n = (header[i].strip() if i < len(header) else "") or f"col{i + 1}"
        base, k = n, 2
        while n.lower() in seen:
            n, k = f"{base}_{k}", k + 1
        seen.add(n.lower())
        names.append(n)
    comma = dialect.delimiter == ";"         # European exports write 1,5 for one and a half
    types = []
    for i in range(width):
        vals = [r[i].strip() for r in data if i < len(r) and r[i].strip()]
        if vals and all(re.fullmatch(r"[+-]?\d+", v) for v in vals):
            types.append("INTEGER")
        elif vals and all(re.fullmatch(r"[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?", v)
                          and ("," not in v or comma) for v in vals):
            types.append("REAL")
        else:
            types.append("TEXT")

    def convert(v, t):
        v = v.strip()
        if v == "":
            return None
        if t == "INTEGER":
            return int(v)
        if t == "REAL":
            return float(v.replace(",", "."))
        return v
    base = table or re.sub(r"\W+", "_", Path(path).stem).strip("_") or "data"
    if base[0].isdigit():
        base = "t_" + base
    name = table or unique_name(db, base)
    temp = "" if db.memory else "TEMP "      # a read-only database still takes temporary tables
    cols = ", ".join(f"{quote_ident(n)} {t}" for n, t in zip(names, types))
    db.conn.execute(f"CREATE {temp}TABLE {quote_ident(name)} ({cols})")
    marks = ", ".join("?" * width)
    db.conn.execute("BEGIN")
    db.conn.executemany(f"INSERT INTO {quote_ident(name)} VALUES ({marks})",
                        ([convert(r[i], types[i]) if i < len(r) else None for i in range(width)] for r in data))
    db.conn.execute("COMMIT")
    db.load()
    return name, len(data)


class Result:
    def __init__(self):
        self.cols, self.rows = [], []
        self.more = False                   # cut off at MAX_ROWS
        self.changed = 0
        self.statements = 0
        self.error = None
        self.failed = None                  # the unit that failed
        self.seconds = 0.0
        self.interrupted = False
        self.note = ""


class Runner:
    """Runs statements on a thread of its own, so the window stays alive and Ctrl-C can stop it."""

    def __init__(self, db):
        self.db = db
        self.thread = None
        self.result = None
        self.started = 0.0

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, units):
        self.result = None
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.work, args=(units,), daemon=True)
        self.thread.start()

    def work(self, units):
        res = Result()
        conn = self.db.conn
        try:
            for unit in units:
                res.failed = unit
                cur = conn.execute(unit[1])
                res.statements += 1
                if cur.description:
                    res.cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(MAX_ROWS + 1)
                    res.more = len(rows) > MAX_ROWS
                    res.rows = rows[:MAX_ROWS]
                elif cur.rowcount > 0:
                    res.changed += cur.rowcount
            res.failed = None
        except sqlite3.Error as e:
            res.error = str(e)
            res.interrupted = "interrupt" in res.error
        res.seconds = time.monotonic() - self.started
        self.result = res

    def stop(self):
        if self.busy:
            self.db.conn.interrupt()


# -- the editor --

class Editor:
    def __init__(self, text):
        self.lines = text.split("\n") or [""]
        self.r = self.c = 0
        self.top = self.left = 0
        self.undo, self.redo = [], []
        self.last, self.last_at = None, 0.0

    def text(self):
        return "\n".join(self.lines)

    def offset(self):
        return sum(len(line) + 1 for line in self.lines[:self.r]) + self.c

    def goto(self, off):
        for r, line in enumerate(self.lines):
            if off <= len(line):
                self.r, self.c = r, off
                return
            off -= len(line) + 1
        self.r, self.c = len(self.lines) - 1, len(self.lines[-1])

    def snapshot(self, kind):
        """Keeps a copy for undo, one per run of the same kind of edit."""
        now = time.monotonic()
        if kind != self.last or now - self.last_at > 1.5 or kind == "other":
            self.undo.append((list(self.lines), self.r, self.c))
            del self.undo[:-UNDO_MAX]
            self.redo.clear()
        self.last, self.last_at = kind, now

    def undo_step(self, back=True):
        src, dst = (self.undo, self.redo) if back else (self.redo, self.undo)
        if not src:
            return False
        dst.append((list(self.lines), self.r, self.c))
        self.lines, self.r, self.c = src.pop()
        self.last = None
        return True

    def insert(self, s, kind="type"):
        self.snapshot(kind)
        line = self.lines[self.r]
        parts = s.split("\n")
        if len(parts) == 1:
            self.lines[self.r] = line[:self.c] + s + line[self.c:]
            self.c += len(s)
            return
        head, tail = line[:self.c], line[self.c:]
        new = [head + parts[0]] + parts[1:-1] + [parts[-1] + tail]
        self.lines[self.r:self.r + 1] = new
        self.r += len(parts) - 1
        self.c = len(parts[-1])

    def newline(self):
        indent = re.match(r"[ \t]*", self.lines[self.r]).group()
        self.insert("\n" + indent[:self.c], "other")

    def backspace(self):
        if self.c > 0:
            self.snapshot("delete")
            line = self.lines[self.r]
            n = 4 if line[:self.c].endswith(TAB) and not line[:self.c].strip() else 1
            self.lines[self.r] = line[:self.c - n] + line[self.c:]
            self.c -= n
        elif self.r > 0:
            self.snapshot("other")
            prev = self.lines[self.r - 1]
            self.lines[self.r - 1] = prev + self.lines.pop(self.r)
            self.r -= 1
            self.c = len(prev)

    def delete(self):
        line = self.lines[self.r]
        if self.c < len(line):
            self.snapshot("delete")
            self.lines[self.r] = line[:self.c] + line[self.c + 1:]
        elif self.r + 1 < len(self.lines):
            self.snapshot("other")
            self.lines[self.r] = line + self.lines.pop(self.r + 1)

    def kill(self, to_end):
        self.snapshot("other")
        line = self.lines[self.r]
        if to_end:
            if self.c == len(line) and self.r + 1 < len(self.lines):
                self.lines[self.r] = line + self.lines.pop(self.r + 1)
            else:
                self.lines[self.r] = line[:self.c]
        else:
            self.lines[self.r] = line[self.c:]
            self.c = 0

    def move(self, dr=0, dc=0):
        if dc:
            if dc < 0 and self.c == 0 and self.r > 0:
                self.r -= 1
                self.c = len(self.lines[self.r])
            elif dc > 0 and self.c == len(self.lines[self.r]) and self.r + 1 < len(self.lines):
                self.r += 1
                self.c = 0
            else:
                self.c = max(0, min(len(self.lines[self.r]), self.c + dc))
        if dr:
            self.r = max(0, min(len(self.lines) - 1, self.r + dr))
            self.c = min(self.c, len(self.lines[self.r]))
        self.last = None

    def word_before(self):
        m = IDENT_CHARS.search(self.lines[self.r][:self.c])
        return m.group() if m else ""

    def append_statement(self, sql):
        """Puts `sql` on lines of its own at the end and the cursor on it."""
        self.snapshot("other")
        while len(self.lines) > 1 and not self.lines[-1].strip():
            self.lines.pop()
        if self.lines[-1].strip():
            self.lines.append("")
        start = len(self.lines)
        self.lines += sql.rstrip().split("\n")
        self.r, self.c = start, 0


# -- the window --

STYLES = {"keyword": (4, curses.A_BOLD), "string": (2, 0), "number": (6, 0), "comment": (8, 0),
          "function": (5, 0), "ident": (3, 0), "param": (3, curses.A_BOLD)}


class App:
    def __init__(self, stdscr, db, args, startup):
        self.s, self.db, self.args = stdscr, db, args
        self.focus = "editor"
        self.buffer_path = buffer_path(db)
        text = read_text(self.buffer_path)
        self.ed = Editor(text if text is not None else INITIAL)
        self.runner = Runner(db)
        self.result = None
        self.order = None                   # (column, descending) the results are sorted by
        self.view = []                      # the rows in display order
        self.rr = self.rc = self.rtop = self.rleft = 0
        self.widths = []
        self.sel = 0                        # schema row
        self.stop_ = 0
        self.open = set()                   # tables expanded in the schema
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.status = ("", 0)
        self.hints = ""                     # completion candidates
        self.history = read_history()
        self.pending_run = None
        for kind, sql in startup:
            if kind == "note":
                self.say(sql)
            else:
                self.ed.append_statement(sql)
                self.pending_run = [("sql", sql, 0, 0)]

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def warn(self, text):
        self.say(text, curses.color_pair(1))

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.s.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h or x < 0:
            return
        if y == h - 1 and x + width >= w:
            width = w - x - 1
        try:
            self.s.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    # -- layout --

    def layout(self):
        h, w = self.s.getmaxyx()
        sw = min(SCHEMA_W, max(18, w // 4))
        ex = sw + 1
        ew = w - ex
        eh = max(4, min(len(self.ed.lines) + 1, (h - 4) * 2 // 5))
        return h, w, sw, ex, ew, eh

    def draw(self):
        s = self.s
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(1))
            s.refresh()
            return
        h, w, sw, ex, ew, eh = self.layout()
        db = self.db
        mode = "in memory" if db.memory else "read-only" if db.readonly else "read-write"
        tags = [mode]
        if not db.memory and db.conn.in_transaction and not self.runner.busy:
            tags.append("IN TRANSACTION")
        tag = " · ".join(tags) + (f" · SQLite {sqlite3.sqlite_version}" if w >= 100 else "")
        self.put(0, 0, " SQL ", curses.color_pair(6) | curses.A_REVERSE | curses.A_BOLD)
        room = w - len(tag) - 9
        label = db.label if cwidth(db.label) <= room else "…" + db.label[-(room - 1):]
        self.put(0, 6, label, curses.A_BOLD, room)
        self.put(0, w - len(tag) - 1, tag, curses.color_pair(1 if "TRANSACTION" in tag else 8))
        self.draw_schema(1, 0, sw, h - 2)
        for y in range(1, h - 1):
            self.put(y, sw, "│", curses.color_pair(8), 1)
        self.draw_editor(1, ex, ew, eh)
        self.draw_status(1 + eh, ex, ew)
        self.draw_results(2 + eh, ex, ew, h - 3 - eh)
        self.draw_footer(h - 1, w)
        if self.focus == "editor" and not self.runner.busy:
            r, c = self.cursor_screen(1, ex, ew, eh)
            curses.curs_set(1)
            s.move(r, c)
        else:
            curses.curs_set(0)
        s.refresh()

    def title(self, y, x, width, text, on):
        attr = (curses.color_pair(6) | curses.A_BOLD) if on else curses.color_pair(8)
        self.put(y, x, f" {text} ", attr | (curses.A_REVERSE if on else 0), cwidth(text) + 2)

    # -- schema pane --

    def schema_rows(self):
        """[(depth, label, attr, object or None, the name it stands for)] for the schema tree."""
        db = self.db
        rows = []
        tables = db.tables()
        if not tables:
            rows.append((0, "(no tables yet)", curses.color_pair(8), None, None))
        for obj in tables:
            schema, kind, name, _, _ = obj
            mark = "▾" if name in self.open else "▸"
            tag = " view" if kind == "view" else " temp" if schema == "temp" else ""
            attr = curses.A_BOLD if kind == "table" else curses.color_pair(5)
            rows.append((0, f"{mark} {clean(name)}{tag}", attr, obj, name))
            if name in self.open:
                n = db.count(schema, name)
                if n is not None:
                    rows.append((1, plural(n, "row"), curses.color_pair(8), obj, name))
                for cname, ctype, notnull, default, pk in db.columns.get(name, []):
                    label = f"{clean(cname)} {ctype.lower()}{' key' if pk else ''}{' not null' if notnull else ''}"
                    rows.append((1, label, curses.color_pair(3 if pk else 0), obj, cname))
                for o in db.objects:
                    if o[3] == name and o[1] in ("index", "trigger"):
                        cols = re.search(r"\((.*)\)", o[4] or "")
                        detail = f" ({cols.group(1)})" if o[1] == "index" and cols else ""
                        rows.append((1, f"{o[1]} {clean(o[2])}{detail}", curses.color_pair(8), o, o[2]))
        return rows

    def draw_schema(self, y0, x0, width, height):
        self.title(y0, x0, width, "schema", self.focus == "schema")
        rows = self.schema_rows()
        self.sel = max(0, min(self.sel, len(rows) - 1))
        body = height - 1
        if self.sel < self.stop_:
            self.stop_ = self.sel
        elif self.sel >= self.stop_ + body:
            self.stop_ = self.sel - body + 1
        for n, (depth, label, attr, _, _) in enumerate(rows[self.stop_:self.stop_ + body]):
            chosen = self.stop_ + n == self.sel and self.focus == "schema"
            text = ("  " * depth) + label
            self.put(y0 + 1 + n, x0, " " + text, attr | (curses.A_REVERSE if chosen else 0), width)

    # -- editor pane --

    def draw_editor(self, y0, x0, width, height):
        ed = self.ed
        self.title(y0, x0, width, "editor", self.focus == "editor")
        body = height - 1
        if ed.r < ed.top:
            ed.top = ed.r
        elif ed.r >= ed.top + body:
            ed.top = ed.r - body + 1
        gutter = len(str(len(ed.lines))) + 1
        tw = width - gutter - 1
        if ed.c < ed.left:
            ed.left = max(0, ed.c - 8)
        elif ed.c >= ed.left + tw:
            ed.left = ed.c - tw + 8
        current = self.current_unit()
        span = (current[2], current[3]) if current else (-1, -1)
        state, off = None, 0
        for r, line in enumerate(ed.lines):
            tokens, next_state = tokenize(line, state)
            if ed.top <= r < ed.top + body:
                y = y0 + 1 + r - ed.top
                inside = span[0] <= off + len(line) and off <= span[1] and self.focus == "editor"
                self.put(y, x0, f"{r + 1:>{gutter - 1}} ", curses.color_pair(6 if inside else 8), gutter)
                self.draw_code(y, x0 + gutter, tw, line, tokens)
            off += len(line) + 1
            state = next_state

    def draw_code(self, y, x, width, line, tokens):
        left = self.ed.left
        styles = [0] * len(line)
        for a, b, style in tokens:
            pair, extra = STYLES[style]
            attr = curses.color_pair(pair) | extra
            for i in range(a, min(b, len(line))):
                styles[i] = attr
        if line.lstrip().startswith("."):
            styles = [curses.color_pair(5) | curses.A_BOLD] * len(line)
        seg, attr0, sx, used = [], None, x, 0
        for i in range(left, len(line)):
            ch = line[i]
            w = cwidth(ch) or 1
            if used + w > width:
                break
            if styles[i] != attr0 and seg:
                self.put(y, sx, "".join(seg), attr0, cwidth("".join(seg)))
                sx += cwidth("".join(seg))
                seg = []
            attr0 = styles[i]
            seg.append(" " if ch == "\t" else ch)
            used += w
        if seg:
            self.put(y, sx, "".join(seg), attr0, cwidth("".join(seg)))

    def cursor_screen(self, y0, x0, width, height):
        ed = self.ed
        gutter = len(str(len(ed.lines))) + 1
        line = ed.lines[ed.r]
        return y0 + 1 + ed.r - ed.top, min(x0 + width - 1, x0 + gutter + cwidth(line[ed.left:ed.c]))

    def current_unit(self):
        """The statement the cursor is in — or, between statements, the one just before it."""
        units = split_units(self.ed.text())
        off = self.ed.offset()
        before = None
        for u in units:
            if u[2] <= off <= u[3]:
                return u
            if u[3] < off:
                before = u
        return before or (units[0] if units else None)

    # -- status and results --

    def draw_status(self, y, x, width):
        self.put(y, x, "─" * width, curses.color_pair(8))
        if self.runner.busy:
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 10]
            text = f" {spin} running… {fmt_ms(time.monotonic() - self.runner.started)} — Ctrl-C stops it "
            self.put(y, x + 1, text, curses.color_pair(3) | curses.A_BOLD, cwidth(text))
            return
        text, attr = self.status
        if self.hints:
            text, attr = " " + self.hints + " ", curses.color_pair(6)
        if text:
            self.put(y, x + 1, text, attr, min(width - 2, cwidth(text)))

    def set_result(self, res):
        self.result = res
        self.order = None
        self.rr = self.rc = self.rtop = self.rleft = 0
        self.view = list(res.rows)
        self.widths = []
        for i, col in enumerate(res.cols):
            vals = (cwidth(show(r[i])) for r in res.rows[:SAMPLE_ROWS])
            self.widths.append(max(3, min(MAX_COL, max([cwidth(clean(col)) + 2, *vals]))))   # + room for ↑
        if res.error:
            where = ""
            if res.failed and res.statements:
                where = f" (statement {res.statements + 1})"
            stop = "stopped" if res.interrupted else "✗ " + res.error
            if "readonly" in res.error:
                stop += " — start sql_tui with --write to change it"
            self.status = (f" {stop}{where} ", curses.color_pair(1) | curses.A_BOLD)
            if res.failed and res.failed[0] == "sql" and not res.interrupted:
                self.ed.goto(res.failed[2] + len(res.failed[1]) - len(res.failed[1].lstrip()))
        elif res.cols:
            more = f" — the first {MAX_ROWS:,}" if res.more else ""
            self.status = (f" ✓ {plural(len(res.rows), 'row')}{more} · {fmt_ms(res.seconds)} ",
                           curses.color_pair(2) | curses.A_BOLD)
        else:
            what = res.note or (plural(res.changed, "row") + " changed" if res.changed else
                                "done" if res.statements == 1 else f"{res.statements} statements done")
            self.status = (f" ✓ {what} · {fmt_ms(res.seconds)} ", curses.color_pair(2) | curses.A_BOLD)

    def visible_cols(self, width):
        """The columns that fit from rleft on, moving rleft so the cursor's column is shown."""
        res = self.result
        nw = len(str(len(self.view))) + 1
        if self.rc < self.rleft:
            self.rleft = self.rc
        while True:
            x, cols = nw + 1, []
            for c in range(self.rleft, len(res.cols)):
                if x + self.widths[c] > width and cols:
                    break
                cols.append(c)
                x += self.widths[c] + 2
            if self.rc in cols or self.rleft >= self.rc:
                return nw, cols
            self.rleft += 1

    def draw_results(self, y0, x0, width, height):
        res = self.result
        self.title(y0, x0, width, "results", self.focus == "results")
        if res is None or not res.cols:
            if res is None:
                self.put(y0 + 2, x0 + 2, "Results show here. F5 runs the statement your cursor is in.",
                         curses.color_pair(8))
            return
        n = len(self.view)
        body = height - 2
        self.rr = max(0, min(self.rr, n - 1))
        self.rc = max(0, min(self.rc, len(res.cols) - 1))
        if self.rr < self.rtop:
            self.rtop = self.rr
        elif self.rr >= self.rtop + body:
            self.rtop = self.rr - body + 1
        nw, cols = self.visible_cols(width - 1)
        on = self.focus == "results"
        x = x0 + nw + 1
        for c in cols:
            name = clean(res.cols[c])
            if self.order and self.order[0] == c:
                name = ("↓ " if self.order[1] else "↑ ") + name
            attr = curses.A_BOLD | curses.A_UNDERLINE | (curses.A_REVERSE if on and c == self.rc else 0)
            self.put(y0 + 1, x, name, attr, self.widths[c])
            x += self.widths[c] + 2
        for i in range(self.rtop, min(n, self.rtop + body)):
            y = y0 + 2 + i - self.rtop
            row = self.view[i]
            self.put(y, x0, f"{i + 1:>{nw}}", curses.color_pair(8), nw)
            x = x0 + nw + 1
            for c in cols:
                v = row[c]
                num = isinstance(v, (int, float)) and not isinstance(v, bool)
                attr = curses.color_pair(8) if v is None else curses.color_pair(6) if num else \
                    curses.color_pair(5) if isinstance(v, bytes) else 0
                if on and i == self.rr:
                    attr |= curses.A_REVERSE if c == self.rc else curses.A_BOLD
                self.put(y, x, fit(show(v), self.widths[c], right=num), attr, self.widths[c])
                x += self.widths[c] + 2
        more = []
        if n > body:
            more.append(f"rows {self.rtop + 1}–{min(n, self.rtop + body)} of {n:,}")
        if cols and (cols[0] > 0 or cols[-1] < len(res.cols) - 1):
            more.append(f"columns {cols[0] + 1}–{cols[-1] + 1} of {len(res.cols)}")
        if more:
            text = " " + " · ".join(more) + " "
            self.put(y0, x0 + width - cwidth(text) - 1, text, curses.color_pair(8))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = {"editor": ["F5 run", "F6 run all", "Tab complete", "⇧Tab pane", "^P history", "^Z undo",
                           "Esc results", "^Q quit"],
                "results": ["⏎ record", "s sort", "c copy cell", "y row", "Y all", "e export", "⇧Tab pane",
                            "? help", "q quit"],
                "schema": ["⏎ open/query", "s CREATE", "i insert name", "c copy", "r reload", "⇧Tab pane",
                           "? help", "q quit"]}[self.focus]
        while len(keys) > 1 and cwidth("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(y, 0, " " + "  ".join(keys), curses.color_pair(8))

    # -- overlays --

    def pager(self, title, lines):
        """Text in a box over the window; arrows scroll, anything else closes it."""
        top = 0
        while True:
            self.draw()
            curses.curs_set(0)
            h, w = self.s.getmaxyx()
            bw, bh = min(w - 4, max(40, max((cwidth(t) for t, _ in lines), default=0) + 4)), min(h - 2, len(lines) + 3)
            y0, x0 = (h - bh) // 2, (w - bw) // 2
            body = bh - 3
            top = max(0, min(top, len(lines) - body))
            for y in range(y0, y0 + bh):
                self.put(y, x0, " " * bw, curses.A_REVERSE)
            self.put(y0, x0 + 1, f" {title} ", curses.A_REVERSE | curses.A_BOLD)
            for n, (text, attr) in enumerate(lines[top:top + body]):
                self.put(y0 + 1 + n, x0 + 2, text, attr | curses.A_REVERSE, bw - 4)
            tail = "↑↓ scroll · any other key closes" if len(lines) > body else "any key closes"
            self.put(y0 + bh - 1, x0 + 2, tail, curses.A_REVERSE | curses.color_pair(8), bw - 4)
            self.s.refresh()
            ch = self.s.getch()
            if ch == -1:                        # the tick, not a key
                continue
            if ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch == curses.KEY_PPAGE:
                top -= body
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                top += body
            elif ch != curses.KEY_RESIZE:
                return

    def menu(self, title, items):
        """A list to choose from; returns the index or None."""
        sel, top = 0, 0
        while True:
            self.draw()
            curses.curs_set(0)
            h, w = self.s.getmaxyx()
            bw, bh = min(w - 4, 100), min(h - 2, len(items) + 3)
            y0, x0 = (h - bh) // 2, (w - bw) // 2
            body = bh - 3
            if sel < top:
                top = sel
            elif sel >= top + body:
                top = sel - body + 1
            for y in range(y0, y0 + bh):
                self.put(y, x0, " " * bw, curses.A_REVERSE)
            self.put(y0, x0 + 1, f" {title} ", curses.A_REVERSE | curses.A_BOLD)
            for n, text in enumerate(items[top:top + body]):
                attr = curses.A_BOLD if top + n == sel else curses.A_REVERSE
                self.put(y0 + 1 + n, x0 + 2, text, attr, bw - 4)
            self.put(y0 + bh - 1, x0 + 2, "↑↓ choose · Enter takes it · Esc", curses.A_REVERSE | curses.color_pair(8),
                     bw - 4)
            self.s.refresh()
            ch = self.s.getch()
            if ch in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = min(len(items) - 1, sel + 1)
            elif ch == curses.KEY_PPAGE:
                sel = max(0, sel - body)
            elif ch == curses.KEY_NPAGE:
                sel = min(len(items) - 1, sel + body)
            elif ch in (10, 13, curses.KEY_ENTER):
                return sel
            elif ch in (27, ord("q"), 3):
                return None

    def prompt(self, label, initial=""):
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                self.draw()
                h, w = self.s.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(6) | curses.A_REVERSE)
                curses.curs_set(1)
                self.s.move(h - 1, min(cwidth(text), w - 2))
                self.s.refresh()
                try:
                    key = self.s.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf).strip()
                    if key in ("\x1b", "\x03"):
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

    # -- running --

    def run(self, everything=False):
        if self.runner.busy:
            self.say("a query is still running — Ctrl-C stops it")
            return
        units = split_units(self.ed.text())
        if not everything:
            u = self.current_unit()
            units = [u] if u else []
        if not units:
            self.warn("nothing to run — write a statement first")
            return
        save_text(self.buffer_path, self.ed.text())
        self.start(units)

    def start(self, units):
        self.hints = ""
        sql_units = []
        for u in units:
            if u[0] == "command":
                if sql_units:                   # what came before it runs first, in order
                    self.wait_for(sql_units)
                    sql_units = []
                    if self.result and self.result.error:
                        return
                self.command(u[1])
            else:
                sql_units.append(u)
                remember(self.history, u[1].strip())
        if sql_units:
            self.runner.start(sql_units)
            save_history(self.history)

    def wait_for(self, units):
        self.runner.start(units)
        self.runner.thread.join()
        self.finish()

    def finish(self):
        res = self.runner.result
        self.runner.thread = None
        if res is None:
            return
        self.set_result(res)
        self.db.load()

    def stop(self):
        if self.runner.busy:
            self.runner.stop()
            self.say("stopping…")

    def command(self, line):
        """The dot commands, as the sqlite3 shell has them."""
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        db = self.db
        res = Result()
        if cmd == ".tables":
            res.cols = ["name", "type", "rows"]
            res.rows = [(o[2], o[1] + (" (temp)" if o[0] == "temp" else ""), db.count(o[0], o[2]))
                        for o in db.tables()]
            res.note = plural(len(res.rows), "table")
        elif cmd == ".schema":
            objs = [o for o in db.objects if not args or o[2].lower() == args[0].lower() or o[3].lower() == args[0].lower()]
            if args and not objs:
                res.error = f"no table or view called {args[0]}"
            res.cols = ["sql"]
            res.rows = [(o[4] + ";",) for o in objs if o[4]]
        elif cmd == ".import":
            if not args:
                res.error = "usage: .import FILE [TABLE]"
            else:
                path = os.path.expanduser(args[0])
                try:
                    name, n = import_csv(db, path, args[1] if len(args) > 1 else None)
                    res.note = f"loaded {plural(n, 'row')} into {name}"
                    self.open.add(name)
                except (OSError, ValueError, sqlite3.Error) as e:
                    res.error = f"can't import {args[0]}: {e}"
        elif cmd == ".export":
            if not args:
                res.error = "usage: .export FILE (writes the results shown now)"
            else:
                self.export(os.path.expanduser(args[0]))
                return
        elif cmd == ".help":
            self.help()
            return
        else:
            res.error = f"unknown command {cmd} — there are .tables .schema .import .export .help"
        res.statements = 1
        self.set_result(res)

    # -- results actions --

    def sort(self):
        res = self.result
        if not res or not res.cols:
            return
        c = self.rc
        if self.order is None or self.order[0] != c:
            self.order = (c, False)
        elif not self.order[1]:
            self.order = (c, True)
        else:
            self.order = None
        if self.order is None:
            self.view = list(res.rows)
            self.say("in the query's order")
            return

        def key(row):
            v = row[c]
            if v is None:
                return (1, 0, "")
            if isinstance(v, (int, float)):
                return (0, 0, v)
            if isinstance(v, bytes):
                return (0, 2, v)
            return (0, 1, str(v).casefold())
        nulls = [r for r in res.rows if r[c] is None]
        rest = sorted((r for r in res.rows if r[c] is not None), key=key, reverse=self.order[1])
        self.view = rest + nulls             # NULLs at the bottom either way
        self.say(f"sorted by {res.cols[c]}{' descending' if self.order[1] else ''}")

    def tsv(self, rows, header=False):
        def cell(v):
            return "" if v is None else str(v).replace("\t", " ").replace("\n", " ") if not isinstance(v, bytes) else v.hex()
        out = ["\t".join(self.result.cols)] if header else []
        out += ["\t".join(cell(v) for v in r) for r in rows]
        return "\n".join(out)

    def export(self, path=None):
        res = self.result
        if not res or not res.cols:
            self.warn("no results to export — run a query first")
            return
        if path is None:
            path = self.prompt(" export the results to: ", "results.csv")
            if not path:
                return
            path = os.path.expanduser(path)
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(res.cols)
                for r in self.view:
                    w.writerow(["" if v is None else v.hex() if isinstance(v, bytes) else v for v in r])
            self.say(f"wrote {plural(len(self.view), 'row')} to {short(os.path.abspath(path))}", curses.color_pair(2))
        except OSError as e:
            self.warn(f"can't write {path}: {e.strerror or e}")

    def record(self):
        res = self.result
        if not res or not self.view:
            return
        row = self.view[self.rr]
        nw = max(cwidth(clean(c)) for c in res.cols)
        lines = []
        h, w = self.s.getmaxyx()
        room = max(20, min(w - 8, 110) - nw - 3)
        for c, v in zip(res.cols, row):
            text = "NULL" if v is None else f"<{len(v)} bytes> {v[:32].hex()}" if isinstance(v, bytes) else str(v)
            attr = curses.color_pair(8) if v is None else 0
            first = True
            for para in text.split("\n"):
                while True:
                    chunk, para = para[:room], para[room:]
                    lines.append((f"{fit(clean(c), nw) if first else ' ' * nw}   {clean(chunk)}", attr))
                    first = False
                    if not para:
                        break
        self.pager(f"row {self.rr + 1} of {len(self.view):,}", lines)

    # -- schema actions --

    def schema_object(self):
        rows = self.schema_rows()
        return rows[self.sel][3] if rows else None

    def schema_enter(self):
        rows = self.schema_rows()
        if not rows or rows[self.sel][3] is None:
            return
        depth, _, _, obj, _ = rows[self.sel]
        name = obj[2] if obj[1] in ("table", "view") else obj[3]
        if depth == 0 and name not in self.open:
            self.open.add(name)
            return
        sql = f"SELECT * FROM {obj[0] + '.' if obj[0] == 'temp' else ''}{quote_ident(name)} LIMIT 1000;"
        self.ed.append_statement(sql)
        self.start([("sql", sql, 0, 0)])

    def help(self):
        self.pager("SQL scratchpad — keys", [(t, 0) for t in HELP.split("\n")])

    def show_history(self):
        if not self.history:
            self.say("no history yet — every query you run goes into it")
            return
        items = [clean(q)[:200] for q in reversed(self.history)]
        i = self.menu("history — Enter puts it at the end of the editor", items)
        if i is not None:
            self.ed.append_statement(list(reversed(self.history))[i])
            self.focus = "editor"

    # -- completion --

    def complete(self):
        ed = self.ed
        word = ed.word_before()
        if not word:
            ed.insert(TAB, "other")
            self.hints = ""
            return
        db = self.db
        if "." in word:
            table, _, prefix = word.rpartition(".")
            names = [c[0] for t, cols in db.columns.items() if t.lower() == table.strip('"').lower() for c in cols]
        else:
            prefix = word
            names = ([o[2] for o in db.tables()] + sorted({c[0] for cols in db.columns.values() for c in cols})
                     + [f + "(" for f in FUNCTIONS] + sorted(KEYWORDS))
        cands = []
        for n in names:
            if n.lower().startswith(prefix.lower()) and n not in cands:
                cands.append(n)
        if not cands:
            self.hints = f"nothing starts with “{prefix}”"
            return
        upper = prefix.isupper()
        cands = [c.upper() if c.upper() in KEYWORDS and upper else c.lower() if c.upper() in KEYWORDS else c
                 for c in cands]
        common = common_prefix([c.lower() for c in cands])
        if len(cands) == 1:
            done = cands[0]
        else:
            done = next((c for c in cands if c.lower().startswith(common)), prefix)[:len(common)]
        if len(done) > len(prefix):
            ed.insert(done[len(prefix):])
        self.hints = "" if len(cands) == 1 else "  ".join(cands[:12]) + ("  …" if len(cands) > 12 else "")

    # -- keys --

    def feed(self, keys):
        """Everything that arrived at once. In the editor, many characters together are a paste,
        which goes in as typed — without the auto-indent a typed Enter adds."""
        chars = [k for k in keys if isinstance(k, str)]
        if self.focus == "editor" and len(chars) >= PASTE_MIN and len(chars) == len(keys) \
                and not any(k in ("\x12", "\x11", "\x10", "\x03", "\x1b") for k in chars):
            text = "".join(chars).replace("\r\n", "\n").replace("\r", "\n").replace("\t", TAB)
            self.ed.insert(text, "other")
            return True
        for k in keys:
            if not self.handle(k):
                return False
        return True

    def handle(self, key):
        code = ord(key) if isinstance(key, str) and len(key) == 1 else key
        if code in (17, curses.KEY_F10):        # Ctrl-Q
            return not self.confirm_quit()
        if code == 3:                           # Ctrl-C
            if self.runner.busy:
                self.stop()
            else:
                self.say("Ctrl-Q quits")
            return True
        if code in (18, curses.KEY_F5):         # Ctrl-R
            self.run()
            return True
        if code == curses.KEY_F6:
            self.run(everything=True)
            return True
        if code == 16:                          # Ctrl-P
            self.show_history()
            return True
        if code == curses.KEY_BTAB:
            order = ["schema", "editor", "results"]
            self.focus = order[(order.index(self.focus) + 1) % 3]
            return True
        if code in (curses.KEY_F2, curses.KEY_F3, curses.KEY_F4):
            self.focus = {curses.KEY_F2: "schema", curses.KEY_F3: "editor", curses.KEY_F4: "results"}[code]
            return True
        if code == curses.KEY_RESIZE:
            return True
        if self.focus == "editor":
            self.edit(key, code)
            return True
        if code in (ord("q"), ord("Q")):
            return not self.confirm_quit()
        if code == ord("?"):
            self.help()
        elif code in (ord("i"), 27) and self.focus == "results":
            self.focus = "editor"
        elif self.focus == "results":
            self.results_key(code)
        else:
            self.schema_key(code)
        return True

    def edit(self, key, code):
        ed = self.ed
        h = self.s.getmaxyx()[0]
        if code != 9:
            self.hints = ""
        if isinstance(key, str) and key.isprintable() and len(key) == 1:
            ed.insert(key)
        elif code in (10, 13, curses.KEY_ENTER):
            ed.newline()
        elif code in (curses.KEY_BACKSPACE, 127, 8):
            ed.backspace()
        elif code in (curses.KEY_DC, 4):
            ed.delete()
        elif code == 9:
            self.complete()
        elif code == curses.KEY_LEFT:
            ed.move(dc=-1)
        elif code == curses.KEY_RIGHT:
            ed.move(dc=1)
        elif code == curses.KEY_UP:
            ed.move(dr=-1)
        elif code == curses.KEY_DOWN:
            ed.move(dr=1)
        elif code in (curses.KEY_HOME, 1):
            ed.c = 0 if ed.c else len(ed.lines[ed.r]) - len(ed.lines[ed.r].lstrip())
        elif code in (curses.KEY_END, 5):
            ed.c = len(ed.lines[ed.r])
        elif code == curses.KEY_PPAGE:
            ed.move(dr=-(h // 3))
        elif code == curses.KEY_NPAGE:
            ed.move(dr=h // 3)
        elif code == 11:                        # Ctrl-K
            ed.kill(True)
        elif code == 21:                        # Ctrl-U
            ed.kill(False)
        elif code == 26:                        # Ctrl-Z
            if not ed.undo_step():
                self.say("nothing to undo")
        elif code == 25:                        # Ctrl-Y
            if not ed.undo_step(back=False):
                self.say("nothing to redo")
        elif code == 27:
            self.focus = "results"

    def results_key(self, code):
        res = self.result
        if not res or not res.cols:
            if code in (10, 13):
                self.focus = "editor"
            return
        h = self.s.getmaxyx()[0]
        page = max(1, h // 2)
        moves = {curses.KEY_UP: (-1, 0), ord("k"): (-1, 0), curses.KEY_DOWN: (1, 0), ord("j"): (1, 0),
                 curses.KEY_LEFT: (0, -1), ord("h"): (0, -1), curses.KEY_RIGHT: (0, 1), ord("l"): (0, 1),
                 curses.KEY_PPAGE: (-page, 0), curses.KEY_NPAGE: (page, 0), ord("g"): (-10 ** 9, 0),
                 ord("G"): (10 ** 9, 0), curses.KEY_HOME: (0, -10 ** 9), ord("0"): (0, -10 ** 9),
                 curses.KEY_END: (0, 10 ** 9), ord("$"): (0, 10 ** 9)}
        if code in moves:
            dr, dc = moves[code]
            self.rr = max(0, min(len(self.view) - 1, self.rr + dr))
            self.rc = max(0, min(len(res.cols) - 1, self.rc + dc))
        elif code in (10, 13, curses.KEY_ENTER):
            self.record()
        elif code == ord("s"):
            self.sort()
        elif code == ord("c") and self.view:
            v = self.view[self.rr][self.rc]
            text = "" if v is None else v.hex() if isinstance(v, bytes) else str(v)
            self.say(f"copied {res.cols[self.rc]}: {clean(text)[:60]}" if copy(text) else "couldn't copy — pbcopy not found")
        elif code == ord("y") and self.view:
            self.say("copied the row" if copy(self.tsv([self.view[self.rr]])) else "couldn't copy")
        elif code == ord("Y"):
            ok = copy(self.tsv(self.view, header=True))
            self.say(f"copied {plural(len(self.view), 'row')} with the header, tab-separated" if ok else "couldn't copy")
        elif code == ord("e"):
            self.export()

    def schema_key(self, code):
        rows = self.schema_rows()
        if code in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif code in (curses.KEY_DOWN, ord("j")):
            self.sel = min(len(rows) - 1, self.sel + 1)
        elif code in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER):
            self.schema_enter()
        elif code in (curses.KEY_LEFT, ord("h")):
            obj = self.schema_object()
            if obj:
                name = obj[2] if obj[1] in ("table", "view") else obj[3]
                self.open.discard(name)
                self.sel = next(i for i, r in enumerate(self.schema_rows()) if r[3] and r[0] == 0 and r[3][2] == name)
        elif code == ord("s"):
            obj = self.schema_object()
            if obj and obj[4]:
                self.pager(f"{obj[1]} {obj[2]}", [(clean(t), 0) for t in obj[4].split("\n")])
        elif code in (ord("i"), ord("c")):
            name = rows[self.sel][4] if rows else None
            if not name:
                return
            text = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else quote_ident(name)
            if code == ord("i"):
                self.ed.insert(text, "other")
                self.focus = "editor"
            else:
                self.say(f"copied {text}" if copy(text) else "couldn't copy")
        elif code == ord("r"):
            self.db.load()
            self.say("schema read again")

    def confirm_quit(self):
        """True when it's fine to quit now."""
        save_text(self.buffer_path, self.ed.text())
        if self.runner.busy:
            self.runner.stop()
            self.runner.thread.join(2)
        db = self.db
        if not db.memory and not db.readonly and db.conn.in_transaction:
            while True:
                self.draw()
                h, w = self.s.getmaxyx()
                self.put(h - 1, 0, " A transaction is open: c commit it, r roll it back, Esc stay ",
                         curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD)
                self.s.refresh()
                ch = self.s.getch()
                if ch in (ord("c"), ord("r")):
                    try:
                        db.conn.execute("COMMIT" if ch == ord("c") else "ROLLBACK")
                    except sqlite3.Error as e:
                        self.warn(str(e))
                        return False
                    return True
                if ch in (27, 3):
                    return False
        return True


# -- what is kept between runs --

def buffer_path(db):
    if db.memory:
        name = "memory"
    else:
        full = os.path.abspath(db.path)
        name = re.sub(r"\W+", "_", Path(full).stem)[:40] + "-" + hashlib.sha1(full.encode()).hexdigest()[:8]
    return os.path.join(STATE_DIR, "buffers", name + ".sql")


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def save_text(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def read_history():
    try:
        with open(os.path.join(STATE_DIR, "history.json"), encoding="utf-8") as f:
            data = json.load(f)
        return [q for q in data if isinstance(q, str)][-HISTORY_MAX:]
    except (OSError, ValueError):
        return []


def remember(history, sql):
    if sql in history:
        history.remove(sql)
    history.append(sql)
    del history[:-HISTORY_MAX]


def save_history(history):
    save_text(os.path.join(STATE_DIR, "history.json"), json.dumps(history, ensure_ascii=False, indent=0))


# -- start --

def read_keys(stdscr):
    """The key that woke us and everything that arrived with it."""
    try:
        keys = [stdscr.get_wch()]
    except curses.error:
        return []
    stdscr.nodelay(True)
    try:
        while len(keys) < 100_000:
            try:
                keys.append(stdscr.get_wch())
            except curses.error:
                break
    finally:
        stdscr.nodelay(False)
        stdscr.timeout(TICK_MS)
    return keys


def run(stdscr, db, args, startup):
    curses.raw()                            # Ctrl-C, Ctrl-Q and Ctrl-S come to us as keys
    curses.use_default_colors()
    for n, colour in enumerate((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                curses.COLOR_MAGENTA, curses.COLOR_CYAN), 1):
        curses.init_pair(n, colour, -1)
    curses.init_pair(8, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, db, args, startup)
    if app.pending_run:
        app.start(app.pending_run)
    while True:
        if app.runner.thread is not None and not app.runner.busy:
            app.finish()
        app.draw()
        keys = read_keys(stdscr)
        if keys and not app.feed(keys):
            return


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description="A SQL scratchpad for SQLite: the schema, an editor and the results side by side.",
        epilog="Database files open read-only unless --write. CSV and TSV files are loaded as tables; with no "
               "database the tables live in memory.")
    parser.add_argument("files", nargs="*", help="a SQLite database, and/or CSV or TSV files to load as tables")
    parser.add_argument("-w", "--write", action="store_true",
                        help="open the database read-write (and create it if it doesn't exist)")
    args = parser.parse_args()
    dbs = [f for f in args.files if is_sqlite(f) or f.lower().endswith(DB_EXTENSIONS)]
    csvs = [f for f in args.files if f not in dbs]
    if len(dbs) > 1:
        parser.error("give one database at a time")
    path = dbs[0] if dbs else None
    if path and not os.path.exists(path) and not args.write:
        parser.error(f"{path} doesn't exist — add --write to create it")
    for f in csvs:
        if not os.path.isfile(f):
            parser.error(f"no such file: {f}")
    try:
        db = Db(path, args.write)
        db.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as e:
        parser.error(f"can't open {path}: {e}")
    startup = []
    for f in csvs:
        try:
            name, n = import_csv(db, f)
            startup.append(("note", f"loaded {f} into {name}: {plural(n, 'row')}"))
            startup.append(("sql", f"SELECT * FROM {quote_ident(name)} LIMIT 1000;"))
        except (OSError, ValueError, sqlite3.Error) as e:
            parser.error(f"can't load {f}: {e}")
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, db, args, startup)
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
