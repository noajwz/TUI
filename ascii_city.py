#!/usr/bin/env python3
"""
ascii_city.py - an endless, procedurally generated ASCII city, seen two ways.

    skyline   the city from outside: parallax layers sliding past as you walk
    street    the city from inside: an avenue receding to a vanishing point,
              neon on the facades, someone smoking on the sidewalk

Controls:
    tab / v                     switch view
    space                       toggle autopilot
    q                           quit

    skyline   left / right    or  h / l   walk           H / L   run
    street    up / down       or  w / s   walk forward   W / S   run
              left / right    or  a / d   cross the street

No dependencies on Linux/macOS (curses ships with Python).
On Windows:  pip install windows-curses
"""

import curses
import math
import random
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FPS = 30
KEY_GRACE = 0.15       # how long a keypress keeps you moving (smooths key repeat)

# --- skyline view ---
CHUNK_W = 200          # how many "world columns" one generated chunk covers
WALK_SPEED = 20.0      # world columns per second
RUN_MULTIPLIER = 3.0

# Each layer is one depth plane of the city.
# parallax: 0.0 = infinitely far away (never moves), 1.0 = right next to you.
# h and w are (min, max); h is a fraction of the available screen height.
LAYERS = [
    dict(parallax=0.20, h=(0.20, 0.45), w=(5, 12),  gap=(1, 4), palette="far"),
    dict(parallax=0.45, h=(0.30, 0.70), w=(7, 16),  gap=(1, 5), palette="mid"),
    dict(parallax=1.00, h=(0.25, 0.98), w=(9, 24),  gap=(2, 7), palette="near"),
]

# --- street view ---
# World axes: x runs across the street (0 = centre line), y up (0 = road
# surface), z away from you down the avenue.
STREET_HALF = 7.0      # the facades stand at x = +/- this
SIDEWALK_X = 5.0       # the kerb; beyond it is pavement
EYE_Y = 3.2            # how high off the road you are looking from
NEAR_Z = 0.6           # anything closer than this is behind your face
FAR_Z = 260.0
CHUNK_Z = 60.0         # world units of street one generated chunk covers
FLOOR_H = 2.4          # world height of one storey
BAY_W = 2.2            # world width of one window bay
STEP_Z = 26.0          # forward walking speed, world units per second
DRIFT_X = 7.0          # sideways speed when crossing the street
MAX_DRIFT = 5.6        # how close to a facade you may get

SMOKE_CYCLE = 7.0      # seconds between drags
DRAG_LEN = 1.2         # how long a drag lasts

WINDOW_GLYPHS = "08XZ:+=*%@o.#"

SIGN_WORDS = [
    "BAR", "OPEN", "HOTEL", "MOTEL", "PHO", "RAMEN", "SUSHI", "COFFEE",
    "24H", "LIVE", "JAZZ", "TATTOO", "PAWN", "DINER", "LAUNDRY", "EAT",
    "BEER", "ROOMS", "CLUB", "KARAOKE", "NOODLES", "LOTTO",
]

# ---------------------------------------------------------------------------
# Colour setup
#
# curses works with numbered "colour pairs" (foreground + background). We build
# one pair per colour we care about, then remember which pair number is which.
# ---------------------------------------------------------------------------

PALETTES = {}      # e.g. {"near": [3, 7, 12, ...]} -> lists of curses pair numbers
NEON = []          # [(lit, unlit), ...] - one entry per neon tube colour
STAR = STREET = HUD = CURB = HAZE = SMOKE = EMBER = EMBER_HOT = 0


def init_colors():
    global PALETTES, NEON, STAR, STREET, HUD, CURB, HAZE, SMOKE, EMBER, EMBER_HOT

    curses.start_color()
    try:
        curses.use_default_colors()   # lets us keep the terminal's own background
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    if curses.COLORS >= 256:
        # xterm-256 colour numbers. Bright saturated up front, muted in back.
        groups = {
            "near": [226, 33, 202, 48, 51, 201, 220, 39, 208, 84, 214, 45],
            "mid":  [31, 130, 100, 65, 96, 67, 137, 72],
            "far":  [24, 60, 23, 238, 59, 66],
        }
        # (tube lit, tube dark) - the dark one is also what the sign spills
        # onto the street below it.
        neon = [(198, 89), (201, 54), (51, 23), (196, 52),
                (208, 94), (46, 22), (141, 60), (226, 58)]
        extras = {"star": 254, "street": 240, "hud": 245, "curb": 246,
                  "haze": 237, "smoke": 244, "ember": 166, "ember_hot": 208}
        attrs = {}
    else:
        # 8-colour fallback: bold = the "bright" version of a colour.
        groups = {
            "near": [curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_RED,
                     curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_MAGENTA],
            "mid":  [curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_GREEN],
            "far":  [curses.COLOR_BLUE, curses.COLOR_BLACK],
        }
        neon = [(curses.COLOR_MAGENTA, curses.COLOR_MAGENTA),
                (curses.COLOR_CYAN, curses.COLOR_CYAN),
                (curses.COLOR_RED, curses.COLOR_RED),
                (curses.COLOR_YELLOW, curses.COLOR_YELLOW)]
        extras = {"star": curses.COLOR_WHITE, "street": curses.COLOR_BLACK,
                  "hud": curses.COLOR_WHITE, "curb": curses.COLOR_WHITE,
                  "haze": curses.COLOR_BLUE, "smoke": curses.COLOR_WHITE,
                  "ember": curses.COLOR_RED, "ember_hot": curses.COLOR_RED}
        attrs = {"near": curses.A_BOLD}

    pair = 1
    for name, colours in groups.items():
        ids = []
        for c in colours:
            curses.init_pair(pair, c, bg)
            ids.append(curses.color_pair(pair) | attrs.get(name, 0))
            pair += 1
        PALETTES[name] = ids

    NEON = []
    for lit, unlit in neon:
        curses.init_pair(pair, lit, bg)
        on = curses.color_pair(pair) | curses.A_BOLD
        pair += 1
        curses.init_pair(pair, unlit, bg)
        off = curses.color_pair(pair)
        pair += 1
        NEON.append((on, off))

    made = {}
    for name, c in extras.items():
        curses.init_pair(pair, c, bg)
        made[name] = curses.color_pair(pair)
        pair += 1

    STAR = made["star"]
    STREET = made["street"]
    HUD = made["hud"] | curses.A_BOLD
    CURB = made["curb"]
    HAZE = made["haze"]
    SMOKE = made["smoke"]
    EMBER = made["ember"]
    EMBER_HOT = made["ember_hot"] | curses.A_BOLD


def tone_colour(name, tone):
    """Resolve a stored 0..1 'tone' to an actual colour in one of the palettes.

    Generation stores the tone rather than the colour so that a given building
    is the same building whether the terminal has 8 colours or 256."""
    pal = PALETTES[name]
    return pal[min(len(pal) - 1, int(tone * len(pal)))]


def _mix(a, b, c):
    """A tiny deterministic hash. Unlike hash() it does not depend on the run."""
    h = ((a * 73856093) ^ (b * 19349663) ^ (c * 83492791)) & 0xFFFFFFFF
    h ^= h >> 13
    return (h * 2654435761) & 0xFFFFFFFF


# ===========================================================================
# SKYLINE VIEW - the city from outside
# ===========================================================================

# ---------------------------------------------------------------------------
# Building generation
#
# A building is just a list of strings (its rows, top to bottom) plus a colour.
# ---------------------------------------------------------------------------

def make_building(rng, avail_rows, spec):
    w = rng.randint(*spec["w"])
    h = max(3, int(avail_rows * rng.uniform(*spec["h"])))

    glyphs = rng.sample(WINDOW_GLYPHS, 2)   # each building uses its own 2 glyphs
    col_step = rng.choice([1, 2, 2, 3])     # spacing between window columns
    row_step = rng.choice([1, 1, 2])        # spacing between window rows
    density = rng.uniform(0.45, 0.90)       # how many of those slots are lit

    rows = []

    # Optional antenna / mast on the roof.
    if w > 3 and rng.random() < 0.55:
        col = rng.randrange(1, w - 1)
        mast = [rng.choice("^|H+")] + ["|"] * rng.randint(0, 3)
        for ch in mast:
            line = [" "] * w
            line[col] = ch
            rows.append("".join(line))

    rows.append("=" * w)                    # the roof line

    for r in range(1, h):
        line = []
        for c in range(w):
            if c == 0 or c == w - 1:
                line.append("|")            # bright vertical edge of the tower
            elif c % col_step == 0 and r % row_step == 0 and rng.random() < density:
                line.append(rng.choice(glyphs))
            else:
                line.append(" ")            # a dark, unlit window
        rows.append("".join(line))

    return {"w": w, "rows": rows, "color": rng.choice(PALETTES[spec["palette"]])}


# ---------------------------------------------------------------------------
# Chunks
#
# The city is infinite, so we can't generate it all up front. Instead the world
# is cut into fixed-width chunks. Chunk number 7 of layer 1 always gets the same
# random seed, so it always generates the exact same buildings - walk away and
# come back and the city is unchanged, without storing anything.
# ---------------------------------------------------------------------------

_cache = {}


def get_chunk(layer_i, chunk_i, avail_rows):
    key = (layer_i, chunk_i, avail_rows)
    if key not in _cache:
        if len(_cache) > 400:
            _cache.clear()   # walked a long way; the seeds rebuild it identically
        spec = LAYERS[layer_i]
        rng = random.Random(f"city|{layer_i}|{chunk_i}")   # the deterministic seed
        buildings = []
        x = chunk_i * CHUNK_W
        end = x + CHUNK_W
        while x < end:
            b = make_building(rng, avail_rows, spec)
            b["x"] = x
            buildings.append(b)
            x += b["w"] + rng.randint(*spec["gap"])
        _cache[key] = buildings
    return _cache[key]


# ---------------------------------------------------------------------------
# Rendering
#
# We never draw straight to the screen. We fill two grids - one of characters,
# one of colours - back to front, so nearer buildings paint over farther ones.
# Then the whole thing gets blitted in one go, which stops the picture flickering.
# ---------------------------------------------------------------------------

def blit(dst_ch, dst_co, b, sx, ground):
    rows = b["rows"]
    height = len(dst_ch)
    width = len(dst_ch[0])
    top = ground - len(rows) + 1
    for i, line in enumerate(rows):
        y = top + i
        if not (0 <= y < height):
            continue
        for j, ch in enumerate(line):
            if ch == " ":
                continue
            x = sx + j
            if 0 <= x < width:
                dst_ch[y][x] = ch
                dst_co[y][x] = b["color"]


def draw_stars(ch, co, cam_x, ground):
    width = len(ch[0])
    off = cam_x * 0.05          # stars drift very slowly = very far away
    for sx in range(width):
        rng = random.Random(f"star|{int(sx + off)}")
        if rng.random() < 0.07:
            y = rng.randrange(0, max(1, ground - 4))
            ch[y][sx] = rng.choice(".*'`")
            co[y][sx] = STAR


def draw_street(ch, co, cam_x, ground):
    height = len(ch)
    width = len(ch[0])

    # Lamp posts, at fixed world positions, drawn in front of everything.
    for wx in range(int(cam_x) - 2, int(cam_x) + width + 2):
        if wx % 17 == 0:
            sx = int(wx - cam_x)
            if 0 <= sx < width:
                for dy, glyph in ((0, "|"), (1, "|"), (2, "|"), (3, "o")):
                    y = ground - dy
                    if 0 <= y < height:
                        ch[y][sx] = glyph
                        co[y][sx] = STAR if glyph == "o" else STREET

    # The road itself, below the buildings.
    for y in range(ground + 1, height):
        for x in range(width):
            wx = int(x + cam_x)
            if y == ground + 2 and wx % 6 < 3:
                ch[y][x] = "-"            # centre line, scrolls as you walk
                co[y][x] = STREET
            elif y == ground + 1 and wx % 11 == 0:
                ch[y][x] = "."
                co[y][x] = STREET


def render_skyline(cam_x, width, height):
    ground = max(3, height - 4)          # the row the buildings stand on
    avail = ground + 1

    ch = [[" "] * width for _ in range(height)]
    co = [[0] * width for _ in range(height)]

    draw_stars(ch, co, cam_x, ground)

    for li, spec in enumerate(LAYERS):
        off = cam_x * spec["parallax"]   # nearer layers slide past faster
        first = int(off // CHUNK_W) - 1
        last = int((off + width) // CHUNK_W) + 1
        for ci in range(first, last + 1):
            for b in get_chunk(li, ci, avail):
                sx = int(round(b["x"] - off))
                if sx + b["w"] < 0 or sx >= width:
                    continue
                blit(ch, co, b, sx, ground)

    draw_street(ch, co, cam_x, ground)
    return ch, co


# ===========================================================================
# STREET VIEW - the city from inside
# ===========================================================================

# ---------------------------------------------------------------------------
# Generation
#
# Same trick as the skyline: the avenue is cut into chunks of CHUNK_Z world
# units, and chunk 7 of the left-hand side always gets the same seed, so the
# block you walked past is still exactly itself when you walk back to it.
#
# A building is a slab standing against x = +/- STREET_HALF, covering z from
# z0 to z1. Its facade is a grid of storeys (FLOOR_H tall) by bays (BAY_W
# wide); which of those cells are lit is a hash of the building seed, so
# nothing about the facade has to be stored either.
# ---------------------------------------------------------------------------

_street_cache = {}


def make_signs(rng, b):
    """Neon for one building: painted flat on the facade, hung out over the
    pavement, or both."""
    floors = max(1, int(b["height"] / FLOOR_H))
    bays = max(1, int((b["z1"] - b["z0"]) / BAY_W))

    flat = []
    if bays >= 4 and floors >= 3 and rng.random() < 0.55:
        text = rng.choice(SIGN_WORDS)[:bays - 1]
        flicker = rng.random() < 0.25
        dead = frozenset(rng.sample(range(len(text)), rng.randint(0, 1))) \
            if flicker else frozenset()
        flat.append({
            "text": text,
            "floor": rng.randrange(1, min(floors, 7)),
            "bay0": rng.randrange(0, bays - len(text) + 1),
            "tone": rng.random(),
            "flicker": flicker,
            "dead": dead,
            "period": rng.uniform(1.7, 5.0),
            "phase": rng.uniform(0.0, 5.0),
        })

    hung = []
    if rng.random() < 0.5:
        text = rng.choice(SIGN_WORDS)
        flicker = rng.random() < 0.3
        dead = frozenset(rng.sample(range(len(text)), rng.randint(0, 1))) \
            if flicker else frozenset()
        # Hang it high enough that the bottom letter clears the pavement.
        top = max(rng.uniform(6.0, 16.0), 4.0 + 1.15 * (len(text) - 1))
        hung.append({
            "text": text,
            "x": b["side"] * (STREET_HALF - 0.7),
            "z": rng.uniform(b["z0"] + 1.0, b["z1"] - 1.0),
            "top": min(top, max(4.5, b["height"] - 0.5)),
            "tone": rng.random(),
            "flicker": flicker,
            "dead": dead,
            "period": rng.uniform(1.7, 5.0),
            "phase": rng.uniform(0.0, 5.0),
        })

    return flat, hung


def make_street_building(rng, side, z0):
    depth = rng.uniform(8.0, 26.0)
    b = {
        "side": side,
        "z0": z0,
        "z1": z0 + depth,
        "height": rng.uniform(10.0, 60.0),
        "tone": rng.random(),
        "glyphs": rng.sample(WINDOW_GLYPHS, 2),
        "density": rng.uniform(0.30, 0.75),
        "seed": rng.randrange(1 << 28),
    }
    b["signs"], b["vsigns"] = make_signs(rng, b)

    # Somebody out on the pavement having a cigarette.
    b["props"] = []
    if rng.random() < 0.45:
        b["props"].append({
            "x": side * rng.uniform(5.5, 6.5),
            "z": rng.uniform(b["z0"] + 1.0, b["z1"] - 1.0),
            "side": side,
            "phase": rng.uniform(0.0, SMOKE_CYCLE),
        })
    return b


def street_chunk(side, chunk_i):
    key = (side, chunk_i)
    if key not in _street_cache:
        if len(_street_cache) > 200:
            _street_cache.clear()   # the seeds rebuild it identically
        rng = random.Random(f"street|{side}|{chunk_i}")
        out = []
        z = chunk_i * CHUNK_Z
        end = z + CHUNK_Z
        while z < end:
            b = make_street_building(rng, side, z)
            out.append(b)
            # Now and then an alley, which you can see clean through to the
            # buildings on the other side of the street.
            z = b["z1"] + (rng.uniform(2.0, 7.0) if rng.random() < 0.25 else 0.0)
        _street_cache[key] = out
    return _street_cache[key]


def building_at(side, z):
    """The building whose frontage covers world z, or None (an alley)."""
    ci = int(math.floor(z / CHUNK_Z))
    for c in (ci, ci - 1):      # a building may straddle the chunk boundary
        for b in street_chunk(side, c):
            if b["z0"] <= z < b["z1"]:
                return b
    return None


def visible_buildings(cam_z, reach):
    c0 = int(math.floor((cam_z - CHUNK_Z) / CHUNK_Z))
    c1 = int(math.floor((cam_z + reach) / CHUNK_Z))
    for side in (-1, 1):
        for ci in range(c0, c1 + 1):
            for b in street_chunk(side, ci):
                if b["z1"] > cam_z - 4.0 and b["z0"] < cam_z + reach:
                    yield b


# ---------------------------------------------------------------------------
# The view: one-point perspective, and the wall spans it produces
# ---------------------------------------------------------------------------

class View:
    """Turns world coordinates into screen cells.

    You never turn your head in this city, only translate, so the projection is
    a plain pinhole with the vanishing point fixed at (cx, horizon). Terminal
    cells are about twice as tall as they are wide, so the vertical focal
    length is half the horizontal one."""

    def __init__(self, cam_x, cam_z, width, height):
        self.cam_x = cam_x
        self.cam_z = cam_z
        self.width = width
        self.height = height
        self.horizon = max(2, min(height - 6, int(height * 0.42)))
        self.cx = (width - 1) / 2.0
        self.fx = max(8.0, width * 0.45)
        self.fy = self.fx * 0.5

    def project(self, x, y, z):
        """World point -> (screen x, screen y, distance), or None if behind you."""
        dz = z - self.cam_z
        if dz < NEAR_Z:
            return None
        return (self.cx + (x - self.cam_x) * self.fx / dz,
                self.horizon - (y - EYE_Y) * self.fy / dz,
                dz)


def wall_span(v, dz, b):
    """The rows a facade occupies in one column: its roof line and its foot."""
    scale = v.fy / dz
    return (int(math.floor(v.horizon - (b["height"] - EYE_Y) * scale)),
            int(math.ceil(v.horizon + EYE_Y * scale)))


def wall_colour(b, dz):
    """Distance fog, done with the three palettes the skyline already uses."""
    if dz < 40.0:
        return tone_colour("near", b["tone"])
    if dz < 90.0:
        return tone_colour("mid", b["tone"])
    return tone_colour("far", b["tone"])


def neon_attr(s, i, now):
    lit, dark = NEON[min(len(NEON) - 1, int(s["tone"] * len(NEON)))]
    if i in s["dead"]:
        return dark             # one letter of this sign gave up long ago
    if s["flicker"]:
        p = (now + s["phase"]) % s["period"]
        if p < 0.07 or 0.13 < p < 0.17:
            return dark
    return lit


def hidden(walls, sx, sy, dz):
    """Is this cell behind a facade? Walls are vertical planes, so one distance
    and one row range per column is all the depth information anyone needs."""
    wdz = walls[0][sx]
    return wdz is not None and wdz < dz and walls[1][sx] <= sy <= walls[2][sx]


# ---------------------------------------------------------------------------
# The passes
# ---------------------------------------------------------------------------

def facade_line(ch, co, sx, row, prev_row, r_lo, r_hi, glyph, attr):
    """A horizontal line on a facade - a roof, an awning - is a slope on
    screen, and a steep one when you are close, so it is drawn as the run of
    rows between where it was in the previous column and where it is now."""
    lo = hi = row
    if prev_row is not None:
        lo, hi = min(row, prev_row), max(row, prev_row)
    for y in range(max(r_lo, lo), min(r_hi, hi) + 1):
        ch[y][sx] = glyph
        co[y][sx] = attr


def draw_wall_column(ch, co, v, sx, dz, b, bay, frac, r_lo, r_hi,
                     edge, new_bay, prev_lines, now):
    """One screen column of one facade. Returns the rows of the lines running
    along it, which the caller feeds back in to keep them joined up."""
    scale = v.fy / dz
    trim = tone_colour("far", b["tone"])

    if edge:
        # The corner of the building. Solid, so it reads as a hard edge and
        # nothing behind it leaks through.
        for y in range(r_lo, r_hi + 1):
            ch[y][sx] = "|"
            co[y][sx] = trim
        return None

    prev_roof, prev_awn = prev_lines if prev_lines else (None, None)
    roof = int(round(v.horizon - (b["height"] - EYE_Y) * scale))
    awning = int(round(v.horizon - (FLOOR_H - EYE_Y) * scale))
    facade_line(ch, co, sx, roof, prev_roof, r_lo, r_hi, "=", trim)
    facade_line(ch, co, sx, awning, prev_awn, r_lo, r_hi, "-", trim)

    # Which flat sign, if any, has a letter in this bay.
    band = None
    for s in b["signs"]:
        k = bay - s["bay0"]
        if 0 <= k < len(s["text"]):
            band = (s, k)
            break

    # A bay is wider than one column once you are close, and a window that
    # filled its whole bay would smear the facade into horizontal stripes.
    # Windows get the middle of their bay; a sign letter gets exactly one
    # column, the first of the bay, so the word does not stutter.
    bay_cols = BAY_W * v.fx / dz
    lit_bay = bay_cols <= 2.0 or 0.30 < frac < 0.70

    colour = wall_colour(b, dz)
    thick = max(1, min(5, int(FLOOR_H * scale * 0.45)))   # windows have height

    for f in range(int(b["height"] / FLOOR_H)):
        y = int(round(v.horizon - ((f + 0.55) * FLOOR_H - EYE_Y) * scale))
        if y > r_hi or y + thick < r_lo:
            continue
        if band is not None and band[0]["floor"] == f:
            if not new_bay:
                continue
            s, k = band
            glyph, attr = s["text"][k], neon_attr(s, k, now)
        elif not lit_bay:
            continue
        elif f == 0:
            if _mix(b["seed"], f, bay) & 3 == 0:
                continue                      # a doorway between the shopfronts
            glyph, attr = "#", colour
        else:
            m = _mix(b["seed"], f, bay)
            if (m & 0xFFFF) / 65535.0 >= b["density"]:
                continue                      # a dark, unlit window
            glyph, attr = b["glyphs"][(m >> 17) & 1], colour
        for dy in range(thick):
            yy = y + dy
            if r_lo <= yy <= r_hi:
                ch[yy][sx] = glyph
                co[yy][sx] = attr
    return roof, awning


def draw_walls(ch, co, v, now):
    """The heart of the street view.

    The facades are two planes at fixed x, so instead of casting a ray per
    column we can just invert the projection: a column with slope t sees the
    plane at x = X at distance (X - cam_x) / t. Whichever side has an actual
    building there and is nearer wins - and where the near side has an alley,
    the far side shows through it.
    """
    width = v.width
    wall_dz = [None] * width
    wall_top = [v.height] * width
    wall_base = [-1] * width
    prev = {-1: None, 1: None}          # what the column to the left saw
    prev_bay = {-1: None, 1: None}
    prev_lines = {-1: None, 1: None}

    for sx in range(width):
        t = (sx - v.cx) / v.fx
        hits = []
        if abs(t) > 1e-6:
            for side in (-1, 1):
                dz = (side * STREET_HALF - v.cam_x) / t
                if dz < NEAR_Z or dz > FAR_Z:
                    prev[side] = prev_bay[side] = prev_lines[side] = None
                    continue
                b = building_at(side, v.cam_z + dz)
                prev_b, prev[side] = prev[side], b
                if b is None:
                    prev_bay[side] = prev_lines[side] = None
                    continue
                pos = (v.cam_z + dz - b["z0"]) / BAY_W
                bay = int(pos)
                prev_k, prev_bay[side] = prev_bay[side], bay
                # The very first column has nothing to its left to compare to,
                # so it never counts as a building corner.
                edge = sx > 0 and b is not prev_b
                hits.append((dz, side, b, bay, pos - bay, edge, bay != prev_k))
        hits.sort(key=lambda h: h[0])       # nearest first

        near_top = None
        for i, (dz, side, b, bay, frac, edge, new_bay) in enumerate(hits):
            top, base = wall_span(v, dz, b)
            r_lo = max(0, top)
            r_hi = min(v.height - 1, base)
            if i:
                # Only the part of the far facade poking above the near roof.
                r_hi = min(r_hi, near_top - 1)
            if r_hi < r_lo:
                prev_lines[side] = None
                continue
            prev_lines[side] = draw_wall_column(
                ch, co, v, sx, dz, b, bay, frac, r_lo, r_hi,
                edge, new_bay, None if edge else prev_lines[side], now)
            if not i:
                near_top = r_lo
                wall_dz[sx] = dz
                wall_top[sx] = r_lo
                wall_base[sx] = r_hi

    return (wall_dz, wall_top, wall_base)


_sky_cache = {}


def draw_sky(ch, co, v, walls):
    """Stars, and a haze of distant rooftops at the end of the avenue.

    Drawn after the walls rather than before: an unlit window is a blank cell,
    so the only thing that knows a building is there is wall_top."""
    off = v.cam_x * 0.2
    for sx in range(v.width):
        top = walls[1][sx] if walls[0][sx] is not None else v.height
        key = int(sx + off)
        if key not in _sky_cache:
            if len(_sky_cache) > 4000:
                _sky_cache.clear()
            rng = random.Random(f"sky|{key}")
            _sky_cache[key] = (rng.random(), rng.random(), rng.choice(".*'`"))
        chance, where, glyph = _sky_cache[key]
        if chance < 0.20:
            y = int(where * max(1, v.horizon))
            if y < top:
                ch[y][sx] = glyph
                co[y][sx] = STAR

    haze_off = v.cam_z * 0.02
    for sx in range(v.width):
        top = walls[1][sx] if walls[0][sx] is not None else v.height
        for d in range(_mix(int(sx + haze_off), 0, 3) % 5):
            y = v.horizon - 1 - d
            if 0 <= y < top:
                ch[y][sx] = ":" if d else "."
                co[y][sx] = HAZE


def collect_glow(cam_z):
    """Where the neon lands on the road: world z -> (colour, which side)."""
    glow = {}
    for b in visible_buildings(cam_z, 90.0):
        for s in b["signs"]:
            z = b["z0"] + (s["bay0"] + len(s["text"]) * 0.5) * BAY_W
            _spill(glow, s, z, b["side"])
        for s in b["vsigns"]:
            _spill(glow, s, s["z"], b["side"])
    return glow


def _spill(glow, s, z, side):
    dark = NEON[min(len(NEON) - 1, int(s["tone"] * len(NEON)))][1]
    for iz in range(int(z) - 5, int(z) + 6):
        glow[iz] = (dark, side)


def draw_ground(ch, co, v, walls, glow):
    """Road and pavement.

    Every screen row below the horizon is one fixed distance - the ground is a
    plane at y = 0 - so the whole row shares a dz, and only the columns between
    the two kerbs need visiting."""
    wall_base = walls[2]
    for y in range(v.horizon + 1, v.height):
        dz = EYE_Y * v.fy / (y - v.horizon)
        if dz > FAR_Z or dz < NEAR_Z:
            continue
        k = v.fx / dz
        zw = v.cam_z + dz
        iz = int(zw)
        spill = glow.get(iz)

        x0 = max(0, int(math.floor(v.cx + (-STREET_HALF - v.cam_x) * k)))
        x1 = min(v.width - 1, int(math.ceil(v.cx + (STREET_HALF - v.cam_x) * k)))
        for sx in range(x0, x1 + 1):
            if y <= wall_base[sx]:
                continue
            xw = v.cam_x + (sx - v.cx) / k
            ax = abs(xw)
            if ax > STREET_HALF:
                continue
            # Sampled on a world-locked grid finer than one cell, so the
            # texture doesn't smear into blocks in the rows nearest your feet.
            if ax > SIDEWALK_X:
                if _mix(int(xw * 3.0), int(zw * 3.0), 11) & 3:
                    continue
                glyph, attr = ",", CURB
            else:
                if _mix(int(xw * 3.0), int(zw * 3.0), 7) & 7:
                    continue
                glyph, attr = ".", STREET
            if spill is not None and xw * spill[1] > -1.0:
                attr = spill[0]
            ch[y][sx] = glyph
            co[y][sx] = attr

        # The kerbs and the centre line, drawn as lines rather than sampled, so
        # they stay unbroken all the way to the vanishing point.
        for sgn in (-1, 1):
            kx = int(round(v.cx + (sgn * SIDEWALK_X - v.cam_x) * k))
            if 0 <= kx < v.width and y > wall_base[kx]:
                ch[y][kx] = "="
                co[y][kx] = CURB
        if iz % 8 < 4:
            mx = int(round(v.cx - v.cam_x * k))
            if 0 <= mx < v.width and y > wall_base[mx]:
                ch[y][mx] = "|"
                co[y][mx] = CURB


# ---------------------------------------------------------------------------
# Things standing on the pavement
#
# These are billboards: flat pictures kept facing you, scaled by distance and
# sampled nearest-neighbour into whatever screen rectangle they land in. '*'
# in the art means the cigarette ember.
# ---------------------------------------------------------------------------

SMOKER_REST = [
    " ,-. ",
    "(- -)",
    " /|\\ ",
    " |#|\\",
    " / \\ ",
    "_| |_",
]

SMOKER_DRAG = [
    " ,-. ",
    "(- o)",
    " /|\\|",
    " |#| ",
    " / \\ ",
    "_| |_",
]

SMOKER_H = 1.9         # how tall he is, in world units
SMOKER_W = 0.79        # chosen so the art keeps its aspect ratio on screen


def put_cell(ch, co, v, walls, sx, sy, dz, glyph, attr):
    """One cell, if it is on the screen and no facade is in front of it."""
    if not (0 <= sx < v.width and 0 <= sy < v.height):
        return
    if hidden(walls, sx, sy, dz):
        return
    ch[sy][sx] = glyph
    co[sy][sx] = attr


def put_point(ch, co, v, walls, x, y, z, glyph, attr):
    pr = v.project(x, y, z)
    if pr is not None:
        put_cell(ch, co, v, walls,
                 int(round(pr[0])), int(round(pr[1])), pr[2], glyph, attr)


def blit_sprite(ch, co, v, walls, art, x, z, y_bot, y_top, w_world, colour):
    """A billboard: a flat picture kept facing you, scaled by distance and
    sampled nearest-neighbour into whatever rectangle it lands in."""
    p_top = v.project(x, y_top, z)
    p_bot = v.project(x, y_bot, z)
    if p_top is None or p_bot is None:
        return
    dz = p_top[2]
    top, bot = p_top[1], p_bot[1]
    half = 0.5 * w_world * v.fx / dz
    left, right = p_top[0] - half, p_top[0] + half

    r0, r1 = max(0, int(top)), min(v.height - 1, int(math.ceil(bot)))
    c0, c1 = max(0, int(left)), min(v.width - 1, int(math.ceil(right)))
    if r1 < r0 or c1 < c0:
        return
    rows, cols = len(art), len(art[0])
    hspan = max(1e-6, bot - top)
    wspan = max(1e-6, right - left)

    for y in range(r0, r1 + 1):
        sr = int((y + 0.5 - top) / hspan * rows)
        if not 0 <= sr < rows:
            continue
        line = art[sr]
        for sx in range(c0, c1 + 1):
            sc = int((sx + 0.5 - left) / wspan * cols)
            if 0 <= sc < cols and line[sc] != " ":
                put_cell(ch, co, v, walls, sx, y, dz, line[sc], colour)


def draw_smoker(ch, co, v, walls, p, now):
    t = (now + p["phase"]) % SMOKE_CYCLE
    dragging = t < DRAG_LEN
    blit_sprite(ch, co, v, walls,
                SMOKER_DRAG if dragging else SMOKER_REST,
                p["x"], p["z"], 0.05, SMOKER_H, SMOKER_W, CURB)

    # The ember is drawn as its own point rather than left to the sprite: it is
    # one cell, and it is the thing you can still pick out at the far end of
    # the street long after he has shrunk to a smudge.
    if dragging:
        ex, ey = p["x"] - p["side"] * 0.12, 1.62          # up at his mouth
    else:
        ex, ey = p["x"] - p["side"] * 0.5, 1.02           # hanging by his hip
    put_point(ch, co, v, walls, ex, ey, p["z"], "o",
              EMBER_HOT if dragging else EMBER)

    # Smoke. No state is kept: every puff's position is a function of how long
    # ago the drag it came from was.
    for k in range(7):
        age = t - DRAG_LEN - k * 0.3
        if not 0.0 < age < 2.8:
            continue
        x = p["x"] - p["side"] * (0.12 + 0.15 * age) + 0.3 * math.sin(age * 1.7 + k)
        put_point(ch, co, v, walls, x, 1.7 + 0.55 * age, p["z"],
                  ".oOo."[min(4, int(age / 0.6))], SMOKE)


def draw_hung_sign(ch, co, v, walls, s, now):
    """A neon sign on a bracket over the pavement, letters stacked downwards -
    the one kind you can still read head-on from the far end of the street.

    Laid out in rows rather than in world units on purpose: spacing the letters
    by a true height makes two of them round onto the same row as the sign
    recedes, and a HOTEL with the T missing is worse than one slightly the
    wrong size."""
    pr = v.project(s["x"], s["top"], s["z"])
    if pr is None:
        return
    sx, top, dz = int(round(pr[0])), int(round(pr[1])), pr[2]
    step = max(1, int(round(1.15 * v.fy / dz)))
    frame = max(1, int(round(0.55 * v.fx / dz)))
    dark = NEON[min(len(NEON) - 1, int(s["tone"] * len(NEON)))][1]

    for y in range(top - 1, top + (len(s["text"]) - 1) * step + 2):
        put_cell(ch, co, v, walls, sx - frame, y, dz, "|", dark)
        put_cell(ch, co, v, walls, sx + frame, y, dz, "|", dark)
    for i, letter in enumerate(s["text"]):
        put_cell(ch, co, v, walls, sx, top + i * step, dz,
                 letter, neon_attr(s, i, now))


def render_street(cam_x, cam_z, width, height, now):
    v = View(cam_x, cam_z, width, height)
    ch = [[" "] * width for _ in range(height)]
    co = [[0] * width for _ in range(height)]

    walls = draw_walls(ch, co, v, now)
    draw_sky(ch, co, v, walls)
    draw_ground(ch, co, v, walls, collect_glow(cam_z))

    # Everything loose on the pavement, farthest first so nearer things win.
    props = []
    for b in visible_buildings(cam_z, 120.0):
        for s in b["vsigns"]:
            props.append((s["z"], "sign", s))
        for p in b["props"]:
            if p["z"] - cam_z < 80.0:
                props.append((p["z"], "smoker", p))
    props.sort(key=lambda p: -p[0])
    for _, kind, thing in props:
        if kind == "sign":
            draw_hung_sign(ch, co, v, walls, thing, now)
        else:
            draw_smoker(ch, co, v, walls, thing, now)

    return ch, co


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

SKYLINE_HUD = " x=%-7d  tab view  arrows/hl walk  HL run  space autopilot  q quit "
STREET_HUD = " z=%-7d  tab view  ws walk  ad cross  WS run  space autopilot  q quit "


def main(stdscr):
    init_colors()
    curses.curs_set(0)          # hide the cursor
    stdscr.nodelay(True)        # don't block waiting for a keypress
    stdscr.keypad(True)         # decode arrow keys into KEY_LEFT / KEY_RIGHT

    street = True               # which view we are in
    sky_x = 0.0                 # the two views keep their own cameras, so
    sky_v = 0.0                 # switching back and forth loses neither
    cam_x = cam_z = 0.0
    vel_x = vel_z = 0.0
    last = {"sky": 0.0, "x": 0.0, "z": 0.0}
    autopilot = False
    frame_time = 1.0 / FPS

    while True:
        now = time.monotonic()

        # --- input -----------------------------------------------------
        key = stdscr.getch()
        while key != -1:
            if key in (ord("q"), 27):
                return
            elif key in (ord("\t"), ord("v")):
                street = not street
                sky_v = vel_x = vel_z = 0.0
            elif key == ord(" "):
                autopilot = not autopilot
            elif key == curses.KEY_RESIZE:
                _cache.clear()
            elif street:
                if key in (curses.KEY_UP, ord("w")):
                    vel_z, last["z"], autopilot = STEP_Z, now, False
                elif key in (curses.KEY_DOWN, ord("s")):
                    vel_z, last["z"], autopilot = -STEP_Z, now, False
                elif key == ord("W"):
                    vel_z, last["z"], autopilot = STEP_Z * RUN_MULTIPLIER, now, False
                elif key == ord("S"):
                    vel_z, last["z"], autopilot = -STEP_Z * RUN_MULTIPLIER, now, False
                elif key in (curses.KEY_LEFT, ord("a"), ord("h")):
                    vel_x, last["x"], autopilot = -DRIFT_X, now, False
                elif key in (curses.KEY_RIGHT, ord("d"), ord("l")):
                    vel_x, last["x"], autopilot = DRIFT_X, now, False
            else:
                if key in (curses.KEY_LEFT, ord("h")):
                    sky_v, last["sky"], autopilot = -WALK_SPEED, now, False
                elif key in (curses.KEY_RIGHT, ord("l")):
                    sky_v, last["sky"], autopilot = WALK_SPEED, now, False
                elif key in (ord("H"), ord("L")):
                    run = WALK_SPEED * RUN_MULTIPLIER
                    sky_v = -run if key == ord("H") else run
                    last["sky"], autopilot = now, False
            key = stdscr.getch()

        # A held-down key repeats, but with gaps. Rather than moving once per
        # keypress (jerky), we keep moving for a moment after each one.
        if autopilot:
            sky_v = WALK_SPEED * 0.6
            vel_z = STEP_Z * 0.6
            vel_x = 2.2 * math.sin(now * 0.19)   # a slow wander across the road
        else:
            if now - last["sky"] > KEY_GRACE:
                sky_v = 0.0
            if now - last["z"] > KEY_GRACE:
                vel_z = 0.0
            if now - last["x"] > KEY_GRACE:
                vel_x = 0.0

        sky_x += sky_v * frame_time
        cam_z += vel_z * frame_time
        cam_x = max(-MAX_DRIFT, min(MAX_DRIFT, cam_x + vel_x * frame_time))

        # --- draw ------------------------------------------------------
        height, width = stdscr.getmaxyx()
        if street:
            ch, co = render_street(cam_x, cam_z, width, height, now)
            hud = STREET_HUD % cam_z
        else:
            ch, co = render_skyline(sky_x, width, height)
            hud = SKYLINE_HUD % sky_x

        stdscr.erase()
        for y in range(height):
            row_ch, row_co = ch[y], co[y]
            for x in range(width):
                c = row_ch[x]
                if c == " ":
                    continue
                # The very last cell of the screen can't be written to.
                if y == height - 1 and x == width - 1:
                    continue
                try:
                    stdscr.addch(y, x, c, row_co[x])
                except curses.error:
                    pass

        try:
            stdscr.addstr(0, 0, hud[:width - 1], HUD)
        except curses.error:
            pass

        stdscr.refresh()
        time.sleep(max(0.0, frame_time - (time.monotonic() - now)))


if __name__ == "__main__":
    curses.wrapper(main)
