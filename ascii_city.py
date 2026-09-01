#!/usr/bin/env python3
"""
ascii_city.py - an endless, procedurally generated ASCII city, seen two ways.

    skyline   the city from outside: parallax layers sliding past as you walk
    street    the city from inside, in the rain: turn where you like, follow the
              neon or take the dark alley, and see whether you find your way out

Controls:
    tab / v                     switch view
    space                       wander on its own
    q                           quit

    skyline   left / right    or  h / l   walk           H / L   run
    street    up / down       or  w / s   walk           W / S   run
              left / right    or  a / d   turn
              , / .                       sidestep

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
RUN_MULTIPLIER = 3.0
BIG = 1 << 30          # added before int() so truncation floors negatives too

# --- skyline view ---
CHUNK_W = 200          # how many "world columns" one generated chunk covers
WALK_SPEED = 20.0      # world columns per second

# Each layer is one depth plane of the city.
# parallax: 0.0 = infinitely far away (never moves), 1.0 = right next to you.
# h and w are (min, max); h is a fraction of the available screen height.
# dens is how many of a facade's windows are lit. Falling away with distance is
# most of what separates the layers: the far bank is a dim shape, the near one
# is a lit building. gap widens as it comes forward so the near layer is a row
# of towers with sky between them rather than a wall across the bottom.
LAYERS = [
    dict(parallax=0.20, h=(0.22, 0.48), w=(4, 10), gap=(2, 6),
         dens=(0.06, 0.22), palette="far"),
    dict(parallax=0.45, h=(0.30, 0.66), w=(6, 15), gap=(3, 8),
         dens=(0.20, 0.50), palette="mid"),
    dict(parallax=1.00, h=(0.28, 0.76), w=(8, 20), gap=(5, 15),
         dens=(0.38, 0.80), palette="near"),
]

# --- street view ---
# The city is a grid of cells; a cell is either open (street, alley, courtyard)
# or one building. World x and z run along the grid, y is up from the road.
CELL = 5.0             # world size of one grid cell, and so one building's front
XP = 9                 # cells from one avenue to the next
ZP = 9                 # ... and from one cross street to the next
EYE_Y = 3.2            # how high off the road you are looking from
BODY = 1.15            # how close to a wall you can get
NEAR_Z = 0.6
MAX_VIEW = 130.0       # how far a wall ray is followed
GROUND_FAR = 80.0
PLANE = 0.9            # half-width of the camera plane: about 84 degrees across
FLOOR_H = 2.4          # world height of one storey
BAY_W = 1.6            # world width of one window bay
PAVE = 1.7             # pavement width, measured in from a facade
WALK = 14.0            # world units per second
TURN = 1.9             # radians per second
SIDESTEP = 8.0

SMOKE_CYCLE = 7.0      # seconds between drags
DRAG_LEN = 1.2         # how long a drag lasts

# --- rain ---
RAIN_LATTICE = 1.35    # world spacing of the drop lattice
RAIN_REACH = 13        # lattice cells around you that can hold a drop
RAIN_TOP = 17.0        # how high above the road a drop starts
RAIN_SPEED = 24.0      # world units per second
RAIN_STREAK = 0.028    # seconds of fall drawn as one streak

# --- lightning ---
# A storm is a weather in its own right, not something that happens to a
# downpour. It is the rarest of them by some way - a few percent of the time -
# and while one is overhead it is violent: it brings its own rain and the
# strikes come every couple of seconds.
STORM_SLOT = 210.0     # seconds per slot in which a storm might blow up
STORM_ODDS = 6         # and only one slot in this many has one
STORM_SHORT = 35.0     # how long the briefest storm lasts
STORM_LONG = 80.0      # and the longest
STRIKE_EVERY = 2.8     # seconds between strikes once one is overhead

# --- the club, and the casino across town ---
CLUB_ODDS = 1200       # one building in this many is one, and it is unmarked
CLUB_BPM = 134.0
CASINO_REACH = 2.3     # how close to the door you have to get to be inside
CASINO_AGAIN = 1.6     # seconds before it deals you another one

CASINO_NAMES = ["ROYALE", "GOLDEN", "LUCKY", "ACES", "MIRAGE", "JACKPOT",
                "FORTUNA", "DIAMOND", "SPHINX", "MONTE"]

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
RAIN = RAIN_FAR = BULB = BULB_DIM = FLASH = CONCRETE = 0
GOLD = GOLD_DIM = ROU_RED = ROU_BLACK = ROU_GREEN = 0


def init_colors():
    global PALETTES, NEON, STAR, STREET, HUD, CURB, HAZE, SMOKE, EMBER
    global EMBER_HOT, RAIN, RAIN_FAR, BULB, BULB_DIM, FLASH, CONCRETE
    global GOLD, GOLD_DIM, ROU_RED, ROU_BLACK, ROU_GREEN

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
            "dark": [235, 236, 237, 238, 239],      # what an alley is lit by
        }
        # (tube lit, tube dark) - the dark one is also what the sign spills
        # onto the wet road below it.
        neon = [(198, 89), (201, 54), (51, 23), (196, 52),
                (208, 94), (46, 22), (141, 60), (226, 58)]
        extras = {"star": 254, "street": 240, "hud": 245, "curb": 246,
                  "haze": 237, "smoke": 244, "ember": 166, "ember_hot": 208,
                  "rain": 110, "rain_far": 60, "bulb": 222, "bulb_dim": 58,
                  "flash": 231, "concrete": 234, "gold": 220,
                  "gold_dim": 136, "rou_red": 196, "rou_black": 252,
                  "rou_green": 46}
        attrs = {}
    else:
        # 8-colour fallback: bold = the "bright" version of a colour.
        groups = {
            "near": [curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_RED,
                     curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_MAGENTA],
            "mid":  [curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_GREEN],
            "far":  [curses.COLOR_BLUE, curses.COLOR_BLACK],
            "dark": [curses.COLOR_BLACK],
        }
        neon = [(curses.COLOR_MAGENTA, curses.COLOR_MAGENTA),
                (curses.COLOR_CYAN, curses.COLOR_CYAN),
                (curses.COLOR_RED, curses.COLOR_RED),
                (curses.COLOR_YELLOW, curses.COLOR_YELLOW)]
        extras = {"star": curses.COLOR_WHITE, "street": curses.COLOR_BLACK,
                  "hud": curses.COLOR_WHITE, "curb": curses.COLOR_WHITE,
                  "haze": curses.COLOR_BLUE, "smoke": curses.COLOR_WHITE,
                  "ember": curses.COLOR_RED, "ember_hot": curses.COLOR_RED,
                  "rain": curses.COLOR_CYAN, "rain_far": curses.COLOR_BLUE,
                  "bulb": curses.COLOR_YELLOW, "bulb_dim": curses.COLOR_BLACK,
                  "flash": curses.COLOR_WHITE, "concrete": curses.COLOR_BLACK,
                  "gold": curses.COLOR_YELLOW, "gold_dim": curses.COLOR_YELLOW,
                  "rou_red": curses.COLOR_RED, "rou_black": curses.COLOR_WHITE,
                  "rou_green": curses.COLOR_GREEN}
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
    RAIN = made["rain"]
    RAIN_FAR = made["rain_far"]
    BULB = made["bulb"] | curses.A_BOLD
    BULB_DIM = made["bulb_dim"]
    FLASH = made["flash"] | curses.A_BOLD
    CONCRETE = made["concrete"]
    GOLD = made["gold"] | curses.A_BOLD
    GOLD_DIM = made["gold_dim"]
    ROU_RED = made["rou_red"] | curses.A_BOLD
    ROU_BLACK = made["rou_black"] | curses.A_BOLD
    ROU_GREEN = made["rou_green"] | curses.A_BOLD


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
    density = rng.uniform(*spec["dens"])    # how many of those slots are lit

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


def draw_stars(ch, co, cam_x, ground, wet):
    """Stars, unless the cloud that is raining on you has come over."""
    if wet > 0.25:
        return
    width = len(ch[0])
    off = cam_x * 0.05          # stars drift very slowly = very far away
    for sx in range(width):
        rng = random.Random(f"star|{int(sx + off)}")
        if rng.random() < 0.07:
            y = rng.randrange(0, max(1, ground - 4))
            ch[y][sx] = rng.choice(".*'`")
            co[y][sx] = STAR


def draw_flash_sky(ch, co, water, flash):
    """The same lightning that is going off over the street, seen from here:
    the sky goes pale and the whole skyline turns into a shape against it."""
    if flash < 0.05:
        return
    width = len(ch[0])
    for y in range(water):
        row, rowc = ch[y], co[y]
        for x in range(width):
            if row[x] == " " and _mix(x, y, 29) % 100 < flash * 96:
                row[x] = "." if flash < 0.5 else ":"
                rowc[x] = FLASH


def reflect(ch, co, water, now, wet, flash):
    """Mirror the city into the water below the far bank.

    A reflection in ASCII lives or dies on being broken up: it is the same
    glyphs, displaced sideways by a swell that changes with depth and time, and
    thinned out the further down it goes until it is gone. Rain roughens the
    surface, so the harder it comes down the less of the city you can read in
    it."""
    height = len(ch)
    width = len(ch[0])
    chop = int(now * 3.0)
    for y in range(water + 1, height):
        d = y - water                      # how far down into the water
        src = water - d
        if src < 0:
            return
        shift = int(round(2.2 * math.sin(now * 1.3 + d * 0.7)
                          + 1.5 * wet * math.sin(now * 4.1 + d * 1.9)))
        keep = 82 - d * 9 - int(30 * wet)
        if keep <= 0:
            return
        row, rowc = ch[src], co[src]
        for x in range(width):
            sx = x + shift
            if not 0 <= sx < width:
                continue
            glyph = row[sx]
            if glyph == " " or _mix(x, y, 61 + chop) % 100 >= keep:
                continue
            ch[y][x] = glyph
            co[y][x] = rowc[sx] if flash > 0.35 else rowc[sx] & ~curses.A_BOLD


def draw_water(ch, co, water, now, wet):
    """The surface itself: the line of the far bank, and the chop on it."""
    height = len(ch)
    width = len(ch[0])
    for x in range(width):
        ch[water][x] = "~" if _mix(x, 0, 41) % 5 else "-"
        co[water][x] = CURB
    every = max(3, 13 - int(8 * wet))      # rain dimples the whole surface
    chop = int(now * 2.0)
    for y in range(water + 1, height):
        row, rowc = ch[y], co[y]
        for x in range(width):
            if row[x] == " " and _mix(x, y * 3 + chop, 83) % every == 0:
                row[x] = "~"
                rowc[x] = RAIN_FAR if wet > 0.3 else STREET


def draw_skyline_rain(ch, co, cam_x, water, now, wet, flash):
    """Rain across the vista, locked to the world so it drifts past as you walk
    rather than sitting still on the screen."""
    if wet < 0.03:
        return
    height = len(ch)
    width = len(ch[0])
    wind = 3.2 * math.sin(now / 23.0)
    span = height + 6
    base = int(cam_x)
    for salt in (907, 911):
        for wx in range(base - 2, base + width + 2):
            m = _mix(wx, salt, 5)
            if (m & 255) > wet * 170:
                continue
            phase = ((m >> 8) & 255) / 256.0
            y = (now * 26.0 + phase * span) % span - 3.0
            x = wx - base
            for dy in range(2 + ((m >> 17) & 1)):
                yy = int(y) - dy
                xx = x + int(dy * wind * 0.2)
                if not (0 <= yy < height and 0 <= xx < width):
                    continue
                if ch[yy][xx] != " ":
                    continue           # it falls behind the city, not over it
                ch[yy][xx] = "|" if abs(wind) < 1.6 else ("\\" if wind > 0 else "/")
                co[yy][xx] = FLASH if flash > 0.35 else (
                    RAIN if yy > water - 6 else RAIN_FAR)


def render_skyline(cam_x, width, height, now):
    """The city from across the water, in whatever weather the street is in.

    Both views share one clock, so the downpour you walked in out of is the
    downpour falling here, and a strike lights them both."""
    wet = rain_intensity(now)
    flash = lightning(now, wet)
    water = max(5, min(height - 4, int(height * 0.58)))
    ground = water - 1                   # the far bank the city stands on
    avail = ground + 1

    ch = [[" "] * width for _ in range(height)]
    co = [[0] * width for _ in range(height)]

    draw_stars(ch, co, cam_x, ground, wet)

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

    draw_flash_sky(ch, co, water, flash)
    if flash >= 0.05:
        dens = int(flash * 70)
        for y in range(water):
            row, rowc = ch[y], co[y]
            for x in range(width):
                if row[x] == " " and _mix(x, y, 97) % 100 < dens:
                    row[x] = ":"
                    rowc[x] = FLASH
    reflect(ch, co, water, now, wet, flash)
    draw_water(ch, co, water, now, wet)
    draw_skyline_rain(ch, co, cam_x, water, now, wet, flash)
    return ch, co


# ===========================================================================
# STREET VIEW - the city from inside
# ===========================================================================

# ---------------------------------------------------------------------------
# The street plan
#
# Every question about the layout is answered by a hash of the cell's own
# coordinates, so the city is infinite, identical every time you walk back to
# it, and stored nowhere. The point of the irregularity below is that you
# should be able to get lost in it:
#
#   - avenues sit on a period but vary in width and offset, and roughly one in
#     thirteen is built over entirely, so two blocks merge into a long one
#   - each block may be split by a one-cell alley, and that alley is cut into
#     stretches, so some of them lead through and some are dead ends
#   - the odd cell inside a block is missing altogether: a yard you can stand in
# ---------------------------------------------------------------------------

_open_cache = {}
_road_cache = {}


def _evict(cache, limit):
    """Drop the oldest third once a cache gets big.

    Wiping the lot instead means the next frame has to rebuild every cell in
    sight at once, which on a long walk is a visible hitch. Dicts keep their
    insertion order, and while you are moving the oldest entries are the ones
    behind you, so this throws away roughly what you have walked past."""
    if len(cache) <= limit:
        return
    for k in list(cache)[:limit // 3]:
        del cache[k]


def _built_over(k, salt):
    return _mix(k, 0, salt) % 13 == 0


def _is_road(i, period, salt):
    key = i * 31 + salt
    hit = _road_cache.get(key)
    if hit is None:
        _evict(_road_cache, 8000)
        k, r = divmod(i, period)
        m = _mix(k, 0, salt)
        # An avenue may be built over, but never two in a row. Left to chance
        # they clump: three in a row leaves a 180-unit stretch with no cross
        # street anywhere in it, and that stops reading as a long block and
        # starts reading as the city having quietly changed into somewhere
        # else. One at a time still merges two blocks, which is the point.
        if _built_over(k, salt) and not _built_over(k - 1, salt):
            hit = False
        else:
            off = (m >> 9) % 2
            hit = off <= r < off + 2 + ((m >> 4) & 1)
        _road_cache[key] = hit
    return hit


def _is_alley(i, j, period, salt, run_salt):
    k, r = divmod(i, period)
    m = _mix(k, 0, salt)
    if (m >> 20) % 3 == 0 or r != 4 + ((m >> 17) % 3):
        return False
    # Broken into stretches: one in five is missing, which is what turns an
    # alley into a dead end you have to back out of.
    return _mix(i, j // 6, run_salt) % 5 != 0


def road_at(i, j):
    """Is this cell part of a proper street - the kind that gets neon?"""
    return _is_road(i, XP, 91) or _is_road(j, ZP, 137)


def is_open(i, j):
    key = i * 1048576 + j
    hit = _open_cache.get(key)
    if hit is None:
        _evict(_open_cache, 60000)
        hit = (road_at(i, j)
               or _is_alley(i, j, XP, 91, 211) or _is_alley(j, i, ZP, 137, 223)
               or _mix(i, j, 57) % 47 == 0)
        _open_cache[key] = hit
    return hit


def open_at(x, z):
    return is_open(int(x / CELL + BIG) - BIG, int(z / CELL + BIG) - BIG)


def can_stand(x, z):
    return (open_at(x - BODY, z) and open_at(x + BODY, z)
            and open_at(x, z - BODY) and open_at(x, z + BODY))


# ---------------------------------------------------------------------------
# What stands on a cell
#
# Each solid cell is one narrow building. Heights are drawn around a base that
# belongs to the whole block, so a block has a skyline of its own rather than
# every building being independently random.
#
# All four faces are generated whether or not anything fronts them, so what a
# building looks like never depends on what happens to be next to it. Which of
# those decorations actually get drawn does: a face on an avenue gets the neon,
# a face on an alley gets a bare bulb and a fire escape.
# ---------------------------------------------------------------------------

FACES = ((-1, 0), (1, 0), (0, -1), (0, 1))     # outward normal of face 0..3

_lots = {}


def make_face(rng, height):
    floors = max(1, int(height / FLOOR_H))
    face = {"flat": None, "hung": None, "smoker": None,
            "bulb": None, "escape": False}

    if floors >= 3 and rng.random() < 0.45:
        text = rng.choice(SIGN_WORDS)
        flicker = rng.random() < 0.25
        face["flat"] = {
            "text": text,
            "floor": rng.randrange(1, min(floors, 6)),
            "step": CELL / (len(text) + 1.0),
            "tone": rng.random(),
            "flicker": flicker,
            "dead": frozenset(rng.sample(range(len(text)), rng.randint(0, 1)))
            if flicker else frozenset(),
            "period": rng.uniform(1.7, 5.0),
            "phase": rng.uniform(0.0, 5.0),
        }

    if rng.random() < 0.45:
        text = rng.choice(SIGN_WORDS)
        flicker = rng.random() < 0.3
        face["hung"] = {
            "text": text,
            "u": rng.uniform(1.2, CELL - 1.2),
            "out": 0.9,
            "top": min(rng.uniform(5.5, 14.0), max(5.0, height - 0.5)),
            "tone": rng.random(),
            "flicker": flicker,
            "dead": frozenset(rng.sample(range(len(text)), rng.randint(0, 1)))
            if flicker else frozenset(),
            "period": rng.uniform(1.7, 5.0),
            "phase": rng.uniform(0.0, 5.0),
        }

    if rng.random() < 0.30:
        face["smoker"] = {"u": rng.uniform(1.2, CELL - 1.2), "out": 1.05,
                          "phase": rng.uniform(0.0, SMOKE_CYCLE)}

    # For the alley side of the same building.
    if rng.random() < 0.45:
        face["bulb"] = {"u": rng.uniform(1.0, CELL - 1.0), "out": 0.45,
                        "y": rng.uniform(2.6, 3.4),
                        "phase": rng.uniform(0.0, 9.0),
                        "dying": rng.random() < 0.3}
    face["escape"] = rng.random() < 0.4
    face["bin"] = rng.uniform(1.2, CELL - 1.2) if rng.random() < 0.3 else None
    return face


def _road_face(i, j, prefer):
    """A face giving onto a proper street, preferring the one asked for, or
    None if the building has no street frontage at all.

    A casino down a five-foot alley can never be stood far enough back from to
    read - its marquee lands off the top of the screen - so a building with
    nothing but alley on every side does not get to be one. That also gives the
    city a division of labour worth having: the casinos out on the main roads
    shouting about it, the club down a back street with no sign at all.

    Safe to ask about the neighbours from inside generation: is_open() and
    road_at() are pure functions of their own coordinates and never call lot(),
    so there is no circle to get stuck in."""
    for f in [prefer] + [k for k in range(4) if k != prefer]:
        nx, nz = FACES[f]
        if is_open(i + nx, j + nz) and road_at(i + nx, j + nz):
            return f
    return None


def lot(i, j):
    key = i * 1048576 + j
    b = _lots.get(key)
    if b is None:
        _evict(_lots, 6000)
        rng = random.Random(f"lot|{i}|{j}")
        base = 11.0 + _mix(i // XP, j // ZP, 43) % 26      # the block's own scale
        b = {
            "height": max(6.0, base * rng.uniform(0.7, 1.5)),
            "tone": rng.random(),
            "glyphs": rng.sample(WINDOW_GLYPHS, 2),
            "density": rng.uniform(0.26, 0.70),
            "seed": rng.randrange(1 << 28),
        }
        b["faces"] = [make_face(rng, b["height"]) for _ in range(4)]
        # Every so often the slab is not offices at all. Drawn last so that
        # deciding it changes nothing about the building it was going to be.
        b["club"] = b["casino"] = None
        if _mix(i, j, 909) % CLUB_ODDS == 0:
            b["height"] = rng.uniform(7.5, 12.0)      # squat, windowless
            b["club"] = {"door": rng.randrange(4),
                         "u": rng.uniform(1.4, CELL - 1.4),
                         "tone": rng.random(),
                         "phase": rng.uniform(0.0, 4.0)}
        elif _mix(i, j, 911) % CLUB_ODDS == 0:
            # Same odds as the club and the exact opposite of it in every
            # other respect.
            face = _road_face(i, j, rng.randrange(4))
            if face is not None:
                b["height"] = rng.uniform(9.0, 16.0)
                b["casino"] = {"door": face,
                               "u": rng.uniform(1.4, CELL - 1.4),
                               "name": rng.choice(CASINO_NAMES),
                               "tone": rng.random(),
                               "sign_tone": rng.random(),
                               "phase": rng.uniform(0.0, 5.0)}
        _lots[key] = b
    return b


def face_origin(i, j, f):
    """Where face f of cell (i, j) starts, and which way it runs.

    Returns (plane, u0, axis): the wall lies at x = plane for faces 0 and 1 and
    at z = plane for 2 and 3; u is measured along the wall from u0."""
    if f == 0:
        return i * CELL, j * CELL, 0
    if f == 1:
        return (i + 1) * CELL, j * CELL, 0
    if f == 2:
        return j * CELL, i * CELL, 1
    return (j + 1) * CELL, i * CELL, 1


def face_point(i, j, f, u, out, y):
    """A world point u along face f, standing `out` clear of the wall."""
    nx, nz = FACES[f]
    plane, u0, axis = face_origin(i, j, f)
    if axis == 0:
        return (plane + nx * out, y, u0 + u)
    return (u0 + u, y, plane + nz * out)


# ---------------------------------------------------------------------------
# The view: a camera you can turn, and the rays it casts
# ---------------------------------------------------------------------------

class View:
    """Turns world coordinates into screen cells, and back again.

    dir is where you are looking and plane is the camera plane across it, the
    standard pair for a grid raycaster: the ray through screen column sx is
    dir + plane * cam, and the distance the cast returns is already measured
    along dir, so it needs no correction for the fisheye at the edges.

    Terminal cells are about twice as tall as they are wide, so the vertical
    focal length is half the horizontal one."""

    def __init__(self, x, z, yaw, width, height):
        self.x = x
        self.z = z
        self.yaw = yaw
        self.width = width
        self.height = height
        self.horizon = max(2, min(height - 6, int(height * 0.44)))
        self.dx = math.sin(yaw)
        self.dz = math.cos(yaw)
        self.plx = self.dz * PLANE          # the camera plane, dir turned 90
        self.plz = -self.dx * PLANE
        self.cx = (width - 1) / 2.0
        self.fx = self.cx / PLANE
        self.fy = self.fx * 0.5
        self.wet = 0.0          # how hard it is raining, 0..1
        self.flash = 0.0        # how bright the lightning is this instant

    def ray(self, sx):
        cam = 2.0 * sx / (self.width - 1) - 1.0
        return self.dx + self.plx * cam, self.dz + self.plz * cam

    def project(self, x, y, z):
        """World point -> (screen x, screen y, distance), or None if behind."""
        rx = x - self.x
        rz = z - self.z
        perp = rx * self.dx + rz * self.dz
        if perp < NEAR_Z:
            return None
        lat = rx * self.dz - rz * self.dx
        return (self.cx + lat * self.fx / perp,
                self.horizon - (y - EYE_Y) * self.fy / perp,
                perp)

    def reflect_row(self, y, perp):
        """Where a thing at height y shows up in the wet road under it: the
        mirror image sits at -y, which is the same column, further down."""
        return self.horizon + (y + EYE_Y) * self.fy / perp


def cast(v, rdx, rdz, limit=4):
    """March a ray through the grid, collecting the walls it meets, nearest
    first. It keeps going past the first one because a taller building behind a
    short one still shows over its roof."""
    cx = v.x / CELL
    cz = v.z / CELL
    i = int(cx + BIG) - BIG
    j = int(cz + BIG) - BIG

    if rdx:
        dt_x = abs(1.0 / rdx)
        if rdx < 0:
            step_i, next_x = -1, (cx - i) * dt_x
        else:
            step_i, next_x = 1, (i + 1 - cx) * dt_x
    else:
        dt_x = next_x = 1e30
        step_i = 0
    if rdz:
        dt_z = abs(1.0 / rdz)
        if rdz < 0:
            step_j, next_z = -1, (cz - j) * dt_z
        else:
            step_j, next_z = 1, (j + 1 - cz) * dt_z
    else:
        dt_z = next_z = 1e30
        step_j = 0

    hits = []
    max_cells = MAX_VIEW / CELL
    while True:
        if next_x < next_z:
            t = next_x
            next_x += dt_x
            i += step_i
            f = 0 if step_i > 0 else 1       # we came in through this face
        else:
            t = next_z
            next_z += dt_z
            j += step_j
            f = 2 if step_j > 0 else 3
        if t > max_cells:
            return hits
        if not is_open(i, j):
            dist = t * CELL
            plane, u0, axis = face_origin(i, j, f)
            if axis == 0:
                u = v.z + rdz * dist - u0
            else:
                u = v.x + rdx * dist - u0
            hits.append((max(dist, 0.35), i, j, f, u))
            if len(hits) >= limit:
                return hits


# ---------------------------------------------------------------------------
# Facades
# ---------------------------------------------------------------------------

def wall_span(v, dist, height):
    scale = v.fy / dist
    return (int(math.floor(v.horizon - (height - EYE_Y) * scale)),
            int(math.ceil(v.horizon + EYE_Y * scale)))


def wall_colour(b, dist, lit, flash=0.0):
    """Distance fog - and an alley face gets the dark palette at any range,
    which is the whole reason an alley reads as somewhere you shouldn't go.

    Except for the half second a lightning flash lasts, when you can suddenly
    see what is down there."""
    if not lit:
        return tone_colour("far" if flash > 0.35 else "dark", b["tone"])
    if dist < 34.0:
        return tone_colour("near", b["tone"])
    if dist < 70.0:
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


def hidden(walls, sx, sy, dist):
    """Is this cell behind a facade? A facade is a vertical plane, so one
    distance and one row range per column is all the depth anyone needs."""
    wd = walls[0][sx]
    return wd is not None and wd < dist and walls[1][sx] <= sy <= walls[2][sx]


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


def draw_club_column(ch, co, v, sx, dist, b, face, u, r_lo, r_hi, edge, cont,
                     now):
    """One column of a windowless concrete slab with a sound system in it.

    It carries no sign of any kind, which is the joke and also the point: the
    only things that give it away are the light going off behind the slit
    windows on the beat, the door, and the queue standing outside in the rain."""
    scale = v.fy / dist
    club = b["club"]
    trim = FLASH if v.flash > 0.35 else CONCRETE
    roof = int(round(v.horizon - (b["height"] - EYE_Y) * scale))

    if edge:
        for y in range(r_lo, r_hi + 1):
            ch[y][sx] = "|"
            co[y][sx] = trim
        return roof, None, None

    facade_line(ch, co, sx, roof, cont[0], r_lo, r_hi, "=", trim)

    # Bare concrete. Speckled rather than filled, so it reads as a mass with
    # no windows in it instead of as a hole in the street.
    for y in range(r_lo, r_hi + 1, 2):
        if _mix(b["seed"], sx & 7, y) % 5 == 0:
            ch[y][sx] = "."
            co[y][sx] = CONCRETE

    lamp = club_light(now, club)

    # A band of slit windows under the roof, and the light behind them.
    slit = int(round(v.horizon - (b["height"] - 1.7 - EYE_Y) * scale))
    if r_lo <= slit <= r_hi and int(u / 1.1) % 2 == 0:
        ch[slit][sx] = "=" if lamp is None else "#"
        co[slit][sx] = CONCRETE if lamp is None else lamp

    # The door. Steel, shut most of the time, and blinding when it is not.
    if face == club["door"] and abs(u - club["u"]) < 0.8:
        head = int(round(v.horizon - (2.3 - EYE_Y) * scale))
        for y in range(max(r_lo, head), r_hi + 1):
            ch[y][sx] = "#" if lamp is not None else "]"
            co[y][sx] = lamp if lamp is not None else CONCRETE
    return roof, None, None


def chase(u, now, period=0.8):
    """A running bulb chase, the kind that goes round a casino sign. Lit is a
    function of where the bulb is and what time it is, so the whole building
    agrees on the pattern without anything being stored.

    The bulbs sit close together on purpose: space them by half a world unit
    and at arm's length one bulb covers eight columns, which reads as a lit
    stripe rather than as a row of lights."""
    return (int(u / 0.2) - int(now * 24.0)) % 3 == 0





def draw_casino_column(ch, co, v, sx, dist, b, face, u, r_lo, r_hi, edge,
                       cont, now):
    """One column of a casino: everything the club is not.

    Bulbs chasing round the roof and the door, its name across the front in
    neon, and every window in the place lit."""
    scale = v.fy / dist
    cas = b["casino"]
    trim = FLASH if v.flash > 0.35 else GOLD_DIM
    roof = int(round(v.horizon - (b["height"] - EYE_Y) * scale))

    if edge:
        for y in range(r_lo, r_hi + 1):
            ch[y][sx] = "|"
            co[y][sx] = trim
        return roof, None, None

    prev_roof, prev_awn, prev_k = cont
    awning = int(round(v.horizon - (FLOOR_H - EYE_Y) * scale))
    facade_line(ch, co, sx, roof, prev_roof, r_lo, r_hi, "=", trim)
    facade_line(ch, co, sx, awning, prev_awn, r_lo, r_hi, "-", trim)

    # Bulbs along the roof and along the canopy over the door.
    for row in (roof, awning):
        if r_lo <= row <= r_hi and chase(u, now):
            ch[row][sx] = "o"
            co[row][sx] = GOLD

    # The name, in neon, across the middle of the front.
    text = cas["name"]
    step = CELL / (len(text) + 1.0)
    letter_k = None
    k = int((u - CELL / (2.0 * (len(text) + 1.0))) / step)
    if 0 <= k < len(text):
        letter_k = k
    sign_floor = max(1, int(b["height"] / FLOOR_H) - 2)
    neon = NEON[min(len(NEON) - 1, int(cas["tone"] * len(NEON)))][0]

    bay = int(u / BAY_W)
    frac = u / BAY_W - bay
    lit_bay = BAY_W * v.fx / dist <= 2.0 or 0.30 < frac < 0.70
    thick = max(1, min(5, int(FLOOR_H * scale * 0.45)))
    gold = tone_colour("near", 0.55)

    for f in range(int(b["height"] / FLOOR_H)):
        y = int(round(v.horizon - ((f + 0.55) * FLOOR_H - EYE_Y) * scale))
        if y > r_hi or y + thick < r_lo:
            continue
        if letter_k is not None and f == sign_floor:
            if prev_k == letter_k:
                continue
            glyph, attr = text[letter_k], neon
        elif not lit_bay:
            continue
        elif f == 0:
            if abs(u - cas["u"]) < 0.9 and face == cas["door"]:
                glyph, attr = ("o", GOLD) if chase(u + now, now) else ("#", GOLD_DIM)
            else:
                glyph, attr = "#", gold
        else:
            # Nobody in a casino ever turns a light off.
            m = _mix(b["seed"] + face, f, bay)
            if (m & 0xFFFF) / 65535.0 >= 0.88:
                continue
            glyph, attr = b["glyphs"][(m >> 17) & 1], gold
        for dy in range(thick):
            yy = y + dy
            if r_lo <= yy <= r_hi:
                ch[yy][sx] = glyph
                co[yy][sx] = attr
    return roof, awning, letter_k


def draw_wall_column(ch, co, v, sx, dist, b, face, u, r_lo, r_hi,
                     edge, cont, lit, now):
    """One screen column of one facade. Returns what the caller needs to keep
    the lines along it joined up in the next column."""
    if b["club"] is not None:
        return draw_club_column(ch, co, v, sx, dist, b, face, u,
                                r_lo, r_hi, edge, cont, now)
    if b["casino"] is not None:
        return draw_casino_column(ch, co, v, sx, dist, b, face, u,
                                  r_lo, r_hi, edge, cont, now)
    scale = v.fy / dist
    trim = tone_colour("dark" if not lit else "far", b["tone"])
    if v.flash > 0.35:
        # Lightning picks out every edge in the city at once, which is what
        # makes a flash read as a flash rather than as the screen blinking.
        trim = FLASH
    roof = int(round(v.horizon - (b["height"] - EYE_Y) * scale))
    awning = int(round(v.horizon - (FLOOR_H - EYE_Y) * scale))

    if edge:
        # A building corner. Solid, so it reads as a hard edge and nothing
        # behind it leaks through the unlit windows. It still reports where its
        # lines are, or the next column would think it was a corner too, and
        # the whole facade would come out as bars.
        for y in range(r_lo, r_hi + 1):
            ch[y][sx] = "|"
            co[y][sx] = trim
        return roof, awning, None

    prev_roof, prev_awn, prev_k = cont
    facade_line(ch, co, sx, roof, prev_roof, r_lo, r_hi, "=", trim)
    if lit:                       # an alley has no shopfronts to put one over
        facade_line(ch, co, sx, awning, prev_awn, r_lo, r_hi, "-", trim)

    fc = b["faces"][face]
    sign = fc["flat"] if lit else None
    letter_k = None
    if sign is not None:
        k = int((u - CELL / (2.0 * (len(sign["text"]) + 1.0))) / sign["step"])
        if 0 <= k < len(sign["text"]):
            letter_k = k

    # A bay is wider than one column once you are close, and a window that
    # filled its whole bay would smear the facade into horizontal stripes.
    # Windows get the middle of their bay; a sign letter gets exactly one
    # column, the first it appears in, so the word does not stutter.
    bay = int(u / BAY_W)
    frac = u / BAY_W - bay
    bay_cols = BAY_W * v.fx / dist
    lit_bay = bay_cols <= 2.0 or 0.30 < frac < 0.70

    colour = wall_colour(b, dist, lit, v.flash)
    thick = max(1, min(5, int(FLOOR_H * scale * 0.45)))   # windows have height
    density = b["density"] if lit else b["density"] * 0.35

    # A fire escape zigzags down the back of an alley building, one landing per
    # storey, and is the only thing up there catching any light.
    escape = (not lit) and fc["escape"] and 0.45 < frac < 0.8

    for f in range(int(b["height"] / FLOOR_H)):
        y = int(round(v.horizon - ((f + 0.55) * FLOOR_H - EYE_Y) * scale))
        if y > r_hi or y + thick < r_lo:
            continue
        if letter_k is not None and sign["floor"] == f:
            if prev_k == letter_k:
                continue
            glyph, attr = sign["text"][letter_k], neon_attr(sign, letter_k, now)
        elif escape and f:
            glyph, attr = "=", trim
        elif not lit_bay:
            continue
        elif f == 0:
            if _mix(b["seed"], face, bay) & 3 == 0:
                continue                      # a doorway between the shopfronts
            glyph, attr = "#", colour
        else:
            m = _mix(b["seed"] + face, f, bay)
            if (m & 0xFFFF) / 65535.0 >= density:
                continue                      # a dark, unlit window
            glyph, attr = b["glyphs"][(m >> 17) & 1], colour
        for dy in range(thick):
            yy = y + dy
            if r_lo <= yy <= r_hi:
                ch[yy][sx] = glyph
                co[yy][sx] = attr
    return roof, awning, letter_k


def draw_walls(ch, co, v, now):
    """Cast one ray per screen column and paint what it hits.

    Within a column the hits are drawn nearest first and each one after is
    clipped to the rows above everything already drawn, so a tall building
    behind a short one shows over its roof and nothing else leaks through."""
    width = v.width
    wall_d = [None] * width
    wall_top = [v.height] * width
    wall_base = [-1] * width
    seen = {}          # (cell, face) -> what the column to the left left off at

    for sx in range(width):
        rdx, rdz = v.ray(sx)
        cover = None
        for n, (dist, i, j, f, u) in enumerate(cast(v, rdx, rdz)):
            b = lot(i, j)
            top, base = wall_span(v, dist, b["height"])
            r_lo = max(0, top)
            r_hi = min(v.height - 1, base)
            if cover is not None:
                r_hi = min(r_hi, cover - 1)
            if r_hi < r_lo:
                continue
            key = (i, j, f)
            was = seen.get(key)
            cont = was[1] if was is not None and was[0] == sx - 1 else None
            if cont is None:
                # The column to the left may have been the building next door
                # on the same plane. That is a terrace, not a corner: draw no
                # bar and let the roof line step between the two heights, which
                # is the only edge actually there.
                for nk in (((i, j - 1, f), (i, j + 1, f)) if f < 2
                           else ((i - 1, j, f), (i + 1, j, f))):
                    w = seen.get(nk)
                    if w is not None and w[0] == sx - 1:
                        cont = (w[1][0], w[1][1], None)
                        break
            nx, nz = FACES[f]
            seen[key] = (sx, draw_wall_column(
                ch, co, v, sx, dist, b, f, u, r_lo, r_hi,
                cont is None and sx > 0, cont or (None, None, None),
                road_at(i + nx, j + nz), now))
            cover = r_lo if cover is None else min(cover, r_lo)
            if not n:
                wall_d[sx] = dist
                wall_top[sx] = r_lo
                wall_base[sx] = r_hi
            if cover <= 0:
                break

    return (wall_d, wall_top, wall_base)


# ---------------------------------------------------------------------------
# Sky, ground and the light lying on it
# ---------------------------------------------------------------------------

def draw_flash(ch, co, v, walls, flash):
    """What a strike actually does: pick out the whole city at once.

    Lighting only the sky leaves every facade the same dark shape it was, and
    the strike reads as the rain having gone white. Filling the blank parts of
    each wall turns the buildings into lit surfaces for the tenth of a second
    they are lit for, which is the entire point of lightning."""
    if flash < 0.05:
        return
    walls_d, walls_top, walls_base = walls
    dens = int(flash * 66)
    for sx in range(v.width):
        if walls_d[sx] is None:
            continue
        lo = max(0, walls_top[sx])
        hi = min(v.height - 1, walls_base[sx])
        for y in range(lo, hi + 1):
            if ch[y][sx] == " " and _mix(sx, y, 97) % 100 < dens:
                ch[y][sx] = ":"
                co[y][sx] = FLASH
    ground = int(flash * 38)
    for y in range(v.horizon + 1, v.height):
        row, rowc = ch[y], co[y]
        for sx in range(v.width):
            if row[sx] == " " and _mix(sx, y, 131) % 100 < ground:
                row[sx] = "."
                rowc[sx] = FLASH


def draw_sky(ch, co, v, walls, wet):
    """Stars, and a haze of distant roofs at the horizon.

    Drawn after the walls rather than before: an unlit window is a blank cell,
    so wall_top is the only thing that knows a building is in the way. The
    stars are keyed by the compass bearing of the ray rather than the screen
    column, so they hold still while you turn under them - and they are gone
    altogether once the cloud comes over.

    A column that hit no wall at all is looking straight out of the city down a
    cross street. Rather than leave a void at the vanishing point it gets a
    taller silhouette, which is what the rest of the city looks like from
    further away than anyone is going to draw it."""
    thin = 2 + int(3.0 * wet)
    for sx in range(v.width):
        empty = walls[0][sx] is None
        top = v.height if empty else walls[1][sx]
        cam = 2.0 * sx / (v.width - 1) - 1.0
        bearing = int((v.yaw + math.atan(cam * PLANE)) * 90.0)
        if wet < 0.25:
            m = _mix(bearing, 0, 71)
            if m % 100 < 9:
                y = (m >> 8) % max(1, v.horizon)
                if y < top:
                    ch[y][sx] = ".*'`"[(m >> 20) & 3]
                    co[y][sx] = STAR
        deep = _mix(bearing // 3, 2, 17) % 7 if empty else 0
        for d in range(max(deep, _mix(bearing, 1, 3) % thin)):
            y = v.horizon - 1 - d
            if 0 <= y < top:
                ch[y][sx] = ":" if d else "."
                co[y][sx] = HAZE
        if v.flash > 0.05:
            # The whole sky goes pale, so everything with a roof on it turns
            # into a black shape against it.
            for y in range(0, min(top, v.horizon)):
                if _mix(sx, y, 29) % 100 < v.flash * 96:
                    ch[y][sx] = "." if v.flash < 0.5 else ":"
                    co[y][sx] = FLASH


GLOW_CELL = 1.6


def collect_glow(v, now, wet):
    """A coarse world map of what colour is spilling onto the ground where.

    Neon does it when the road is wet; a bare bulb over an alley door does it
    whatever the weather, and is often the only thing down there that does."""
    glow = {}
    reach = 7.0
    for i, j, b in near_lots(v, 60.0):
        for f in range(4):
            nx, nz = FACES[f]
            if not is_open(i + nx, j + nz):
                continue
            if b["club"] is not None:
                if f == b["club"]["door"]:
                    lamp = club_light(now, b["club"])
                    if lamp is not None:
                        p = face_point(i, j, f, b["club"]["u"], 1.0, 0.0)
                        _spill(glow, p[0], p[2], 5.5, lamp)
                continue
            if b["casino"] is not None:
                p = face_point(i, j, f, CELL * 0.5, 1.0, 0.0)
                _spill(glow, p[0], p[2], 6.0, GOLD_DIM)
                continue
            fc = b["faces"][f]
            if road_at(i + nx, j + nz):
                if wet < 0.12:
                    continue
                for s in (fc["flat"], fc["hung"]):
                    if s is None:
                        continue
                    u = s.get("u", CELL * 0.5)
                    p = face_point(i, j, f, u, 1.0, 0.0)
                    dark = NEON[min(len(NEON) - 1,
                                    int(s["tone"] * len(NEON)))][1]
                    _spill(glow, p[0], p[2], reach * wet, dark)
            elif fc["bulb"] is not None:
                p = face_point(i, j, f, fc["bulb"]["u"], 1.0, 0.0)
                _spill(glow, p[0], p[2], 3.4, BULB_DIM)
    return glow


def _spill(glow, x, z, reach, attr):
    gi = int(x / GLOW_CELL + BIG) - BIG
    gj = int(z / GLOW_CELL + BIG) - BIG
    n = max(1, int(reach / GLOW_CELL))
    for a in range(gi - n, gi + n + 1):
        for b in range(gj - n, gj + n + 1):
            if (a - gi) ** 2 + (b - gj) ** 2 <= n * n:
                glow[a * 65536 + b] = attr


def draw_ground(ch, co, v, walls, glow, wet, now):
    """Road, pavement and kerb.

    Every screen row below the horizon is one fixed distance - the ground is a
    plane at y = 0 - so a row's world positions are a straight line from the
    left ray to the right one, and stepping along it costs an add per column."""
    width = v.width
    wall_base = walls[2]
    rdx0 = v.dx - v.plx
    rdz0 = v.dz - v.plz
    rdx1 = v.dx + v.plx
    rdz1 = v.dz + v.plz
    inv = 1.0 / CELL
    ripple = wet > 0.45

    for y in range(v.horizon + 1, v.height):
        d = EYE_Y * v.fy / (y - v.horizon)
        if d > GROUND_FAR:
            continue
        wx = v.x + rdx0 * d
        wz = v.z + rdz0 * d
        stepx = (rdx1 - rdx0) * d / (width - 1)
        stepz = (rdz1 - rdz0) * d / (width - 1)
        li = lj = None
        solid = False
        x0 = y0 = 0.0

        for sx in range(width):
            x, z = wx, wz
            wx += stepx
            wz += stepz
            if y <= wall_base[sx]:
                continue
            i = int(x * inv + BIG) - BIG
            j = int(z * inv + BIG) - BIG
            if i != li or j != lj:
                li, lj = i, j
                solid = not is_open(i, j)
                x0, y0 = i * CELL, j * CELL
            if solid:
                continue

            # How far this point is from the nearest wall decides whether it is
            # pavement, the kerb line, or road.
            fx_ = x - x0
            fz_ = z - y0
            edge = 1e9
            if fx_ < PAVE and not is_open(i - 1, j):
                edge = fx_
            elif CELL - fx_ < PAVE and not is_open(i + 1, j):
                edge = CELL - fx_
            if fz_ < edge and fz_ < PAVE and not is_open(i, j - 1):
                edge = fz_
            elif CELL - fz_ < edge and CELL - fz_ < PAVE and not is_open(i, j + 1):
                edge = CELL - fz_

            g = glow.get(((int(x / GLOW_CELL + BIG) - BIG) * 65536)
                         + (int(z / GLOW_CELL + BIG) - BIG))
            if edge < PAVE - 0.3:
                if _mix(int(x * 3.0), int(z * 3.0), 11) & 3:
                    continue
                glyph, attr = ",", CURB
            elif edge < 1e8:
                glyph, attr = "=", CURB               # the kerb
            else:
                h = _mix(int(x * 3.0), int(z * 3.0), 7)
                if ripple and h & 31 == 0:
                    glyph, attr = "~", RAIN_FAR       # rain standing on the road
                elif h & 7:
                    continue
                else:
                    glyph, attr = ".", STREET
            if g is not None:
                attr = g
            if v.flash > 0.3 and _mix(int(x * 2.0), int(z * 2.0), 19) % 3 == 0:
                attr = FLASH
            ch[y][sx] = glyph
            co[y][sx] = attr


# ---------------------------------------------------------------------------
# Things standing in the street
#
# These are billboards: flat pictures kept facing you, scaled by distance and
# sampled nearest-neighbour into whatever screen rectangle they land in.
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

BIN = [
    "  ______  ",
    " /##  ##\\ ",
    " |##  ##| ",
    " |______| ",
]

RAVER_UP = [
    "\\,-./",
    " (-) ",
    " \\|/ ",
    "  |  ",
    " / \\ ",
    "_| |_",
]

RAVER_DOWN = [
    " ,-. ",
    " (-) ",
    "/ | \\",
    "  |  ",
    " / \\ ",
    "_| |_",
]

SMOKER_H = 1.9         # how tall he is, in world units
SMOKER_W = 0.79        # chosen so the art keeps its aspect ratio on screen


def put_cell(ch, co, v, walls, sx, sy, dist, glyph, attr):
    """One cell, if it is on the screen and no facade is in front of it."""
    if not (0 <= sx < v.width and 0 <= sy < v.height):
        return
    if hidden(walls, sx, sy, dist):
        return
    ch[sy][sx] = glyph
    co[sy][sx] = attr


def put_point(ch, co, v, walls, x, y, z, glyph, attr):
    pr = v.project(x, y, z)
    if pr is not None:
        put_cell(ch, co, v, walls,
                 int(round(pr[0])), int(round(pr[1])), pr[2], glyph, attr)


def put_lit(ch, co, v, walls, x, y, z, glyph, attr, wet, now):
    """A light, and its reflection in the wet road under it."""
    pr = v.project(x, y, z)
    if pr is None:
        return
    sx, sy, d = int(round(pr[0])), int(round(pr[1])), pr[2]
    put_cell(ch, co, v, walls, sx, sy, d, glyph, attr)
    if wet > 0.15:
        ry = v.reflect_row(y, d) + 1.5 * wet * math.sin(now * 2.7 + sx * 0.9)
        if _mix(sx, int(ry), int(now * 6.0)) % 5 < 1 + int(3 * wet):
            put_cell(ch, co, v, walls, sx, int(round(ry)), d, ":", attr)


def blit_sprite(ch, co, v, walls, art, x, z, y_bot, y_top, w_world, colour):
    p_top = v.project(x, y_top, z)
    p_bot = v.project(x, y_bot, z)
    if p_top is None or p_bot is None:
        return
    dist = p_top[2]
    if dist < 1.6:
        # Close enough that you are standing in it. Blowing a six-row sprite up
        # to forty rows of nearest-neighbour porridge helps nobody.
        return
    top, bot = p_top[1], p_bot[1]
    half = 0.5 * w_world * v.fx / dist
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
                put_cell(ch, co, v, walls, sx, y, dist, line[sc], colour)


def draw_smoker(ch, co, v, walls, p, now, wet):
    t = (now + p["phase"]) % SMOKE_CYCLE
    dragging = t < DRAG_LEN
    x, _, z = p["world"]
    blit_sprite(ch, co, v, walls,
                SMOKER_DRAG if dragging else SMOKER_REST,
                x, z, 0.05, SMOKER_H, SMOKER_W, CURB)

    # The ember is drawn as its own point rather than left to the sprite: it is
    # one cell, and it is the thing you can still pick out at the far end of
    # the street long after he has shrunk to a smudge.
    nx, nz = p["normal"]
    if dragging:
        ex, ez, ey = x + nx * 0.12, z + nz * 0.12, 1.62      # up at his mouth
    else:
        ex, ez, ey = x + nx * 0.5, z + nz * 0.5, 1.02        # down by his hip
    put_lit(ch, co, v, walls, ex, ey, ez, "o",
            EMBER_HOT if dragging else EMBER, wet, now)

    # Smoke. No state is kept: every puff's position is a function of how long
    # ago the drag it came from was.
    for k in range(7):
        age = t - DRAG_LEN - k * 0.3
        if not 0.0 < age < 2.8:
            continue
        drift = 0.12 + 0.15 * age
        wobble = 0.3 * math.sin(age * 1.7 + k)
        put_point(ch, co, v, walls,
                  x + nx * drift + nz * wobble, 1.7 + 0.55 * age,
                  z + nz * drift - nx * wobble,
                  ".oOo."[min(4, int(age / 0.6))], SMOKE)


def draw_raver(ch, co, v, walls, p, now, wet):
    """Someone outside the club with a glow stick in each hand.

    They are on the same clock as the sound system, so the whole pavement is
    dancing to the same beat and goes white together on the kick. The sticks
    are drawn as a short trail of their own past positions, which is the only
    way a single moving cell reads as a light rather than as a speck."""
    club = p["club"]
    t = now * CLUB_BPM / 60.0 + club["phase"]
    # All on the same beat, but not all on the same frame of it.
    up = (t + p["swing"] * 0.18) % 1.0 < 0.5
    lamp = club_light(now, club)
    x, _, z = p["world"]
    blit_sprite(ch, co, v, walls, RAVER_UP if up else RAVER_DOWN,
                x, z, 0.05, SMOKER_H, SMOKER_W,
                lamp if lamp is not None else CURB)

    lit, dark = NEON[min(len(NEON) - 1, int(p["tone"] * len(NEON)))]
    tx, tz = p["tangent"]
    for hand in (0, math.pi):
        for m in range(3):
            a = (t + p["swing"]) * math.pi - m * 0.24 + hand
            off = 0.44 * math.sin(a)
            y = 1.5 + (0.42 if up else 0.0) + 0.34 * math.cos(a)
            put_point(ch, co, v, walls, x + tx * off, y, z + tz * off,
                      "*o."[m], lit if m == 0 else dark)


def draw_marquee(ch, co, v, walls, cas, world, now):
    """The word, in lights, across the front of a casino.

    Laid out in screen space with the letters side by side. A marquee that
    shrank with distance is a smear long before you can stand far enough back
    to see the whole building - and on a fifteen-foot street you never can - so
    this one is a stylised fixed size. Unreadable is worse than the wrong size,
    which is the same call the hung signs make about their rows."""
    pr = v.project(world[0], world[1], world[2])
    if pr is None:
        return
    sx, sy, dist = int(round(pr[0])), int(round(pr[1])), pr[2]
    neon = NEON[min(len(NEON) - 1, int(cas["sign_tone"] * len(NEON)))][0]
    for row, (word, attr) in enumerate((("CASINO", neon), (cas["name"], GOLD))):
        x0 = sx - len(word) // 2
        for k, c in enumerate(word):
            put_cell(ch, co, v, walls, x0 + k, sy + row, dist, c, attr)
    span = max(len("CASINO"), len(cas["name"])) // 2 + 2
    for k in range(-span, span + 1):
        for dy in (-1, 2):
            if chase(k * 0.2 + dy * 0.4, now):
                put_cell(ch, co, v, walls, sx + k, sy + dy, dist, "o", GOLD)


def draw_hung_sign(ch, co, v, walls, s, world, now, wet):
    """A neon sign on a bracket over the pavement, letters stacked downwards -
    the one kind you can still read head-on from the far end of the street.

    Laid out in rows rather than in world units on purpose: spacing the letters
    by a true height makes two of them round onto the same row as the sign
    recedes, and a HOTEL with the T missing is worse than one slightly the
    wrong size."""
    pr = v.project(world[0], s["top"], world[2])
    if pr is None:
        return
    sx, top, dist = int(round(pr[0])), int(round(pr[1])), pr[2]
    step = max(1, int(round(1.15 * v.fy / dist)))
    frame = max(1, int(round(0.55 * v.fx / dist)))
    dark = NEON[min(len(NEON) - 1, int(s["tone"] * len(NEON)))][1]

    for y in range(top - 1, top + (len(s["text"]) - 1) * step + 2):
        put_cell(ch, co, v, walls, sx - frame, y, dist, "|", dark)
        put_cell(ch, co, v, walls, sx + frame, y, dist, "|", dark)
    for i, letter in enumerate(s["text"]):
        attr = neon_attr(s, i, now)
        put_cell(ch, co, v, walls, sx, top + i * step, dist, letter, attr)
        if wet > 0.15:
            y = s["top"] - i * 1.15
            ry = v.reflect_row(y, dist) + 2.0 * wet * math.sin(now * 2.3 + i)
            if _mix(sx, int(ry), int(now * 5.0)) % 6 < 1 + int(3 * wet):
                put_cell(ch, co, v, walls, sx, int(round(ry)), dist, ":", attr)


def near_lots(v, reach):
    """Solid cells within reach that are not behind you."""
    n = int(reach / CELL) + 1
    ci = int(v.x / CELL + BIG) - BIG
    cj = int(v.z / CELL + BIG) - BIG
    for i in range(ci - n, ci + n + 1):
        for j in range(cj - n, cj + n + 1):
            rx = (i + 0.5) * CELL - v.x
            rz = (j + 0.5) * CELL - v.z
            if rx * v.dx + rz * v.dz < -CELL:
                continue
            if not is_open(i, j):
                yield i, j, lot(i, j)


def draw_props(ch, co, v, walls, now, wet):
    """Everything hanging off a facade or standing in front of one, drawn
    farthest first so the near ones win."""
    props = []
    for i, j, b in near_lots(v, 62.0):
        for f in range(4):
            nx, nz = FACES[f]
            if not is_open(i + nx, j + nz):
                continue
            if b["casino"] is not None:
                # It has to be obvious from down the street what this is.
                if f == b["casino"]["door"]:
                    w = face_point(i, j, f, CELL * 0.5, 0.6, 4.6)
                    props.append((_d2(v, w), "marquee", b["casino"], w, (nx, nz)))
                continue
            if b["club"] is not None:
                # Nobody is getting in for a while. Some of them are dancing
                # anyway and the rest are smoking about it.
                if f == b["club"]["door"]:
                    for q in range(5):
                        w = face_point(i, j, f, 0.45 + q * 1.0, 1.15, 0.0)
                        seed = b["seed"] + q * 37
                        if seed % 5 < 3:
                            props.append((_d2(v, w), "raver",
                                          {"club": b["club"],
                                           "tone": (seed % 97) / 97.0,
                                           "swing": (seed % 31) / 31.0,
                                           "tangent": (-nz, nx)},
                                          w, (nx, nz)))
                        else:
                            props.append((_d2(v, w), "smoker",
                                          {"phase": seed % 100 / 14.0},
                                          w, (nx, nz)))
                continue
            fc = b["faces"][f]
            lit = road_at(i + nx, j + nz)
            if lit:
                if fc["hung"] is not None:
                    s = fc["hung"]
                    w = face_point(i, j, f, s["u"], s["out"], 0.0)
                    props.append((_d2(v, w), "hung", s, w, (nx, nz)))
                if fc["smoker"] is not None:
                    s = fc["smoker"]
                    w = face_point(i, j, f, s["u"], s["out"], 0.0)
                    props.append((_d2(v, w), "smoker", s, w, (nx, nz)))
            else:
                if fc["bulb"] is not None:
                    s = fc["bulb"]
                    w = face_point(i, j, f, s["u"], s["out"], s["y"])
                    props.append((_d2(v, w), "bulb", s, w, (nx, nz)))
                if fc["bin"] is not None:
                    w = face_point(i, j, f, fc["bin"], 0.9, 0.0)
                    props.append((_d2(v, w), "bin", None, w, (nx, nz)))
    props.sort(key=lambda p: -p[0])

    for _, kind, s, world, normal in props:
        if kind == "marquee":
            draw_marquee(ch, co, v, walls, s, world, now)
        elif kind == "hung":
            draw_hung_sign(ch, co, v, walls, s, world, now, wet)
        elif kind == "raver":
            s["world"] = world
            draw_raver(ch, co, v, walls, s, now, wet)
        elif kind == "smoker":
            draw_smoker(ch, co, v, walls,
                        {"world": world, "normal": normal, "phase": s["phase"]},
                        now, wet)
        elif kind == "bin":
            blit_sprite(ch, co, v, walls, BIN, world[0], world[2],
                        0.0, 1.3, 1.5, CURB)
        else:
            # A bare bulb over a back door. The failing ones stutter, and an
            # alley lit by one of those is worse than an alley lit by none.
            on = True
            if s["dying"]:
                p = (now * 3.1 + s["phase"]) % 1.0
                on = p > 0.22 or int(now * 11 + s["phase"]) % 3 == 0
            put_lit(ch, co, v, walls, world[0], world[1], world[2],
                    "o" if on else ".", BULB if on else BULB_DIM, wet, now)


def _d2(v, w):
    return (w[0] - v.x) ** 2 + (w[2] - v.z) ** 2


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------

# The cheat menu can pin the weather. Index 0 hands it back to the sky.
# (name, rain to pin it at or None to let the sky decide, is it a storm)
WEATHER_STEPS = [("auto", None, False), ("dry", 0.0, False),
                 ("drizzle", 0.18, False), ("rain", 0.5, False),
                 ("downpour", 0.95, False), ("storm", 0.98, True)]
_weather_i = 0
_forced_strike = None       # when a strike was called for, rather than due


def cycle_weather():
    global _weather_i
    _weather_i = (_weather_i + 1) % len(WEATHER_STEPS)
    return WEATHER_STEPS[_weather_i][0]


def weather_name():
    return WEATHER_STEPS[_weather_i][0]


def force_strike(now):
    global _forced_strike
    _forced_strike = now


def storm_window(now):
    """(start, length) of the storm overhead, or None.

    Storms sit on a slot the way strikes used to, but a long one: one slot in
    six has a storm in it and a slot is three and a half minutes, so they come
    round about every twenty. A storm can run past the end of its own slot, so
    the slot before this one has to be asked as well."""
    k = int(now / STORM_SLOT)
    for kk in (k, k - 1):
        m = _mix(kk, 0, 617)
        if m % STORM_ODDS:
            continue
        start = kk * STORM_SLOT + ((m >> 8) & 1023) / 1023.0 * STORM_SLOT * 0.5
        length = STORM_SHORT + ((m >> 19) & 255) / 255.0 * (STORM_LONG - STORM_SHORT)
        if start <= now < start + length:
            return start, length
    return None


def storming(now):
    """Is there a storm overhead - either because the sky says so, or because
    the cheat menu is holding one there."""
    lock, forced = WEATHER_STEPS[_weather_i][1:]
    if lock is not None:
        return forced
    return storm_window(now) is not None


def rain_intensity(now):
    """It comes and goes. Three slow waves that never quite line up, so the
    weather swells to a downpour, eases off, and now and then stops - and a
    storm brings its own rain regardless of where those waves happen to be."""
    lock = WEATHER_STEPS[_weather_i][1]
    if lock is not None:
        return lock
    v = (0.42
         + 0.42 * math.sin(now / 41.0)
         + 0.26 * math.sin(now / 13.7 + 1.2)
         + 0.13 * math.sin(now / 5.3 + 2.6))
    storm = storm_window(now)
    if storm is not None:
        start, length = storm
        t = now - start
        edge = min(1.0, t / 7.0, (length - t) / 7.0)    # it rolls in and off
        v = max(v, 0.55 + 0.43 * edge)
    return max(0.0, min(1.0, v))


def _strike_flash(t, twice):
    """The shape of one strike, as a brightness over time.

    Sub-flashes riding on a glow that does not go out until the strike is over.
    The flicker on its own measured as three isolated frames with dark gaps
    between them - at thirty frames a second that is a blink, and one you can
    easily sit through without noticing the city lit up at all. The glow
    underneath is what turns it into a single event you can see, and the tail
    is most of what you actually register.

    Shared, so a strike you asked for is the same strike the storm was going to
    have anyway."""
    if not 0.0 <= t < 1.7:
        return 0.0
    peak = 0.0
    for n, (delay, amp, span) in enumerate(((0.00, 1.00, 0.10),
                                            (0.16, 0.88, 0.09),
                                            (0.36, 0.62, 0.08))):
        if n == 2 and twice:
            break
        if 0.0 <= t - delay < span:
            peak = amp
    return max(peak, 0.78 * math.exp(-t * 2.3))


def lightning(now, wet):
    """How bright the sky is this instant, 0..1.

    Storms only happen in heavy rain, and even then only one slot in seven has
    a strike in it, so this is quiet for minutes at a time - which is the whole
    point of it."""
    if _forced_strike is not None:
        b = _strike_flash(now - _forced_strike, False)
        if b:
            return b                    # asked for, so it comes even when dry
    if not storming(now):
        return 0.0
    # Inside one they come thick and fast: every slot has a strike in it, and a
    # slot is under three seconds, so with a flash lasting most of two the sky
    # is rarely dark for long.
    k = int(now / STRIKE_EVERY)
    m = _mix(k, 0, 401)
    at = k * STRIKE_EVERY + ((m >> 8) & 1023) / 1023.0 * (STRIKE_EVERY - 1.6)
    # Not all of them are overhead. Scaling each strike gives the storm some
    # near ones and some away over the next district, which is the difference
    # between weather and a metronome.
    power = 0.45 + ((m >> 12) & 255) / 255.0 * 0.55
    return power * _strike_flash(now - at, bool((m >> 20) & 1))


def club_light(now, club):
    """What is coming out of the club this instant, or None while it is dark.

    Four to the floor at 134, with the strobe let off the leash at the end of
    every eighth bar."""
    t = now * CLUB_BPM / 60.0 + club["phase"]
    beat = t % 1.0
    if (int(t) // 4) % 8 == 7 and int(t * 8) % 2:
        return FLASH                    # the strobe run that ends a phrase
    if beat < 0.11:
        return FLASH                    # the kick
    if beat < 0.32:
        return NEON[min(len(NEON) - 1, int(club["tone"] * len(NEON)))][0]
    return None


def weather_word(now, wet):
    if storming(now):
        return "storm"
    if wet < 0.04:
        return "dry"
    if wet < 0.3:
        return "drizzle"
    if wet < 0.7:
        return "rain"
    return "downpour"


def draw_rain(ch, co, v, walls, now, wet):
    """Drops on a world-locked lattice, so they hold still relative to the city
    while you walk and turn through them.

    Each is drawn as the short segment it fell through since the frame before,
    which is what gives the streak its slant - both the wind's and the
    perspective's - without any of it being faked in screen space."""
    if wet < 0.03:
        return
    wind = 0.5 * math.sin(now / 23.0) + 0.2 * math.sin(now / 7.1)
    gi = int(v.x / RAIN_LATTICE + BIG) - BIG
    gj = int(v.z / RAIN_LATTICE + BIG) - BIG
    fall = now * RAIN_SPEED / RAIN_TOP
    limit = PLANE * 1.25

    for a in range(gi - RAIN_REACH, gi + RAIN_REACH + 1):
        for b in range(gj - RAIN_REACH, gj + RAIN_REACH + 1):
            m = _mix(a, b, 313)
            if (m & 1023) > wet * 1023:
                continue
            x = (a + ((m >> 10) & 63) / 64.0) * RAIN_LATTICE
            z = (b + ((m >> 16) & 63) / 64.0) * RAIN_LATTICE
            rx, rz = x - v.x, z - v.z
            perp = rx * v.dx + rz * v.dz
            if perp < NEAR_Z:
                continue
            if abs(rx * v.dz - rz * v.dx) > perp * limit:
                continue
            if not open_at(x, z):
                continue                      # it is not raining indoors

            phase = ((m >> 22) & 255) / 256.0
            drop = (fall + phase) % 1.0
            y = RAIN_TOP * (1.0 - drop)
            y0 = min(RAIN_TOP, y + RAIN_SPEED * RAIN_STREAK)
            blow = wind * (RAIN_TOP - y)
            blow0 = wind * (RAIN_TOP - y0)
            p1 = v.project(x + blow, y, z)
            p0 = v.project(x + blow0, y0, z)
            if p0 is None or p1 is None:
                continue
            attr = FLASH if v.flash > 0.35 else (
                RAIN if perp < 9.0 else RAIN_FAR)
            _streak(ch, co, v, walls, p0, p1, attr)


def _streak(ch, co, v, walls, p0, p1, attr):
    x0, y0 = p0[0], p0[1]
    x1, y1 = p1[0], p1[1]
    steps = min(3, max(1, int(abs(y1 - y0))))
    glyph = "|" if abs(x1 - x0) < 0.9 else ("\\" if x1 > x0 else "/")
    for k in range(steps + 1):
        t = k / (steps + 1.0)
        put_cell(ch, co, v, walls,
                 int(round(x0 + (x1 - x0) * t)),
                 int(round(y0 + (y1 - y0) * t)),
                 p1[2], glyph, attr)


# ---------------------------------------------------------------------------

def render_street(v, now):
    ch = [[" "] * v.width for _ in range(v.height)]
    co = [[0] * v.width for _ in range(v.height)]
    wet = v.wet = rain_intensity(now)
    v.flash = lightning(now, wet)

    walls = draw_walls(ch, co, v, now)
    draw_sky(ch, co, v, walls, wet)
    draw_ground(ch, co, v, walls, collect_glow(v, now, wet), wet, now)
    draw_flash(ch, co, v, walls, v.flash)
    draw_props(ch, co, v, walls, now, wet)
    draw_rain(ch, co, v, walls, now, wet)
    return ch, co, wet


# ---------------------------------------------------------------------------
# Finding your way, and letting it find its own
# ---------------------------------------------------------------------------

def start_position():
    """Somewhere out in the open to begin, near the origin."""
    for r in range(0, 40):
        for i in range(-r, r + 1):
            for j in (-r, r):
                for a, b in ((i, j), (j, i)):
                    x, z = (a + 0.5) * CELL, (b + 0.5) * CELL
                    if can_stand(x, z):
                        return x, z
    return 0.0, 0.0


def probe(x, z, yaw, reach):
    """How far you could walk on this bearing before something stopped you."""
    dx, dz = math.sin(yaw), math.cos(yaw)
    d = 1.0
    while d < reach:
        if not can_stand(x + dx * d, z + dz * d):
            return d
        d += 1.2
    return reach


def wander(x, z, yaw, now):
    """Which way to lean, given what is open around you.

    Picking the single best-scoring direction makes the walk twitch, because
    the winner changes outright from one frame to the next. Blending all of
    them by score gives a heading that moves continuously instead, and one that
    sits between two equally good options rather than flipping between them."""
    acc = weight = 0.0
    for off in (-1.15, -0.75, -0.4, -0.15, 0.0, 0.15, 0.4, 0.75, 1.15):
        clear = probe(x, z, yaw + off, 22.0)
        score = clear + 5.0 * math.cos(off) + 7.0 * math.sin(now * 0.11 + off * 2.7)
        w = math.exp(score / 6.0)
        acc += w * off
        weight += w
    return acc / weight


# ===========================================================================
# THE CASINO - the one place in the city you can walk into
# ===========================================================================

# A single-zero wheel, in the order the pockets actually come in. That order is
# not decorative: it is what puts high next to low and red next to black all
# the way round, and you can see it turning.
WHEEL = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
         10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]
REDS = frozenset([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32,
                  34, 36])

DIGITS = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": ("  #", "  #", "  #", "  #", "  #"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
}

# Everything else in this file is a hash of where you are, so that the city is
# the same city every time you walk back to it. This one thing is not, and must
# not be: a wheel you could predict is not a wheel.
_spin_rng = random.Random()

SPIN_TIME = 5.2        # seconds of ball before it drops
HOLD_TIME = 3.6        # and how long the number stays up afterwards


def pocket_colour(n):
    return ROU_GREEN if n == 0 else (ROU_RED if n in REDS else ROU_BLACK)


class Spin:
    """One throw of the ball.

    The result is drawn first and the flight is then solved to end on it: the
    ball eases to a stop over a whole number of turns plus exactly the offset
    that lands it in the right pocket, so the wheel is honest but the animation
    never has to guess where it is going."""

    def __init__(self, now, name):
        self.name = name
        self.t0 = now
        self.result = _spin_rng.randrange(37)
        self.idx = WHEEL.index(self.result)
        self.wheel0 = _spin_rng.uniform(0.0, math.tau)
        self.wheel_w = -0.55 - _spin_rng.random() * 0.25   # the wheel's own turn
        self.ball0 = _spin_rng.uniform(0.0, math.tau)
        turns = 7 + _spin_rng.randrange(4)
        end = self.wheel_at(SPIN_TIME) + self.idx * math.tau / 37.0
        # However far round that is from where the ball starts, plus whole turns.
        self.sweep = turns * math.tau + (end - self.ball0) % math.tau

    def wheel_at(self, t):
        return self.wheel0 + self.wheel_w * t

    def ball_at(self, t):
        if t >= SPIN_TIME:
            return self.wheel_at(t) + self.idx * math.tau / 37.0
        k = 1.0 - t / SPIN_TIME
        return self.ball0 + self.sweep * (1.0 - k * k * k)

    def settled(self, now):
        return now - self.t0 >= SPIN_TIME

    def done(self, now):
        return now - self.t0 >= SPIN_TIME + HOLD_TIME


def casino_at(x, z):
    """The casino you are standing in the doorway of, or None."""
    ci = int(x / CELL + BIG) - BIG
    cj = int(z / CELL + BIG) - BIG
    for i in range(ci - 2, ci + 3):
        for j in range(cj - 2, cj + 3):
            if is_open(i, j):
                continue
            b = lot(i, j)
            if b["casino"] is None:
                continue
            f = b["casino"]["door"]
            nx, nz = FACES[f]
            if not is_open(i + nx, j + nz):
                continue
            p = face_point(i, j, f, b["casino"]["u"], 1.4, 0.0)
            if ((p[0] - x) ** 2 + (p[2] - z) ** 2) < CASINO_REACH ** 2:
                return b["casino"]
    return None


def _text(ch, co, y, x, s, attr):
    for k, c in enumerate(s):
        if 0 <= y < len(ch) and 0 <= x + k < len(ch[0]) and c != " ":
            ch[y][x + k] = c
            co[y][x + k] = attr


def _big(ch, co, y, x, s, attr):
    """The winning number, twice as wide as the little font so it reads square."""
    for row in range(5):
        col = x
        for c in s:
            for px in DIGITS.get(c, ("   ",) * 5)[row]:
                if px != " ":
                    for d in (0, 1):
                        if 0 <= y + row < len(ch) and 0 <= col + d < len(ch[0]):
                            ch[y + row][col + d] = "#"
                            co[y + row][col + d] = attr
                col += 2
            col += 2


def render_casino_room(spin, width, height, now):
    """Inside.

    The wheel is the middle of a room rather than the whole screen: a ceiling
    with the lights chasing along it, a back wall of slot machines with their
    reels going, the table with people round it, and the way out behind you. A
    window with no space for the room gets the wheel on its own, and one with
    no space for that gets a line of text."""
    ch = [[" "] * width for _ in range(height)]
    co = [[0] * width for _ in range(height)]
    t = now - spin.t0
    cx = (width - 1) / 2.0
    top, floor = 1, height - 1
    roomy = width >= 46 and height >= 22

    if roomy:
        for x in range(width):                       # the ceiling, in lights
            lit = chase(x * 0.2, now)
            ch[top][x] = "o" if lit else "="
            co[top][x] = GOLD if lit else GOLD_DIM
        banner = "CASINO  %s" % spin.name
        _text(ch, co, top + 1, int(cx - len(banner) / 2), banner, GOLD)
        for x in range(2, width - 6, 9):             # a back wall of slots
            if abs(x + 2 - cx) < len(banner) / 2 + 3:
                continue
            reel = "".join("7$@&%"[(_mix(x, k, 53) + int(now * (3 + k))) % 5]
                           for k in range(2))
            _text(ch, co, top + 2, x, ".--.", GOLD_DIM)
            _text(ch, co, top + 3, x, "|" + reel + "|", ROU_RED)
        band_top, band_bot = top + 5, floor - 5
    else:
        band_top, band_bot = top, floor - 1

    cy = (band_top + band_bot) / 2.0
    rx = min(width * 0.36, 42.0)
    ry = min((band_bot - band_top) / 2.0 - 0.5, 10.0)

    if rx < 12 or ry < 3.5:
        say = ("%s: %d" % (spin.name, spin.result) if spin.settled(now)
               else "%s: ..." % spin.name)
        _text(ch, co, height // 2, 1, say,
              pocket_colour(spin.result) if spin.settled(now) else GOLD)
        _text(ch, co, min(height - 1, height // 2 + 2), 1, "s to leave", GOLD_DIM)
        return ch, co

    wa = spin.wheel_at(t)
    for k, num in enumerate(WHEEL):
        a = wa + k * math.tau / 37.0
        label = str(num)
        x = int(round(cx + rx * math.cos(a))) - (len(label) - 1) // 2
        y = int(round(cy - ry * math.sin(a)))
        attr = pocket_colour(num)
        if spin.settled(now) and num == spin.result:
            attr = attr | curses.A_REVERSE
        _text(ch, co, y, x, label, attr)

    # The ball rides inside the numbers, and drops into the pocket at the end.
    ba = spin.ball_at(t)
    br = 0.72 if t < SPIN_TIME else 0.86
    bx = int(round(cx + rx * br * math.cos(ba)))
    by = int(round(cy - ry * br * math.sin(ba)))
    if 0 <= by < height and 0 <= bx < width:
        ch[by][bx] = "O" if spin.settled(now) else "o"
        co[by][bx] = FLASH

    mid = int(cy)
    if spin.settled(now):
        label = str(spin.result)
        w = len(label) * 8 - 2
        _big(ch, co, mid - 2, int(cx - w / 2), label, pocket_colour(spin.result))
        word = "ZERO" if spin.result == 0 else (
            "RED" if spin.result in REDS else "BLACK")
        if spin.result:
            word += "  %s  %s" % ("EVEN" if spin.result % 2 == 0 else "ODD",
                                  "1-18" if spin.result <= 18 else "19-36")
        _text(ch, co, mid + 4, int(cx - len(word) / 2), word,
              pocket_colour(spin.result))
    else:
        say = "RIEN NE VA PLUS" if t > SPIN_TIME * 0.45 else "FAITES VOS JEUX"
        _text(ch, co, mid, int(cx - len(say) / 2), say, GOLD_DIM)

    if roomy:
        edge = min(height - 4, int(cy + ry) + 2)     # the near lip of the table
        for x in range(max(0, int(cx - rx - 4)), min(width, int(cx + rx + 5))):
            ch[edge][x] = "="
            co[edge][x] = GOLD_DIM
        for k in range(-2, 3):                       # and who is round it
            px = int(cx + k * 15) - 2
            for r, line in enumerate((" ,-. ", "(- -)", "_/|\\_")):
                if edge + 1 + r < height:
                    _text(ch, co, edge + 1 + r, px, line, CURB)

    _text(ch, co, floor, max(1, int(cx) - 10), "-- the way out (s) --", GOLD_DIM)
    return ch, co


# ===========================================================================
# CHEATS
#
# None of this generates anything. The city is a pure function of its
# coordinates, so the menu searches for what is already out there and moves you
# to it: a club is exactly as rare after you have teleported to one as it was
# before, and CLUB_ODDS is only ever read, never leaned on. A jump is precisely
# equivalent to having walked there.
# ===========================================================================

CHEAT_KEY = ord("`")
CHEAT_SKIP = 40.0      # ignore what is already underfoot, so a repeat moves on
CHEAT_RINGS = 200      # cells of search before giving up and saying so

CHEAT_PLACES = [
    ("1", "club", "club"),
    ("2", "casino", "casino"),
    ("3", "dark alley", "alley"),
    ("4", "dead end", "deadend"),
    ("5", "crossroads", "crossroads"),
    ("6", "long street", "street"),
    ("7", "back to start", "start"),
    ("8", "far away", "far"),
    ("9", "inside a casino", "inside"),
]


def _rings(ci, cj, limit):
    """Cells outward from (ci, cj), nearest ring first."""
    yield ci, cj
    for r in range(1, limit):
        for d in range(-r, r):
            yield ci + d, cj - r
            yield ci + r, cj + d
            yield ci - d, cj + r
            yield ci - r, cj - d


def best_view(x, z):
    """Whichever way you can see furthest from here, and how far that is."""
    best, ang = -1.0, 0.0
    for k in range(8):
        a = k * math.tau / 8.0
        d = probe(x, z, a, 45.0)
        if d > best:
            best, ang = d, a
    return ang, best


def best_yaw(x, z):
    return best_view(x, z)[0]


def _door_spot(i, j, salt, key):
    """A club or a casino, and somewhere to stand looking at its door.

    The hash is tested before is_open(), because it throws out 1199 cells in
    1200 for the price of one multiply, and lot() - which builds a whole
    building - is only ever reached on an actual hit."""
    if _mix(i, j, salt) % CLUB_ODDS or is_open(i, j):
        return None
    place = lot(i, j)[key]
    if place is None:
        return None
    f = place["door"]
    nx, nz = FACES[f]
    if not is_open(i + nx, j + nz):
        return None
    # Far enough back to see the whole front of it. Standing on the doorstep
    # puts your nose against a facade and shows you nothing.
    for back in (9.0, 7.0, 11.0, 5.5, 13.0, 4.0, 3.0):
        p = face_point(i, j, f, place["u"], back, 0.0)
        if can_stand(p[0], p[2]):
            return p[0], p[2], math.atan2(-nx, -nz)
    return None


def _alley_spot(i, j, dead):
    if not is_open(i, j) or road_at(i, j):
        return None
    if dead:
        # A dead end is an alley cell walled on three sides. Stand in the cell
        # in front of it and look in, which is the view worth having.
        ways = [(a, b) for a, b in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1))
                if is_open(a, b)]
        if len(ways) != 1:
            return None
        a, b = ways[0]
        x, z = (a + 0.5) * CELL, (b + 0.5) * CELL
        if not can_stand(x, z):
            return None
        return x, z, math.atan2(float(i - a), float(j - b))
    x, z = (i + 0.5) * CELL, (j + 0.5) * CELL
    if not can_stand(x, z):
        return None
    # A one-cell courtyard walled in on every side is technically an alley and
    # is no use to look at, so insist on being able to see down it.
    yaw, clear = best_view(x, z)
    if clear < 9.0:
        return None
    return x, z, yaw


def _crossroads_spot(i, j):
    if not (_is_road(i, XP, 91) and _is_road(j, ZP, 137)) or not is_open(i, j):
        return None
    x, z = (i + 0.5) * CELL, (j + 0.5) * CELL
    if not can_stand(x, z):
        return None
    return x, z, best_yaw(x, z)


def _long_street(x, z):
    """The longest clear run of street nearby. Stops as soon as it finds one
    worth looking down, because probing every road cell for a hundred units is
    far more work than the rest of the menu put together."""
    ci = int(x / CELL + BIG) - BIG
    cj = int(z / CELL + BIG) - BIG
    best = None
    for i, j in _rings(ci, cj, 26):
        if not (is_open(i, j) and road_at(i, j)):
            continue
        px, pz = (i + 0.5) * CELL, (j + 0.5) * CELL
        if (px - x) ** 2 + (pz - z) ** 2 < CHEAT_SKIP ** 2 or not can_stand(px, pz):
            continue
        for k in range(4):
            a = k * math.tau / 4.0
            run = probe(px, pz, a, 110.0)
            if best is None or run > best[0]:
                best = (run, px, pz, a)
        if best is not None and best[0] >= 90.0:
            break
    return None if best is None else (best[1], best[2], best[3])


def _far_away(x, z):
    a = _mix(int(x), int(z), 733)
    ang = (a & 0xFFFF) / 65535.0 * math.tau
    dist = 3000.0 + (a >> 16) % 5000
    ci = int((x + math.sin(ang) * dist) / CELL + BIG) - BIG
    cj = int((z + math.cos(ang) * dist) / CELL + BIG) - BIG
    for i, j in _rings(ci, cj, 60):
        if not is_open(i, j):
            continue
        px, pz = (i + 0.5) * CELL, (j + 0.5) * CELL
        if can_stand(px, pz):
            return px, pz, best_yaw(px, pz)
    return None


def _casino_doorway(x, z):
    """Standing in the door of the nearest casino, which is what puts you in
    it. No skipping the one underfoot here: if you are outside a casino and ask
    to go in, you mean that one."""
    ci = int(x / CELL + BIG) - BIG
    cj = int(z / CELL + BIG) - BIG
    for i, j in _rings(ci, cj, CHEAT_RINGS):
        if _mix(i, j, 911) % CLUB_ODDS or is_open(i, j):
            continue
        c = lot(i, j)["casino"]
        if c is None:
            continue
        f = c["door"]
        nx, nz = FACES[f]
        if not is_open(i + nx, j + nz):
            continue
        p = face_point(i, j, f, c["u"], 1.4, 0.0)
        if can_stand(p[0], p[2]):
            return p[0], p[2], math.atan2(-nx, -nz)
    return None


def find_place(x, z, kind):
    """Somewhere of this kind to stand, and which way to face. None if there is
    none within reach - the panel says so rather than the search running on."""
    if kind == "start":
        sx, sz = start_position()
        return sx, sz, best_yaw(sx, sz)
    if kind == "street":
        return _long_street(x, z)
    if kind == "far":
        return _far_away(x, z)
    if kind == "inside":
        return _casino_doorway(x, z)

    ci = int(x / CELL + BIG) - BIG
    cj = int(z / CELL + BIG) - BIG
    skip = CHEAT_SKIP ** 2
    for i, j in _rings(ci, cj, CHEAT_RINGS):
        if kind == "club":
            hit = _door_spot(i, j, 909, "club")
        elif kind == "casino":
            hit = _door_spot(i, j, 911, "casino")
        elif kind == "crossroads":
            hit = _crossroads_spot(i, j)
        else:
            hit = _alley_spot(i, j, kind == "deadend")
        if hit is not None and (hit[0] - x) ** 2 + (hit[1] - z) ** 2 >= skip:
            return hit
    return None


def draw_cheats(ch, co, note):
    """The panel, drawn over the live frame rather than instead of it, so that
    what you change to the weather happens in front of you."""
    height, width = len(ch), len(ch[0])
    body = [("", "CHEATS")]
    body += [(k, name) for k, name, _ in CHEAT_PLACES]
    body += [("", ""), ("w", "weather: " + weather_name()), ("L", "strike now"),
             ("", ""), ("`", "close")]
    if note:
        body += [("", note)]

    w, h = 23, len(body) + 2
    if width < w + 4 or height < h + 2:
        return                          # no room; the city is more use
    x0, y0 = 2, 1
    for r in range(h):
        for c in range(w):
            corner = r in (0, h - 1) and c in (0, w - 1)
            side = r in (0, h - 1) or c in (0, w - 1)
            ch[y0 + r][x0 + c] = ("+" if corner else
                                  "-" if r in (0, h - 1) else
                                  "|" if side else " ")
            co[y0 + r][x0 + c] = GOLD_DIM if side else 0
    for r, (k, label) in enumerate(body):
        y = y0 + 1 + r
        if k:
            _text(ch, co, y, x0 + 2, k, GOLD)
            _text(ch, co, y, x0 + 4, label[:w - 6], HUD)
        elif label:
            _text(ch, co, y, x0 + 2, label[:w - 4],
                  GOLD if label == "CHEATS" else FLASH)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

ARROWS = (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN)

SKYLINE_HUD = " x=%-6d %s  tab view  arrows/hl walk  HL run  space wander  ` cheats "
STREET_HUD = " %d,%d %s  tab view  ws walk  ad turn  ,. step  space wander  ` cheats "
ROULETTE_HUD = " inside the %s casino at %d,%d  -  s walks back out  -  q quit "


def main(stdscr):
    init_colors()
    curses.curs_set(0)          # hide the cursor
    stdscr.nodelay(True)        # don't block waiting for a keypress
    stdscr.keypad(True)         # decode arrow keys into KEY_LEFT / KEY_RIGHT

    street = True               # which view we are in
    sky_x = 0.0                 # the two views keep their own cameras, so
    sky_v = 0.0                 # switching back and forth loses neither
    cam_x, cam_z = start_position()
    yaw = 0.0
    fwd = side = spin = 0.0
    last = {"sky": 0.0, "f": 0.0, "s": 0.0, "t": 0.0}
    steer = 0.0                 # the autopilot's heading, eased not snapped
    blocked = False             # did the last step run into something
    wheel = None                # the roulette, while you stand in a casino
    wheel_at = 0.0
    cheats = False              # is the cheat panel up
    note = ""                   # and what did it last do
    autopilot = False
    frame_time = 1.0 / FPS

    while True:
        now = time.monotonic()

        # --- input -----------------------------------------------------
        key = stdscr.getch()
        while key != -1:
            if key == curses.KEY_RESIZE:
                _cache.clear()
            elif cheats and key not in ARROWS:
                # The letters belong to the panel while it is up. The arrows
                # do not, so you can still walk about with it open, which is
                # the whole reason it is drawn over the view.
                if key in (CHEAT_KEY, 27):
                    cheats, note = False, ""
                elif key == ord("q"):
                    return
                elif key == ord("w"):
                    note = "weather: " + cycle_weather()
                elif key == ord("L"):
                    force_strike(now)
                    note = "lightning"
                else:
                    for k, name, kind in CHEAT_PLACES:
                        if key != ord(k):
                            continue
                        spot = find_place(cam_x, cam_z, kind)
                        if spot is None:
                            note = "no " + name + " near"
                        else:
                            cam_x, cam_z, yaw = spot
                            fwd = side = spin = steer = 0.0
                            autopilot = False
                            wheel = None
                            street = True
                            note = "-> " + name
                        break
            elif key in (ord("q"), 27):
                return
            elif key == CHEAT_KEY:
                cheats, note = True, ""
            elif key in (ord("\t"), ord("v")):
                street = not street
                sky_v = fwd = side = spin = 0.0
            elif key == ord(" "):
                autopilot = not autopilot
            elif street:
                if key in (curses.KEY_UP, ord("w"), ord("W")):
                    fwd = WALK * (RUN_MULTIPLIER if key == ord("W") else 1.0)
                    last["f"], autopilot = now, False
                elif key in (curses.KEY_DOWN, ord("s"), ord("S")):
                    fwd = -WALK * (RUN_MULTIPLIER if key == ord("S") else 1.0)
                    last["f"], autopilot = now, False
                elif key in (curses.KEY_LEFT, ord("a")):
                    spin, last["t"], autopilot = -TURN, now, False
                elif key in (curses.KEY_RIGHT, ord("d")):
                    spin, last["t"], autopilot = TURN, now, False
                elif key == ord(","):
                    side, last["s"], autopilot = -SIDESTEP, now, False
                elif key == ord("."):
                    side, last["s"], autopilot = SIDESTEP, now, False
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
            # Ease towards the heading rather than jumping onto it, and slow
            # down through a turn the way you would walking round a corner -
            # but turn away smartly when the last step got you nowhere.
            steer += ((wander(cam_x, cam_z, yaw, now) - steer)
                      * min(1.0, frame_time * (7.0 if blocked else 3.0)))
            spin = max(-TURN, min(TURN, steer * 2.0))
            want = WALK * (0.66 - 0.30 * min(1.0, abs(steer)))
            fwd += (want - fwd) * min(1.0, frame_time * 2.5)
            side = 0.0
            sky_v += (WALK_SPEED * 0.6 - sky_v) * min(1.0, frame_time * 2.5)
        else:
            if now - last["sky"] > KEY_GRACE:
                sky_v = 0.0
            if now - last["f"] > KEY_GRACE:
                fwd = 0.0
            if now - last["s"] > KEY_GRACE:
                side = 0.0
            if now - last["t"] > KEY_GRACE:
                spin = 0.0

        sky_x += sky_v * frame_time
        yaw += spin * frame_time
        # Move on each axis in turn, so running into a wall at an angle slides
        # you along it instead of stopping you dead.
        dx = math.sin(yaw) * fwd + math.cos(yaw) * side
        dz = math.cos(yaw) * fwd - math.sin(yaw) * side
        nx = cam_x + dx * frame_time
        moved = False
        if can_stand(nx, cam_z):
            cam_x, moved = nx, True
        nz = cam_z + dz * frame_time
        if can_stand(cam_x, nz):
            cam_z, moved = nz, True
        blocked = not moved and (fwd or side)

        # Walking into a casino doorway is the one way into a building in
        # this city. Walk back out and the wheel goes away; stand there and it
        # deals you another, because that is what a casino is for.
        house = casino_at(cam_x, cam_z) if street else None
        if house is None:
            wheel = None
        elif wheel is None or (wheel.done(now)
                               and now - wheel_at > CASINO_AGAIN):
            wheel = Spin(now, house["name"])
            wheel_at = now + SPIN_TIME + HOLD_TIME

        # --- draw ------------------------------------------------------
        height, width = stdscr.getmaxyx()
        if wheel is not None:
            ch, co = render_casino_room(wheel, width, height, now)
            hud = ROULETTE_HUD % (wheel.name, cam_x, cam_z)
        elif street:
            v = View(cam_x, cam_z, yaw, width, height)
            ch, co, wet = render_street(v, now)
            hud = STREET_HUD % (cam_x, cam_z, weather_word(now, wet))
        else:
            ch, co = render_skyline(sky_x, width, height, now)
            hud = SKYLINE_HUD % (sky_x, weather_word(now, rain_intensity(now)))
        if cheats:
            draw_cheats(ch, co, note)

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
