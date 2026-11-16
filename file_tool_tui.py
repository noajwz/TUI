#!/usr/bin/env python3
"""File tool — find duplicate files under a folder and rename a folder's files in bulk (macOS).

Duplicates: files are grouped by size, then by a hash of their first 64 kB, then by a hash of the
whole file, so only files that could be the same are read in full. Hard links are one file, not
duplicates; files that live only in iCloud are skipped rather than downloaded. Extra copies are
marked by hand or by a rule and moved to the Trash, and one copy of every set always stays.

Rename: find and replace (plain or regex), a name template with a counter, the date and the folder
name, case and extension rules — all previewed old → new with conflicts flagged before anything
is touched. Renames go through temporary names, so swaps and chains work, a failure puts
everything back, and u undoes the last batch.
"""

import argparse
import curses
import errno
import fnmatch
import hashlib
import locale
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor

TICK_MS = 250
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 60, 12
MIN_SIZE = 1                   # bytes; every empty file is "the same" as every other
PARTIAL_BYTES = 64 * 1024      # the first pass hashes this much of each candidate
CHUNK = 1 << 20
HASH_THREADS = 4
TRASH = "/usr/bin/trash"
SF_DATALESS = 0x40000000       # st_flags: the contents are in iCloud, opening it downloads them
# skipped unless --all, along with everything whose name starts with a dot
IGNORED = {"__pycache__", "node_modules"}
# app data rather than your files; skipped unless --all, but can be scanned by naming it
SKIPPED_PATHS = {os.path.expanduser("~/Library")}
# packages whose insides belong to an app: trashing a file in there breaks the library
BUNDLES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".aplibrary", ".imovielibrary",
           ".fcpbundle", ".logicx", ".band", ".framework", ".bundle", ".pkg", ".xcodeproj")

KEEP_RULES = ("oldest", "newest", "shortest path")
CASES = ("as is", "lower", "upper", "title")
EXT_RULES = ("keep", "lower", "upper")       # or ".xyz", set with E
SORTS = ("name", "date", "size")
FIELDS = ("name", "n", "date", "folder")
TEMPLATE_RE = re.compile(r"\{([^{}]*)\}")

Rule = namedtuple("Rule", "find replace regex nocase template case ext start")
DEFAULT_RULE = Rule("", "", False, False, "{name}", "as is", "keep", 1)
# raw is the name as the disk has it (NFD on APFS), name the NFC form the rules work on
Item = namedtuple("Item", "raw name mtime size")
# new is None when the name stays as it is
Change = namedtuple("Change", "item new problem")


class Stopped(Exception):
    pass


# -- duplicates --

class File:
    __slots__ = ("path", "size", "mtime", "links", "marked")

    def __init__(self, path, size, mtime):
        self.path, self.size, self.mtime = path, size, mtime
        self.links = []                    # other paths of the same inode
        self.marked = False


class Group:
    """Files with the same contents."""
    __slots__ = ("size", "files")

    def __init__(self, files):
        self.size, self.files = files[0].size, files

    def extra(self):
        return self.size * (len(self.files) - 1)


class Scan:
    """Walks the tree and finds the duplicates on a thread of its own. Groups are published as
    they are confirmed, biggest files first; the UI reads `groups` under `lock`."""

    def __init__(self, root, min_size=MIN_SIZE, everything=False):
        self.root, self.min_size, self.everything = root, min_size, everything
        self.lock = threading.Lock()
        self.phase = "listing"             # then "comparing" (first 64 kB), "hashing", "done"
        self.error = None
        self.stop = False
        self.files = self.bytes = 0
        self.cloud = self.unreadable = self.bundles = self.volumes = 0
        self.todo_files = self.done_files = self.todo_bytes = self.done_bytes = 0
        self.groups = []
        self.started, self.took = time.monotonic(), None

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.work()
        except Stopped:
            return
        except OSError as e:
            self.error = f"can't read the folder: {e.strerror or e}"
        self.took = time.monotonic() - self.started
        self.phase = "done"

    def work(self):
        sets = [fs for fs in self.walk().values() if len(fs) > 1]
        sets.sort(key=lambda fs: -fs[0].size)
        pool = ThreadPoolExecutor(HASH_THREADS)
        try:
            self.begin("comparing", [f for fs in sets for f in fs], PARTIAL_BYTES)
            heads = self.hash_all(pool, [f for fs in sets for f in fs], PARTIAL_BYTES)
            alike = [same for fs in sets for same in split(fs, heads)]
            self.publish([s for s in alike if s[0].size <= PARTIAL_BYTES])   # read whole already
            rest = [s for s in alike if s[0].size > PARTIAL_BYTES]
            self.begin("hashing", [f for s in rest for f in s], None)
            for same in rest:              # biggest first: the sets that matter most show first
                self.publish(split(same, self.hash_all(pool, same, None)))
        finally:
            # a stopped scan drops the files still queued, or quitting would wait for them all
            pool.shutdown(wait=not self.stop, cancel_futures=True)

    def walk(self):
        """{size: [File]} of every regular file big enough, one File per inode."""
        by_size, seen = {}, {}
        device = os.stat(self.root).st_dev
        stack = [self.root]
        while stack:
            if self.stop:
                raise Stopped
            folder = stack.pop()
            try:
                it = os.scandir(folder)
            except OSError:
                if folder == self.root:
                    raise
                self.unreadable += 1
                continue
            try:
                with it:
                    for e in it:
                        self.add(e, device, stack, by_size, seen)
            except OSError:                # a listing can fail halfway: a mount that times out
                if folder == self.root:
                    raise
                self.unreadable += 1
        return by_size

    def add(self, e, device, stack, by_size, seen):
        """One directory entry: a folder to visit, or a file to compare."""
        name = e.name
        if not self.everything and (name.startswith(".") or name in IGNORED or e.path in SKIPPED_PATHS):
            return
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != device:
                self.volumes += 1          # another disk, or a network or cloud mount
            elif not self.everything and name.lower().endswith(BUNDLES):
                self.bundles += 1
            else:
                stack.append(e.path)
            return
        if not stat.S_ISREG(st.st_mode):
            return                         # symlinks, sockets, devices
        self.files += 1
        self.bytes += st.st_size
        if st.st_size < self.min_size:
            return
        if getattr(st, "st_flags", 0) & SF_DATALESS:
            self.cloud += 1
            return
        f = seen.get((st.st_dev, st.st_ino))
        if f:
            f.links.append(e.path)
            return
        f = seen[(st.st_dev, st.st_ino)] = File(e.path, st.st_size, st.st_mtime)
        by_size.setdefault(st.st_size, []).append(f)

    def begin(self, phase, files, limit):
        with self.lock:
            self.phase = phase
            self.todo_files, self.done_files = len(files), 0
            self.todo_bytes = sum(f.size if limit is None else min(f.size, limit) for f in files)
            self.done_bytes = 0

    def hash_all(self, pool, files, limit):
        """{File: digest}; files that can't be read are left out."""
        return {f: h for f, h in zip(files, pool.map(lambda f: self.digest(f, limit), files)) if h}

    def digest(self, f, limit):
        if self.stop:
            raise Stopped
        h = hashlib.blake2b(digest_size=20)
        left = f.size if limit is None else min(f.size, limit)
        try:
            with open(f.path, "rb") as fh:
                while left > 0:
                    if self.stop:
                        raise Stopped
                    block = fh.read(min(CHUNK, left))
                    if not block:
                        break
                    h.update(block)
                    left -= len(block)
                    with self.lock:
                        self.done_bytes += len(block)
        except OSError:
            with self.lock:
                self.unreadable += 1
            return None
        finally:
            with self.lock:
                self.done_files += 1
        return h.digest()

    def publish(self, sets):
        if sets:
            with self.lock:
                self.groups.extend(Group(s) for s in sets)

    def snapshot(self):
        """The groups that still have two or more files, most space in extra copies first."""
        with self.lock:
            groups = [g for g in self.groups if len(g.files) > 1]
        groups.sort(key=lambda g: (-g.extra(), g.files[0].path))
        return groups


def split(files, digests):
    """`files` split by digest, keeping the parts with two or more."""
    parts = {}
    for f in files:
        if f in digests:
            parts.setdefault(digests[f], []).append(f)
    return [sorted(p, key=lambda f: f.path) for p in parts.values() if len(p) > 1]


def unchanged(f):
    """Still the file the scan hashed, as far as size and modification time tell."""
    try:
        st = os.lstat(f.path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == f.size and st.st_mtime == f.mtime


def pick(files, rule):
    """The copy a keep rule keeps."""
    if rule == "oldest":
        return min(files, key=lambda f: (f.mtime, f.path))
    if rule == "newest":
        return max(files, key=lambda f: (f.mtime, f.path))
    return min(files, key=lambda f: (len(f.path), f.path))


def trash(paths):
    """Moves `paths` to the Trash; returns the set of those that are gone, and an error or None."""
    if os.path.exists(TRASH):
        cmds = [[TRASH, *paths[i:i + 100]] for i in range(0, len(paths), 100)]
    else:
        cmds = [["osascript", "-e", "on run argv", "-e",
                 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
                 "-e", "end run", p] for p in paths]
    error = None
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            error = str(e)
            break
        if res.returncode != 0:
            lines = (res.stderr or res.stdout).strip().splitlines()
            error = lines[-1] if lines else f"exit status {res.returncode}"
    return {p for p in paths if not os.path.lexists(p)}, error


# -- rename --

def natural(name):
    """Sort key that puts IMG_9 before IMG_10."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def compile_find(rule):
    """The find pattern, or None when there is nothing to find. Raises re.error."""
    if not rule.find:
        return None
    flags = re.IGNORECASE if rule.nocase else 0
    return re.compile(rule.find if rule.regex else re.escape(rule.find), flags)


def parse_template(template):
    """[(literal, field, arg)] — the last one has no field. Raises ValueError."""
    parts, pos = [], 0
    for m in TEMPLATE_RE.finditer(template):
        field, _, arg = m[1].partition(":")
        if field not in FIELDS:
            raise ValueError(f"unknown {{{m[1]}}} — use {{name}}, {{n}}, {{n:3}}, {{date}} or {{folder}}")
        if field == "n" and arg and not arg.isdigit():
            raise ValueError("{n:…} takes a width, like {n:3}")
        parts.append((template[pos:m.start()], field, arg))
        pos = m.end()
    parts.append((template[pos:], None, None))
    if any("{" in lit or "}" in lit for lit, _, _ in parts):
        raise ValueError("a { or } without its pair")
    return parts


def recase(text, case):
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    if case == "title":                    # str.title() would make "it's" into "It'S"
        return re.sub(r"[^\W_]+(?:['’][^\W_]+)*", lambda m: m[0][:1].upper() + m[0][1:].lower(), text)
    return text


def new_name(name, rule, pattern, parts, n, mtime, folder):
    """What one file is called after the rule: find/replace on the whole name, then the template,
    case and extension rule on its two halves. Raises re.error for a bad replacement."""
    if pattern:
        name = pattern.sub(rule.replace if rule.regex else lambda m: rule.replace, name)
    stem, ext = os.path.splitext(name)
    out = []
    for lit, field, arg in parts:
        out.append(lit)
        if field == "name":
            out.append(stem)
        elif field == "n":
            out.append(str(n).zfill(int(arg or 0)))
        elif field == "date":
            out.append(time.strftime(arg or "%Y-%m-%d", time.localtime(mtime)))
        elif field == "folder":
            out.append(folder)
    stem = recase("".join(out), rule.case)
    if rule.ext == "lower":
        ext = ext.lower()
    elif rule.ext == "upper":
        ext = ext.upper()
    elif rule.ext not in EXT_RULES:
        ext = rule.ext
    return stem + ext


def name_key(insensitive):
    """How the volume compares names: APFS ignores Unicode normalisation, and by default case."""
    if insensitive:
        return lambda s: unicodedata.normalize("NFD", s).casefold()
    return lambda s: unicodedata.normalize("NFD", s)


def make_plan(chosen, fixed, rule, folder, insensitive):
    """([Change] for `chosen` in order, error). `fixed` are the raw names of everything else in
    the folder, which keep their names and so can be collided with. A rule that can't run at all
    gives ([], why)."""
    try:
        pattern = compile_find(rule)
        parts = parse_template(rule.template)
    except re.error as e:
        return [], f"find: {e}"
    except ValueError as e:
        return [], f"name: {e}"
    changes = []
    for i, item in enumerate(chosen):
        try:
            new = new_name(item.name, rule, pattern, parts, rule.start + i, item.mtime, folder)
        except (re.error, IndexError) as e:
            return [], f"replace: {e}"
        changes.append((item, None if new == item.name else new))
    key = name_key(insensitive)
    staying = Counter(key(n) for n in fixed)
    staying.update(key(item.raw) for item, new in changes if new is None)
    taken = Counter(key(new) for _, new in changes if new is not None)
    out = []
    for item, new in changes:
        problem = None
        if new is None:
            pass
        elif new in ("", ".", ".."):
            problem = "empty name"
        elif "/" in new or "\0" in new:
            problem = "has a /"
        elif len(new.encode()) > 255:
            problem = "longer than 255 bytes"
        elif staying[key(new)]:
            problem = "already exists"
        elif taken[key(new)] > 1:
            problem = "two files get this name"
        out.append(Change(item, new, problem))
    return out, None


def case_insensitive(folder, names):
    """Whether the volume ignores case, tried on names that are there. pathconf can't tell on
    macOS, so look the swapped-case name up: the same inode means it ignored the case."""
    for path in [folder] + [os.path.join(folder, n) for n in names[:50]]:
        head, tail = os.path.split(path)
        if tail.swapcase() == tail:
            continue
        try:
            a = os.lstat(path)
            b = os.lstat(os.path.join(head, tail.swapcase()))
        except FileNotFoundError:
            return False
        except OSError:
            continue
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    return True                            # nothing to try it on: APFS's default


def apply_moves(folder, moves):
    """Renames [(old, new)] inside `folder` all or nothing. Every file goes to a temporary name
    first, so swaps and chains work; on any failure everything is put back. Returns (error,
    stranded): the names that couldn't be put back, which is hopefully never anything."""
    def at(n):
        return os.path.join(folder, n)
    tag = f".file_tool-{os.getpid()}-{time.monotonic_ns()}-"
    staged, done = [], []
    try:
        for i, (old, new) in enumerate(moves):
            tmp = f"{tag}{i}"
            os.rename(at(old), at(tmp))
            staged.append((old, tmp, new))
        for old, tmp, new in staged:
            if os.path.lexists(at(new)):   # rename() would replace it without a word
                raise FileExistsError(errno.EEXIST, "already exists", new)
            os.rename(at(tmp), at(new))
            done.append((old, tmp, new))
        return None, []
    except OSError as e:
        error = f"{os.path.basename(e.filename or '?')}: {e.strerror or e}"
    stranded, waiting = [], {tmp for _, tmp, _ in staged} - {tmp for _, tmp, _ in done}
    for old, tmp, new in reversed(done):
        try:
            os.rename(at(new), at(tmp))
            waiting.add(tmp)
        except OSError:
            stranded.append(new)
    for old, tmp, new in reversed(staged):
        if tmp not in waiting:
            continue
        try:
            if os.path.lexists(at(old)):
                raise FileExistsError
            os.rename(at(tmp), at(old))
        except OSError:
            stranded.append(tmp)
    return error, stranded


class Renamer:
    """The files of one folder and the rule that renames them."""

    def __init__(self, folder, everything=False):
        self.folder, self.everything = folder, everything
        self.rule = DEFAULT_RULE
        self.sort = "name"
        self.glob = ""
        self.excluded = set()              # raw names left out with space
        self.history = []                  # [(moves, rule)] for undo
        self.items, self.others = [], []
        self.error = None
        self.insensitive = True
        self.cache = None
        self.load()

    def load(self):
        items, others = [], []
        try:
            with os.scandir(self.folder) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        others.append(e.name)
                        continue
                    hidden = e.name.startswith(".") and not self.everything
                    if stat.S_ISREG(st.st_mode) and not hidden:
                        items.append(Item(e.name, unicodedata.normalize("NFC", e.name), st.st_mtime, st.st_size))
                    else:
                        others.append(e.name)   # folders, links, hidden files: not renamed, but taken
            self.error = None
        except OSError as e:
            self.error = f"can't read the folder: {e.strerror or e}"
        self.items, self.others = items, others
        self.insensitive = case_insensitive(self.folder, [i.raw for i in items] + others)
        self.excluded &= {i.raw for i in items}
        self.cache = None

    def shown(self):
        """The files the glob lets through, in the sort order the counter follows."""
        items = self.items
        if self.glob:
            pat = self.glob.casefold()
            if not any(c in pat for c in "*?["):
                pat = f"*{pat}*"
            items = [i for i in items if fnmatch.fnmatchcase(i.name.casefold(), pat)]
        if self.sort == "date":
            return sorted(items, key=lambda i: (i.mtime, natural(i.name)))
        if self.sort == "size":
            return sorted(items, key=lambda i: (i.size, natural(i.name)))
        return sorted(items, key=lambda i: natural(i.name))

    def plan(self):
        """(shown items, {raw: Change}, error), cached until anything it depends on changes;
        load() drops the cache, since the listing is not part of the key."""
        state = (self.rule, self.sort, self.glob, frozenset(self.excluded))
        if self.cache and self.cache[0] == state:
            return self.cache[1]
        shown = self.shown()
        chosen = [i for i in shown if i.raw not in self.excluded]
        picked = {i.raw for i in chosen}
        fixed = self.others + [i.raw for i in self.items if i.raw not in picked]
        changes, error = make_plan(chosen, fixed, self.rule, os.path.basename(self.folder), self.insensitive)
        result = (shown, {c.item.raw: c for c in changes}, error)
        self.cache = (state, result)
        return result

    def moves(self):
        _, changes, _ = self.plan()
        return [(c.item.raw, c.new) for c in changes.values() if c.new]


# -- formatting --

def fmt_size(n):
    """Decimal units, as Finder shows them."""
    for unit, limit in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


def parse_size(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", text.lower())
    if not m:
        raise ValueError(text)
    return int(float(m[1]) * 1000 ** " kmgt".index(m[2] or " "))


def plural(n, word, many=None):
    return f"{n:,} {word if n == 1 else many or word + 's'}"


def fmt_seconds(s):
    return f"{s:.1f} s" if s < 10 else f"{s:.0f} s" if s < 120 else f"{s // 60:.0f} min {s % 60:.0f} s"


def clean(name):
    """Filenames come NFD-normalised from APFS and may hold control characters."""
    name = unicodedata.normalize("NFC", name)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        name = "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in name)
    return name.encode("utf-8", "replace").decode("utf-8")


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


def fit_left(text, width):
    """Like fit(), but a path keeps its end: the file name matters more than the top folders."""
    if cwidth(text) <= width:
        return fit(text, width)
    kept, used = [], 1
    for ch in reversed(text):
        w = cwidth(ch)
        if used + w > width:
            break
        kept.append(ch)
        used += w
    return fit("…" + "".join(reversed(kept)), width)


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


# -- the window --

class App:
    def __init__(self, stdscr, root, args):
        self.stdscr, self.root, self.args = stdscr, root, args
        self.prefix = root.rstrip("/") + "/"
        self.tab = 0                       # 0 duplicates, 1 rename
        self.scan = Scan(root, args.min_size, args.all)
        self.scan.start()
        self.dsel, self.dtop = None, 0     # selected File
        self.keep = KEEP_RULES[0]
        self.search = ""
        self.rn = Renamer(root, args.all)
        self.rsel, self.rtop = 0, 0
        self.confirm = None                # (question, action) waiting for y
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.shown = ""

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

    def rel(self, path):
        return clean(path[len(self.prefix):] if path.startswith(self.prefix) else path)

    # -- drawing --

    def draw(self, footer=True):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.put(0, 0, " File tool ", curses.color_pair(1) | curses.A_BOLD)
        x = 12
        for i, name in enumerate(("1 Duplicates", "2 Rename")):
            self.put(0, x, f" {name} ", curses.A_REVERSE | curses.A_BOLD if i == self.tab else curses.color_pair(5))
            x += len(name) + 3
        label = short(self.root)
        self.put(0, x + 1, fit_left(label, w - x - 2).rstrip().rjust(w - x - 2), curses.A_BOLD)
        self.shown = ""
        if self.tab == 0:
            self.draw_dupes_header(w)
            self.draw_dupes(4, h - 5, w)
        else:
            self.draw_rename_header(w)
            self.draw_rename(4, h - 5, w)
        if footer:
            self.draw_footer(h - 1, w)
        s.refresh()

    def draw_footer(self, y, w):
        if self.confirm:
            self.put(y, 0, f" {self.confirm[0]}  y/n", curses.color_pair(3) | curses.A_REVERSE | curses.A_BOLD)
            return
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        if self.tab == 0:
            keys = ["␣ mark", f"a mark all (keep {self.keep})" if not self.search else "a mark matches",
                    "t trash", "⇥ rename", "k keep rule", "⏎ Finder", "o open", "/ find", "c copy", "r rescan"]
        else:
            keys = ["f find", "r replace", "n name", "⏎ rename", "u undo", "⇥ dupes", "␣ in/out", "c case",
                    "e ext", "x regex", "i ignore case", "s sort", "# start", "/ filter", "0 reset",
                    "o Finder", "R reread"]
        room = w - 2 - (len(self.shown) + 2 if self.shown else 0)
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > room:
            keys.pop()                     # the least needed go first in a narrow window
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))
        if self.shown:
            self.put(y, w - len(self.shown) - 1, self.shown, curses.color_pair(1))

    def scroll(self, top, index, first, height, count):
        """The new top row: `index` in view, and `first` (its group's header) too if it fits."""
        if first < top:
            top = first
        if index >= top + height:
            top = index - height + 1
        return max(0, min(top, count - height))

    # -- duplicates tab --

    def groups(self):
        groups = self.scan.snapshot()
        if self.search:
            needle = self.search.casefold()
            groups = [g for g in groups if any(needle in self.rel(f.path).casefold() for f in g.files)]
        return groups

    def dupe_rows(self, groups):
        rows = []
        for g in groups:
            rows.append((g, None))
            rows.extend((g, f) for f in g.files)
        return rows

    def draw_dupes_header(self, w):
        sc = self.scan
        if sc.error:
            self.put(1, 1, sc.error, curses.color_pair(4))
        elif sc.phase == "listing":
            self.put(1, 1, f"listing… {plural(sc.files, 'file')} · {fmt_size(sc.bytes)}", curses.color_pair(3))
        elif sc.phase in ("comparing", "hashing"):
            what = "comparing the first 64 kB of" if sc.phase == "comparing" else "reading in full"
            pct = sc.done_bytes / sc.todo_bytes * 100 if sc.todo_bytes else 100
            self.put(1, 1, f"{what} {sc.done_files:,} of {plural(sc.todo_files, 'file')} · "
                     f"{fmt_size(sc.done_bytes)} of {fmt_size(sc.todo_bytes)} ({pct:.0f}%)", curses.color_pair(3))
        else:
            self.put(1, 1, f"{plural(sc.files, 'file')} · {fmt_size(sc.bytes)} scanned in {fmt_seconds(sc.took)}",
                     curses.color_pair(5))
        groups = self.scan.snapshot()
        copies = sum(len(g.files) - 1 for g in groups)
        extra = sum(g.extra() for g in groups)
        marked = [f for g in groups for f in g.files if f.marked]
        line = (f"{plural(len(groups), 'set')} of duplicates · {plural(copies, 'extra copy', 'extra copies')}"
                f" · {fmt_size(extra)} in extra copies")
        self.put(2, 1, line, curses.color_pair(1) | curses.A_BOLD)
        notes = []
        if marked:
            notes.append((f"{len(marked):,} marked, {fmt_size(sum(f.size for f in marked))}", curses.color_pair(4) | curses.A_BOLD))
        if self.search:
            notes.append((f"/ {self.search}", curses.color_pair(1)))
        skipped = [plural(sc.cloud, "file") + " only in iCloud" if sc.cloud else "",
                   f"{sc.unreadable:,} unreadable" if sc.unreadable else "",
                   plural(sc.bundles, "package") + " not opened" if sc.bundles else "",
                   plural(sc.volumes, "other volume") if sc.volumes else ""]
        if any(skipped):
            notes.append(("skipped: " + ", ".join(s for s in skipped if s), curses.color_pair(5)))
        x = 1
        for text, attr in notes:
            self.put(3, x, text, attr, len(text))
            x += cwidth(text) + 3

    def draw_dupes(self, y0, height, w):
        groups = self.groups()
        rows = self.dupe_rows(groups)
        if not rows:
            if self.scan.phase == "done" and not self.scan.error:
                self.put(y0 + 1, 2, "nothing matches" if self.search else "no duplicates here", curses.color_pair(5))
            return
        index = self.dupe_index(rows)
        first = index - 1 if index > 0 and rows[index - 1][1] is None else index
        self.dtop = self.scroll(self.dtop, index, first, height, len(rows))
        date_w = 10
        for i, (g, f) in enumerate(rows[self.dtop:self.dtop + height]):
            y = y0 + i
            if f is None:
                n_marked = sum(x.marked for x in g.files)
                text = f"{len(g.files)} × {fmt_size(g.size)}"
                self.put(y, 1, text, curses.color_pair(1) | curses.A_BOLD)
                tail = f"{fmt_size(g.extra())} extra" + (f" · {n_marked} marked" if n_marked else "")
                self.put(y, 3 + len(text), tail, curses.color_pair(5))
                continue
            chosen = self.dtop + i == index
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 3, "✗" if f.marked else "·", base | (curses.color_pair(4) | curses.A_BOLD if f.marked else curses.color_pair(5)), 1)
            path = self.rel(f.path) + (f"  (+{plural(len(f.links), 'hard link')})" if f.links else "")
            path_w = w - 5 - date_w - 2
            attr = base | (curses.color_pair(4) if f.marked else 0)
            self.put(y, 5, fit_left(path, path_w), attr, path_w)
            self.put(y, w - date_w - 1, time.strftime("%Y-%m-%d", time.localtime(f.mtime)), base | curses.color_pair(5), date_w)
        if len(rows) > height:
            self.shown = f"{self.dtop + 1}–{min(len(rows), self.dtop + height)} of {len(rows):,}"

    def dupe_index(self, rows):
        for i, (_, f) in enumerate(rows):
            if f is not None and f is self.dsel:
                return i
        return next((i for i, (_, f) in enumerate(rows) if f is not None), 0)

    def dupe_move(self, delta, by_group=False):
        rows = self.dupe_rows(self.groups())
        files = [i for i, (_, f) in enumerate(rows) if f is not None]
        if not files:
            return
        index = self.dupe_index(rows)
        pos = files.index(index) if index in files else 0
        if by_group:
            heads = [i for i, (_, f) in enumerate(rows) if f is None]
            here = max((h for h in heads if h < index), default=0)
            later = [h for h in heads if (h > here if delta > 0 else h < here)]
            if later:
                self.dsel = rows[(min(later) if delta > 0 else max(later)) + 1][1]
            return
        pos = max(0, min(len(files) - 1, pos + delta))
        self.dsel = rows[files[pos]][1]

    def selected_file(self):
        rows = self.dupe_rows(self.groups())
        if not rows:
            return None
        g, f = rows[self.dupe_index(rows)]
        return f

    def group_of(self, f):
        return next((g for g in self.scan.snapshot() if f in g.files), None)

    def toggle_mark(self):
        f = self.selected_file()
        if not f:
            return
        g = self.group_of(f)
        if not f.marked and sum(not x.marked for x in g.files) == 1:
            self.warn("that is the last unmarked copy — one copy of each set always stays")
            return
        f.marked = not f.marked
        self.dupe_move(1)

    def auto_mark(self):
        groups = self.groups()
        needle = self.search.casefold()
        count = 0
        for g in groups:
            if needle:                     # mark what matches, keeping one copy, if possible one that doesn't
                hits = [f for f in g.files if needle in self.rel(f.path).casefold()]
                misses = [f for f in g.files if f not in hits]
                keep = next((f for f in misses if not f.marked), None) or (misses and misses[0]) or pick(hits, self.keep)
                keep.marked = False
                for f in hits:
                    f.marked = f is not keep
            else:
                keep = pick(g.files, self.keep)
                for f in g.files:
                    f.marked = f is not keep
            count += sum(f.marked for f in g.files)
        what = f"matching “{self.search}”" if needle else f"keeping the {self.keep} of each set"
        self.say(f"{plural(count, 'file')} marked, {what}" + ("" if self.scan.phase == "done" else " — the scan is still going"))

    def unmark_all(self):
        for g in self.scan.snapshot():
            for f in g.files:
                f.marked = False
        self.say("marks cleared")

    def ask_trash(self):
        marked = [f for g in self.scan.snapshot() for f in g.files if f.marked]
        if not marked:
            self.warn("nothing marked — space marks a file, a marks every extra copy")
            return
        links = sum(len(f.links) for f in marked)
        extra = f" and their {plural(links, 'hard link')}" if links else ""
        self.confirm = (f"Move {plural(len(marked), 'marked file')}{extra} ({fmt_size(sum(f.size for f in marked))}) to the Trash?",
                        self.trash_marked)

    def trash_marked(self):
        """Checks every marked file and a kept copy of it against the scan before it goes."""
        paths, going, skipped = [], [], 0
        for g in self.scan.snapshot():
            marked = [f for f in g.files if f.marked]
            if not marked:
                continue
            if not any(not f.marked and unchanged(f) for f in g.files):
                skipped += len(marked)     # the copy that would stay is gone or changed
                continue
            for f in marked:
                if unchanged(f):
                    going.append((g, f))
                    paths += [f.path, *f.links]
                else:
                    skipped += 1
                    f.marked = False
        if not going:
            self.warn("nothing moved: the files changed since the scan — r rescans")
            return
        gone, error = trash(paths)
        freed = moved = 0
        for g, f in going:
            if f.path in gone:
                g.files.remove(f)
                f.marked = False
                moved += 1
                freed += f.size
        text = f"Moved {plural(moved, 'file')} ({fmt_size(freed)}) to the Trash; empty it to free the space"
        if skipped:
            text += f" · {skipped:,} skipped, changed since the scan"
        if error or moved < len(going):
            self.warn(f"{text} · {len(going) - moved:,} not moved: {error or 'still there'}")
        else:
            self.say(text, curses.color_pair(2))
        self.rn.load()

    def rescan(self):
        self.scan.stop = True
        self.scan = Scan(self.root, self.args.min_size, self.args.all)
        self.scan.start()
        self.dsel, self.dtop = None, 0
        self.say("scanning again")

    def remap(self, moves):
        """Keeps the duplicates list right after files in the rename folder change names."""
        renamed = {os.path.join(self.root, old): os.path.join(self.root, new) for old, new in moves}
        for g in self.scan.snapshot():
            for f in g.files:
                f.path = renamed.get(f.path, f.path)
                f.links = [renamed.get(p, p) for p in f.links]

    # -- rename tab --

    def draw_rename_header(self, w):
        rn, rule = self.rn, self.rn.rule
        shown, changes, error = rn.plan()
        n_change = sum(1 for c in changes.values() if c.new)
        n_bad = sum(1 for c in changes.values() if c.problem)
        line = f"{plural(len(shown), 'file')}"
        if rn.glob:
            line += f" matching “{rn.glob}” of {len(rn.items):,}"
        if rn.excluded:
            line += f" · {len(rn.excluded & {i.raw for i in shown}):,} left out"
        self.put(1, 1, line, curses.color_pair(5))
        x = 2 + cwidth(line) + 1
        if rn.error or error:
            self.put(1, x, rn.error or error, curses.color_pair(4) | curses.A_BOLD)
        elif n_bad:
            self.put(1, x, f"{n_change:,} to rename · {plural(n_bad, 'conflict')}", curses.color_pair(4) | curses.A_BOLD)
        else:
            self.put(1, x, f"{n_change:,} to rename" if n_change else "nothing changes yet", curses.color_pair(2) | curses.A_BOLD)

        def field(y, x, key, label, value, on=True):
            self.put(y, x, key, curses.color_pair(1) | curses.A_BOLD, len(key))
            self.put(y, x + len(key) + 1, label, curses.color_pair(5), len(label))
            attr = curses.A_BOLD if on else curses.color_pair(5)
            self.put(y, x + len(key) + len(label) + 2, value, attr, cwidth(value))
            return x + len(key) + len(label) + cwidth(value) + 5

        x = field(2, 1, "f", "find", f"“{rule.find}”" if rule.find else "—", bool(rule.find))
        x = field(2, x, "r", "replace", f"“{rule.replace}”" if rule.find else "—", bool(rule.find))
        x = field(2, x, "x", "regex", "on" if rule.regex else "off", rule.regex)
        field(2, x, "i", "ignore case", "on" if rule.nocase else "off", rule.nocase)
        x = field(3, 1, "n", "name", rule.template, rule.template != "{name}")
        x = field(3, x, "c", "case", rule.case, rule.case != "as is")
        x = field(3, x, "e", "ext", rule.ext, rule.ext != "keep")
        x = field(3, x, "s", "sort", rn.sort, True)
        field(3, x, "#", "from", str(rule.start), "{n" in rule.template)

    def draw_rename(self, y0, height, w):
        shown, changes, error = self.rn.plan()
        if not shown:
            self.put(y0 + 1, 2, "nothing matches the filter" if self.rn.glob else "no files in this folder", curses.color_pair(5))
            return
        self.rsel = max(0, min(self.rsel, len(shown) - 1))
        self.rtop = self.scroll(self.rtop, self.rsel, self.rsel, height, len(shown))
        col = (w - 8) // 2
        for i, item in enumerate(shown[self.rtop:self.rtop + height]):
            y = y0 + i
            chosen = self.rtop + i == self.rsel
            base = curses.A_REVERSE if chosen else 0
            out = item.raw in self.rn.excluded
            c = changes.get(item.raw)
            self.put(y, 0, " " * w, base)
            self.put(y, 1, "○" if out else "●", base | curses.color_pair(5 if out else 1), 1)
            self.put(y, 3, clean(item.name), base | (curses.color_pair(5) if out or not (c and c.new) else 0), col)
            if c and c.new:
                bad = c.problem is not None
                self.put(y, 4 + col, "→", base | curses.color_pair(4 if bad else 2), 1)
                text = clean(c.new) + (f"  ✗ {c.problem}" if bad else "")
                self.put(y, 6 + col, text, base | curses.color_pair(4 if bad else 2) | (curses.A_BOLD if bad else 0), w - 7 - col)
            elif out:
                self.put(y, 6 + col, "left out", base | curses.color_pair(5), w - 7 - col)
        if len(shown) > height:
            self.shown = f"{self.rtop + 1}–{min(len(shown), self.rtop + height)} of {len(shown):,}"

    def set_rule(self, **kw):
        self.rn.rule = self.rn.rule._replace(**kw)

    def edit(self, label, field, live=True):
        """A prompt that previews the rule while it is typed; ESC puts the old value back."""
        old = getattr(self.rn.rule, field)
        text = self.prompt(label, old, (lambda t: self.set_rule(**{field: t})) if live else None)
        self.set_rule(**{field: old if text is None else text})

    def ask_rename(self):
        rn = self.rn
        shown, changes, error = rn.plan()
        if rn.error or error:
            self.warn(rn.error or error)
            return
        bad = sum(1 for c in changes.values() if c.problem)
        if bad:
            self.warn(f"{plural(bad, 'conflict')} — change the rule, or leave those files out with space")
            return
        moves = rn.moves()
        if not moves:
            self.warn("no names change — f find, n name, c case or e ext set the rule")
            return
        self.confirm = (f"Rename {plural(len(moves), 'file')}?", lambda: self.rename(moves))

    def rename(self, moves):
        rn = self.rn
        error, stranded = apply_moves(rn.folder, moves)
        if error:
            if stranded:
                self.warn(f"{error} — and these could not be put back: {', '.join(map(clean, stranded))}")
            else:
                self.warn(f"nothing renamed, all put back: {error}")
            rn.load()
            return
        rn.history.append((moves, rn.rule))
        rn.rule = DEFAULT_RULE
        rn.load()
        self.remap(moves)
        self.say(f"Renamed {plural(len(moves), 'file')} — u undoes", curses.color_pair(2))

    def undo(self):
        rn = self.rn
        if not rn.history:
            self.warn("nothing to undo")
            return
        moves, rule = rn.history[-1]
        back = [(new, old) for old, new in moves]
        error, stranded = apply_moves(rn.folder, back)
        rn.load()
        if error:
            extra = f"; stuck under a temporary name: {', '.join(map(clean, stranded))}" if stranded else ""
            self.warn(f"couldn't undo: {error}{extra}")
            return
        rn.history.pop()
        rn.rule = rule                     # the rule comes back too, to adjust and try again
        self.remap(back)
        self.say(f"Undone: {plural(len(moves), 'file')} have their old names back")

    def rename_item(self):
        shown, _, _ = self.rn.plan()
        return shown[self.rsel] if shown and self.rsel < len(shown) else None

    def toggle_item(self):
        item = self.rename_item()
        if item:
            self.rn.excluded ^= {item.raw}
            self.rsel += 1

    def toggle_all(self):
        shown, _, _ = self.rn.plan()
        names = {i.raw for i in shown}
        if names <= self.rn.excluded:
            self.rn.excluded -= names
        else:
            self.rn.excluded |= names

    # -- input --

    def prompt(self, label, initial="", live=None):
        curses.curs_set(1)
        buf = list(initial)
        try:
            while True:
                if live:
                    live("".join(buf))
                self.draw(footer=False)
                h, w = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(1) | curses.A_REVERSE)
                self.stdscr.move(h - 1, max(0, min(cwidth(text), w - 2)))
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue               # the tick: redraw, the scan may have moved on
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf)
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
                    return "".join(buf)
        finally:
            curses.curs_set(0)

    def reveal(self, path, how="reveal"):
        cmd = ["open", "-R", path] if how == "reveal" else ["open", path]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            self.say(("shown in Finder: " if how == "reveal" else "opened ") + self.rel(path))
        except (OSError, subprocess.CalledProcessError):
            self.warn(f"couldn't open {self.rel(path)}")

    def copy(self, path):
        try:
            subprocess.run(["pbcopy"], input=path.encode(), check=True)
            self.say(f"copied {clean(path)}")
        except (OSError, subprocess.CalledProcessError):
            self.warn("couldn't copy — pbcopy not found")

    def handle(self, ch):
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
        if ch in (9, curses.KEY_BTAB) or ch in (ord("1"), ord("2")):
            self.tab = (1 - self.tab) if ch in (9, curses.KEY_BTAB) else ch - ord("1")
            if self.tab == 1:
                self.rn.load()             # files may have come and gone meanwhile
            return True
        page = max(1, self.stdscr.getmaxyx()[0] - 6)
        steps = {curses.KEY_UP: -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if self.tab == 0:
            self.handle_dupes(ch, steps)   # k is the keep rule there
        else:
            self.handle_rename(ch, steps | {ord("k"): -1})
        return True

    def handle_dupes(self, ch, steps):
        f = self.selected_file()
        if ch in steps:
            self.dupe_move(steps[ch])
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("]")):
            self.dupe_move(1, by_group=True)
        elif ch in (curses.KEY_LEFT, ord("h"), ord("[")):
            self.dupe_move(-1, by_group=True)
        elif ch == ord(" "):
            self.toggle_mark()
        elif ch == ord("a"):
            self.auto_mark()
        elif ch in (ord("u"), ord("A")):
            self.unmark_all()
        elif ch == ord("k"):
            self.keep = KEEP_RULES[(KEEP_RULES.index(self.keep) + 1) % len(KEEP_RULES)]
            self.say(f"a now keeps the {self.keep} copy of each set")
        elif ch in (ord("t"), ord("d"), curses.KEY_DC):
            self.ask_trash()
        elif ch in (curses.KEY_ENTER, 10, 13) and f:
            self.reveal(f.path)
        elif ch == ord("o") and f:
            self.reveal(f.path, "open")
        elif ch == ord("c") and f:
            self.copy(f.path)
        elif ch == ord("/"):
            text = self.prompt(" find in paths: ", self.search)
            if text is not None:
                self.search, self.dtop = text.strip(), 0
        elif ch == 27 and self.search:
            self.search, self.dtop = "", 0
        elif ch == ord("r"):
            self.rescan()

    def handle_rename(self, ch, steps):
        rn = self.rn
        if ch in steps:
            self.rsel = max(0, self.rsel + steps[ch])
        elif ch == ord(" "):
            self.toggle_item()
        elif ch == ord("a"):
            self.toggle_all()
        elif ch == ord("f"):
            self.edit(" find: ", "find")
        elif ch == ord("r"):
            if not rn.rule.find:
                self.warn("set f find first — replace swaps what find matches")
            else:
                hint = " replace (\\1 for a group): " if rn.rule.regex else " replace with: "
                self.edit(hint, "replace")
        elif ch == ord("n"):
            self.edit(" name — {name} {n} {n:3} {date} {folder}: ", "template")
        elif ch == ord("x"):
            self.set_rule(regex=not rn.rule.regex)
        elif ch == ord("i"):
            self.set_rule(nocase=not rn.rule.nocase)
        elif ch == ord("c"):
            self.set_rule(case=CASES[(CASES.index(rn.rule.case) + 1) % len(CASES)])
        elif ch == ord("e"):
            i = EXT_RULES.index(rn.rule.ext) if rn.rule.ext in EXT_RULES else -1
            self.set_rule(ext=EXT_RULES[(i + 1) % len(EXT_RULES)])
        elif ch == ord("E"):
            text = self.prompt(" new extension for every file (empty: keep): ",
                               rn.rule.ext.lstrip(".") if rn.rule.ext not in EXT_RULES else "")
            if text is not None:
                text = text.strip().lstrip(".")
                self.set_rule(ext="." + text if text else "keep")
        elif ch == ord("s"):
            rn.sort = SORTS[(SORTS.index(rn.sort) + 1) % len(SORTS)]
        elif ch == ord("#"):
            text = self.prompt(" number from: ", str(rn.rule.start))
            if text is not None:
                if text.strip().isdigit():
                    self.set_rule(start=int(text))
                else:
                    self.warn("a whole number, like 1")
        elif ch == ord("/"):
            text = self.prompt(" only files matching (*.jpg, or any part of the name): ", rn.glob)
            if text is not None:
                rn.glob, self.rsel = text.strip(), 0
        elif ch == 27 and rn.glob:
            rn.glob = ""
        elif ch in (curses.KEY_ENTER, 10, 13):
            self.ask_rename()
        elif ch == ord("u"):
            self.undo()
        elif ch == ord("0"):
            rn.rule = DEFAULT_RULE
            self.say("rule reset")
        elif ch == ord("R"):
            rn.load()
            self.say("folder read again")
        elif ch == ord("o"):
            item = self.rename_item()
            if item:
                self.reveal(os.path.join(rn.folder, item.raw))


def run(stdscr, root, args):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, set headers, keys
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # new names, done
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # progress, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # marked, conflicts, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    stdscr.timeout(TICK_MS)
    app = App(stdscr, root, args)
    if args.rename:
        app.tab = 1
    while True:
        app.draw()
        ch = stdscr.getch()
        if ch != -1 and not app.handle(ch):
            app.scan.stop = True
            return


def print_dupes(root, args):
    """The duplicate sets as plain lines, biggest waste first; pipeable."""
    scan = Scan(root, args.min_size, args.all)
    scan.run()
    if scan.error:
        print(f"error: {scan.error}", file=sys.stderr)
        return 1
    groups = scan.snapshot()
    for g in groups:
        print(f"{len(g.files)} × {fmt_size(g.size)}  ({fmt_size(g.extra())} extra)")
        for f in g.files:
            links = f"  (+{plural(len(f.links), 'hard link')}: {', '.join(f.links)})" if f.links else ""
            print(f"  {f.path}{links}")
    sys.stdout.flush()                     # the summary comes after the sets, even through a pipe
    skipped = []
    if scan.cloud:
        skipped.append(f"{plural(scan.cloud, 'file')} only in iCloud")
    if scan.unreadable:
        skipped.append(f"{scan.unreadable:,} unreadable")
    if scan.bundles:
        skipped.append(f"{plural(scan.bundles, 'package')} not opened")
    if scan.volumes:
        skipped.append(plural(scan.volumes, "other volume"))
    print(f"{plural(scan.files, 'file')} in {fmt_seconds(scan.took)}: {plural(len(groups), 'set')} of duplicates, "
          f"{fmt_size(sum(g.extra() for g in groups))} in extra copies"
          + (f"; skipped {', '.join(skipped)}" if skipped else ""), file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicate files under a folder, and rename the folder's files in bulk.",
        epilog="Hidden files, ~/Library, node_modules, __pycache__ and the insides of packages such as "
               ".app and .photoslibrary are skipped unless --all. The scan never leaves the folder's volume.")
    parser.add_argument("path", nargs="?", default=".", help="folder to work in (default: the current one)")
    parser.add_argument("-m", "--min-size", type=parse_size, default=MIN_SIZE, metavar="SIZE",
                        help="ignore files smaller than this, like 100k or 5M (default: 1 byte, so not empty files)")
    parser.add_argument("-a", "--all", action="store_true",
                        help="include hidden files, ~/Library, caches and package contents")
    parser.add_argument("-r", "--rename", action="store_true", help="start on the Rename tab")
    parser.add_argument("--dupes", action="store_true", help="print the duplicates instead of opening the window")
    args = parser.parse_args()
    root = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(root):
        parser.error(f"not a folder: {args.path}")
    if args.dupes:
        raise SystemExit(print_dupes(root, args))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, root, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
