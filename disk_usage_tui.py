#!/usr/bin/env python3
"""What is filling the disk: a size-sorted folder tree, a treemap and a file-type list (macOS).

In the manner of WinDirStat. The scan runs on worker threads and can be browsed while it fills
in. Sizes are allocated bytes (st_blocks), the space a file really takes on disk, so sparse and
compressed files count for what they occupy and iCloud files that are not downloaded count as
nothing; a file with several hard links is counted once. Nothing is deleted outright: `t` moves
an item to the Trash, where it still takes space until the Trash is emptied.
"""

import argparse
import curses
import gc
import heapq
import itertools
import locale
import math
import os
import queue
import stat
import subprocess
import sys
import threading
import time
import unicodedata

WORKERS = 8                    # scandir/lstat release the GIL, so threads overlap the syscalls
TOP_FILES = 500                # kept for the Largest files view
TICK_MS = 250                  # repaint interval while scanning
MAP_REFRESH = 1.0              # the treemap is re-laid at most this often while scanning
MIN_W, MIN_H = 60, 12
MAP_MIN_H = 22                 # below this many rows the treemap is left out
NAME_MAX, BAR_MAX = 110, 50     # list columns in a wide window
DATA_VOLUME = "/System/Volumes/Data"
FIRMLINKS = "/usr/share/firmlinks"
TRASH = "/usr/bin/trash"

QUEUED, DONE, DENIED, OTHER_VOLUME, ALIAS = range(5)
NOTES = {DENIED: "no access", OTHER_VOLUME: "other volume", ALIAS: "counted at /"}

VIEWS = ["Tree", "Types", "Largest files"]

# Treemap colours: the biggest extensions get one each, as in WinDirStat; the rest share OTHER.
PALETTE = [(60, 120, 230), (220, 60, 60), (70, 180, 70), (230, 200, 40), (60, 200, 215),
           (200, 80, 200), (245, 145, 45), (145, 110, 235), (165, 215, 60), (235, 115, 165),
           (40, 150, 145), (180, 130, 80)]
OTHER = (170, 170, 170)
FOLDER = (110, 110, 120)       # a folder too small to open up, or a lump of tiny items
UNFOCUSED = (55, 55, 60)       # Types view: everything that is not the selected type
HIGHLIGHT = (255, 255, 255)

# Cushion shading (van Wijk & van de Wetering): each nesting level adds a ridge of height
# CUSHION_H * CUSHION_F**depth, lit from the top left.
CUSHION_H = 0.40
CUSHION_F = 0.75
AMBIENT = 0.40
DIFFUSE = 0.70
_L = math.sqrt(1 + 1 + 100)
LIGHT = (-1 / _L, -1 / _L, 10 / _L)
SHADES = 32                    # intensity steps per colour
LUMP_PX = 0.4                  # items smaller than this many pixels are drawn as one lump

EIGHTHS = " ▏▎▍▌▋▊▉"


class Node:
    """A file or folder. `size`, `files` and `denied` (unreadable folders) include everything
    below; `todo` counts the folder listings still outstanding in the subtree, so 0 means the
    numbers are final."""

    __slots__ = ("name", "parent", "kids", "size", "files", "denied", "todo", "state")

    def __init__(self, name, parent, is_dir, size=0):
        self.name = name
        self.parent = parent
        self.kids = [] if is_dir else None
        self.size = size
        self.files = 0 if is_dir else 1
        self.denied = 0
        self.todo = 0
        self.state = QUEUED if is_dir else DONE

    @property
    def is_dir(self):
        return self.kids is not None

    def path(self):
        parts, n = [], self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return os.path.join(*reversed(parts))

    def within(self, other):
        n = self
        while n is not None:
            if n is other:
                return True
            n = n.parent
        return False


def ext_of(name):
    dot = name.rfind(".")
    if dot <= 0 or len(name) - dot > 12:
        return ""
    return name[dot + 1:].lower()


def walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if n.kids:
            stack.extend(n.kids)


def firmlinked(root):
    """Paths on the Data volume that a scan of / already reaches through a firmlink (/Users is
    /System/Volumes/Data/Users, and both report the same device)."""
    if root != "/":
        return set()
    skip = set()
    try:
        with open(FIRMLINKS) as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 2 and parts[1]:
                    skip.add(os.path.join(DATA_VOLUME, parts[1]))
    except OSError:
        skip.add(DATA_VOLUME)          # no table: leave the whole volume to the firmlinks
    return skip


class Scan:
    """The tree of one scan, filled in by worker threads. Everything that touches sizes or the
    shared tables does so under `lock`; the UI reads without it."""

    def __init__(self, path, workers=WORKERS):
        self.path = path
        self.root = Node(path, None, True)
        self.dev = os.lstat(path).st_dev
        self.skip = firmlinked(path)
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.ext = {}                  # extension -> [bytes, files]
        self.top = []                  # min-heap of (size, seq, node), the largest files
        self.floor = 1                 # a file must be at least this big to enter `top`
        self.links = {}                # inode -> node, for files with more than one link
        self.seq = itertools.count()
        self.files = self.dirs = self.denied = 0
        self.current = path
        self.started = time.monotonic()
        self.elapsed = None
        self.stopped = False
        self.root.todo = 1
        self.queue.put((self.root, path))
        for _ in range(workers):
            threading.Thread(target=self.work, daemon=True).start()

    @property
    def busy(self):
        return self.root.todo > 0

    def stop(self):
        self.stopped = True

    def work(self):
        while True:
            node, path = self.queue.get()
            if not self.stopped:
                self.list_dir(node, path)

    def list_dir(self, node, path):
        self.current = path
        kids, subdirs, big, exts = [], [], [], {}
        total = nfiles = 0
        state = DONE
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    size = st.st_blocks * 512
                    if stat.S_ISDIR(st.st_mode):
                        child = Node(entry.name, node, True, size)
                        if st.st_dev != self.dev:
                            child.state = OTHER_VOLUME
                        elif entry.path in self.skip:
                            child.state = ALIAS
                        else:
                            child.todo = 1
                            subdirs.append((child, entry.path))
                    else:
                        child = Node(entry.name, node, False, size)
                        if st.st_nlink > 1:
                            with self.lock:
                                if st.st_ino in self.links:
                                    child.size = size = 0
                                else:
                                    self.links[st.st_ino] = child
                        nfiles += 1
                        e = exts.get(ext := ext_of(entry.name))
                        if e is None:
                            exts[ext] = [size, 1]
                        else:
                            e[0] += size
                            e[1] += 1
                        if size >= self.floor:
                            big.append(child)
                    kids.append(child)
                    total += size
        except OSError:
            state = DENIED
        with self.lock:
            node.kids = kids
            node.state = state
            n = node
            while n is not None:
                n.size += total
                n.files += nfiles
                n.denied += state == DENIED
                n.todo += len(subdirs) - 1
                n = n.parent
            for ext, (b, c) in exts.items():
                e = self.ext.get(ext)
                if e is None:
                    self.ext[ext] = [b, c]
                else:
                    e[0] += b
                    e[1] += c
            for child in big:
                self.offer(child)
            self.files += nfiles
            self.dirs += 1
            self.denied += state == DENIED
            if not self.root.todo:
                self.elapsed = time.monotonic() - self.started
        for item in subdirs:
            self.queue.put(item)

    def offer(self, node):
        if node.size < self.floor:
            return
        item = (node.size, next(self.seq), node)
        if len(self.top) < TOP_FILES:
            heapq.heappush(self.top, item)
        else:
            heapq.heapreplace(self.top, item)
        if len(self.top) >= TOP_FILES:
            self.floor = self.top[0][0] + 1

    def largest(self):
        return sorted(self.top, key=lambda t: -t[0])

    def detach(self, node):
        """Take a subtree out of the tree and out of every total. Only while the scan is idle."""
        with self.lock:
            parent = node.parent
            parent.kids.remove(node)
            n = parent
            while n is not None:
                n.size -= node.size
                n.files -= node.files
                n.denied -= node.denied
                n = n.parent
            self.denied -= node.denied
            for leaf in walk(node):
                if leaf.kids is None and (e := self.ext.get(ext := ext_of(leaf.name))):
                    e[0] -= leaf.size
                    e[1] -= 1
                    if e[1] <= 0:
                        del self.ext[ext]
            self.top = [t for t in self.top if not t[2].within(node)]
            heapq.heapify(self.top)
            self.floor = self.top[0][0] + 1 if len(self.top) >= TOP_FILES else 1
            self.links = {k: v for k, v in self.links.items() if not v.within(node)}

    def rescan(self, node):
        """Replace a folder with a fresh scan of it; returns the new node, or None if it is gone."""
        path = node.path()
        parent = node.parent
        self.detach(node)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        fresh = Node(node.name, parent, True, st.st_blocks * 512)
        fresh.todo = 1
        with self.lock:
            parent.kids.append(fresh)
            n = parent
            while n is not None:
                n.size += fresh.size
                n.todo += 1
                n = n.parent
            self.started = time.monotonic()
            self.elapsed = None
        self.queue.put((fresh, path))
        return fresh


def volume(path):
    """(capacity, free) of the volume. On APFS the capacity is the whole container's, and the
    used part includes the other volumes in it (System, VM, Preboot) and local snapshots."""
    try:
        s = os.statvfs(path)
    except OSError:
        return 0, 0
    return s.f_blocks * s.f_frsize, s.f_bavail * s.f_frsize


def fmt_size(n):
    """Decimal units, as Finder shows them."""
    for unit, limit in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


def fmt_secs(s):
    s = int(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


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


def bar(fraction, width):
    """A bar in eighths of a cell, padded to `width`."""
    eighths = max(0, min(width * 8, round(fraction * width * 8)))
    if fraction > 0:
        eighths = max(eighths, 1)
    full, part = divmod(eighths, 8)
    text = "█" * full + (EIGHTHS[part] if part else "")
    return text + " " * (width - len(text))


# --- treemap ---------------------------------------------------------------------------------

def squarify(weights, x, y, w, h):
    """Squarified treemap (Bruls et al.): `weights`, largest first and summing to w*h, become
    rectangles (x, y, w, h) in the same order, each row laid along the shorter side."""
    out = []
    i, n = 0, len(weights)
    while i < n:
        short = min(w, h)
        if short <= 0:
            out.extend((x, y, 0.0, 0.0) for _ in range(n - i))
            break
        first = weights[i]
        s = first
        s2 = short * short
        worst = max(s2 * first / (s * s), (s * s) / (s2 * first)) if first > 0 else math.inf
        j = i + 1
        while j < n:
            a = weights[j]
            t = s + a
            new = max(s2 * first / (t * t), (t * t) / (s2 * a)) if a > 0 else math.inf
            if new > worst:
                break
            s, worst, j = t, new, j + 1
        if w >= h:
            cw = s / h if h else 0
            yy = y
            for a in weights[i:j]:
                rh = a / cw if cw else 0
                out.append((x, yy, cw, rh))
                yy += rh
            x += cw
            w -= cw
        else:
            rh = s / w if w else 0
            xx = x
            for a in weights[i:j]:
                rw = a / rh if rh else 0
                out.append((xx, y, rw, rh))
                xx += rw
            y += rh
            h -= rh
        i = j
    return out


def ridge(s, x0, y0, x1, y1, h):
    s0, s1, s2, s3 = s
    if x1 > x0:
        s1 += 4 * h * (x1 + x0) / (x1 - x0)
        s0 -= 4 * h / (x1 - x0)
    if y1 > y0:
        s3 += 4 * h * (y1 + y0) / (y1 - y0)
        s2 -= 4 * h / (y1 - y0)
    return (s0, s1, s2, s3)


CUBE = (0, 95, 135, 175, 215, 255)


def xterm(rgb, colors=256):
    """The nearest xterm colour number: 256-colour cube or grey ramp, or one of 8 primaries."""
    r, g, b = rgb
    if colors < 256:
        return (1 if r > 127 else 0) | (2 if g > 127 else 0) | (4 if b > 127 else 0)

    def near(c):
        return min(range(6), key=lambda k: abs(CUBE[k] - c))

    ci = near(r), near(g), near(b)
    cube_i = 16 + 36 * ci[0] + 6 * ci[1] + ci[2]
    if max(r, g, b) - min(r, g, b) > 24:
        return cube_i                  # a coloured shade must not fall onto the grey ramp
    cube = tuple(CUBE[k] for k in ci)
    gray = max(0, min(23, round((r + g + b) / 3 - 8) // 10))
    gv = 8 + gray * 10
    dc = sum((a - c) ** 2 for a, c in zip((r, g, b), cube))
    dg = sum((a - gv) ** 2 for a in (r, g, b))
    return cube_i if dc <= dg else 232 + gray


def xterm_rgb(n):
    if n >= 232:
        return (8 + (n - 232) * 10,) * 3
    if n >= 16:
        n -= 16
        return CUBE[n // 36], CUBE[n // 6 % 6], CUBE[n % 6]
    return tuple(255 if n & bit else 0 for bit in (1, 2, 4))


def color_mode():
    """'rgb' for 24-bit colour, else '256', '8' or None. curses cannot tell 24-bit apart."""
    if not curses.has_colors() or curses.COLORS < 8:
        return None
    if (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
            or os.environ.get("TERM_PROGRAM") in ("ghostty", "iTerm.app", "WezTerm")):
        return "rgb"
    return "256" if curses.COLORS >= 256 else "8"


class Inks:
    """The treemap's colours. The map is written to the terminal directly rather than through
    curses, because curses attributes can only name colour pairs up to 255 and a shaded map of
    half-block pixels needs thousands of foreground/background combinations. An ink is an index
    into `rgb` and the SGR parameters for it, `fg` and `bg`."""

    def __init__(self, mode):
        self.mode = mode
        self.rgb, self.fg, self.bg = [], [], []
        self.ids = {}
        self.tables = {}

    def ink(self, rgb):
        rgb = tuple(max(0, min(255, round(c))) for c in rgb)
        if self.mode == "rgb":
            key = rgb
        else:
            key = xterm(rgb, 256 if self.mode == "256" else 8)
        i = self.ids.get(key)
        if i is None:
            i = self.ids[key] = len(self.rgb)
            if self.mode == "rgb":
                self.rgb.append(rgb)
                self.fg.append("38;2;%d;%d;%d" % rgb)
                self.bg.append("48;2;%d;%d;%d" % rgb)
            elif self.mode == "256":
                self.rgb.append(xterm_rgb(key))
                self.fg.append(f"38;5;{key}")
                self.bg.append(f"48;5;{key}")
            else:
                self.rgb.append(xterm_rgb(key))
                self.fg.append(f"3{key}")
                self.bg.append(f"4{key}")
        return i

    def shade_table(self, rgb):
        """SHADES inks from dark to bright for one base colour; 8 colours get no shading."""
        table = self.tables.get(rgb)
        if table is None:
            top = AMBIENT + DIFFUSE
            if self.mode == "8":
                table = [self.ink(rgb)] * SHADES
            else:
                table = [self.ink(tuple(c * (k / (SHADES - 1)) * top for c in rgb)) for k in range(SHADES)]
            self.tables[rgb] = table
        return table

    def text_on(self, ink):
        """Black or white text, whichever reads on `ink`."""
        r, g, b = self.rgb[ink]
        return self.ink((0, 0, 0) if 0.3 * r + 0.59 * g + 0.11 * b > 120 else (255, 255, 255))


class Treemap:
    """A cushion treemap of `root` on a w x h grid of half-cell pixels (two per character cell,
    which makes them about square). `pix` holds inks, `who` the node under each pixel and
    `rects` the rectangle of every node that was laid out."""

    def __init__(self, root, w, h, color_of, inks):
        self.root, self.w, self.h = root, w, h
        self.color_of = color_of
        self.inks = inks
        self.pix = [0] * (w * h)
        self.who = [root] * (w * h)
        self.rects = {}
        self.lay(root, 0.0, 0.0, float(w), float(h), (0.0, 0.0, 0.0, 0.0), CUSHION_H)

    def lay(self, node, x0, y0, x1, y1, s, h):
        s = ridge(s, x0, y0, x1, y1, h)
        self.rects[node] = (x0, y0, x1, y1)
        area = (x1 - x0) * (y1 - y0)
        kids = node.kids
        if kids and area >= 1:
            ordered = sorted((k for k in kids if k.size > 0), key=lambda k: k.size, reverse=True)
            total = sum(k.size for k in ordered)
            if total > 0:
                scale = area / total
                weights = []
                for k in ordered:
                    wgt = k.size * scale
                    if wgt < LUMP_PX:
                        break
                    weights.append(wgt)
                objs = ordered[:len(weights)]
                rest = area - sum(weights)
                if len(objs) < len(ordered) and rest > 0:
                    weights.append(rest)
                    objs.append(None)
                for obj, (rx, ry, rw, rh) in zip(objs, squarify(weights, x0, y0, x1 - x0, y1 - y0)):
                    if obj is None:
                        self.paint(rx, ry, rx + rw, ry + rh, ridge(s, rx, ry, rx + rw, ry + rh, h * CUSHION_F),
                                   FOLDER, node)
                    else:
                        self.lay(obj, rx, ry, rx + rw, ry + rh, s, h * CUSHION_F)
                return
        self.paint(x0, y0, x1, y1, s, self.color_of(node), node)

    def paint(self, x0, y0, x1, y1, s, rgb, node):
        w = self.w
        ix0, ix1 = max(0, math.ceil(x0 - 0.5)), min(w, math.ceil(x1 - 0.5))
        iy0, iy1 = max(0, math.ceil(y0 - 0.5)), min(self.h, math.ceil(y1 - 0.5))
        if ix0 >= ix1 or iy0 >= iy1:
            return
        table = self.inks.shade_table(rgb)
        s0, s1, s2, s3 = s
        lx, ly, lz = LIGHT
        top = SHADES - 1
        k = top / (AMBIENT + DIFFUSE)
        pix, who = self.pix, self.who
        nxs = [-(2 * s0 * (ix + 0.5) + s1) for ix in range(ix0, ix1)]
        for iy in range(iy0, iy1):
            ny = -(2 * s2 * (iy + 0.5) + s3)
            ny2 = ny * ny + 1
            lyz = ny * ly + lz
            base = iy * w + ix0
            for i, nx in enumerate(nxs):
                cos = (nx * lx + lyz) / math.sqrt(nx * nx + ny2)
                v = AMBIENT + DIFFUSE * cos if cos > 0 else AMBIENT
                pix[base + i] = table[min(top, int(v * k))]
            who[base:base + len(nxs)] = [node] * len(nxs)

    def pixel_box(self, node):
        r = self.rects.get(node)
        if r is None:
            return None
        x0, y0, x1, y1 = r
        ix0, ix1 = max(0, math.ceil(x0 - 0.5)), min(self.w, math.ceil(x1 - 0.5))
        iy0, iy1 = max(0, math.ceil(y0 - 0.5)), min(self.h, math.ceil(y1 - 0.5))
        if ix0 >= ix1 or iy0 >= iy1:
            # too small to own a pixel: mark the one its centre falls in
            cx = min(self.w - 1, max(0, int((x0 + x1) / 2)))
            cy = min(self.h - 1, max(0, int((y0 + y1) / 2)))
            return cx, cy, cx + 1, cy + 1
        return ix0, iy0, ix1, iy1


# --- the app ---------------------------------------------------------------------------------

class App:
    def __init__(self, stdscr, path):
        self.stdscr = stdscr
        self.path = path
        self.scan = Scan(path)
        self.view = 0
        self.sel = self.scan.root
        self.open = {self.scan.root}
        self.type_i = 0
        self.big_i = 0
        self.tops = [0, 0, 0]          # first visible row, per view
        self.show_map = True
        self.message = ""
        self.message_at = 0
        self.confirm = None            # node waiting for y/n before going to the Trash
        self.colors = {}               # extension -> PALETTE index
        self.mode = color_mode()
        self.inks = Inks(self.mode)
        self.pairs = {}                # xterm colour -> curses attribute, for the swatches
        self.treemap = None
        self.map_key = None
        self.map_size = -1
        self.map_at = 0
        self.map_top = 0               # screen row of the map, for mouse clicks
        self.map_geom = None           # (row, rows, width) of the map on screen
        self.map_cells = None          # what is on the terminal there now
        self.map_next = None           # what draw_map() wants there, written after refresh
        self.list_top = 0
        self.list_rows = 0
        self.rows = []                 # what the list shows: nodes, or extensions
        self.last_click = (0, -1)
        self.frozen = False

    # -- data --

    def tree_rows(self):
        rows, stack = [], [(self.scan.root, 0)]
        while stack:
            node, depth = stack.pop()
            rows.append((node, depth))
            if node.kids and node in self.open:
                kids = sorted(node.kids, key=lambda k: (-k.size, k.name))
                stack.extend((k, depth + 1) for k in reversed(kids))
        return rows

    def type_rows(self):
        return sorted(((ext, b, c) for ext, (b, c) in list(self.scan.ext.items()) if c > 0),
                      key=lambda t: -t[1])

    def update_colors(self):
        top = [ext for ext, _, _ in self.type_rows()[:len(PALETTE)]]
        keep = {ext: i for ext, i in self.colors.items() if ext in top}
        free = [i for i in range(len(PALETTE)) if i not in keep.values()]
        for ext in top:
            if ext not in keep:
                keep[ext] = free.pop(0)
        self.colors = keep

    def color_of(self, node, focus=None):
        if node.kids is not None:
            return FOLDER if focus is None else UNFOCUSED
        ext = ext_of(node.name)
        if focus is not None and ext != focus:
            return UNFOCUSED
        return self.ext_color(ext)

    def ext_color(self, ext):
        i = self.colors.get(ext)
        return PALETTE[i] if i is not None else OTHER

    def selected(self):
        """The node the keys act on, in whichever view."""
        if self.view == 0:
            return self.sel
        if self.view == 2:
            big = self.scan.largest()
            if big:
                return big[min(self.big_i, len(big) - 1)][2]
        return None

    def focus_ext(self):
        if self.view == 1 and self.rows:
            return self.rows[min(self.type_i, len(self.rows) - 1)][0]
        return None

    def map_root(self):
        if self.view == 0 and self.sel.parent is not None:
            return self.sel.parent
        return self.scan.root

    def reveal(self, node):
        """Select `node` in the tree, opening the folders above it."""
        n = node.parent
        while n is not None:
            self.open.add(n)
            n = n.parent
        self.sel = node
        self.view = 0

    def say(self, text):
        self.message = text
        self.message_at = time.monotonic()

    # -- drawing --

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    def swatch(self, rgb):
        """A curses attribute for a colour, for the few places curses itself draws one."""
        n = xterm(rgb, curses.COLORS)
        attr = self.pairs.get(n)
        if attr is None:
            pair = 16 + len(self.pairs)
            if pair > 255:
                return 0
            curses.init_pair(pair, n, -1)
            attr = self.pairs[n] = curses.color_pair(pair)
        return attr

    def draw(self):
        scr = self.stdscr
        scr.erase()
        h, w = scr.getmaxyx()
        self.map_next = None
        if h < MIN_H or w < MIN_W:
            self.set_map_geom(None)
            self.put(0, 0, f"Window too small: {w}x{h}, needs {MIN_W}x{MIN_H}")
            scr.refresh()
            return
        scan = self.scan
        if not scan.busy and not self.frozen:
            gc.freeze()                # the tree is permanent: keep the collector off it
            gc.enable()
            self.frozen = True
        self.draw_header(w)
        map_on = self.show_map and h >= MAP_MIN_H and self.mode is not None
        body = h - 5
        list_h = max(3, body * 9 // 20) if map_on else body
        self.list_top, self.list_rows = 4, list_h
        self.draw_list(4, list_h, w)
        if map_on:
            self.draw_map(4 + list_h, h - 1 - (4 + list_h), w)
        else:
            self.set_map_geom(None)
        self.draw_footer(h - 1, w)
        scr.refresh()
        if self.map_next is not None:
            self.write_map()

    def set_map_geom(self, geom):
        """Curses believes the map's cells are blank, so when the map moves or goes away it would
        leave the old pixels behind: have it repaint the whole screen instead."""
        if geom != self.map_geom:
            self.map_geom = geom
            self.map_cells = None
            self.stdscr.clearok(True)

    def write_map(self):
        """Put the map on the terminal ourselves, after curses has drawn the rest. Only cells
        that differ from what is there already are sent, between a cursor save and restore so
        curses does not lose track of where it left the cursor."""
        y0, rows, w = self.map_geom
        cells, old = self.map_next, self.map_cells
        fg_sgr, bg_sgr = self.inks.fg, self.inks.bg
        out = ["\0337"]
        fg = bg = None
        for r in range(rows):
            base = r * w
            c = 0
            while c < w:
                if old is not None and old[base + c] == cells[base + c]:
                    c += 1
                    continue
                if cells[base + c][0] is None:     # second half of a wide character
                    c -= 1
                out.append(f"\033[{y0 + r + 1};{c + 1}H")
                while True:
                    ch, cf, cb = cells[base + c]
                    c += 1
                    if ch is not None:
                        if ch != " " and cf != fg:
                            out.append(f"\033[{fg_sgr[cf]}m")
                            fg = cf
                        if cb != bg:
                            out.append(f"\033[{bg_sgr[cb]}m")
                            bg = cb
                        out.append(ch)
                    if c >= w:
                        break
                    nxt = cells[base + c]
                    if old is not None and old[base + c] == nxt and nxt[0] is not None:
                        break
        if len(out) == 1:
            return
        out.append("\033[0m\0338")
        data = "".join(out).encode("utf-8")
        fd = sys.stdout.fileno()
        while data:
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                time.sleep(0.01)
        self.map_cells = cells

    def draw_header(self, w):
        scan = self.scan
        dim, accent = curses.color_pair(5), curses.color_pair(1)
        if scan.busy:
            took = time.monotonic() - scan.started
            status = (f"scanning  {scan.files:,} files  {scan.dirs:,} folders  {fmt_secs(took)}  "
                      f"{clean(scan.current)}")
        else:
            status = f"{scan.files:,} files in {scan.dirs:,} folders, scanned in {fmt_secs(scan.elapsed or 0)}"
        title = f" Disk usage  {clean(scan.path)}  "
        self.put(0, 0, title, accent | curses.A_BOLD)
        self.put(0, cwidth(title), status, curses.color_pair(3) if scan.busy else dim)

        cap, free = volume(scan.path)
        used = max(0, cap - free)
        seen = min(scan.root.size, used)
        # Scanning / , what was not seen is unreadable folders and the other APFS volumes;
        # scanning a folder, it is mostly everything outside it.
        unseen = "not seen" if scan.path == "/" else "elsewhere"
        text = (f" {fmt_size(seen)} here   {fmt_size(used - seen)} {unseen}   {fmt_size(free)} free"
                f"   of {fmt_size(cap)}")
        bw = max(10, min(50, w - cwidth(text) - 3))
        x = 1
        if cap:
            cells = [round(seen / cap * bw), round(used / cap * bw)]
            for ch, n, attr in (("█", cells[0], accent), ("▒", cells[1] - cells[0], dim),
                                ("░", bw - cells[1], dim)):
                if n > 0:
                    self.put(1, x, ch * n, attr, n)
                    x += n
        self.put(1, 1 + bw, text)

        x = 1
        for i, name in enumerate(VIEWS):
            label = f" {i + 1} {name} "
            self.put(2, x, label, curses.A_REVERSE | curses.A_BOLD if i == self.view else dim)
            x += cwidth(label) + 1

    def columns(self, w):
        """Column widths. In a wide window the name stops at NAME_MAX and the bar takes what it
        can up to BAR_MAX, so the numbers stay within reach of the names."""
        size_w, pct_w = 9, 7
        files_w = 12 if w >= 90 else 0
        bar_w = max(6, min(24, w // 7))
        name_w = w - size_w - pct_w - files_w - bar_w - 4
        if name_w > NAME_MAX:
            bar_w = min(BAR_MAX, bar_w + name_w - NAME_MAX)
            name_w = NAME_MAX
        return name_w, bar_w, pct_w, size_w, files_w

    def draw_list(self, y0, rows, w):
        dim = curses.color_pair(5)
        name_w, bar_w, pct_w, size_w, files_w = self.columns(w)
        if self.view == 0:
            heads = ("Name", "Share of folder", "", "Size", "Files")
            self.rows = self.tree_rows()
            idx = next((i for i, (n, _) in enumerate(self.rows) if n is self.sel), 0)
            self.sel = self.rows[idx][0]
        elif self.view == 1:
            heads = ("Type", "Share of scan", "", "Size", "Files")
            self.rows = self.type_rows()
            idx = self.type_i = max(0, min(self.type_i, len(self.rows) - 1))
        else:
            heads = ("Path", "", "", "Size", "")
            self.rows = self.scan.largest()
            idx = self.big_i = max(0, min(self.big_i, len(self.rows) - 1))
        if self.view == 2:
            self.put(y0 - 1, 1, fit(heads[0], w - size_w - 3) + fit(heads[3], size_w), dim | curses.A_BOLD)
        else:
            head = (fit(heads[0], name_w) + " " + fit(heads[1], bar_w + pct_w) + " "
                    + f"{heads[3]:>{size_w}}" + (f"{heads[4]:>{files_w}}" if files_w else ""))
            self.put(y0 - 1, 1, head, dim | curses.A_BOLD)

        top = self.tops[self.view]
        if idx < top:
            top = idx
        elif idx >= top + rows:
            top = idx - rows + 1
        top = max(0, min(top, max(0, len(self.rows) - rows)))
        self.tops[self.view] = top

        total = self.scan.root.size or 1
        for r in range(rows):
            i = top + r
            if i >= len(self.rows):
                break
            y = y0 + r
            mark = i == idx
            if self.view == 0:
                self.draw_tree_row(y, self.rows[i], mark, w)
            elif self.view == 1:
                ext, b, c = self.rows[i]
                self.put(y, 1, "██", self.swatch(self.ext_color(ext)))
                label = f".{ext}" if ext else "(no extension)"
                text = (fit(label, name_w - 3) + " " + bar(b / total, bar_w) + f"{b / total:>{pct_w}.1%}"
                        + " " + f"{fmt_size(b):>{size_w}}" + (f"{c:>{files_w},}" if files_w else ""))
                self.put(y, 4, text, curses.A_REVERSE if mark else 0)
            else:
                size, _, node = self.rows[i]
                rel = os.path.relpath(node.path(), self.scan.path)
                text = fit_left(clean(rel), w - size_w - 3) + f"{fmt_size(size):>{size_w}}"
                self.put(y, 1, text, curses.A_REVERSE if mark else 0)
        if not self.rows:
            self.put(y0, 2, "Nothing yet." if self.scan.busy else "Nothing here.", dim)

    def draw_tree_row(self, y, row, mark, w):
        node, depth = row
        name_w, bar_w, pct_w, size_w, files_w = self.columns(w)
        parent = node.parent
        if node.is_dir:
            arrow = "▾ " if node in self.open and node.kids else "▸ " if node.kids else "  "
        else:
            arrow = "  "
        name = clean(node.name) + ("/" if node.is_dir and node.parent is not None else "")
        note = NOTES.get(node.state)
        if note is None and node.denied:
            note = f"{node.denied:,} unreadable inside"
        label = "  " * depth + arrow + name
        if parent is not None:
            share = node.size / parent.size if parent.size else 0
        else:
            cap, free = volume(self.scan.path)
            share = node.size / (cap - free) if cap > free else 0
        attr = curses.A_REVERSE if mark else 0
        base = attr | (curses.A_BOLD if node.is_dir else 0)
        self.put(y, 1, fit(label, name_w), base | (curses.color_pair(5) if node.state > DONE else 0))
        if note and cwidth(label) + 4 < name_w:
            x = 1 + cwidth(label) + 2
            note = f"({note})"
            self.put(y, x, note, attr | curses.color_pair(4 if node.state == DENIED or node.denied else 5),
                     min(cwidth(note), name_w - x + 1))
        x = 2 + name_w
        self.put(y, x, bar(share, bar_w), attr | curses.color_pair(1))
        x += bar_w
        self.put(y, x, f"{share:>{pct_w}.1%} ", attr)
        x += pct_w + 1
        scanning = node.is_dir and node.todo > 0
        self.put(y, x, f"{fmt_size(node.size):>{size_w}}", attr | (curses.color_pair(3) if scanning else 0))
        x += size_w
        if files_w:
            self.put(y, x, f"{node.files:>{files_w},}" if node.is_dir else " " * files_w, attr | curses.color_pair(5))

    def layout_map(self, w, h):
        root = self.map_root()
        focus = self.focus_ext()
        key = (root, w, h, focus, self.scan is not None and id(self.scan))
        now = time.monotonic()
        stale = root.size != self.map_size
        if key != self.map_key or (stale and (not self.scan.busy or now - self.map_at >= MAP_REFRESH)):
            self.update_colors()
            self.treemap = Treemap(root, w, h, lambda n: self.color_of(n, focus), self.inks)
            self.map_key, self.map_size, self.map_at = key, root.size, now
        return self.treemap

    def draw_map(self, y0, rows, w):
        """The map's title through curses; the pixels into `map_next`, for write_map()."""
        dim = curses.color_pair(5)
        root = self.map_root()
        title = f"─ {clean(root.path())}  {fmt_size(root.size)} "
        if self.view == 1 and self.focus_ext() is not None:
            ext = self.focus_ext()
            title += f"· showing {'.' + ext if ext else 'files without an extension'} "
        self.put(y0, 0, title + "─" * max(0, w - cwidth(title)), dim)
        y0 += 1
        rows -= 1
        self.map_top = y0
        if rows < 2:
            self.set_map_geom(None)
            return
        self.set_map_geom((y0, rows, w))
        tm = self.layout_map(w, rows * 2)
        hi = self.inks.ink(HIGHLIGHT)
        pix = list(tm.pix)
        sel = self.selected()
        box = tm.pixel_box(sel) if sel is not None and self.view != 1 else None
        if box and not (sel is root and self.view == 0):
            x0, py0, x1, py1 = box
            for x in range(x0, x1):
                pix[py0 * w + x] = pix[(py1 - 1) * w + x] = hi
            for y in range(py0, py1):
                pix[y * w + x0] = pix[y * w + x1 - 1] = hi
        cells = []
        for r in range(rows):
            top = pix[2 * r * w:(2 * r + 1) * w]
            bot = pix[(2 * r + 1) * w:(2 * r + 2) * w]
            cells.extend((" ", t, t) if t == b else ("▀", t, b) for t, b in zip(top, bot))
        self.draw_labels(tm, cells, rows, w, root, sel)
        self.map_next = cells

    def draw_labels(self, tm, cells, rows, w, root, sel):
        """Names over the rectangles of the map root's children (and the selection) where they fit."""
        inks = self.inks
        kids = [k for k in (root.kids or []) if k in tm.rects]
        if sel is not None and sel is not root and sel not in kids and self.view != 1:
            kids.append(sel)
        for node in kids:
            x0, py0, x1, py1 = tm.pixel_box(node)
            r0 = (py0 + 1) // 2        # first cell row with both pixels inside
            width = x1 - x0 - 2
            if 2 * r0 + 1 >= py1 or r0 >= rows or width < 4:
                continue
            name = "".join(ch for ch in clean(node.name) if cwidth(ch))
            label = f"{name} {fmt_size(node.size)}"
            if cwidth(label) > width:
                label = name
            text = fit(label, min(width, cwidth(label))).rstrip(" ")
            if node is sel:
                fg, bg = inks.ink((0, 0, 0)), inks.ink(HIGHLIGHT)
            else:
                bg = cells[r0 * w + x0 + 1][2]
                fg = inks.text_on(bg)
            x = x0 + 1
            for ch in text:
                cw = cwidth(ch)
                if x + cw > x1 - 1:
                    break
                cells[r0 * w + x] = (ch, fg, bg)
                if cw == 2:
                    cells[r0 * w + x + 1] = (None, fg, bg)
                x += cw

    def draw_footer(self, y, w):
        if self.confirm is not None:
            n = self.confirm
            self.put(y, 0, f" Move “{clean(n.name)}” ({fmt_size(n.size)}) to the Trash?  y = yes, any other key = no",
                     curses.color_pair(4) | curses.A_BOLD)
            return
        if self.message and time.monotonic() - self.message_at < 6:
            self.put(y, 0, " " + self.message, curses.color_pair(3))
            return
        keys = "↑↓ move  ←→ close/open  Tab view  o Finder  t trash  r rescan  m map  q quit"
        self.put(y, 0, " " + keys, curses.color_pair(5))
        if not self.scan.busy and self.scan.denied:
            hint = (f"{self.scan.denied:,} folders unreadable: give the terminal Full Disk Access"
                    " (System Settings › Privacy & Security) and reopen it")
            x = cwidth(keys) + 4
            if w - x < 30:
                return
            self.put(y, x, hint, curses.color_pair(4))

    # -- actions --

    def move(self, delta):
        if self.view == 0:
            rows = self.tree_rows()
            idx = next((i for i, (n, _) in enumerate(rows) if n is self.sel), 0)
            self.sel = rows[max(0, min(len(rows) - 1, idx + delta))][0]
        elif self.view == 1:
            self.type_i = max(0, self.type_i + delta)
        else:
            self.big_i = max(0, self.big_i + delta)

    def right(self):
        n = self.sel
        if not n.kids:
            return
        if n in self.open:
            self.sel = max(n.kids, key=lambda k: (k.size, k.name))
        else:
            self.open.add(n)

    def left(self):
        n = self.sel
        if n in self.open and n.kids:
            self.open.discard(n)
        elif n.parent is not None:
            self.sel = n.parent

    def trash(self, node):
        self.confirm = None
        path = node.path()
        if os.path.exists(TRASH):
            cmd = [TRASH, path]
        else:
            cmd = ["osascript", "-e", 'on run argv', "-e",
                   'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
                   "-e", "end run", path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.say(f"Could not move to the Trash: {e}")
            return
        if res.returncode != 0 or os.path.lexists(path):
            err = (res.stderr or res.stdout).strip().splitlines()
            self.say(f"Could not move to the Trash: {err[-1] if err else 'it is still there'}")
            return
        parent = node.parent
        siblings = sorted(parent.kids, key=lambda k: (-k.size, k.name))
        i = siblings.index(node)
        self.scan.detach(node)
        rest = [k for k in siblings if k is not node]
        if self.view == 0:
            self.sel = rest[min(i, len(rest) - 1)] if rest else parent
        self.say(f"Moved {clean(node.name)} ({fmt_size(node.size)}) to the Trash; empty the Trash to free the space")

    def rescan(self):
        """The selected folder in the Tree view (the file's folder, for a file); everything in the
        other two, whose rows span the whole tree."""
        node = self.scan.root
        if self.view == 0:
            node = self.sel if self.sel.is_dir else self.sel.parent
        if node.state in (OTHER_VOLUME, ALIAS):
            self.say(f"{clean(node.name)} is not scanned: {NOTES[node.state]}")
            return
        if node.parent is None:
            self.scan.stop()
            self.scan = Scan(self.path)
            gc.disable()
            self.frozen = False
            self.sel = self.scan.root
            self.open = {self.scan.root}
            self.say("Rescanning everything")
            return
        was_open = node in self.open
        fresh = self.scan.rescan(node)
        self.open.discard(node)
        if fresh is None:
            self.sel = node.parent
            self.say(f"{clean(node.name)} is gone")
            return
        if was_open:
            self.open.add(fresh)
        if self.view == 0:
            self.sel = fresh
        self.say(f"Rescanning {clean(fresh.path())}")

    def click(self, mx, my, double):
        if self.list_top <= my < self.list_top + self.list_rows:
            i = self.tops[self.view] + my - self.list_top
            if i >= len(self.rows):
                return
            if self.view == 0:
                self.sel = self.rows[i][0]
                if double and self.sel.kids:
                    self.open.symmetric_difference_update({self.sel})
            elif self.view == 1:
                self.type_i = i
            else:
                self.big_i = i
                if double:
                    self.reveal(self.rows[i][2])
            return
        tm = self.treemap
        if (tm is None or self.map_geom is None or not (self.map_top <= my < self.map_top + tm.h // 2)
                or mx >= tm.w):
            return
        leaf = tm.who[(my - self.map_top) * 2 * tm.w + mx]
        if self.view != 0:
            self.reveal(leaf)
            return
        # one level deeper per click: the item under the pointer just below the selection
        chain, n = [], leaf
        while n is not None and n is not tm.root:
            chain.append(n)
            n = n.parent
        chain.reverse()
        if not chain:
            return
        k = next((i for i, c in enumerate(chain) if c is self.sel), -1)
        self.reveal(chain[k + 1] if 0 <= k < len(chain) - 1 else chain[0])

    def handle(self, ch):
        if self.confirm is not None:
            if ch in (ord("y"), ord("Y")):
                self.trash(self.confirm)
            else:
                self.confirm = None
                self.say("Kept it")
            return True
        page = max(1, self.list_rows - 1)
        # Not ESC: a mouse or key sequence ncurses doesn't know arrives as a bare ESC plus junk.
        if ch == ord("q"):
            return False
        elif ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif ch == curses.KEY_PPAGE:
            self.move(-page)
        elif ch == curses.KEY_NPAGE:
            self.move(page)
        elif ch == curses.KEY_HOME:
            self.move(-10 ** 9)
        elif ch == curses.KEY_END:
            self.move(10 ** 9)
        elif ch in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER):
            if self.view == 0:
                self.right()
            elif self.view == 2 and self.selected() is not None:
                self.reveal(self.selected())
        elif ch in (curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127, 8):
            if self.view == 0:
                self.left()
        elif ch == 9:
            self.view = (self.view + 1) % len(VIEWS)
        elif ch == curses.KEY_BTAB:
            self.view = (self.view - 1) % len(VIEWS)
        elif ch in (ord("1"), ord("2"), ord("3")):
            self.view = ch - ord("1")
        elif ch == curses.KEY_RESIZE:
            self.map_cells = None
            self.stdscr.clearok(True)
        elif ch == ord("m"):
            self.show_map = not self.show_map
        elif ch == ord("o"):
            node = self.selected()
            if node is not None:
                try:
                    subprocess.run(["open", "-R", node.path()], capture_output=True, timeout=10)
                    self.say(f"Shown in Finder: {clean(node.path())}")
                except (OSError, subprocess.TimeoutExpired) as e:
                    self.say(f"Could not open Finder: {e}")
        elif ch in (ord("t"), curses.KEY_DC):
            node = self.selected()
            if node is None:
                self.say("Pick a file or folder in the Tree or Largest files view")
            elif self.scan.busy:
                self.say("Wait for the scan to finish before moving things to the Trash")
            elif node.parent is None:
                self.say("That is the folder being scanned")
            else:
                self.confirm = node
        elif ch == ord("r"):
            if self.scan.busy:
                self.say("Already scanning")
            else:
                self.rescan()
        elif ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                return True
            if bstate & curses.BUTTON4_PRESSED:
                self.move(-3)
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000):
                self.move(3)
            elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                now = time.monotonic()
                double = self.last_click[1] == my and now - self.last_click[0] < 0.4
                self.last_click = (now, my)
                self.click(mx, my, double)
        return True


def run(stdscr, path):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, bars
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # scanning, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # the Trash prompt
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    gc.disable()                   # millions of nodes, no cycles worth collecting mid-scan
    app = App(stdscr, path)
    while True:
        stdscr.timeout(TICK_MS if app.scan.busy else 1000)
        app.draw()
        ch = stdscr.getch()
        if ch == -1:
            continue
        if not app.handle(ch):
            return


def print_once(path, limit):
    gc.disable()
    scan = Scan(path)
    tty = sys.stderr.isatty()
    while scan.busy:
        time.sleep(0.2)
        if tty:
            print(f"\r{scan.files:,} files, {fmt_size(scan.root.size)}…", end="", file=sys.stderr)
    if tty:
        print("\r\033[K", end="", file=sys.stderr)
    root = scan.root
    cap, free = volume(path)
    print(f"{path}: {fmt_size(root.size)} in {scan.files:,} files, {scan.dirs:,} folders "
          f"({fmt_secs(scan.elapsed)}); volume {fmt_size(cap - free)} used, {fmt_size(free)} free")
    if scan.denied:
        print(f"{scan.denied:,} folders could not be read")
    print()
    for kid in sorted(root.kids, key=lambda k: -k.size)[:limit]:
        note = NOTES.get(kid.state)
        share = kid.size / root.size if root.size else 0
        print(f"{fmt_size(kid.size):>9} {share:6.1%}  {clean(kid.name)}{'/' if kid.is_dir else ''}"
              + (f"  ({note})" if note else ""))
    print("\nLargest files")
    for size, _, node in scan.largest()[:limit]:
        print(f"{fmt_size(size):>9}  {clean(node.path())}")
    print("\nBy type")
    for ext, (b, c) in sorted(scan.ext.items(), key=lambda t: -t[1][0])[:limit]:
        print(f"{fmt_size(b):>9}  {c:>10,}  {'.' + ext if ext else '(no extension)'}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="What is filling the disk: a folder tree by size, a treemap and file types.")
    parser.add_argument("path", nargs="?", default="/",
                        help="folder to scan (default: / , the whole disk)")
    parser.add_argument("--once", action="store_true",
                        help="scan, print the biggest items, largest files and types, and exit")
    parser.add_argument("--top", type=int, default=20, metavar="N", help="rows per list with --once")
    args = parser.parse_args()
    path = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.isdir(path):
        parser.error(f"not a folder: {args.path}")
    if args.once:
        raise SystemExit(print_once(path, args.top))
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, path)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
