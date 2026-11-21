#!/usr/bin/env python3
"""Sunken — a roguelike in a drowned city, in the terminal.

Go down twelve depths of the sunken city of Vell, take the Tide-Heart from the altar in the
Drowned King's palace, and get back to the current you came in by while the palace floods.
Death is final; the hall of records remembers every run.

- The water moves. Each depth has a tide that rises and falls on a clock, so passages open at
  low tide and flood at high, and the city sits lower the deeper you go. Deep water hides you
  from things on land and quenches your lantern, but every turn under costs breath.
- Darkness is cover. The lantern shows the way and gives you away; shutter it and you see only
  what other lights show. Monsters spot you by light and hear you by noise: wading is loud,
  sneaking is quiet, and anything struck while it is unaware takes several times the damage.
- You become what you find. There are no classes: skills grow with use, and relics, pacts made
  at drowned shrines, and gear shape the build.
- Vials, shells and rods are unknown until tried; what they look like is shuffled every run.
- Beasts can be tamed — weakened, asleep, or busy with bait — and fight beside you.

The run is saved when you quit (S) and on every new depth, and picked up again next time.
"""

import argparse
import curses
import heapq
import json
import locale
import os
import pickle
import random
import tempfile
import textwrap
import time
from collections import deque

W, H = 80, 28                  # the map; the window scrolls over it when smaller
MAX_DEPTH = 12
STATE_DIR = os.path.expanduser("~/.local/state/sunken_tui")
SAVE_PATH = os.path.join(STATE_DIR, "save.pickle")
RECORDS_PATH = os.path.join(STATE_DIR, "records.json")
MIN_W, MIN_H = 80, 24
LOG_LINES = 3
VIEW_RADIUS = 14
SUBMERGED_VIEW = 3             # how far you see with your head under water
LANTERN_RADIUS = 4
LAMP_RADIUS = 5
CORAL_RADIUS = 2
OIL_START, OIL_MAX, OIL_FLASK = 650, 1000, 300
HP_START, HP_PER_DEPTH = 24, 4
BREATH_BASE = 12
TIDE_PERIOD = 60               # turns between steps of the tide
TIDES = ("low", "mid", "high", "mid")
TIDE_SHIFT = {"low": -0.07, "mid": 0.0, "high": 0.08}   # share of the floor added to the water
THROW_RANGE = 8
MAX_INVENTORY = 22
SKILL_STEPS = (0, 6, 15, 28, 45, 66, 92, 124, 162, 206)   # experience for each skill level
SKILLS = ("melee", "stealth", "swimming", "aim", "kinship")
LETTERS = "abcdefghijklmnopqrstuvwxyz"

rng = random.Random()

DIRS8 = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
DIRS4 = ((0, -1), (-1, 0), (1, 0), (0, 1))

# -- tiles --

WALL, FLOOR, DOOR, OPEN, STAIRS, CORAL, KELP, STATUE, SHRINE, SPENT, ALTAR, EXIT, LAMP, DARK_LAMP = range(14)
WALKABLE = frozenset({FLOOR, OPEN, STAIRS, KELP, SHRINE, SPENT, ALTAR, EXIT})
OPAQUE = frozenset({WALL, DOOR, CORAL, KELP})
SOLID = frozenset({WALL, CORAL})           # sound and gas don't pass
TILES = {  # tile: (glyph, colour, description)
    WALL: ("#", "stone", "a wall of sunken stone"),
    FLOOR: (".", "sand", "silted paving"),
    DOOR: ("+", "brown", "a swollen wooden door"),
    OPEN: ("'", "brown", "an open door"),
    STAIRS: (">", "white", "stairs going down"),
    CORAL: ("§", "magenta", "glowing coral"),
    KELP: ('"', "green", "a stand of kelp — nothing sees through it, or into it"),
    STATUE: ("Ω", "pale", "a barnacled statue"),
    SHRINE: ("_", "teal", "a drowned shrine — step on it to hear its offer"),
    SPENT: ("_", "gray", "a shrine whose pact is made"),
    ALTAR: ("_", "gold", "the altar of the Tide-Heart"),
    EXIT: ("◎", "cyan", "the upward current — bring the Tide-Heart here"),
    LAMP: ("¶", "yellow", "a lit street lamp (walk into it to douse it)"),
    DARK_LAMP: ("¶", "gray", "a dark street lamp (walk into it to light it)"),
}
FLOOR_COLOUR = {"streets": "sand", "canals": "sand", "cathedral": "stone", "caves": "brown", "palace": "pale"}

# name: (256-colour, 8-colour, bold in 8 colours)
PALETTE = {
    "white": (255, 7, True), "pale": (252, 7, False), "gray": (245, 7, False), "dark": (239, 0, True),
    "stone": (246, 7, False), "sand": (180, 3, False), "brown": (137, 3, False), "red": (196, 1, True),
    "green": (71, 2, False), "lime": (118, 2, True), "yellow": (227, 3, True), "gold": (214, 3, True),
    "orange": (208, 3, False), "blue": (33, 4, True), "deep": (25, 4, False), "shallow": (38, 6, False),
    "cyan": (51, 6, True), "teal": (37, 6, False), "magenta": (170, 5, True), "purple": (99, 5, False),
    "pink": (211, 5, True), "ice": (159, 6, True),
}

# -- the depths --

DEPTHS = {  # depth: (name, theme, said on arrival)
    1: ("The Drowned Docks", "streets", "Water laps at the quays of Vell. Somewhere far below, the Tide-Heart beats."),
    2: ("The Fishmarket", "streets", "Rotten stalls and the smell of old brine."),
    3: ("The Lantern Streets", "streets", "Some of the old street lamps still burn. Who keeps them lit?"),
    4: ("The Canal Quarter", "canals", "Black canals cut through the streets, and the bridges are few."),
    5: ("The Bathhouses", "canals", "Steam once rose here. Now only the water does."),
    6: ("The Guild Halls", "canals", "The great halls of the merchant guilds, half under water."),
    7: ("The Sunken Cathedral", "cathedral", "Pillars rise into the dark. The dead still keep the rites."),
    8: ("The Ossuary", "cathedral", "Bones in the walls, bones in the water."),
    9: ("The Archive of Tides", "cathedral", "Here the priests measured the sea. The sea measured them back."),
    10: ("The Abyssal Cleft", "caves", "The city ends and the rock splits open. Coral glows in the deep."),
    11: ("The Pale Gardens", "caves", "Gardens of white coral, and things that tend them."),
    12: ("The Drowned Palace", "palace", "The King's palace. The Tide-Heart lies on its altar, and the King sleeps beside it."),
}
RELIC_DEPTHS = {2, 3, 5, 7, 9, 11}
SHRINE_DEPTHS = {3, 6, 9}
HERMIT_DEPTHS = {4, 8}

# -- creatures --


def _m(name, glyph, colour, depth, hp, dmg, acc, ac, speed, sight, dark, flags, verb, desc, pack=(1, 1), freq=5):
    return {"name": name, "glyph": glyph, "colour": colour, "depth": depth, "hp": hp, "dmg": dmg, "acc": acc,
            "ac": ac, "speed": speed, "sight": sight, "dark": dark, "flags": frozenset(flags.split()),
            "verb": verb, "desc": desc, "pack": pack, "freq": freq}


# sight: how far it sees you when you stand in light; dark: when you don't
MONSTERS = {
    "rat": _m("gutter rat", "r", "brown", (1, 4), 5, "1d2", 1, 1, 110, 7, 2, "beast swim", "bites",
              "A gutter rat, slick from the canals. They come in squabbling packs.", (2, 3), 10),
    "crab": _m("mudcrab", "c", "red", (1, 5), 9, "1d3", 1, 3, 80, 5, 1, "beast swim", "pinches",
               "A mudcrab with a shell like a cobblestone. Slow, and hard to hurt.", (1, 2), 8),
    "moth": _m("lamp moth", "m", "pale", (1, 6), 4, "1d2", 3, 3, 130, 10, 0, "beast fly oil", "flutters at",
               "A pale moth as big as a hand, mad for light. It drinks the oil out of a lantern.", (1, 3), 6),
    "leech": _m("bog leech", "l", "green", (1, 6), 7, "1d3", 2, 1, 90, 4, 2, "aquatic swim drain", "latches onto",
                "A bog leech, a black ribbon in the water. It drinks what it bites.", freq=7),
    "drowned": _m("drowned sailor", "z", "teal", (1, 8), 12, "1d5", 1, 1, 70, 0, 1, "undead swim blind", "claws",
                  "A drowned sailor, still walking the streets it died in. Blind — but it hears every splash.",
                  (1, 2), 9),
    "scav": _m("scavenger", "h", "orange", (2, 7), 10, "1d5", 2, 2, 100, 8, 1, "humanoid light thief", "stabs",
               "A scavenger picking the city clean, a hooded lantern at its belt. Quick fingers.", freq=7),
    "eel": _m("grey eel", "e", "cyan", (3, 9), 11, "1d6", 3, 2, 110, 5, 3, "beast aquatic deep swim shock", "bites",
              "A grey eel, unseen in deep water until it strikes. Its bite sends a shock through the water."),
    "hound": _m("tide hound", "d", "blue", (3, 9), 12, "1d6", 3, 2, 120, 8, 3, "beast swim", "bites",
                "A tide hound, lean and web-footed. Hunts in packs, and swims as well as it runs.", (2, 3), 8),
    "cut": _m("cutthroat", "h", "red", (4, 10), 16, "1d8", 4, 3, 110, 8, 2, "humanoid stealthy", "knifes",
              "A cutthroat who works the dark lanes. You rarely see one before it strikes.", freq=7),
    "angler": _m("anglerjaw", "A", "yellow", (4, 11), 20, "2d5", 4, 3, 90, 6, 3,
                 "beast aquatic deep swim lure ambush", "snaps at",
                 "An anglerjaw. The little light dancing over the deep water is its lure.", freq=6),
    "bloat": _m("bloated corpse", "b", "green", (4, 10), 9, "1d3", 1, 0, 60, 3, 1, "undead swim burst", "slaps",
                "A bloated corpse, swollen with rot. Puncture it and the gas comes out.", freq=6),
    "shell": _m("shellback", "C", "red", (5, 12), 30, "2d4", 3, 7, 80, 6, 2, "beast swim", "crushes",
                "A shellback, a crab the size of a cart. Its shell turns most blades.", freq=6),
    "jelly": _m("glass jelly", "j", "ice", (6, 12), 14, "1d4", 5, 1, 80, 3, 3, "aquatic swim paralyze unseen",
                "stings", "A glass jelly, near invisible in water. Its sting numbs the limbs.", freq=6),
    "priest": _m("drowned priest", "p", "purple", (7, 12), 22, "1d6", 3, 3, 100, 8, 2, "undead humanoid summon",
                 "strikes", "A drowned priest, still saying the old rites. The dead rise when it calls.", freq=5),
    "wraith": _m("lantern wraith", "W", "pale", (7, 12), 18, "1d5", 5, 4, 120, 10, 1, "undead fly oil douse",
                 "touches", "A lantern wraith, drawn to flame. Its touch drinks the light out of a lantern.", freq=5),
    "stalker": _m("reef stalker", "S", "teal", (7, 12), 24, "2d5", 5, 4, 110, 9, 4, "beast swim stealthy", "rakes",
                  "A reef stalker, patient and silent. It strikes from where you aren't looking.", freq=6),
    "golem": _m("salt golem", "G", "stone", (8, 12), 45, "2d7", 4, 5, 70, 6, 1, "construct dissolve", "pounds",
                "A salt golem, packed hard by the sea. Deep water slowly dissolves it.", freq=4),
    "siren": _m("siren", "s", "pink", (8, 12), 20, "1d6", 4, 3, 100, 8, 3, "aquatic swim song", "rakes",
                "A siren, singing from the water. Her song pulls you toward the deep.", freq=5),
    "kelp": _m("kelp horror", "K", "lime", (9, 12), 34, "2d4", 5, 3, 100, 3, 3, "stationary grab hidden", "lashes",
               "A kelp horror, a knot of weed that grabs and holds whatever passes.", freq=4),
    "knight": _m("pale knight", "P", "pale", (10, 12), 40, "2d8", 6, 7, 100, 7, 2, "undead humanoid", "cleaves",
                 "A pale knight of the drowned court, in barnacled plate.", freq=5),
    "lurker": _m("abyssal lurker", "L", "purple", (10, 12), 50, "3d5", 6, 4, 100, 6, 4, "aquatic deep swim ambush",
                 "engulfs", "An abyssal lurker: a mouth in the dark water, waiting.", freq=4),
    "king": _m("the Drowned King", "Ψ", "gold", (12, 12), 140, "3d6", 7, 6, 100, 9, 4,
               "undead humanoid boss summon surge swim", "smites",
               "The Drowned King, keeper of the Tide-Heart. He sleeps lightly on his sunken throne.", freq=0),
    "hermit": _m("hermit", "H", "green", (0, 0), 30, "1d2", 0, 2, 100, 6, 2, "humanoid neutral", "pokes",
                 "A hermit who trades in the dark for pearls. Bump into them to see their wares.", freq=0),
}
PET_NAMES = ("Pip", "Barnacle", "Moss", "Silt", "Brine", "Wick", "Tuck", "Juniper", "Scuttle", "Pebble", "Bramble",
             "Gristle", "Marrow", "Fennel", "Drift", "Kipper", "Nib", "Sorrel", "Tansy", "Wren")

# -- things to find --

WEAPONS = {  # key: (name, damage, accuracy, sneak attack multiplier, from depth, flags)
    "knife": ("rusted knife", "1d4", 1, 4, 0, ""),
    "dirk": ("sailor's dirk", "1d5", 2, 5, 1, ""),
    "hook": ("boat hook", "1d6", 0, 3, 1, "pierce"),
    "cutlass": ("cutlass", "1d8", 1, 3, 2, ""),
    "harpoon": ("harpoon", "1d7", 0, 3, 3, "throw"),
    "maul": ("anchor maul", "2d6", -2, 2, 4, "heavy knock"),
    "trident": ("trident", "2d4", 1, 3, 6, "pierce"),
    "saber": ("coral saber", "2d5", 2, 4, 8, ""),
    "axe": ("whalebone axe", "3d4", -1, 2, 9, "heavy"),
}
WEAPON_NOTES = {"pierce": "hits the creature behind the one you strike, too",
                "throw": "made for throwing: 2d6 when thrown", "heavy": "slow to swing",
                "knock": "knocks creatures back"}
ARMOURS = {  # key: (name, armour, weight, noise, from depth, flags)
    "coat": ("oilskin coat", 1, 0, 0, 0, ""),
    "leathers": ("salt-stiff leathers", 2, 1, 0, 1, ""),
    "sharkskin": ("sharkskin vest", 3, 1, 0, 3, "swim"),
    "diving": ("diver's harness", 2, 2, 1, 3, "breath"),
    "scale": ("fish-scale mail", 5, 2, 1, 4, ""),
    "plate": ("drowned knight's plate", 7, 3, 2, 7, ""),
    "pearl": ("pearl-shell mail", 5, 1, 0, 9, ""),
}
VIALS = {  # key: (name, frequency, what it does)
    "mending": ("mending", 22, "Closes wounds and draws out poison."),
    "gills": ("gills", 10, "Lets you breathe water for a long while."),
    "nightsight": ("nightsight", 10, "Lets you see in the dark for a long while."),
    "swiftness": ("swiftness", 8, "Makes you fast for a short while."),
    "vigor": ("vigor", 6, "Toughens you for good: four more maximum health."),
    "brine": ("brine", 10, "Poison. Better thrown, where it bursts into a poisonous cloud."),
    "murk": ("murk", 9, "Confuses whoever drinks it, or is splashed by it."),
    "phosphor": ("phosphor", 9, "Makes the drinker glow. Thrown, it coats the ground in glowing algae."),
    "ink": ("ink", 9, "Bursts into a cloud of ink that nothing can see through."),
}
SHELLS = {
    "mapping": ("mapping", 12, "Shows you the layout of the whole depth."),
    "current": ("the current", 10, "Sweeps you away to somewhere else on this depth."),
    "lore": ("lore", 14, "Tells you what one unknown thing you carry is."),
    "song": ("kinship", 7, "A song that tames the nearest beast you can see."),
    "tide": ("the tide", 10, "Turns the tide to its next stage."),
    "warding": ("warding", 9, "Hurls back and frightens everything next to you."),
    "echo": ("echoes", 8, "A roar that wakes the whole depth. Best avoided."),
    "swarm": ("the swarm", 6, "Calls up hostile creatures around you. Best avoided."),
    "honing": ("honing", 10, "Sharpens your weapon (+1), or your armour if you have no weapon."),
    "cleansing": ("cleansing", 6, "Crumbles any cursed charm you wear."),
}
RODS = {
    "spark": ("sparks", 10, "A bolt of lightning — and lightning spreads through water."),
    "frost": ("frost", 9, "A bolt of cold that freezes the water around where it lands."),
    "slumber": ("slumber", 9, "Puts a creature to sleep."),
    "blink": ("blinking", 7, "Carries you in an instant along the line you point it."),
    "beckon": ("beckoning", 7, "Yanks a creature to you."),
    "slowing": ("slowing", 8, "Slows a creature down."),
}
CHARMS = {
    "hush": ("hush", 10, "Your steps are quieter."),
    "knitting": ("knitting", 9, "Your wounds close three times as fast."),
    "guard": ("guarding", 10, "+2 armour."),
    "fury": ("fury", 9, "+2 melee damage."),
    "tides": ("the tides", 8, "You swim faster and hold your breath longer."),
    "sight": ("sight", 8, "Your lantern reaches a tile further, and you see what hides under the water."),
    "clamor": ("clamor", 7, "Cursed: every step you take rings out."),
    "anchor": ("the anchor", 6, "Cursed: it drags you under in deep water."),
}
CURSED = frozenset({"clamor", "anchor"})
VIAL_LOOKS = (("murky green", "green"), ("pearly", "white"), ("inky", "purple"), ("amber", "orange"),
              ("rust-red", "red"), ("sea-blue", "blue"), ("milky", "pale"), ("violet", "magenta"),
              ("silvery", "stone"), ("bile-yellow", "yellow"), ("rose", "pink"), ("smoky", "gray"))
ROD_LOOKS = ("driftwood", "bone", "coral", "brass", "black glass", "whalebone", "iron", "jade", "pewter")
CHARM_LOOKS = ("red-cord knot", "shark-tooth", "sea-glass bead", "brass fish", "carved bone", "silver hook",
               "pearl ring", "wire tangle", "jet button")
SYLLABLES = ("vor", "kesh", "ul", "tha", "mar", "oru", "sel", "dri", "nak", "ith", "bo", "quel", "zan", "ep",
             "lo", "rhu", "sa", "ten", "gal", "om", "vex", "ira")
RELICS = {  # key: (name, what it does, cooldown when activated, or 0 when it is always on)
    "fang": ("Lamprey Fang", "Your blows drink: you heal a quarter of the melee damage you deal.", 0),
    "pearl": ("Pearl of the Deep", "You breathe water as easily as air.", 0),
    "monocle": ("Cartographer's Monocle", "The layout of each new depth is known as you arrive.", 0),
    "clock": ("Tide Clock", "Activate to turn the tide to its next stage.", 60),
    "bell": ("Bell of Saint Vell", "Activate: a peal that stuns everything within 4 — and wakes the depth.", 50),
    "ink": ("Ink Sac of the Old Squid", "Activate: a cloud of ink around you that nothing sees through.", 40),
    "gloves": ("Eelskin Gloves", "Your blows shock anything standing in water, for 1d6 more.", 0),
    "wick": ("Everburning Wick", "Your lantern burns without oil.", 0),
    "whistle": ("Whistle of the Pack", "Two more companions can follow you. Activate to call them to you.", 40),
    "shell": ("Hermit Crab Shell", "+3 armour, and every blow against you is 1 weaker.", 0),
    "mask": ("Mask of the Unseen", "In darkness, things must come twice as close to notice you.", 0),
    "eye": ("Abyssal Eye", "You see 3 tiles into darkness, and what hides under the water.", 0),
    "crown": ("Barnacle Crown", "You heal 1 health every 4 turns.", 0),
    "conch": ("Siren's Conch", "Activate: a creature you can see (not the King) fights for you for 40 turns.", 70),
    "compass": ("Drowned Compass", "The stairs down are marked on each depth as you arrive.", 0),
}
PACTS = {  # key: (name, boon, bane)
    "gills": ("Gills of the Faithful", "You breathe water.", "Your lantern lights one tile less."),
    "shark": ("Blood of the Shark", "+3 melee damage.", "Vials of mending heal half as much."),
    "shadow": ("The Shadowed Way", "Things notice you from only two-thirds as far.", "6 less maximum health."),
    "tide": ("Tidebound", "+2 armour and +2 damage while you stand in water.", "−2 armour on dry ground."),
    "kin": ("Kin of the Deep", "Companions have half again their health, and one more can follow.",
            "You can't wear heavy armour."),
    "bones": ("Hollow Bones", "You move and act a fifth faster.", "A fifth less maximum health."),
    "eater": ("Lantern-Eater", "You see 2 tiles into darkness.", "Your lantern burns oil twice as fast."),
    "salt": ("Salt Skin", "+4 armour.", "Your steps are louder."),
}
CATEGORY_FREQ = (("vial", 30), ("shell", 24), ("rod", 9), ("weapon", 9), ("armor", 7), ("charm", 7), ("bait", 9))
PRICES = {"vial": 6, "shell": 8, "rod": 14, "weapon": 12, "armor": 14, "charm": 16, "bait": 3, "oil": 5}
STACKABLE = frozenset({"vial", "shell", "bait", "oil"})
KIND_GLYPH = {"weapon": (")", "pale"), "armor": ("[", "brown"), "vial": ("!", "pink"), "shell": ("?", "white"),
              "rod": ("/", "orange"), "charm": ("=", "gold"), "relic": ("♦", "gold"), "bait": (":", "orange"),
              "oil": ("¡", "yellow"), "pearls": ("°", "white"), "heart": ("♥", "red")}


# -- small helpers --

def roll(dice):
    n, _, sides = dice.partition("d")
    return sum(rng.randint(1, int(sides)) for _ in range(int(n)))


def weighted(pairs):
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    for key, w in pairs:
        r -= w
        if r <= 0:
            return key
    return pairs[-1][0]


def cheb(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


def sign(n):
    return (n > 0) - (n < 0)


def inside(x, y):
    return 0 <= x < W and 0 <= y < H


def line(x0, y0, x1, y1):
    """The tiles from (x0, y0) to (x1, y1), the start left out (Bresenham)."""
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err, x, y = dx + dy, x0, y0
    while (x, y) != (x1, y1):
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
        pts.append((x, y))
    return pts


def span(a, b):
    return range(a, b + 1) if b >= a else range(a, b - 1, -1)


# octant transforms for shadowcasting: (xx, xy, yx, yy)
OCTANTS = ((1, 0, 0, 1), (0, 1, 1, 0), (0, -1, 1, 0), (-1, 0, 0, 1),
           (-1, 0, 0, -1), (0, -1, -1, 0), (0, 1, -1, 0), (1, 0, 0, -1))


def shadowcast(lv, ox, oy, radius):
    """The tiles visible from (ox, oy) within `radius`: recursive shadowcasting. Ink is opaque."""
    seen = {(ox, oy)}
    tiles, clouds = lv.tiles, lv.clouds
    r2 = radius * radius + radius

    def opaque(x, y):
        if not (0 <= x < W and 0 <= y < H):
            return True
        if tiles[y * W + x] in OPAQUE:
            return True
        c = clouds.get((x, y))
        return c is not None and c[0] == "ink"

    def scan(row, start, end, xx, xy, yx, yy):
        if start < end:
            return
        new_start = start
        for j in range(row, radius + 1):
            dx, dy = -j - 1, -j
            blocked = False
            while dx <= 0:
                dx += 1
                x, y = ox + dx * xx + dy * xy, oy + dx * yx + dy * yy
                l_slope, r_slope = (dx - 0.5) / (dy + 0.5), (dx + 0.5) / (dy - 0.5)
                if start < r_slope:
                    continue
                if end > l_slope:
                    break
                if dx * dx + dy * dy <= r2 and 0 <= x < W and 0 <= y < H:
                    seen.add((x, y))
                if blocked:
                    if opaque(x, y):
                        new_start = r_slope
                    else:
                        blocked = False
                        start = new_start
                elif opaque(x, y) and j < radius:
                    blocked = True
                    scan(j + 1, start, l_slope, xx, xy, yx, yy)
                    new_start = r_slope
            if blocked:
                break

    for octant in OCTANTS:
        scan(1, 1.0, 0.0, *octant)
    return seen


# -- things in the world --

class Item:
    def __init__(self, kind, key=None, count=1):
        self.kind, self.key, self.count = kind, key, count
        self.ench = 0
        self.charges = rng.randint(3, 6) if kind == "rod" else 0

    def copy_one(self):
        one = Item(self.kind, self.key)
        one.ench, one.charges = self.ench, self.charges
        return one


class Player:
    def __init__(self):
        self.x = self.y = 0
        self.hp = HP_START
        self.vigor = 0
        self.breath = BREATH_BASE
        self.oil = OIL_START
        self.lantern = True
        self.sneaking = False
        self.inv = []
        self.weapon = self.armor = None
        self.charms = []
        self.relics = []
        self.cooldown = {}
        self.pacts = []
        self.xp = {s: 0.0 for s in SKILLS}
        self.status = {}
        self.pearls = 0
        self.nxt = 0.0
        self.depth_max = 1
        self.kills = 0
        self.regen = 0


class Monster:
    def __init__(self, kind, x, y, uid):
        d = MONSTERS[kind]
        self.kind, self.x, self.y, self.uid = kind, x, y, uid
        self.maxhp = self.hp = d["hp"]
        self.team = "hostile"
        self.state = "wander"
        self.target = self.dest = None
        self.search = 0
        self.status = {}
        self.nxt = 0.0
        self.pet = None
        self.order = "follow"
        self.kills = self.bonus = 0
        self.carry = None
        self.cool = 0
        self.foe = None
        self.revealed = False
        self.lure = None
        self.wares = None
        self.charm_turns = 0
        self.raised = 0                    # drowned called up by a priest or the King
        self.stuck = 0

    @property
    def d(self):
        return MONSTERS[self.kind]

    def has(self, flag):
        return flag in MONSTERS[self.kind]["flags"]


# -- one depth --

class Level:
    def __init__(self, depth):
        self.depth = depth
        self.name, self.theme, self.flavour = DEPTHS[depth]
        n = W * H
        self.tiles = bytearray(n)
        self.elev = [0.0] * n
        self.water = bytearray(n)
        self.mem_water = bytearray(n)
        self.seen = bytearray(n)
        self.mem_items = {}
        self.items = {}
        self.monsters = []
        self.ice, self.clouds, self.glow = {}, {}, {}
        self.rooms, self.lamps, self.corals = [], [], []
        self.canal, self.bridges = set(), set()
        self.shrines = {}
        self.ignored = set()
        self.start = self.stairs = self.altar = None
        self.vault = None
        self.tide_i, self.tide_clock = 1, TIDE_PERIOD
        self.flood = 0.0
        self.light_dirty = True
        self.static_light = set()
        self.floor_idx, self.sorted_elev = [], []

    def t(self, x, y):
        return self.tiles[y * W + x]

    def put(self, x, y, tile):
        self.tiles[y * W + x] = tile
        self.light_dirty = True

    def wat(self, x, y):
        """0 dry, 1 shallow, 2 deep; ice is walked on as if dry."""
        return 0 if (x, y) in self.ice else self.water[y * W + x]

    @property
    def tide(self):
        return TIDES[self.tide_i]

    def finish_water(self):
        self.floor_idx = [i for i in range(W * H) if self.tiles[i] not in SOLID]
        self.sorted_elev = sorted(self.elev[i] for i in self.floor_idx)
        self.update_water()

    def update_water(self):
        """Water fills the lowest share of the floor: more the deeper the depth, and with the tide."""
        f = 0.08 + 0.032 * (self.depth - 1) + TIDE_SHIFT[self.tide] + self.flood
        f = max(0.0, min(0.95, f))
        s, n = self.sorted_elev, len(self.sorted_elev)
        wet = s[min(n - 1, int(f * n))] if f > 0 else -1e9
        deep = s[min(n - 1, int(f * 0.48 * n))] if f > 0 else -1e9
        for i in self.floor_idx:
            e = self.elev[i]
            self.water[i] = 2 if e < deep else 1 if e < wet else 0

    def add_item(self, x, y, item):
        self.items.setdefault((x, y), []).append(item)


# -- building a depth --

def generate(depth):
    for _ in range(200):
        lv = Level(depth)
        if build(lv):
            return lv
    raise RuntimeError(f"couldn't build depth {depth}")


def build(lv):
    theme = lv.theme
    if theme == "caves":
        if not carve_caves(lv):
            return False
    elif theme == "palace":
        carve_rooms(lv, theme, [(20, 2, 40, 12), (33, 21, 14, 5)])
    else:
        carve_rooms(lv, theme)
    if theme == "canals":
        carve_canals(lv)
    make_elevation(lv)
    decorate(lv)
    if not place_start_and_stairs(lv):
        return False
    lv.finish_water()
    grow_kelp(lv)
    return True


def carve_rooms(lv, theme, fixed=()):
    big = theme == "cathedral"
    rooms = list(fixed)
    target = {"streets": 15, "canals": 12, "cathedral": 10, "palace": 10}[theme]
    for _ in range(500):
        if len(rooms) >= target:
            break
        w = rng.randint(6, 15) if big else rng.randint(4, 11)
        h = rng.randint(4, 8) if big else rng.randint(3, 6)
        x, y = rng.randint(2, W - w - 3), rng.randint(2, H - h - 3)
        if any(x - 2 < rx + rw and rx - 2 < x + w and y - 2 < ry + rh and ry - 2 < y + h for rx, ry, rw, rh in rooms):
            continue
        rooms.append((x, y, w, h))
    for x, y, w, h in rooms:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                lv.put(xx, yy, FLOOR)
    centres = [(x + w // 2, y + h // 2) for x, y, w, h in rooms]
    linked, edges = {0}, []
    while len(linked) < len(rooms):             # a spanning tree, so everything connects
        best = None
        for i in linked:
            for j in range(len(rooms)):
                if j not in linked:
                    d = abs(centres[i][0] - centres[j][0]) + abs(centres[i][1] - centres[j][1])
                    if best is None or d < best[0]:
                        best = (d, i, j)
        linked.add(best[2])
        edges.append(best[1:])
    for _ in range(len(rooms) // 3):            # and a few loops, so there's more than one way round
        edges.append(tuple(rng.sample(range(len(rooms)), 2)))
    for a, b in edges:
        corridor(lv, centres[a], centres[b], theme == "streets" and rng.random() < 0.35)
    lv.rooms = rooms
    chance = {"streets": 0.55, "canals": 0.4, "cathedral": 0.5, "palace": 0.6}[theme]
    for x, y, w, h in rooms:
        ring = ([(xx, y - 1) for xx in range(x, x + w)] + [(xx, y + h) for xx in range(x, x + w)]
                + [(x - 1, yy) for yy in range(y, y + h)] + [(x + w, yy) for yy in range(y, y + h)])
        for cx, cy in ring:
            if lv.t(cx, cy) != FLOOR:
                continue
            across = lv.t(cx - 1, cy) == WALL and lv.t(cx + 1, cy) == WALL
            along = lv.t(cx, cy - 1) == WALL and lv.t(cx, cy + 1) == WALL
            if (across or along) and rng.random() < chance:
                lv.put(cx, cy, DOOR)


def corridor(lv, a, b, wide):
    (x1, y1), (x2, y2) = a, b
    if rng.random() < 0.5:
        pts = [(x, y1) for x in span(x1, x2)] + [(x2, y) for y in span(y1, y2)]
    else:
        pts = [(x1, y) for y in span(y1, y2)] + [(x, y2) for x in span(x1, x2)]
    for x, y in pts:
        for ex, ey in ((0, 0), (1, 0), (0, 1)) if wide else ((0, 0),):
            cx, cy = x + ex, y + ey
            if 1 <= cx < W - 1 and 1 <= cy < H - 1 and lv.t(cx, cy) == WALL:
                lv.put(cx, cy, FLOOR)


def carve_canals(lv):
    for _ in range(rng.randint(2, 3)):
        cells = []
        if rng.random() < 0.6:
            y = rng.randint(4, H - 6)
            for x in range(1, W - 1):
                if rng.random() < 0.15:
                    y = max(3, min(H - 5, y + rng.choice((-1, 1))))
                cells.append(((x, y), (x, y + 1)))
        else:
            x = rng.randint(6, W - 8)
            for y in range(1, H - 1):
                if rng.random() < 0.15:
                    x = max(3, min(W - 5, x + rng.choice((-1, 1))))
                cells.append(((x, y), (x + 1, y)))
        for pair in cells:
            for cx, cy in pair:
                if 1 <= cx < W - 1 and 1 <= cy < H - 1:
                    lv.put(cx, cy, FLOOR)
                    lv.canal.add((cx, cy))
        for k in rng.sample(range(4, len(cells) - 4), 3):
            lv.bridges.update(cells[k])


def carve_caves(lv):
    """Cellular-automaton caverns; every pocket of a fair size is kept and tunnelled to the rest."""
    g = [[1 if x in (0, W - 1) or y in (0, H - 1) or rng.random() < 0.45 else 0 for x in range(W)]
         for y in range(H)]
    for _ in range(4):
        n = [[1] * W for _ in range(H)]
        for y in range(1, H - 1):
            for x in range(1, W - 1):
                n[y][x] = 1 if sum(g[yy][xx] for yy in (y - 1, y, y + 1) for xx in (x - 1, x, x + 1)) >= 5 else 0
        g = n
    regions, seen = [], set()
    for y in range(H):
        for x in range(W):
            if g[y][x] or (x, y) in seen:
                continue
            region, queue = [], deque([(x, y)])
            seen.add((x, y))
            while queue:
                cx, cy = queue.popleft()
                region.append((cx, cy))
                for dx, dy in DIRS8:
                    nx, ny = cx + dx, cy + dy
                    if inside(nx, ny) and not g[ny][nx] and (nx, ny) not in seen:
                        seen.add((nx, ny))
                        queue.append((nx, ny))
            if len(region) >= 15:
                regions.append(region)
    if not regions:
        return False
    regions.sort(key=len, reverse=True)
    for region in regions:
        for x, y in region:
            lv.put(x, y, FLOOR)
    joined = list(regions[0])
    for region in regions[1:]:
        a = rng.choice(region)
        b = min(rng.sample(joined, min(40, len(joined))), key=lambda q: abs(q[0] - a[0]) + abs(q[1] - a[1]))
        corridor(lv, a, b, rng.random() < 0.5)
        joined += region
    return sum(1 for i in range(W * H) if lv.tiles[i] == FLOOR) >= 0.3 * W * H


def noise_layer(cell):
    gw, gh = W // cell + 2, H // cell + 2
    g = [[rng.random() for _ in range(gw)] for _ in range(gh)]
    out = [0.0] * (W * H)
    for y in range(H):
        fy = y / cell
        iy = int(fy)
        ty = fy - iy
        ty = ty * ty * (3 - 2 * ty)
        for x in range(W):
            fx = x / cell
            ix = int(fx)
            tx = fx - ix
            tx = tx * tx * (3 - 2 * tx)
            a, b, c, d = g[iy][ix], g[iy][ix + 1], g[iy + 1][ix], g[iy + 1][ix + 1]
            out[y * W + x] = a + (b - a) * tx + (c - a) * ty + (a - b - c + d) * tx * ty
    return out


def make_elevation(lv):
    """Where the water collects: smooth noise, with rooms sunk or raised whole, canals low."""
    a, b = noise_layer(7), noise_layer(16)
    e = [0.55 * a[i] + 0.45 * b[i] for i in range(W * H)]
    for k, (x0, y0, w, h) in enumerate(lv.rooms):
        cells = [y * W + x for y in range(y0, y0 + h) for x in range(x0, x0 + w)]
        mean = sum(e[i] for i in cells) / len(cells)
        r = rng.random()
        offset = -0.22 if r < 0.3 else 0.18 if r < 0.5 else 0.0
        if lv.theme == "palace" and k == 0:
            offset = 0.25                       # the throne hall stands dry, mostly
        for i in cells:
            e[i] = 0.5 * e[i] + 0.5 * mean + offset
    for x, y in lv.canal:
        e[y * W + x] -= 0.5
    for x, y in lv.bridges:
        e[y * W + x] = 3.0
    lv.elev = e


def decorate(lv):
    d, theme = lv.depth, lv.theme
    p = (0.006 + 0.005 * d) * (2.5 if theme == "caves" else 1)
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            if (lv.t(x, y) == WALL and rng.random() < p
                    and any(lv.t(x + dx, y + dy) == FLOOR for dx, dy in DIRS4)):
                lv.put(x, y, CORAL)
                lv.corals.append((x, y))
    for x0, y0, w, h in lv.rooms:
        if theme in ("cathedral", "palace") and w >= 8 and h >= 5:
            for x in range(x0 + 2, x0 + w - 2, 3):
                for y in (y0 + 1, y0 + h - 2):
                    lv.put(x, y, STATUE)
        elif theme == "streets" and w >= 5 and h >= 4 and rng.random() < 0.2:
            lv.put(x0 + w // 2, y0 + h // 2, STATUE)
    lamps = {"streets": 7, "canals": 5, "cathedral": 4, "palace": 6}.get(theme, 0)
    lit = 0.85 if theme == "palace" else 0.8 if d <= 3 else 0.5 if d <= 6 else 0.3
    rooms = lv.rooms[1:] or lv.rooms
    for _ in range(lamps * 3):
        if len(lv.lamps) >= lamps or not rooms:
            break
        x0, y0, w, h = rng.choice(rooms)
        x, y = rng.choice([(x0, y0), (x0 + w - 1, y0), (x0, y0 + h - 1), (x0 + w - 1, y0 + h - 1),
                           (x0 + w // 2, y0), (x0 + w // 2, y0 + h - 1)])
        outside = [(x + dx, y + dy) for dx, dy in DIRS4
                   if not (x0 <= x + dx < x0 + w and y0 <= y + dy < y0 + h)]
        if lv.t(x, y) == FLOOR and all(lv.t(*q) in SOLID for q in outside):
            lv.put(x, y, LAMP if rng.random() < lit else DARK_LAMP)
            lv.lamps.append((x, y))


def bfs_dist(lv, start):
    dist = [-1] * (W * H)
    sx, sy = start
    dist[sy * W + sx] = 0
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        d = dist[y * W + x] + 1
        for dx, dy in DIRS8:
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H:
                i = ny * W + nx
                if dist[i] < 0 and (lv.tiles[i] in WALKABLE or lv.tiles[i] == DOOR):
                    dist[i] = d
                    queue.append((nx, ny))
    return dist


def place_start_and_stairs(lv):
    if lv.theme == "palace":
        lv.start, lv.altar = (40, 24), (40, 3)
        lv.put(40, 3, ALTAR)
        lv.put(40, 4, FLOOR)
    else:
        if lv.rooms:
            x0, y0, w, h = lv.rooms[0]
            spots = [(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h) if lv.t(x, y) == FLOOR]
        else:
            spots = [(i % W, i // W) for i in range(W * H) if lv.tiles[i] == FLOOR]
        if not spots:
            return False
        lv.start = rng.choice(spots)
    dist = bfs_dist(lv, lv.start)
    walk = [i for i in range(W * H) if lv.tiles[i] in WALKABLE or lv.tiles[i] == DOOR]
    if sum(1 for i in walk if dist[i] >= 0) < 0.9 * len(walk):
        return False
    if lv.depth < MAX_DEPTH:
        far = max(dist)
        cands = [i for i in range(W * H) if lv.tiles[i] == FLOOR and dist[i] >= 0.6 * far
                 and (i % W, i // W) not in lv.canal]
        if not cands:
            return False
        i = rng.choice(cands)
        lv.stairs = (i % W, i // W)
        lv.put(*lv.stairs, STAIRS)
    for pos in (lv.start, lv.stairs, lv.altar):
        if pos:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    lv.elev[(pos[1] + dy) * W + pos[0] + dx] = 5.0     # always dry
    lv.dist_start = dist
    return True


def grow_kelp(lv):
    wet = [i for i in lv.floor_idx if lv.water[i] and lv.tiles[i] == FLOOR]
    if not wet:
        return
    for _ in range(2 + lv.depth // 2):
        i = rng.choice(wet)
        x, y = i % W, i // W
        for _ in range(rng.randint(3, 10)):
            if lv.t(x, y) == FLOOR and lv.water[y * W + x]:
                lv.put(x, y, KELP)
            dx, dy = rng.choice(DIRS8)
            if 0 < x + dx < W - 1 and 0 < y + dy < H - 1 and lv.water[(y + dy) * W + x + dx]:
                x, y = x + dx, y + dy


# -- the game --

class Game:
    """Everything about a run, and nothing about the screen: the UI asks it things and calls
    its actions, which return how long they took (0 for no time at all)."""

    def __init__(self, seed=None):
        self.seed = seed if seed is not None else random.randrange(10 ** 9)
        rng.seed(self.seed)
        looks = list(VIAL_LOOKS)
        rng.shuffle(looks)
        words = set()
        while len(words) < len(SHELLS):
            words.add(" ".join("".join(rng.sample(SYLLABLES, rng.randint(1, 2))).upper()
                               for _ in range(rng.randint(1, 2))))
        self.looks = {"vial": dict(zip(VIALS, looks)), "shell": dict(zip(SHELLS, sorted(words, key=lambda _: rng.random()))),
                      "rod": dict(zip(RODS, rng.sample(ROD_LOOKS, len(RODS)))),
                      "charm": dict(zip(CHARMS, rng.sample(CHARM_LOOKS, len(CHARMS))))}
        self.known = set()
        self.relic_pool = list(RELICS)
        rng.shuffle(self.relic_pool)
        self.pact_pool = list(PACTS)
        self.player = Player()
        self.log = []
        self.turn, self.time = 0, 0.0
        self.over, self.cause = None, ""
        self.heart_taken, self.flood_clock = False, 0
        self.king_dead = False
        self.pending = None
        self.disturb = False
        self.visible, self.lit, self.lures = set(), set(), set()
        self.noticed = set()
        self.fields = {}
        self.uid = 0
        self.pets_used = set()
        self.score = 0
        self.rng_state = None
        self.start_kit()
        self.new_level(1)

    # -- messages and names --

    def msg(self, text, colour="white"):
        if self.log and self.log[-1][1] == text and self.log[-1][0] == self.turn:
            self.log[-1][3] += 1
        else:
            self.log.append([self.turn, text, colour, 1])
            del self.log[:-400]
        if colour in ("red", "yellow", "orange", "magenta"):
            self.disturb = True

    def the(self, m, cap=False):
        if not self.player_sees(m) and m.team != "ally":
            s = "something"
        elif m.pet:
            s = m.pet
        elif m.has("boss"):
            s = m.d["name"]
        else:
            s = "the " + m.d["name"]
        return s[0].upper() + s[1:] if cap else s

    def a(self, m):
        name = m.d["name"]
        if m.has("boss"):
            return name
        return ("an " if name[0] in "aeiou" else "a ") + name

    def names(self, it):
        """(singular, plural) of what an item is called right now."""
        k, key = it.kind, it.key
        if k == "weapon":
            s = WEAPONS[key][0]
            s = f"{it.ench:+d} {s}" if it.ench else s
            return s, s + "s"
        if k == "armor":
            s = ARMOURS[key][0]
            s = f"{it.ench:+d} {s}" if it.ench else s
            return s, s
        if k == "vial":
            if key in self.known:
                return f"vial of {VIALS[key][0]}", f"vials of {VIALS[key][0]}"
            look = self.looks["vial"][key][0]
            return f"{look} vial", f"{look} vials"
        if k == "shell":
            if key in self.known:
                return f"shell of {SHELLS[key][0]}", f"shells of {SHELLS[key][0]}"
            word = self.looks["shell"][key]
            return f"shell etched “{word}”", f"shells etched “{word}”"
        if k == "rod":
            if key in self.known:
                s = f"rod of {RODS[key][0]} ({it.charges})"
                return s, s
            look = self.looks["rod"][key]
            return f"{look} rod", f"{look} rods"
        if k == "charm":
            if key in self.known:
                return f"charm of {CHARMS[key][0]}", f"charms of {CHARMS[key][0]}"
            look = self.looks["charm"][key]
            return f"{look} charm", f"{look} charms"
        if k == "relic":
            return RELICS[key][0], RELICS[key][0]
        if k == "bait":
            return "sweetfish", "sweetfish"
        if k == "oil":
            return "flask of lamp oil", "flasks of lamp oil"
        if k == "pearls":
            return "pearl", "pearls"
        return "the Tide-Heart", "the Tide-Heart"

    def item_name(self, it, article=True):
        one, many = self.names(it)
        if it.count > 1 or it.kind == "pearls":
            return f"{it.count} {many if it.count > 1 else one}"
        if not article or it.kind in ("relic", "heart"):
            return one
        return ("an " if one[0] in "aeiou" else "a ") + one

    def item_glyph(self, it):
        glyph, colour = KIND_GLYPH[it.kind]
        if it.kind == "vial":
            colour = self.looks["vial"][it.key][1]
        return glyph, colour

    def describe_item(self, it):
        k, key = it.kind, it.key
        if k == "weapon":
            name, dice, acc, sneak, _, flags = WEAPONS[key]
            notes = [WEAPON_NOTES[f] for f in flags.split()]
            return (f"Damage {dice}{'+%d' % it.ench if it.ench else ''}, accuracy {acc + it.ench:+d}; "
                    f"strikes on the unaware do ×{sneak}." + (" It " + "; it ".join(notes) + "." if notes else ""))
        if k == "armor":
            name, ac, weight, noise, _, flags = ARMOURS[key]
            extra = {"swim": " You swim faster in it.", "breath": " You hold your breath twice as long in it."}
            heavy = " Heavy: it drags in deep water." if weight >= 2 else ""
            return f"Armour {ac + it.ench}.{' Noisy.' if noise else ''}{heavy}{extra.get(flags, '')}"
        if k in ("vial", "shell", "rod", "charm"):
            if key not in self.known:
                return "You don't know what this does. Try it — or read a shell of lore."
            table = {"vial": VIALS, "shell": SHELLS, "rod": RODS, "charm": CHARMS}[k]
            return table[key][2]
        if k == "relic":
            return RELICS[key][1]
        if k == "bait":
            return ("A sweet little fish. Thrown, it draws hungry beasts to eat it; offered while taming, "
                    "it makes a beast much likelier to come with you.")
        if k == "oil":
            return "Oil for your lantern."
        if k == "heart":
            return "The Tide-Heart. Bring it to the upward current where you came in."
        return ""

    def identify(self, key, quiet=False):
        if key in self.known:
            return
        self.known.add(key)
        if quiet:
            return
        for table, word in ((VIALS, "vial"), (SHELLS, "shell"), (RODS, "rod"), (CHARMS, "charm")):
            if key in table:
                name = table[key][0]
                self.msg(f"(So that was a {word} of {name}.)", "gray")

    # -- the player's numbers --

    def level(self, skill):
        xp = self.player.xp[skill]
        return max(i for i, need in enumerate(SKILL_STEPS) if xp >= need)

    def gain(self, skill, amount):
        before = self.level(skill)
        self.player.xp[skill] += amount
        after = self.level(skill)
        if after > before:
            self.msg(f"Your {skill} improves to {after}.", "cyan")

    def maxhp(self):
        p = self.player
        m = HP_START + HP_PER_DEPTH * (p.depth_max - 1) + p.vigor
        if "shadow" in p.pacts:
            m -= 6
        if "bones" in p.pacts:
            m = int(m * 0.8)
        return max(5, m)

    def charm_keys(self):
        return {c.key for c in self.player.charms}

    def in_water(self):
        p = self.player
        return self.lv.wat(p.x, p.y) > 0

    def submerged(self):
        p = self.player
        return self.lv.wat(p.x, p.y) == 2

    def armour(self):
        p = self.player
        ac = ARMOURS[p.armor.key][1] + p.armor.ench if p.armor else 0
        if "guard" in self.charm_keys():
            ac += 2
        if "shell" in p.relics:
            ac += 3
        if "salt" in p.pacts:
            ac += 4
        if "tide" in p.pacts:
            ac += 2 if self.in_water() else -2
        return max(0, ac)

    def weight(self):
        p = self.player
        return (ARMOURS[p.armor.key][2] if p.armor else 0) + (2 if "anchor" in self.charm_keys() else 0)

    def breathes_water(self):
        p = self.player
        return "pearl" in p.relics or "gills" in p.pacts or "gills" in p.status

    def max_breath(self):
        p = self.player
        b = BREATH_BASE + 3 * self.level("swimming")
        if p.armor and ARMOURS[p.armor.key][5] == "breath":
            b *= 2
        if "tides" in self.charm_keys():
            b += 10
        return b

    def lantern_lit(self):
        p = self.player
        return p.lantern and (p.oil > 0 or "wick" in p.relics) and not self.submerged()

    def light_radius(self):
        p = self.player
        r = LANTERN_RADIUS + ("sight" in self.charm_keys()) - ("gills" in p.pacts)
        return max(1, r)

    def darkvision(self):
        p = self.player
        r = 0
        if "nightsight" in p.status:
            r = 5
        if "eye" in p.relics:
            r = max(r, 3)
        if "eater" in p.pacts:
            r = max(r, 2)
        return r

    def sees_under_water(self):
        return "eye" in self.player.relics or "sight" in self.charm_keys()

    def speed(self):
        p = self.player
        s = 100.0
        if "bones" in p.pacts:
            s *= 1.2
        if "haste" in p.status:
            s *= 2
        if "slow" in p.status:
            s *= 0.5
        return s

    def mspeed(self, m):
        s = float(m.d["speed"])
        if "slow" in m.status:
            s *= 0.5
        if "haste" in m.status:
            s *= 2
        return s

    def ally_cap(self):
        p = self.player
        return 1 + self.level("kinship") // 3 + (2 if "whistle" in p.relics else 0) + ("kin" in p.pacts)

    def pets(self):
        return [m for m in self.lv.monsters if m.team == "ally" and not m.charm_turns]

    def melee_bonus(self):
        p = self.player
        b = self.level("melee") // 2 + (p.weapon.ench if p.weapon else 0)
        if "fury" in self.charm_keys():
            b += 2
        if "shark" in p.pacts:
            b += 3
        if "tide" in p.pacts and self.in_water():
            b += 2
        return b

    def sneak_mult(self):
        p = self.player
        return WEAPONS[p.weapon.key][3] if p.weapon else 3

    # -- starting out --

    def start_kit(self):
        p = self.player
        knife, coat = Item("weapon", "knife"), Item("armor", "coat")
        p.inv += [knife, coat, Item("vial", "mending"), Item("bait", "sweetfish", 2),
                  Item("vial", rng.choice([k for k in VIALS if k not in ("mending", "brine")]))]
        p.weapon, p.armor = knife, coat
        self.known.add("mending")

    def spawn(self, kind, x, y, state="wander"):
        self.uid += 1
        m = Monster(kind, x, y, self.uid)
        m.state = state
        m.nxt = self.time + rng.random() * 100
        self.lv.monsters.append(m)
        return m

    def random_item(self, depth):
        kind = weighted(CATEGORY_FREQ)
        count = 1
        if kind == "vial":
            key = weighted([(k, v[1]) for k, v in VIALS.items()])
        elif kind == "shell":
            key = weighted([(k, v[1]) for k, v in SHELLS.items()])
        elif kind == "rod":
            key = weighted([(k, v[1]) for k, v in RODS.items()])
        elif kind == "charm":
            key = weighted([(k, v[1]) for k, v in CHARMS.items()])
        elif kind == "weapon":
            key = rng.choice([k for k, v in WEAPONS.items() if k != "knife" and v[4] <= depth])
        elif kind == "armor":
            key = rng.choice([k for k, v in ARMOURS.items() if k != "coat" and v[4] <= depth])
        else:
            key, count = "sweetfish", rng.randint(1, 3)
        it = Item(kind, key, count)
        if kind in ("weapon", "armor"):
            r = rng.random()
            if r < 0.25:
                it.ench = rng.randint(1, 1 + depth // 4)
            elif r < 0.33:
                it.ench = -1
        return it

    def spot_ok(self, kind, i):
        lv = self.lv
        pos = (i % W, i // W)
        if pos == lv.start or any((m.x, m.y) == pos for m in lv.monsters):
            return False
        t, w = lv.tiles[i], lv.water[i]
        f = MONSTERS[kind]["flags"]
        if kind == "kelp":
            return t == KELP
        if t not in (FLOOR, KELP):
            return False
        if "deep" in f:
            return w == 2
        if "aquatic" in f:
            return w >= 1
        return w == 0 and t == FLOOR

    def populate(self, lv):
        d = lv.depth
        if 2 <= d < MAX_DEPTH and rng.random() < 0.5:     # a sunken vault, under water even at low tide
            cands = [r for r in lv.rooms[1:] if r[2] * r[3] <= 45
                     and not (lv.stairs and r[0] <= lv.stairs[0] < r[0] + r[2] and r[1] <= lv.stairs[1] < r[1] + r[3])]
            if cands:
                lv.vault = rng.choice(cands)
                x0, y0, w, h = lv.vault
                for y in range(y0, y0 + h):
                    for x in range(x0, x0 + w):
                        lv.elev[y * W + x] = -2.0 - rng.random() * 0.1
                lv.finish_water()
        dist = lv.dist_start
        reach = [i for i in range(W * H) if dist[i] >= 0 and lv.tiles[i] in (FLOOR, KELP)]
        far = [i for i in reach if dist[i] > 8] or reach

        def free_floor(pool, dry=False):
            cands = [i for i in pool if (not dry or lv.water[i] == 0) and lv.tiles[i] == FLOOR
                     and (i % W, i // W) not in lv.items and (i % W, i // W) != lv.start]
            if not cands:
                return None
            i = rng.choice(cands)
            return i % W, i // W

        table = [(k, v["freq"]) for k, v in MONSTERS.items() if v["freq"] and v["depth"][0] <= d <= v["depth"][1]]
        if not any(lv.tiles[i] == KELP for i in reach):
            table = [(k, f) for k, f in table if k != "kelp"]
        for _ in range(3 + d * 4 // 5 + rng.randint(0, 2)):
            kind = weighted(table)
            spots = [i for i in far if self.spot_ok(kind, i)]
            if not spots:
                continue
            i = rng.choice(spots)
            group = [(i % W, i // W)]
            lo, hi = MONSTERS[kind]["pack"]
            for _ in range(rng.randint(lo, hi) - 1):
                x, y = group[0]
                near = [(x + dx, y + dy) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)
                        if inside(x + dx, y + dy) and self.spot_ok(kind, (y + dy) * W + x + dx)
                        and (x + dx, y + dy) not in group]
                if near:
                    group.append(rng.choice(near))
            f = MONSTERS[kind]["flags"]
            state = "ambush" if "ambush" in f or "stationary" in f else "sleep" if rng.random() < 0.65 else "wander"
            for x, y in group:
                self.spawn(kind, x, y, state)

        for _ in range(6 + rng.randint(0, 3)):
            pos = free_floor(reach)
            if pos:
                lv.add_item(*pos, self.random_item(d))
        for _ in range(rng.randint(3, 5)):
            pos = free_floor(reach)
            if pos:
                lv.add_item(*pos, Item("pearls", None, rng.randint(1, 2 + d)))
        for _ in range(1 + (rng.random() < 0.6) + (d >= 7)):
            pos = free_floor(reach)
            if pos:
                lv.add_item(*pos, Item("oil"))
        relic_here = d < MAX_DEPTH and self.relic_pool and (d in RELIC_DEPTHS or rng.random() < 0.15)
        if lv.vault:
            x0, y0, w, h = lv.vault
            inner = [y * W + x for y in range(y0, y0 + h) for x in range(x0, x0 + w)]
            if relic_here:
                pos = free_floor(inner)
                if pos:
                    lv.add_item(*pos, Item("relic", self.relic_pool.pop()))
                    relic_here = False
            for _ in range(2):
                pos = free_floor(inner)
                if pos:
                    lv.add_item(*pos, self.random_item(d + 2))
            pos = free_floor(inner)
            if pos:
                lv.add_item(*pos, Item("pearls", None, rng.randint(6, 14)))
        if relic_here:
            pos = free_floor(far)
            if pos:
                lv.add_item(*pos, Item("relic", self.relic_pool.pop()))
        if d < MAX_DEPTH and (d in SHRINE_DEPTHS or rng.random() < 0.12) and len(self.pact_pool) >= 2:
            rooms = [r for r in lv.rooms[1:] if r != lv.vault] or lv.rooms
            for _ in range(10):
                if rooms:
                    x0, y0, w, h = rng.choice(rooms)
                    x, y = x0 + w // 2, y0 + h // 2
                else:                           # caves: anywhere dry and out of the way
                    x, y = free_floor(far, dry=True) or lv.start
                if lv.t(x, y) == FLOOR and (x, y) != lv.stairs and (x, y) not in lv.items:
                    lv.put(x, y, SHRINE)
                    lv.elev[y * W + x] = 5.0
                    lv.shrines[(x, y)] = rng.sample(self.pact_pool, 2)
                    break
        if d in HERMIT_DEPTHS:
            pos = free_floor(far, dry=True)
            if pos:
                m = self.spawn("hermit", *pos, "idle")
                m.team = "neutral"
                m.wares = []
                for _ in range(4):
                    it = self.random_item(d + 1)
                    m.wares.append((it, PRICES[it.kind] * it.count + rng.randint(0, 3)))
                m.wares.append((Item("oil"), PRICES["oil"]))
        if d == MAX_DEPTH:
            lv.add_item(*lv.altar, Item("heart"))
            self.spawn("king", 40, 4, "sleep")
            for x in (36, 44):
                self.spawn("knight", x, 6, "sleep")
        lv.finish_water()

    def new_level(self, depth, allies=()):
        lv = generate(depth)
        self.lv = lv
        self.populate(lv)
        p = self.player
        p.x, p.y = lv.start
        for a in allies:
            spots = [(p.x + dx, p.y + dy) for dx, dy in DIRS8
                     if lv.t(p.x + dx, p.y + dy) in WALKABLE and not self.monster_at(p.x + dx, p.y + dy)]
            a.x, a.y = spots[0] if spots else (p.x, p.y)
            a.nxt = self.time
            a.foe = None
            lv.monsters.append(a)
        if depth > p.depth_max:
            p.depth_max = depth
            p.hp = min(self.maxhp(), p.hp + HP_PER_DEPTH)
        self.noticed = set()
        self.fields = {}
        self.msg(f"Depth {depth}: {lv.name}.", "cyan")
        self.msg(lv.flavour, "gray")
        if "monocle" in p.relics:
            self.reveal_map()
        if "compass" in p.relics and lv.stairs:
            lv.seen[lv.stairs[1] * W + lv.stairs[0]] = 1
        self.compute_view()

    def reveal_map(self):
        lv = self.lv
        for y in range(H):
            for x in range(W):
                i = y * W + x
                if lv.tiles[i] not in SOLID or any(inside(x + dx, y + dy) and lv.t(x + dx, y + dy) not in SOLID
                                                   for dx, dy in DIRS8):
                    lv.seen[i] = 1
                    lv.mem_water[i] = lv.water[i]

    # -- seeing --

    def compute_light(self):
        lv, p = self.lv, self.player
        if lv.light_dirty:
            lit = set()
            for x, y in lv.lamps:
                if lv.t(x, y) == LAMP:
                    lit |= shadowcast(lv, x, y, LAMP_RADIUS)
            for x, y in lv.corals:
                lit |= shadowcast(lv, x, y, CORAL_RADIUS)
            lv.static_light = lit
            lv.light_dirty = False
        lit = set(lv.static_light)
        for x, y in lv.glow:
            lit |= shadowcast(lv, x, y, 2)
        if self.lantern_lit():
            lit |= shadowcast(lv, p.x, p.y, self.light_radius())
        if "glow" in p.status:
            lit |= shadowcast(lv, p.x, p.y, 2)
        self.lures = set()
        for m in lv.monsters:
            if m.has("light") or "glow" in m.status:
                lit |= shadowcast(lv, m.x, m.y, 3 if m.has("light") else 1)
            if m.lure:
                self.lures.add(m.lure)
                lit |= shadowcast(lv, *m.lure, 2)
        self.lit = {q for q in lit if not (q in lv.clouds and lv.clouds[q][0] == "ink")}

    def compute_view(self):
        self.compute_light()
        lv, p = self.lv, self.player
        sub = self.submerged()
        fov = shadowcast(lv, p.x, p.y, SUBMERGED_VIEW if sub else VIEW_RADIUS)
        dv = self.darkvision()
        lit = self.lit
        vis = set()
        for q in fov:
            dx, dy = q[0] - p.x, q[1] - p.y
            if q in lit or max(abs(dx), abs(dy)) <= 1 or (dv and dx * dx + dy * dy <= dv * dv + dv):
                vis.add(q)
        self.visible = vis
        for x, y in vis:
            i = y * W + x
            lv.seen[i] = 1
            lv.mem_water[i] = lv.wat(x, y)
            items = lv.items.get((x, y))
            if items:
                lv.mem_items[(x, y)] = self.item_glyph(items[-1])
            else:
                lv.mem_items.pop((x, y), None)
        for m in lv.monsters:
            if m.team == "hostile" and m.uid not in self.noticed and self.player_sees(m):
                self.noticed.add(m.uid)
                how = {"sleep": " (asleep)", "ambush": " (lurking)", "hunt": " — and it has seen you"}.get(m.state, "")
                self.msg(f"You see {self.a(m)}{how}.", "yellow")

    def player_sees(self, m):
        p = self.player
        if (m.x, m.y) not in self.visible:
            return False
        if m.team == "ally" or cheb(m.x, m.y, p.x, p.y) <= 1:
            return True
        if m.has("unseen") and not self.sees_under_water():
            return False
        if m.has("hidden") and not m.revealed:
            return False
        if m.has("aquatic") and self.lv.wat(m.x, m.y) == 2 and not self.sees_under_water():
            return False
        return True

    def los(self, x0, y0, x1, y1):
        lv = self.lv
        for x, y in line(x0, y0, x1, y1)[:-1]:
            if lv.tiles[y * W + x] in OPAQUE or (x, y) in lv.clouds and lv.clouds[(x, y)][0] == "ink":
                return False
        return True

    def monster_sees(self, m):
        """Whether a hostile creature can see you right now: light, distance, cover and water."""
        p, lv = self.player, self.lv
        d = cheb(m.x, m.y, p.x, p.y)
        if m.has("blind"):
            return d <= 1
        if d <= 1:
            return True
        if lv.t(p.x, p.y) == KELP:
            return False
        if self.submerged() and not m.has("swim") and not m.has("aquatic") and d > 2:
            return False
        lit = (p.x, p.y) in self.lit
        r = m.d["sight"] if lit else m.d["dark"]
        if not lit and "mask" in p.relics:
            r //= 2
        if "shadow" in p.pacts:
            r = r * 2 // 3
        return d <= r and self.los(m.x, m.y, p.x, p.y)

    def seen_by(self):
        return [m for m in self.lv.monsters if m.team == "hostile" and m.state == "hunt" and self.monster_sees(m)]

    def monster_at(self, x, y):
        for m in self.lv.monsters:
            if m.x == x and m.y == y:
                return m
        return None

    def occupied(self, x, y):
        p = self.player
        return (p.x == x and p.y == y) or self.monster_at(x, y) is not None

    # -- noise --

    def noise(self, x, y, loud):
        """A sound at (x, y) that carries `loud` steps through open ground: it wakes sleepers
        and draws the curious."""
        if loud <= 0:
            return
        lv = self.lv
        dist = {(x, y): 0}
        queue = deque([(x, y)])
        while queue:
            cx, cy = queue.popleft()
            d = dist[(cx, cy)]
            if d >= loud:
                continue
            for dx, dy in DIRS8:
                nx, ny = cx + dx, cy + dy
                if inside(nx, ny) and (nx, ny) not in dist:
                    t = lv.tiles[ny * W + nx]
                    if t not in SOLID:
                        dist[(nx, ny)] = d + (3 if t == DOOR else 1)
                        queue.append((nx, ny))
        for m in lv.monsters:
            if m.team != "hostile":
                continue
            d = dist.get((m.x, m.y))
            if d is None or d > loud:
                continue
            if m.state == "sleep":
                if "slumber" in m.status:
                    continue
                if rng.random() < 0.2 + 0.6 * (1 - d / (loud + 1)):
                    m.state, m.target, m.search = "search", (x, y), 10
                    if self.player_sees(m):
                        self.msg(f"{self.the(m, True)} stirs.", "yellow")
            elif m.state in ("wander", "search", "ambush") and not m.has("stationary"):
                if m.state != "ambush" or d <= 3:
                    m.state, m.target, m.search = "search", (x, y), 10
            elif m.state == "hunt" and not self.monster_sees(m):
                m.target = (x, y)

    def step_noise(self, water):
        p = self.player
        n = {0: 4, 1: 7, 2: 3}[water]
        if p.sneaking:
            n //= 2
        if p.armor:
            n += ARMOURS[p.armor.key][3]
        n += 2 * ("salt" in p.pacts) + 6 * ("clamor" in self.charm_keys())
        n -= self.level("stealth") // 2 + 2 * ("hush" in self.charm_keys())
        return max(0 if p.sneaking else 1, n)

    # -- time --

    def spend(self, cost):
        """The player used `cost` time; everything else acts until it is the player's turn again."""
        p = self.player
        if not cost or self.over:
            return
        p.nxt += cost * 100.0 / self.speed()
        self.fields = {}
        self.run_world()
        while not self.over and "paralyzed" in p.status:
            p.nxt += 100
            self.fields = {}
            self.run_world()
        self.compute_view()

    def run_world(self):
        p = self.player
        while not self.over:
            due = None
            for m in self.lv.monsters:
                if m.nxt < p.nxt and (due is None or m.nxt < due.nxt):
                    due = m
            if due is None:
                break
            self.tick_to(due.nxt)
            if self.over:
                return
            if due in self.lv.monsters:
                cost = self.monster_act(due)
                due.nxt += max(1, cost) * 100.0 / self.mspeed(due)
        self.tick_to(p.nxt)

    def tick_to(self, t):
        while self.time + 100 <= t and not self.over:
            self.time += 100
            self.world_tick()

    def world_tick(self):
        p, lv = self.player, self.lv
        self.turn += 1
        if self.heart_taken:
            self.flood_clock -= 1
            if self.flood_clock <= 0:
                self.flood_clock = 12
                lv.flood += 0.035
                lv.update_water()
                self.msg("The water rises through the palace!", "blue")
        else:
            lv.tide_clock -= 1
            if lv.tide_clock <= 0:
                self.turn_tide()
        melted = False
        for pos in list(lv.ice):
            lv.ice[pos] -= 1
            if lv.ice[pos] <= 0:
                del lv.ice[pos]
                melted = melted or pos in self.visible
        if melted:
            self.msg("The ice cracks and melts.", "ice")
        for pos in list(lv.clouds):
            kind, turns = lv.clouds[pos]
            if turns <= 1:
                del lv.clouds[pos]
            else:
                lv.clouds[pos] = (kind, turns - 1)
            if kind == "gas":
                if (p.x, p.y) == pos:
                    self.hurt(1, "choked on rot-gas")
                m = self.monster_at(*pos)
                if m and not (m.has("undead") or m.has("construct")):
                    self.damage_monster(m, 1, None)
        for pos in list(lv.glow):
            lv.glow[pos] -= 1
            if lv.glow[pos] <= 0:
                del lv.glow[pos]
        self.tick_status(p.status, True)
        if "poison" in p.status:
            self.hurt(1, "poisoned")
        if self.submerged():
            if not self.breathes_water():
                w = self.weight()
                p.breath -= 1 + (w >= 2) + (w >= 3)
                if p.breath <= 0:
                    p.breath = 0
                    self.hurt(2 + (w >= 3), "drowned")
                    self.msg("You are drowning!", "red")
                elif p.breath <= 3:
                    self.msg("Your lungs burn — you need air!", "orange")
                elif p.breath in (self.max_breath() // 2, self.max_breath() // 2 - 1):
                    self.msg("Half your breath is gone.", "yellow")
            self.gain("swimming", 0.4)
        else:
            p.breath = min(self.max_breath(), p.breath + 3)
        if p.lantern and not self.submerged() and p.oil > 0 and "wick" not in p.relics:
            p.oil = max(0, p.oil - (2 if "eater" in p.pacts else 1))
            if p.oil in (150, 151):
                self.msg("Your lantern is running low on oil.", "yellow")
            if p.oil == 0:
                self.msg("Your lantern gutters out.", "orange")
        p.regen += 1
        if p.regen >= (3 if "knitting" in self.charm_keys() else 7):
            p.regen = 0
            p.hp = min(self.maxhp(), p.hp + 1)
        if "crown" in p.relics and self.turn % 4 == 0:
            p.hp = min(self.maxhp(), p.hp + 1)
        for k in list(p.cooldown):
            p.cooldown[k] -= 1
            if p.cooldown[k] <= 0:
                del p.cooldown[k]
                self.msg(f"The {RELICS[k][0]} is ready again.", "gold")
        for m in list(lv.monsters):
            self.tick_status(m.status, False, m)
            if "poison" in m.status:
                self.damage_monster(m, 1, None)
            if m.charm_turns:
                m.charm_turns -= 1
                if m.charm_turns == 0 and m in lv.monsters:
                    m.team, m.state, m.target = "hostile", "hunt", (p.x, p.y)
                    self.msg(f"{self.the(m, True)} shakes off the conch's spell!", "orange")
            if m.team == "ally" and m.hp < m.maxhp and self.turn % 6 == 0:
                m.hp += 1

    def tick_status(self, status, is_player, m=None):
        ends = {"haste": "You slow down.", "gills": "Your gills close up.", "nightsight": "The dark thickens again.",
                "glow": "You stop glowing.", "confused": "Your head clears.", "poison": "The poison has run its course."}
        for k in list(status):
            status[k] -= 1
            if status[k] <= 0:
                del status[k]
                if is_player and k in ends:
                    self.msg(ends[k], "gray")
                if m is not None and k == "slumber" and m.state == "sleep":
                    m.state = "wander"

    def turn_tide(self):
        lv = self.lv
        before = TIDE_SHIFT[lv.tide]
        lv.tide_i = (lv.tide_i + 1) % len(TIDES)
        lv.tide_clock = TIDE_PERIOD + rng.randint(-8, 8)
        lv.update_water()
        rising = TIDE_SHIFT[lv.tide] > before
        self.msg(f"The tide {'rises' if rising else 'falls'}: {lv.tide} tide.", "blue")

    # -- moving creatures --

    def move_class(self, m):
        f = m.d["flags"]
        if "fly" in f:
            return "fly"
        if "deep" in f:
            return "deep"
        if "aquatic" in f:
            return "aquatic"
        return "swim" if "swim" in f else "walk"

    def allowed(self, cls, x, y):
        lv = self.lv
        t = lv.tiles[y * W + x]
        if t not in WALKABLE and t != DOOR:
            return False
        if cls == "fly" or cls == "swim":
            return True
        w = lv.wat(x, y)
        if cls == "deep":
            return w == 2 and t != DOOR
        if cls == "aquatic":
            return w >= 1 and t != DOOR
        return w < 2

    def field(self, target, cls):
        """Steps from every tile to `target` for creatures that move like `cls`, shared by all of
        them for one player turn."""
        key = (target, cls)
        f = self.fields.get(key)
        if f is not None:
            return f
        f = [-1] * (W * H)
        tx, ty = target
        f[ty * W + tx] = 0
        queue = deque([target])
        while queue:
            x, y = queue.popleft()
            d = f[y * W + x] + 1
            for dx, dy in DIRS8:
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H and f[ny * W + nx] < 0 and self.allowed(cls, nx, ny):
                    f[ny * W + nx] = d
                    queue.append((nx, ny))
        self.fields[key] = f
        return f

    def step_toward(self, m, target):
        cls = self.move_class(m)
        f = self.field(target, cls)
        here = f[m.y * W + m.x]
        here = here if here >= 0 else 1000 + cheb(m.x, m.y, *target)
        best, best_score = [], None
        for dx, dy in DIRS8:
            nx, ny = m.x + dx, m.y + dy
            if not inside(nx, ny) or not self.allowed(cls, nx, ny) or self.occupied(nx, ny):
                continue
            v = f[ny * W + nx]
            score = v if v >= 0 else 1000 + cheb(nx, ny, *target)
            if best_score is None or score < best_score:
                best, best_score = [(nx, ny)], score
            elif score == best_score:
                best.append((nx, ny))
        if not best or best_score > here or (best_score == here and rng.random() < 0.6):
            return False
        self.move_monster(m, *rng.choice(best))
        return True

    def step_away(self, m):
        p = self.player
        cls = self.move_class(m)
        opts = [(m.x + dx, m.y + dy) for dx, dy in DIRS8
                if inside(m.x + dx, m.y + dy) and self.allowed(cls, m.x + dx, m.y + dy)
                and not self.occupied(m.x + dx, m.y + dy)]
        far = [q for q in opts if cheb(*q, p.x, p.y) > cheb(m.x, m.y, p.x, p.y)]
        if far:
            self.move_monster(m, *rng.choice(far))
            return True
        return False

    def random_step(self, m):
        cls = self.move_class(m)
        opts = [(m.x + dx, m.y + dy) for dx, dy in DIRS8
                if inside(m.x + dx, m.y + dy) and self.allowed(cls, m.x + dx, m.y + dy)
                and not self.occupied(m.x + dx, m.y + dy)]
        if opts:
            self.move_monster(m, *rng.choice(opts))

    def move_monster(self, m, x, y):
        lv = self.lv
        if lv.t(x, y) == DOOR:
            lv.put(x, y, OPEN)
            if (x, y) in self.visible:
                self.msg(f"{self.the(m, True)} pushes a door open.", "gray")
            return
        m.x, m.y = x, y

    def nearest_water(self, m, deep):
        lv = self.lv
        best = None
        for dx in range(-6, 7):
            for dy in range(-6, 7):
                x, y = m.x + dx, m.y + dy
                if inside(x, y) and lv.wat(x, y) >= (2 if deep else 1) and lv.t(x, y) in WALKABLE:
                    d = max(abs(dx), abs(dy))
                    if best is None or d < best[0]:
                        best = (d, (x, y))
        return best[1] if best else None

    def environment(self, m):
        """Water that a creature can't live in. Returns True when that was its turn."""
        lv = self.lv
        w = lv.wat(m.x, m.y)
        fish = m.has("aquatic")
        if (fish and w == 0) or (m.has("deep") and w < 2):
            goal = self.nearest_water(m, m.has("deep"))
            if goal and cheb(m.x, m.y, *goal) <= 1 and not self.occupied(*goal):
                m.x, m.y = goal
                return True
            if (m.x, m.y) in self.visible:
                self.msg(f"{self.the(m, True)} flops in the shallows, stranded.", "gray")
            self.damage_monster(m, 2, None)
            if goal:
                d = (sign(goal[0] - m.x), sign(goal[1] - m.y))
                nx, ny = m.x + d[0], m.y + d[1]
                if lv.t(nx, ny) in WALKABLE and not self.occupied(nx, ny):
                    m.x, m.y = nx, ny
            return True
        if m.has("dissolve") and w == 2:
            if (m.x, m.y) in self.visible:
                self.msg(f"{self.the(m, True)} dissolves in the deep water!", "cyan")
            self.damage_monster(m, 4, None)
        if w == 2 and self.move_class(m) == "walk":
            if self.player_sees(m):
                self.msg(f"{self.the(m, True)} thrashes in the deep water.", "gray")
            self.damage_monster(m, 2, None)
            for dx, dy in DIRS8:
                nx, ny = m.x + dx, m.y + dy
                if inside(nx, ny) and lv.wat(nx, ny) < 2 and lv.t(nx, ny) in WALKABLE and not self.occupied(nx, ny):
                    m.x, m.y = nx, ny
                    break
            return True
        return False

    # -- creatures' turns --

    def monster_act(self, m):
        if m.hp <= 0 or m not in self.lv.monsters:
            return 100
        if any(k in m.status for k in ("paralyzed", "stun", "slumber")):
            return 100
        if m.team == "neutral":
            return 100
        if self.environment(m) or m not in self.lv.monsters:
            return 100
        if m.team == "ally":
            return self.ally_act(m)
        return self.hostile_act(m)

    def notice_chance(self, m, d):
        p = self.player
        lit = (p.x, p.y) in self.lit
        c = 0.5 + (0.25 if lit else 0) - 0.05 * self.level("stealth")
        c -= 0.2 if p.sneaking else 0
        c -= 0.15 if "hush" in self.charm_keys() else 0
        if d <= 1:
            c += 0.35
        return max(0.08, min(0.97, c))

    def notice(self, m):
        p = self.player
        m.state, m.target = "hunt", (p.x, p.y)
        if self.player_sees(m) and not m.has("stealthy"):
            self.msg(f"{self.the(m, True)} notices you!", "orange")

    def hostile_act(self, m):
        p, lv = self.player, self.lv
        m.cool = max(0, m.cool - 1)
        d = cheb(m.x, m.y, p.x, p.y)
        if m.state == "sleep":
            if d <= 1 and (p.x, p.y) in self.lit and rng.random() < 0.1:
                self.notice(m)
            return 100
        if "fear" in m.status or m.state == "flee":
            if m.state == "flee":
                m.search -= 1
                if m.search <= 0:
                    m.state = "wander"
            if not self.step_away(m) and d <= 1:
                return self.attack_player(m)
            return 100
        if m.has("lure") and (m.lure is None or rng.random() < 0.08):
            spots = [(m.x + dx, m.y + dy) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)
                     if inside(m.x + dx, m.y + dy) and lv.wat(m.x + dx, m.y + dy) >= 1 and (dx or dy)]
            m.lure = rng.choice(spots) if spots else None
        sees = self.monster_sees(m)
        if m.has("oil") and self.lantern_lit() and d <= 10 and self.los(m.x, m.y, p.x, p.y):
            if m.state != "hunt":
                self.notice(m)
            sees = True
        elif sees and m.state != "hunt":
            if m.state == "ambush" and d > 2 and not (m.has("aquatic") and self.in_water() and d <= 4):
                sees = False
            elif rng.random() < self.notice_chance(m, d):
                self.notice(m)
        if m.state == "hunt":
            if sees:
                m.target = (p.x, p.y)
                if self.ability(m, d):
                    return 100
            if d <= 1:
                foe = m.foe if m.foe in lv.monsters and cheb(m.x, m.y, m.foe.x, m.foe.y) <= 1 else None
                if foe and rng.random() < 0.5:
                    return self.monster_attack(m, foe)
                return self.attack_player(m)
            if m.foe in lv.monsters and cheb(m.x, m.y, m.foe.x, m.foe.y) <= 1:
                return self.monster_attack(m, m.foe)
            if m.has("stationary"):
                return 100
            moved = self.step_toward(m, m.target) if m.target else False
            if not moved:
                blocker = next((a for a in lv.monsters if a.team == "ally" and cheb(a.x, a.y, m.x, m.y) <= 1), None)
                if blocker:
                    return self.monster_attack(m, blocker)
            if not sees and ((m.x, m.y) == m.target or not moved):
                m.state, m.search = "search", 8
            return 100
        if m.state == "search":
            if m.target and (m.x, m.y) != m.target and m.search > 4:
                if not self.step_toward(m, m.target):
                    m.search -= 1
            else:
                self.random_step(m)
                m.search -= 1
            m.search -= 0.5
            if m.search <= 0:
                m.state, m.target = "wander", None
            return 100
        if m.state == "eat":
            items = lv.items.get(m.target, [])
            bait = next((it for it in items if it.kind == "bait"), None)
            if not bait:
                m.state = "wander"
                return 100
            if (m.x, m.y) != m.target:
                if not self.step_toward(m, m.target) and cheb(m.x, m.y, *m.target) <= 1:
                    m.search -= 1
            else:
                m.search -= 1
                if m.search <= 0:
                    bait.count -= 1
                    if bait.count <= 0:
                        items.remove(bait)
                        if not items:
                            del lv.items[m.target]
                    if self.player_sees(m):
                        self.msg(f"{self.the(m, True)} finishes the sweetfish.", "gray")
                    m.state = "wander"
            return 100
        if m.state in ("ambush", "idle") or m.has("stationary"):
            return 100
        if m.dest is None or (m.x, m.y) == m.dest or rng.random() < 0.03:
            cls = self.move_class(m)
            for _ in range(20):
                i = rng.choice(lv.floor_idx)
                if self.allowed(cls, i % W, i // W):
                    m.dest = (i % W, i // W)
                    break
        if m.dest and rng.random() < 0.8 and not self.step_toward(m, m.dest):
            m.stuck += 1
            if m.stuck > 4:
                m.dest, m.stuck = None, 0
        return 100

    def ability(self, m, d):
        """Special moves when it can see you. Returns True when it used its turn on one."""
        p, lv = self.player, self.lv
        if m.cool:
            return False
        if m.has("summon") and d <= 8:
            limit = 3 if m.has("boss") else 2
            if m.raised < limit and rng.random() < 0.5:
                spots = [(m.x + dx, m.y + dy) for dx, dy in DIRS8
                         if lv.t(m.x + dx, m.y + dy) in WALKABLE and not self.occupied(m.x + dx, m.y + dy)]
                if spots:
                    z = self.spawn("drowned", *rng.choice(spots), "hunt")
                    z.target = (p.x, p.y)
                    self.noticed.add(z.uid)
                    m.raised += 1
                    m.cool = 10 if m.has("boss") else 8
                    if self.player_sees(m):
                        self.msg(f"{self.the(m, True)} calls out, and a drowned sailor rises from the water!", "magenta")
                    return True
        if m.has("song") and 2 <= d <= 7:
            m.cool = 4
            if rng.random() < 0.55:
                nx, ny = p.x + sign(m.x - p.x), p.y + sign(m.y - p.y)
                if lv.t(nx, ny) in WALKABLE and not self.occupied(nx, ny):
                    p.x, p.y = nx, ny
                    self.msg(f"{self.the(m, True)}'s song draws you toward the water!", "magenta")
                    if self.submerged():
                        self.msg("You slip under the surface.", "blue")
                    self.compute_view()
                    return True
            self.msg(f"You shake off {self.the(m)}'s song.", "gray")
            return True
        if m.has("surge") and d <= 3:
            m.cool = 7
            dx, dy = sign(p.x - m.x), sign(p.y - m.y)
            for _ in range(2):
                nx, ny = p.x + dx, p.y + dy
                if lv.t(nx, ny) in WALKABLE and not self.occupied(nx, ny):
                    p.x, p.y = nx, ny
            self.msg("The Drowned King raises a wave that hurls you back!", "magenta")
            self.hurt(roll("1d6"), "swept away by the Drowned King")
            self.compute_view()
            return True
        return False

    def ally_act(self, a):
        p, lv = self.player, self.lv
        foes = [m for m in lv.monsters if m.team == "hostile" and cheb(m.x, m.y, a.x, a.y) <= 7
                and cheb(m.x, m.y, p.x, p.y) <= 9 and self.los(a.x, a.y, m.x, m.y)
                and not (m.has("hidden") and not m.revealed)]
        if a.order == "hold":
            near = [m for m in foes if cheb(m.x, m.y, a.x, a.y) <= 1]
            return self.monster_attack(a, near[0]) if near else 100
        if foes:
            f = min(foes, key=lambda m: (m.state != "hunt", cheb(m.x, m.y, a.x, a.y)))
            if cheb(f.x, f.y, a.x, a.y) <= 1:
                return self.monster_attack(a, f)
            if self.step_toward(a, (f.x, f.y)):
                return 100
        if cheb(a.x, a.y, p.x, p.y) > 2:
            self.step_toward(a, (p.x, p.y))
        return 100

    # -- fighting --

    def attack_player(self, m):
        p = self.player
        unseen = not self.player_sees(m)
        if m.has("hidden"):
            m.revealed = True
        if rng.randint(1, 20) + m.d["acc"] >= 10 + self.armour() or (m.has("stealthy") and unseen):
            dmg = roll(m.d["dmg"]) + m.bonus
            if m.has("stealthy") and unseen:
                dmg *= 2
                self.msg("Something strikes you from the dark!", "red")
            ac = self.armour()
            dmg -= rng.randint(0, ac // 3) + ("shell" in p.relics)
            dmg = max(1, dmg)
            if m.has("shock") and self.in_water():
                dmg += roll("1d4")
                extra = " — the shock runs through the water"
            else:
                extra = ""
            self.msg(f"{self.the(m, True)} {m.d['verb']} you{extra}.", "red" if dmg >= 5 else "orange")
            if m.has("drain"):
                m.hp = min(m.maxhp, m.hp + dmg)
            if m.has("oil") and p.oil > 0 and "wick" not in p.relics:
                p.oil = max(0, p.oil - (60 if m.has("douse") else 25))
                self.msg("It drinks the oil from your lantern!", "orange")
            if m.has("douse") and p.lantern:
                p.lantern = False
                self.msg("Your lantern is snuffed out!", "orange")
            if m.has("paralyze") and rng.random() < 0.25:
                p.status["paralyzed"] = 2
                self.msg("The sting numbs you — you can't move!", "red")
            if m.has("grab"):
                p.status["held"] = 3
            if m.has("thief") and rng.random() < 0.35:
                loot = [it for it in p.inv if it not in (p.weapon, p.armor) and it not in p.charms
                        and it.kind != "heart"]
                if loot:
                    it = rng.choice(loot)
                    p.inv.remove(it)
                    m.carry = it
                    m.state, m.search = "flee", 25
                    self.msg(f"{self.the(m, True)} snatches your {self.names(it)[0]} and runs!", "red")
            self.hurt(dmg, f"killed by {self.a(m)}")
        else:
            self.msg(f"{self.the(m, True)} misses you.", "gray")
        return 100

    def monster_attack(self, att, dfn):
        vis = self.player_sees(att) or self.player_sees(dfn)
        if rng.randint(1, 20) + att.d["acc"] + att.bonus >= 10 + dfn.d["ac"]:
            dmg = roll(att.d["dmg"]) + att.bonus
            if vis:
                self.msg(f"{self.the(att, True)} {att.d['verb']} {self.the(dfn)}.",
                         "red" if dfn.team == "ally" else "white")
            if dfn.team == "hostile":
                dfn.foe = att
                if dfn.state != "hunt":
                    dfn.state, dfn.target = "hunt", (att.x, att.y)
            else:
                att.foe = dfn
            self.damage_monster(dfn, dmg, att)
        elif vis:
            self.msg(f"{self.the(att, True)} misses {self.the(dfn)}.", "gray")
        self.noise(att.x, att.y, 4)
        return 100

    def unaware(self, m):
        return m.state in ("sleep", "wander", "search", "eat", "ambush", "idle") or any(
            k in m.status for k in ("slumber", "paralyzed", "stun"))

    def player_attack(self, m, dx, dy):
        p = self.player
        wkey = p.weapon.key if p.weapon else None
        wd = WEAPONS[wkey] if wkey else ("fists", "1d2", 0, 3, 0, "")
        flags = wd[5].split()
        if m.has("hidden"):
            m.revealed = True
        if self.unaware(m):
            dmg = (roll(wd[1]) + self.melee_bonus()) * self.sneak_mult()
            self.msg(f"You strike {self.the(m)} unawares!", "lime")
            self.gain("stealth", 3)
            loud = 2
        else:
            if rng.randint(1, 20) + 3 + wd[2] + self.level("melee") + (p.weapon.ench if p.weapon else 0) < 10 + m.d["ac"]:
                self.msg(f"You miss {self.the(m)}.", "gray")
                m.state, m.target = "hunt", (p.x, p.y)
                self.noise(p.x, p.y, 5)
                return 140 if "heavy" in flags else 100
            dmg = roll(wd[1]) + self.melee_bonus()
            self.msg(f"You hit {self.the(m)}.")
            loud = 6
        if "gloves" in p.relics and self.lv.wat(m.x, m.y) > 0:
            dmg += roll("1d6")
        self.gain("melee", 1)
        if "fang" in p.relics:
            p.hp = min(self.maxhp(), p.hp + max(1, dmg // 4))
        if "pierce" in flags:
            behind = self.monster_at(m.x + dx, m.y + dy)
            if behind and behind.team == "hostile":
                self.msg(f"Your blow carries through to {self.the(behind)}.")
                self.damage_monster(behind, roll(wd[1]) + self.melee_bonus(), "player")
        died = self.damage_monster(m, max(1, dmg), "player")
        if not died and m.team == "hostile":
            m.state, m.target = "hunt", (p.x, p.y)
            if "knock" in flags:
                nx, ny = m.x + dx, m.y + dy
                if self.allowed(self.move_class(m), nx, ny) and not self.occupied(nx, ny):
                    m.x, m.y = nx, ny
                    self.msg(f"{self.the(m, True)} is knocked back.")
        self.noise(p.x, p.y, loud)
        return 140 if "heavy" in flags else 100

    def damage_monster(self, m, dmg, killer):
        if m not in self.lv.monsters:
            return True
        m.hp -= dmg
        if m.has("hidden"):
            m.revealed = True
        if m.hp <= 0:
            self.kill(m, killer)
            return True
        if m.team == "hostile" and killer == "player" and m.state != "hunt":
            m.state, m.target = "hunt", (self.player.x, self.player.y)
        if "slumber" in m.status and dmg > 0:
            del m.status["slumber"]
            if m.state == "sleep":
                m.state = "hunt"
        return False

    def kill(self, m, killer):
        lv, p = self.lv, self.player
        lv.monsters.remove(m)
        seen = (m.x, m.y) in self.visible
        if m.team == "ally":
            self.msg(f"{m.pet or self.the(m, True)} dies.", "red")
        elif seen:
            word = "is destroyed" if m.has("undead") else "crumbles" if m.has("construct") else "dies"
            self.msg(f"{self.the(m, True)} {word}.", "white")
        if m.carry:
            lv.add_item(m.x, m.y, m.carry)
        if m.has("burst"):
            self.cloud(m.x, m.y, 1, "gas", 8)
            if seen:
                self.msg("It bursts in a cloud of foul gas!", "green")
        if m.kind == "king":
            self.king_dead = True
            self.msg("The Drowned King falls, and the water around his throne goes still.", "gold")
        if killer == "player":
            p.kills += 1
        elif isinstance(killer, Monster) and killer.team == "ally" and killer in lv.monsters:
            killer.kills += 1
            self.gain("kinship", 1.5)
            if killer.kills % 2 == 0 and not killer.charm_turns:
                killer.maxhp += 3
                killer.hp += 3
                killer.bonus += 1
                self.msg(f"{killer.pet} grows stronger.", "cyan")
        for other in lv.monsters:
            if other.foe is m:
                other.foe = None

    def hurt(self, dmg, cause):
        p = self.player
        p.hp -= dmg
        self.disturb = True
        if p.hp <= 0 and not self.over:
            p.hp = 0
            self.over = "dead"
            self.cause = f"{cause} on depth {self.lv.depth}"
            self.record()

    # -- the player's actions; each returns the time it took --

    def player_move(self, dx, dy):
        p, lv = self.player, self.lv
        if "confused" in p.status and rng.random() < 0.5:
            dx, dy = rng.choice(DIRS8)
        x, y = p.x + dx, p.y + dy
        if not inside(x, y):
            return 0
        m = self.monster_at(x, y)
        if m:
            if m.team == "ally":
                return self.swap(m)
            if m.team == "neutral":
                self.pending = ("trade", m)
                return 0
            return self.player_attack(m, dx, dy)
        if "held" in p.status and rng.random() < 0.6:
            self.msg("The kelp holds you fast!", "yellow")
            return 100
        t = lv.t(x, y)
        if t == DOOR:
            lv.put(x, y, OPEN)
            self.noise(x, y, 3)
            return 100
        if t in (LAMP, DARK_LAMP):
            return self.toggle_lamp(x, y)
        if t not in WALKABLE:
            return 0
        was, now = lv.wat(p.x, p.y), lv.wat(x, y)
        p.x, p.y = x, y
        if now == 2:
            cost = 140 - 8 * self.level("swimming") - 25 * ("tides" in self.charm_keys())
            cost -= 25 if p.armor and ARMOURS[p.armor.key][5] == "swim" else 0
            cost = max(70, cost)
        else:
            cost = 110 if now == 1 else 100
        if p.sneaking:
            cost = cost * 3 // 2
        self.noise(x, y, self.step_noise(now))
        if now == 2 and was < 2:
            dark = " Your lantern goes dark." if p.lantern and p.oil > 0 else ""
            self.msg("You dive under the water." + dark, "blue")
            if self.weight() >= 3 and not self.breathes_water():
                self.msg("Your armour drags you down!", "orange")
        elif was == 2 and now < 2:
            self.msg("You come up for air.", "blue")
        self.practise_stealth()
        self.arrive()
        return cost

    def practise_stealth(self):
        p = self.player
        if any(m.team == "hostile" and m.state != "hunt" and cheb(m.x, m.y, p.x, p.y) <= 6
               for m in self.lv.monsters):
            self.gain("stealth", 0.6 if p.sneaking else 0.3)

    def arrive(self):
        p, lv = self.player, self.lv
        pos = (p.x, p.y)
        items = lv.items.get(pos)
        if items:
            if len(items) == 1:
                self.msg(f"You see {self.item_name(items[0])} here.", "gray")
            else:
                self.msg(f"There are {len(items)} things here.", "gray")
        t = lv.t(*pos)
        if t == STAIRS:
            self.msg("There are stairs down here: > to go down.", "gray")
        elif t == SHRINE:
            self.pending = ("shrine", pos)
        elif t == EXIT:
            if any(it.kind == "heart" for it in p.inv):
                self.over = "won"
                self.cause = "brought the Tide-Heart up out of the Drowned Palace"
                self.msg("The current takes you up, the Tide-Heart beating in your arms.", "gold")
                self.record()
            else:
                self.msg("The current pulls upward. But you came for the Tide-Heart.", "gray")

    def swap(self, a):
        p = self.player
        if not self.allowed(self.move_class(a), p.x, p.y):
            self.msg(f"{a.pet} can't go there.", "gray")
            return 0
        a.x, a.y, p.x, p.y = p.x, p.y, a.x, a.y
        self.arrive()
        return 100

    def toggle_lamp(self, x, y):
        lv = self.lv
        if lv.t(x, y) == LAMP:
            lv.put(x, y, DARK_LAMP)
            self.msg("You douse the street lamp.", "gray")
            self.noise(x, y, 1)
        elif self.lantern_lit():
            lv.put(x, y, LAMP)
            self.msg("You light the street lamp from your lantern.", "yellow")
        else:
            self.msg("You need a lit lantern to light it.", "gray")
            return 0
        return 100

    def wait(self):
        return 100

    def toggle_lantern(self):
        p = self.player
        if not p.lantern and p.oil <= 0 and "wick" not in p.relics:
            self.msg("Your lantern has no oil.", "gray")
            return 0
        p.lantern = not p.lantern
        if p.lantern:
            self.msg("You open the lantern's shutter." + (" (It stays dark under water.)" if self.submerged() else ""),
                     "yellow")
        else:
            self.msg("You shutter your lantern. Darkness closes in.", "gray")
        return 50

    def toggle_sneak(self):
        p = self.player
        p.sneaking = not p.sneaking
        self.msg("You move carefully now: quieter, but slower." if p.sneaking else "You stop sneaking.", "gray")

    def close_door(self, dx, dy):
        p, lv = self.player, self.lv
        x, y = p.x + dx, p.y + dy
        if lv.t(x, y) != OPEN:
            self.msg("There's no open door there.", "gray")
            return 0
        if self.monster_at(x, y) or lv.items.get((x, y)):
            self.msg("Something is in the way.", "gray")
            return 0
        lv.put(x, y, DOOR)
        self.noise(x, y, 2)
        return 100

    def command_allies(self):
        pets = self.pets()
        if not pets:
            self.msg("You have no companions.", "gray")
            return
        order = "hold" if pets[0].order == "follow" else "follow"
        for a in pets:
            a.order = order
        names = ", ".join(a.pet for a in pets)
        self.msg(f"{names}: {'hold this spot' if order == 'hold' else 'follow me'}.", "cyan")

    def descend(self):
        p, lv = self.player, self.lv
        if lv.t(p.x, p.y) != STAIRS:
            return 0
        coming, left = [], []
        for a in self.pets():
            if a.order == "follow" and cheb(a.x, a.y, p.x, p.y) <= 3 and self.move_class(a) not in ("aquatic", "deep"):
                coming.append(a)
            else:
                left.append(a)
        for a in coming:
            lv.monsters.remove(a)
        for a in left:
            self.msg(f"{a.pet} stays behind.", "orange")
        self.new_level(lv.depth + 1, coming)
        try:
            save_game(self)
        except OSError:
            pass
        return 0

    # -- items --

    def take(self, it):
        p = self.player
        if it.kind == "pearls":
            p.pearls += it.count
            self.msg(f"You gather {self.item_name(it)}.", "white")
            return True
        if it.kind == "relic":
            p.relics.append(it.key)
            name, desc, _ = RELICS[it.key]
            self.msg(f"You take the {name}! {desc}", "gold")
            if it.key == "monocle":
                self.reveal_map()
            return True
        if it.kind == "oil" and (p.oil + OIL_FLASK <= OIL_MAX or "wick" in p.relics):
            if "wick" in p.relics:
                self.msg("You don't need the oil any more, so you leave it.", "gray")
                return False
            p.oil += OIL_FLASK
            self.msg("You pour the oil into your lantern.", "yellow")
            return True
        if it.kind in STACKABLE:
            for have in p.inv:
                if have.kind == it.kind and have.key == it.key:
                    have.count += it.count
                    self.msg(f"You pick up {self.item_name(it)} ({LETTERS[p.inv.index(have)]}).")
                    return True
        if len(p.inv) >= MAX_INVENTORY:
            self.msg("Your pack is full.", "orange")
            return False
        p.inv.append(it)
        self.msg(f"You pick up {self.item_name(it)} ({LETTERS[p.inv.index(it)]}).")
        if it.kind == "heart":
            self.take_heart()
        return True

    def pickup(self):
        p, lv = self.player, self.lv
        pos = (p.x, p.y)
        items = lv.items.get(pos)
        if not items:
            self.msg("There's nothing here.", "gray")
            return 0
        left = [it for it in items if not self.take(it)]
        if left:
            lv.items[pos] = left
            lv.ignored.add(pos)
        else:
            del lv.items[pos]
            lv.mem_items.pop(pos, None)
        return 100

    def take_heart(self):
        lv, p = self.lv, self.player
        self.heart_taken = True
        self.flood_clock = 10
        lv.put(*lv.start, EXIT)
        self.msg("You lift the Tide-Heart from its altar. It beats in your hands like something alive.", "gold")
        self.msg("The palace groans and the sea pours in. Get back to where you came in — the current waits there!",
                 "red")
        for m in lv.monsters:
            if m.team == "hostile":
                if m.kind == "king":
                    m.state, m.target = "hunt", (p.x, p.y)
                    m.status.pop("slumber", None)
                elif m.state in ("sleep", "wander"):
                    m.state, m.target, m.search = "search", (p.x, p.y), 10

    def split_one(self, it):
        p = self.player
        if it.count > 1:
            it.count -= 1
            return it.copy_one()
        p.inv.remove(it)
        if p.weapon is it:
            p.weapon = None
        if p.armor is it:
            p.armor = None
        return it

    def drop(self, it):
        p, lv = self.player, self.lv
        if it in p.charms:
            if it.key in CURSED:
                self.msg("The charm won't come off!", "red")
                return 0
            p.charms.remove(it)
        if it.kind == "heart":
            self.msg("You won't let go of the Tide-Heart.", "gray")
            return 0
        p.inv.remove(it)
        if p.weapon is it:
            p.weapon = None
        if p.armor is it:
            p.armor = None
        lv.add_item(p.x, p.y, it)
        lv.ignored.add((p.x, p.y))
        self.msg(f"You drop {self.item_name(it)}.", "gray")
        return 100

    def equip(self, it):
        p = self.player
        if it.kind == "weapon":
            if p.weapon is it:
                p.weapon = None
                self.msg(f"You put away {self.item_name(it)}. You'll fight with your fists.", "gray")
            else:
                p.weapon = it
                self.msg(f"You wield {self.item_name(it)}.")
            return 100
        if it.kind == "armor":
            if p.armor is it:
                p.armor = None
                self.msg(f"You take off {self.item_name(it)}.", "gray")
            else:
                if ARMOURS[it.key][2] >= 2 and "kin" in p.pacts:
                    self.msg("Your pact with the deep forbids heavy armour.", "orange")
                    return 0
                p.armor = it
                self.msg(f"You put on {self.item_name(it)}.")
            return 200
        if it.kind == "charm":
            if it in p.charms:
                if it.key in CURSED:
                    self.msg("The charm won't come off!", "red")
                    return 0
                p.charms.remove(it)
                self.msg(f"You take off {self.item_name(it)}.", "gray")
                return 100
            if len(p.charms) >= 2:
                self.msg("You already wear two charms.", "gray")
                return 0
            p.charms.append(it)
            self.identify(it.key, quiet=True)
            name = CHARMS[it.key][0]
            if it.key in CURSED:
                self.msg(f"The charm tightens around your wrist — it is a cursed charm of {name}! "
                         f"{CHARMS[it.key][2]}", "red")
            else:
                self.msg(f"You put on a charm of {name}. {CHARMS[it.key][2]}", "cyan")
            return 100
        self.msg("You can't wear that.", "gray")
        return 0

    def drink(self, it):
        p = self.player
        if it.kind == "oil":
            if p.oil + OIL_FLASK > OIL_MAX + 100:
                self.msg("Your lantern is nearly full.", "gray")
                return 0
            self.split_one(it)
            p.oil = min(OIL_MAX, p.oil + OIL_FLASK)
            self.msg("You fill your lantern.", "yellow")
            return 100
        if it.kind != "vial":
            self.msg("You can't drink that.", "gray")
            return 0
        key = self.split_one(it).key
        maxhp = self.maxhp()
        if key == "mending":
            amount = max(10, maxhp * 3 // 5) // (2 if "shark" in p.pacts else 1)
            p.hp = min(maxhp, p.hp + amount)
            p.status.pop("poison", None)
            self.msg("Warmth spreads through you; your wounds close.", "lime")
        elif key == "gills":
            p.status["gills"] = 150
            self.msg("Gills open along your neck! You can breathe water.", "cyan")
        elif key == "nightsight":
            p.status["nightsight"] = 200
            self.msg("The dark thins before your eyes.", "cyan")
        elif key == "swiftness":
            p.status["haste"] = 25
            self.msg("You feel as quick as a darting fish.", "cyan")
        elif key == "vigor":
            p.vigor += 4
            p.hp = min(self.maxhp(), p.hp + 8)
            self.msg("You feel hardier. (+4 maximum health)", "lime")
        elif key == "brine":
            p.status["poison"] = 8
            self.msg("Bitter brine! You retch — you are poisoned.", "orange")
        elif key == "murk":
            p.status["confused"] = 10
            self.msg("The world swirls like stirred silt.", "orange")
        elif key == "phosphor":
            p.status["glow"] = 120
            self.msg("Your skin begins to glow. Anything can see you now.", "orange")
        elif key == "ink":
            self.cloud(p.x, p.y, 2, "ink", 15)
            self.msg("Ink billows out of the vial all around you!", "purple")
        self.identify(key)
        return 100

    def cloud(self, x, y, r, kind, turns):
        lv = self.lv
        for yy in range(y - r, y + r + 1):
            for xx in range(x - r, x + r + 1):
                if inside(xx, yy) and lv.t(xx, yy) not in SOLID and (xx - x) ** 2 + (yy - y) ** 2 <= r * r + r:
                    lv.clouds[(xx, yy)] = (kind, turns)

    def actors_near(self, x, y, r):
        p = self.player
        out = [m for m in self.lv.monsters if cheb(m.x, m.y, x, y) <= r]
        return out, cheb(p.x, p.y, x, y) <= r

    def shatter(self, key, x, y):
        """A vial breaking at (x, y); returns whether anything showed what it was."""
        lv, p = self.lv, self.player
        seen = (x, y) in self.visible
        mons, me = self.actors_near(x, y, 1)
        self.noise(x, y, 5)
        if key == "mending":
            for m in mons:
                m.hp = min(m.maxhp, m.hp + 12)
            if me:
                p.hp = min(self.maxhp(), p.hp + 8)
            if mons or me:
                self.msg("The splash closes wounds.", "lime")
                return seen
        elif key == "swiftness":
            for m in mons:
                m.status["haste"] = 15
            if mons:
                self.msg("Everything splashed starts moving faster.", "orange")
                return seen
        elif key == "brine":
            self.cloud(x, y, 1, "gas", 10)
            self.msg("The vial bursts into a cloud of poison.", "green")
            return seen
        elif key == "murk":
            for m in mons:
                m.status["confused"] = 8
            if me:
                p.status["confused"] = 5
            self.msg("A cloud of murk spreads and swirls.", "teal")
            return seen
        elif key == "phosphor":
            for yy in range(y - 2, y + 3):
                for xx in range(x - 2, x + 3):
                    if inside(xx, yy) and lv.t(xx, yy) not in SOLID:
                        lv.glow[(xx, yy)] = 150
            for m in self.actors_near(x, y, 2)[0]:
                m.status["glow"] = 100
            self.msg("Glowing algae splatters over everything around!", "cyan")
            return True
        elif key == "ink":
            self.cloud(x, y, 2, "ink", 15)
            self.msg("Ink billows out where the vial broke.", "purple")
            return True
        if seen:
            self.msg("The vial shatters harmlessly.", "gray")
        return False

    def projectile(self, tx, ty, reach):
        """The tiles something thrown or zapped at (tx, ty) passes, up to where it stops."""
        p, lv = self.player, self.lv
        out = []
        for x, y in line(p.x, p.y, tx, ty)[:reach]:
            if lv.t(x, y) in OPAQUE and lv.t(x, y) != KELP or lv.t(x, y) in (STATUE, LAMP, DARK_LAMP):
                break
            out.append((x, y))
            if self.monster_at(x, y):
                break
        return out

    def throw(self, it, tx, ty):
        p, lv = self.player, self.lv
        if (tx, ty) == (p.x, p.y):
            return 0
        if it in p.charms:
            self.msg("Take the charm off first.", "gray")
            return 0
        if it.kind == "heart":
            self.msg("You won't throw the Tide-Heart.", "gray")
            return 0
        path = self.projectile(tx, ty, THROW_RANGE)
        if not path:
            self.msg("There's no room to throw.", "gray")
            return 0
        one = self.split_one(it)
        x, y = path[-1]
        m = self.monster_at(x, y)
        name = self.names(one)[0]
        if one.kind == "vial":
            if self.shatter(one.key, x, y):
                self.identify(one.key)
            return 100
        if one.kind == "weapon" and m:
            aim = self.level("aim")
            dice = "2d6" if "throw" in WEAPONS[one.key][5] else WEAPONS[one.key][1]
            sneak = self.unaware(m)
            if sneak or rng.randint(1, 20) + aim + 2 + one.ench >= 10 + m.d["ac"]:
                dmg = (roll(dice) + aim // 2 + one.ench) * (2 if sneak else 1)
                self.msg(f"The {name} hits {self.the(m)}.")
                self.gain("aim", 2)
                self.damage_monster(m, max(1, dmg), "player")
            else:
                self.msg(f"The {name} misses {self.the(m)}.", "gray")
                if m.team == "hostile":
                    m.state, m.target = "hunt", (p.x, p.y)
            self.noise(x, y, 3)
        elif m:
            self.msg(f"The {name} bounces off {self.the(m)}.", "gray")
        lv.add_item(x, y, one)
        if one.kind == "bait":
            self.msg("The sweetfish lands with a wet slap.", "gray")
            for b in lv.monsters:
                if (b.team == "hostile" and b.has("beast") and cheb(b.x, b.y, x, y) <= 8 and b.state != "sleep"
                        and (b.state != "hunt" or rng.random() < 0.4)):
                    b.state, b.target, b.search = "eat", (x, y), 6
                    if self.player_sees(b):
                        self.msg(f"{self.the(b, True)} goes for the sweetfish.", "yellow")
        return 100

    def zap(self, it, tx, ty):
        p, lv = self.player, self.lv
        if (tx, ty) == (p.x, p.y):
            return 0
        if it.charges <= 0:
            self.msg("The rod is spent. Nothing happens.", "gray")
            return 100
        it.charges -= 1
        key, aim = it.key, self.level("aim")
        if key == "blink":
            path = self.projectile(tx, ty, 8)
            free = [q for q in path if not self.monster_at(*q) and lv.t(*q) in WALKABLE]
            if free:
                p.x, p.y = free[-1]
                self.msg("You blink across the gap!", "cyan")
                self.identify(key)
                self.arrive()
            else:
                self.msg("The rod twitches, but there is nowhere to go.", "gray")
            return 100
        path = self.projectile(tx, ty, 10)
        if not path:
            self.msg("There's no room for it to fly.", "gray")
            return 100
        x, y = path[-1]
        m = self.monster_at(x, y)
        seen = (x, y) in self.visible
        if m:
            self.gain("aim", 2)
        if key == "spark":
            self.msg("A spark leaps from the rod!", "yellow")
            if m:
                self.damage_monster(m, roll("2d6") + aim, "player")
            if lv.wat(x, y) >= 1:
                self.msg("Lightning crackles through the water!", "yellow")
                self.shock_water(x, y, roll("1d6") + aim // 2, m)
            self.noise(x, y, 6)
            self.identify(key)
        elif key == "frost":
            if m:
                self.damage_monster(m, roll("1d6") + aim // 2, "player")
                if m in lv.monsters:
                    m.status["paralyzed"] = 2
            froze = False
            for yy in range(y - 1, y + 2):
                for xx in range(x - 1, x + 2):
                    if inside(xx, yy) and lv.water[yy * W + xx] >= 1 and lv.t(xx, yy) in WALKABLE:
                        lv.ice[(xx, yy)] = 40
                        froze = True
                        fish = self.monster_at(xx, yy)
                        if fish and (fish.has("aquatic") or fish.has("deep")):
                            fish.status["paralyzed"] = 4
            self.msg("Frost blooms" + ("; the water freezes solid." if froze else "."), "ice")
            if seen or m:
                self.identify(key)
        elif key == "slumber":
            if m and not m.has("boss") and m.team == "hostile":
                m.state = "sleep"
                m.status["slumber"] = 15 + 2 * aim
                self.msg(f"{self.the(m, True)} falls asleep.", "cyan")
                self.identify(key)
            elif m and m.has("boss"):
                self.msg(f"{self.the(m, True)} shrugs off the grey mist.", "gray")
                self.identify(key)
            else:
                self.msg("A grey mist drifts from the rod and fades.", "gray")
        elif key == "beckon":
            if m:
                pts = line(m.x, m.y, p.x, p.y)[:-1]
                dest = (m.x, m.y)
                for q in pts:
                    if self.occupied(*q) or lv.t(*q) not in WALKABLE:
                        break
                    dest = q
                m.x, m.y = dest
                self.msg(f"{self.the(m, True)} is yanked toward you!", "cyan")
                self.identify(key)
            else:
                self.msg("The rod tugs at nothing.", "gray")
        elif key == "slowing":
            if m:
                m.status["slow"] = 20
                self.msg(f"{self.the(m, True)} slows down.", "cyan")
                self.identify(key)
            else:
                self.msg("The rod hums and goes quiet.", "gray")
        return 100

    def shock_water(self, x, y, dmg, skip):
        """Lightning spreads through the water joined to (x, y), five tiles at most."""
        lv, p = self.lv, self.player
        wet, queue = {(x, y)}, deque([(x, y)])
        while queue:
            cx, cy = queue.popleft()
            for dx, dy in DIRS8:
                q = (cx + dx, cy + dy)
                if inside(*q) and q not in wet and lv.wat(*q) >= 1 and cheb(*q, x, y) <= 5:
                    wet.add(q)
                    queue.append(q)
        for m in list(lv.monsters):
            if (m.x, m.y) in wet and m is not skip:
                self.damage_monster(m, dmg, "player")
        if (p.x, p.y) in wet:
            self.msg("The lightning finds you through the water!", "red")
            self.hurt(dmg, "electrocuted by their own rod")

    def read(self, it):
        p, lv = self.player, self.lv
        if self.submerged():
            self.msg("You can't read a shell under water.", "gray")
            return 0
        key = self.split_one(it).key
        self.identify(key, quiet=True)
        name = SHELLS[key][0]
        if key == "mapping":
            self.reveal_map()
            self.msg("The shell's lines become a map of the whole depth.", "cyan")
        elif key == "current":
            spots = [i for i in lv.floor_idx if lv.water[i] == 0 and lv.tiles[i] == FLOOR
                     and cheb(i % W, i // W, p.x, p.y) > 12 and not self.occupied(i % W, i // W)]
            if spots:
                i = rng.choice(spots)
                p.x, p.y = i % W, i // W
            self.msg("A rush of water sweeps you away!", "cyan")
            self.arrive()
        elif key == "lore":
            if any(self.unknown(x) for x in p.inv):
                self.pending = ("identify",)
            else:
                self.msg("The shell hums with knowledge, but you know all you carry.", "gray")
        elif key == "song":
            beasts = sorted((m for m in lv.monsters if m.team == "hostile" and m.has("beast") and self.player_sees(m)),
                            key=lambda m: cheb(m.x, m.y, p.x, p.y))
            if not beasts:
                self.msg("A low song rolls out of the shell, but no beast is there to hear it.", "gray")
            elif len(self.pets()) >= self.ally_cap():
                self.msg(f"The song reaches {self.the(beasts[0])}, but you can't look after another companion.", "gray")
            else:
                self.befriend(beasts[0])
        elif key == "tide":
            if self.heart_taken:
                self.msg("The sea is pouring in. No shell will turn this tide.", "gray")
            else:
                self.turn_tide()
        elif key == "warding":
            pushed = 0
            for m in list(lv.monsters):
                if m.team == "hostile" and cheb(m.x, m.y, p.x, p.y) <= 1:
                    dx, dy = sign(m.x - p.x), sign(m.y - p.y)
                    for _ in range(2):
                        if self.allowed(self.move_class(m), m.x + dx, m.y + dy) and not self.occupied(m.x + dx, m.y + dy):
                            m.x, m.y = m.x + dx, m.y + dy
                    m.status["fear"] = 8
                    pushed += 1
            self.msg("A ring of force bursts out of the shell!" + (" Everything near you is hurled back."
                                                                 if pushed else ""), "cyan")
        elif key == "echo":
            self.msg("A deafening roar rolls out of the shell and echoes through the depth!", "red")
            self.noise(p.x, p.y, 40)
        elif key == "swarm":
            kinds = [k for k, v in MONSTERS.items() if v["freq"] and v["depth"][0] <= lv.depth <= v["depth"][1]
                     and self.move_class_of(k) in ("walk", "swim", "fly")]
            n = 0
            for dx, dy in rng.sample(DIRS8, 8):
                x, y = p.x + dx, p.y + dy
                if n < 3 and lv.t(x, y) in WALKABLE and not self.occupied(x, y):
                    m = self.spawn(rng.choice(kinds), x, y, "hunt")
                    m.target = (p.x, p.y)
                    n += 1
            self.msg("The shell shrieks — and things answer!", "red")
        elif key == "honing":
            target = p.weapon or p.armor
            if target:
                target.ench += 1
                self.msg(f"Your {self.names(target)[0]} gleams.", "cyan")
            else:
                self.msg("The shell crumbles, with nothing to hone.", "gray")
        elif key == "cleansing":
            cursed = [c for c in p.charms if c.key in CURSED]
            for c in cursed:
                p.charms.remove(c)
                p.inv.remove(c)
            self.msg("Your cursed charm crumbles to dust." if cursed else "A clean light washes over you.", "cyan")
        self.msg(f"(It was a shell of {name}.)", "gray")
        return 100

    def move_class_of(self, kind):
        f = MONSTERS[kind]["flags"]
        return "fly" if "fly" in f else "deep" if "deep" in f else "aquatic" if "aquatic" in f else (
            "swim" if "swim" in f else "walk")

    def shop_name(self, it):
        """What the hermit calls it: hermits know their wares."""
        had = it.key in self.known
        self.known.add(it.key)
        name = self.item_name(it)
        if not had:
            self.known.discard(it.key)
        return name

    def unknown(self, it):
        return it.kind in ("vial", "shell", "rod", "charm") and it.key not in self.known

    def identify_item(self, it):
        self.identify(it.key, quiet=True)
        self.msg(f"It is {self.item_name(it)}: {self.describe_item(it)}", "cyan")

    def tame(self, dx, dy):
        p = self.player
        m = self.monster_at(p.x + dx, p.y + dy)
        if not m or m.team != "hostile" or not self.player_sees(m):
            self.msg("There's nothing there to tame.", "gray")
            return 0
        if not m.has("beast"):
            self.msg(f"{self.the(m, True)} is no beast. It won't be tamed.", "gray")
            return 0
        if len(self.pets()) >= self.ally_cap():
            self.msg(f"You can't look after more companions (you can keep {self.ally_cap()}).", "gray")
            return 0
        chance = 0.08 + 0.07 * self.level("kinship") + 0.5 * (1 - m.hp / m.maxhp)
        chance += {"eat": 0.45, "sleep": 0.3}.get(m.state, 0)
        bait = next((it for it in p.inv if it.kind == "bait"), None)
        if bait:
            self.split_one(bait)
            chance += 0.2
            self.msg(f"You hold out a sweetfish to {self.the(m)}.", "gray")
        self.gain("kinship", 1)
        if rng.random() < chance:
            self.befriend(m)
            self.gain("kinship", 5)
        else:
            m.state, m.target = "hunt", (p.x, p.y)
            self.msg(f"{self.the(m, True)} snaps at your hand and won't be calmed.", "orange")
            self.noise(p.x, p.y, 3)
        return 100

    def befriend(self, m, turns=0):
        p = self.player
        m.team, m.state, m.foe, m.target = "ally", "ally", None, None
        m.status.pop("slumber", None)
        m.status.pop("fear", None)
        m.charm_turns = turns
        if turns:
            self.msg(f"{self.the(m, True)} turns to fight at your side — for now.", "cyan")
            return
        free = [n for n in PET_NAMES if n not in self.pets_used] or list(PET_NAMES)
        m.pet = rng.choice(free)
        self.pets_used.add(m.pet)
        if "kin" in p.pacts:
            m.maxhp = m.maxhp * 3 // 2
        m.hp = max(m.hp, m.maxhp // 2)
        self.msg(f"{self.the(m, True)} settles down and comes to your side. You call it {m.pet}.", "lime")
        for other in self.lv.monsters:
            if other.foe is m:
                other.foe = None

    def activate(self, key, target=None):
        p, lv = self.player, self.lv
        if key not in p.relics or not RELICS[key][2]:
            return 0
        if p.cooldown.get(key):
            self.msg(f"The {RELICS[key][0]} isn't ready yet ({p.cooldown[key]} turns).", "gray")
            return 0
        if key == "clock":
            if self.heart_taken:
                self.msg("The Tide Clock's hands spin uselessly. The sea is coming in.", "gray")
                return 0
            self.turn_tide()
        elif key == "bell":
            n = 0
            for m in lv.monsters:
                if m.team == "hostile" and cheb(m.x, m.y, p.x, p.y) <= 4:
                    m.status["stun"] = 1 if m.has("boss") else 4
                    n += 1
            self.msg("The Bell of Saint Vell peals! Everything near you reels." if n
                     else "The Bell of Saint Vell peals through the water.", "gold")
            self.noise(p.x, p.y, 15)
        elif key == "ink":
            self.cloud(p.x, p.y, 2, "ink", 15)
            self.msg("A cloud of ink pours out around you.", "purple")
        elif key == "whistle":
            pets = self.pets()
            if not pets:
                self.msg("You whistle, but no companion answers.", "gray")
            for a in pets:
                spots = [(p.x + dx, p.y + dy) for dx, dy in DIRS8
                         if self.allowed(self.move_class(a), p.x + dx, p.y + dy) and not self.occupied(p.x + dx, p.y + dy)]
                if spots:
                    a.x, a.y = rng.choice(spots)
            if pets:
                self.msg("You whistle, and your companions come running.", "cyan")
        elif key == "conch":
            m = self.monster_at(*target) if target else None
            if not m or m.team != "hostile" or not self.player_sees(m) or m.has("boss"):
                self.msg("The conch needs a creature you can see — and the King won't listen.", "gray")
                return 0
            self.befriend(m, turns=40)
        p.cooldown[key] = RELICS[key][2]
        return 100

    def take_pact(self, key, pos):
        p, lv = self.player, self.lv
        name, boon, bane = PACTS[key]
        p.pacts.append(key)
        if key in self.pact_pool:
            self.pact_pool.remove(key)
        lv.put(*pos, SPENT)
        lv.shrines.pop(pos, None)
        self.msg(f"You make the pact of {name}. {boon} {bane}", "magenta")
        if key == "kin" and p.armor and ARMOURS[p.armor.key][2] >= 2:
            self.msg(f"Your {self.names(p.armor)[0]} slides off you.", "orange")
            p.armor = None
        p.hp = min(p.hp, self.maxhp())
        return 100

    def buy(self, m, i):
        p = self.player
        it, price = m.wares[i]
        if p.pearls < price:
            self.msg(f"“That's {price} pearls. You have {p.pearls}.”", "gray")
            return False
        if len(p.inv) >= MAX_INVENTORY and it.kind not in STACKABLE:
            self.msg("Your pack is full.", "orange")
            return False
        if it.kind in ("vial", "shell", "rod", "charm"):
            self.identify(it.key, quiet=True)
        p.pearls -= price
        m.wares.pop(i)
        if it.kind == "oil":
            p.oil = min(OIL_MAX, p.oil + OIL_FLASK)
            self.msg("The hermit fills your lantern.", "yellow")
        else:
            self.take(it)
        return True

    def describe(self, x, y):
        """What you know about a tile, for looking."""
        lv, p = self.lv, self.player
        i = y * W + x
        if not lv.seen[i]:
            return "You haven't seen that."
        vis = (x, y) in self.visible
        parts = []
        if (x, y) == (p.x, p.y):
            parts.append("You")
        m = self.monster_at(x, y)
        if vis and m and self.player_sees(m):
            if m.team == "ally":
                parts.append(f"{m.pet or self.the(m, True)}, your companion ({m.hp}/{m.maxhp})"
                             + (f", fighting for you {m.charm_turns} more turns" if m.charm_turns else ""))
            else:
                state = {"sleep": "asleep", "wander": "unaware of you", "search": "searching",
                         "hunt": "hunting you", "eat": "eating", "ambush": "lying in wait", "flee": "fleeing",
                         "idle": "trading"}.get(m.state, m.state)
                parts.append(f"{self.a(m).capitalize()} — {state}. {m.d['desc']}")
        items = lv.items.get((x, y)) if vis else None
        if items:
            parts.append("On the ground: " + ", ".join(self.item_name(it) for it in items))
        elif not vis and (x, y) in lv.mem_items:
            parts.append("You remember something lying here")
        if vis and (x, y) in self.lures:
            parts.append("A little light dances over the water")
        t = lv.t(x, y)
        w = lv.wat(x, y) if vis else lv.mem_water[i]
        if (x, y) in lv.ice and vis:
            ground = "ice"
        elif t in (FLOOR, OPEN) and w:
            ground = "deep water" if w == 2 else "shallow water"
        else:
            ground = TILES[t][2] + (" in deep water" if w == 2 and t == KELP else "")
        if vis:
            cl = lv.clouds.get((x, y))
            if cl:
                ground += ", in a cloud of " + ("ink" if cl[0] == "ink" else "rot-gas")
            ground += ", lit" if (x, y) in self.lit else ", in darkness"
        else:
            ground += " (remembered)"
        parts.append(ground[0].upper() + ground[1:])
        return ". ".join(parts) + "."

    # -- getting about without pressing every key --

    def path_step(self, goal_fn, deep_cost):
        """The first step on the cheapest known path to a tile `goal_fn` likes (Dijkstra over
        what you have seen); None if there's none. Deep water costs `deep_cost`, or is avoided."""
        lv, p = self.lv, self.player
        start = (p.x, p.y)
        best, parent = {start: 0}, {}
        heap = [(0, start)]
        while heap:
            c, q = heapq.heappop(heap)
            if c > best[q]:
                continue
            if q != start and goal_fn(q):
                while parent[q] != start:
                    q = parent[q]
                return q[0] - p.x, q[1] - p.y
            for dx, dy in DIRS8:
                nx, ny = q[0] + dx, q[1] + dy
                if not inside(nx, ny):
                    continue
                i = ny * W + nx
                t = lv.tiles[i]
                if not lv.seen[i] or (t not in WALKABLE and t != DOOR):
                    continue
                w = lv.mem_water[i]
                step = 2 if w == 1 else 1
                if w == 2:
                    if deep_cost is None:
                        continue
                    step = deep_cost
                if t == SHRINE and not goal_fn((nx, ny)):
                    step += 20
                m = self.monster_at(nx, ny)
                if m and m.team == "hostile" and (nx, ny) in self.visible:
                    continue
                nc = c + step
                if nc < best.get((nx, ny), 1e9):
                    best[(nx, ny)] = nc
                    parent[(nx, ny)] = q
                    heapq.heappush(heap, (nc, (nx, ny)))
        return None

    def threats(self):
        return [m for m in self.lv.monsters if m.team == "hostile" and m.state == "hunt" and self.player_sees(m)]

    def explore_step(self):
        lv, p = self.lv, self.player
        if self.threats():
            return None, "Not while something is hunting you."
        pos = (p.x, p.y)
        if lv.items.get(pos) and pos not in lv.ignored:
            return ("pickup",), None

        def goal(q):
            if q in lv.mem_items and q not in lv.ignored:
                return True
            x, y = q
            t = lv.tiles[y * W + x]
            return (t in WALKABLE or t == DOOR) and any(
                inside(x + dx, y + dy) and not lv.seen[(y + dy) * W + x + dx] for dx, dy in DIRS8)

        step = self.path_step(goal, None)
        if step is None and not self.submerged():
            if lv.stairs and lv.seen[lv.stairs[1] * W + lv.stairs[0]]:
                return None, "Explored all you can reach on foot. Press > to travel to the stairs."
            if self.weight() >= 3 and not self.breathes_water():
                return None, "The rest is under deep water, and your armour would drag you down."
            if p.breath < self.max_breath():
                return None, "The way on is under water. Get your breath back first."
        if step is None:
            step = self.path_step(goal, 5)      # the only way on is a swim
            if step is None:
                return None, "There's nothing left to explore."
        return ("move",) + step, None

    def travel_step(self, goal):
        if self.threats():
            return None, "Not while something is hunting you."
        p = self.player
        if (p.x, p.y) == goal:
            return None, None
        step = self.path_step(lambda q: q == goal, 12)
        if step is None:
            return None, "You don't know a way there."
        return ("move",) + step, None

    # -- the end --

    def record(self):
        p = self.player
        won = self.over == "won"
        self.score = (p.depth_max * 100 + p.pearls * 3 + p.kills * 5 + len(p.relics) * 25 + (1000 if won else 0))
        entry = {"date": time.strftime("%Y-%m-%d %H:%M"), "won": won, "depth": p.depth_max, "turns": self.turn,
                 "pearls": p.pearls, "kills": p.kills, "score": self.score, "cause": self.cause,
                 "relics": [RELICS[k][0] for k in p.relics], "pacts": [PACTS[k][0] for k in p.pacts],
                 "allies": [a.pet for a in self.pets()], "seed": self.seed}
        records = load_records()
        records.append(entry)
        records.sort(key=lambda r: -r["score"])
        write_json(RECORDS_PATH, records)
        try:
            os.unlink(SAVE_PATH)
        except OSError:
            pass


# -- saving --

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_records():
    try:
        with open(RECORDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_game(game):
    if game.over:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    game.rng_state = rng.getstate()
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(game, f)
        os.replace(tmp, SAVE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_game():
    """The saved run, or None. It stays on disk until the run ends, so a crash doesn't lose it."""
    try:
        with open(SAVE_PATH, "rb") as f:
            game = pickle.load(f)
    except (OSError, pickle.UnpicklingError, AttributeError, EOFError, ImportError):
        return None
    if game.rng_state:
        rng.setstate(game.rng_state)
    return game


# -- the screen --

MOVES = {ord("h"): (-1, 0), ord("j"): (0, 1), ord("k"): (0, -1), ord("l"): (1, 0),
         ord("y"): (-1, -1), ord("u"): (1, -1), ord("b"): (-1, 1), ord("n"): (1, 1),
         curses.KEY_LEFT: (-1, 0), curses.KEY_RIGHT: (1, 0), curses.KEY_UP: (0, -1), curses.KEY_DOWN: (0, 1),
         curses.KEY_HOME: (-1, -1), curses.KEY_PPAGE: (1, -1), curses.KEY_END: (-1, 1), curses.KEY_NPAGE: (1, 1),
         curses.KEY_A1: (-1, -1), curses.KEY_A3: (1, -1), curses.KEY_C1: (-1, 1), curses.KEY_C3: (1, 1),
         ord("1"): (-1, 1), ord("2"): (0, 1), ord("3"): (1, 1), ord("4"): (-1, 0), ord("6"): (1, 0),
         ord("7"): (-1, -1), ord("8"): (0, -1), ord("9"): (1, -1)}
RUNS = {ord(c.upper()): MOVES[ord(c)] for c in "hjklyubn"}
STATE_WORDS = {"sleep": "asleep", "wander": "unaware", "search": "searching", "hunt": "hunting", "eat": "eating",
               "ambush": "lurking", "flee": "fleeing", "idle": "trader"}
STATUS_WORDS = {"haste": "fast", "gills": "gills", "nightsight": "nightsight", "glow": "glowing",
                "confused": "confused", "poison": "poisoned", "paralyzed": "numb", "held": "held", "slow": "slowed"}

INTRO = """\
The city of Vell went under the sea in a single night, and its king went down with it,
holding the Tide-Heart: the thing that kept the sea out.

You have come to take it back. Twelve depths below the docks, in the Drowned Palace, the
Tide-Heart lies on its altar, and the Drowned King sleeps beside it. Take it, and get back
to the current where you came into the palace before the sea fills it.

How to stay alive down there:

  · Light gives you away. Your lantern (f) shows the way, and shows you to everything.
    Shuttered, you see only what other lights show. Street lamps can be doused.
  · Noise draws them. Wading through shallows is loud; sneaking (s) is quiet.
  · Anything that hasn't noticed you takes several times the damage from your blade.
  · The tide rises and falls. Deep water (≈) hides you and costs breath.
  · Vials, shells and rods are unknown until you try them.
  · Beasts can be tamed (T) — weakened, asleep, or busy with a sweetfish.

Press ? at any time for the keys. Press any key to begin."""

HELP = """\
Moving         h j k l y u b n, the arrow keys or the number pad. Capitals run.
               Walk into a door to open it, into a lamp to douse or light it,
               into a creature to attack it, into a companion to swap places.
  o            explore automatically (stops when something shows up)
  >            go down the stairs, or travel to them once you've seen them
  .  5         wait a turn          R   rest until healed
  s            sneak: half the noise, but slower
  f            shutter or open your lantern

Things         g ,  pick up          i   your pack (choose an item to use it)
  q  drink a vial or pour oil        r   read a shell
  z  zap a rod                       t   throw something
  e  wield, wear, put on or take off d   drop
  a  activate a relic

Creatures      T  tame a beast next to you (sweetfish help)
               c  tell companions to follow you or hold their ground
               x  look around (Tab steps through creatures)

Other          m  messages    @  you, your skills and discoveries
               S  save and quit (the run continues next time)
               Q  give up the run

The screen     @ you   ~ shallow water   ≈ deep water   " kelp   § glowing coral
               ¶ street lamp   Ω statue   + door   > stairs down   _ shrine
               ! vial   ? shell   / rod   ) weapon   [ armour   = charm
               ♦ relic   : sweetfish   ¡ lamp oil   ° pearls   * a light over the water
               Underlined creatures are your companions.

How it works   · Monsters see you from far away when you stand in light, and only
                 close up in darkness. Kelp hides you completely; so does deep
                 water, from anything that can't swim.
               · Noise wakes sleepers and draws the curious: walking 4, wading 7,
                 fighting 6. Sneaking halves it; your stealth skill lowers it.
               · Striking a creature that isn't hunting you does several times the
                 damage — knives and dirks the most.
               · Each depth's tide moves every ~60 turns. Deep water costs breath;
                 heavy armour drags you under.
               · Skills (melee, stealth, swimming, aim, kinship) grow as you use them.
               · Each new depth gives you 4 more maximum health.
               · Shrines offer pacts: a gift with a price. Hermits sell for pearls.
               · Unknown items: try them, throw vials at enemies, or read lore.
               · Taming works best on a beast that is hurt, asleep or eating."""


class UI:
    def __init__(self, stdscr, game):
        self.s, self.g = stdscr, game
        self.pal = {}
        colours = curses.has_colors()
        rich = colours and curses.COLORS >= 256
        for n, (name, (c256, c8, bold)) in enumerate(PALETTE.items(), 1):
            attr = 0
            if colours:
                curses.init_pair(n, c256 if rich else c8, -1)
                attr = curses.color_pair(n)
            if not rich and bold:
                attr |= curses.A_BOLD
            self.pal[name] = attr
        self.mark = 0
        self.last_target = None

    def c(self, name):
        return self.pal.get(name, 0)

    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        text = text[:max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.s.addstr(y, x, text, attr)
        except curses.error:
            pass

    def layout(self):
        h, w = self.s.getmaxyx()
        side = 32 if w >= 116 else 28 if w >= 100 else 25
        return h, w, side, w - side - 1, h - LOG_LINES - 1

    # -- drawing --

    def draw(self, cursor=None, path=(), note=None):
        s = self.s
        s.erase()
        h, w, side, vw, vh = self.layout()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"Sunken needs a window of at least {MIN_W}×{MIN_H} (this one is {w}×{h}).", self.c("red"))
            s.refresh()
            return
        g, lv, p = self.g, self.g.lv, self.g.player
        ox = -((vw - W) // 2) if vw >= W else max(0, min(W - vw, p.x - vw // 2))
        oy = -((vh - H) // 2) if vh >= H else max(0, min(H - vh, p.y - vh // 2))
        mons = {(m.x, m.y): m for m in lv.monsters}
        trail = set(path)
        for sy in range(vh):
            y = sy + oy
            if not 0 <= y < H:
                continue
            run, run_attr, run_x = [], None, 0
            for sx in range(vw):
                x = sx + ox
                if not 0 <= x < W:
                    continue
                ch, attr = self.cell(x, y, mons)
                if (x, y) in trail and (x, y) != cursor:
                    attr = self.c("yellow")
                    ch = "·" if ch in ". ~≈" else ch
                if (x, y) == cursor:
                    attr |= curses.A_REVERSE
                if attr != run_attr:
                    if run:
                        self.put(sy, run_x, "".join(run), run_attr)
                    run, run_attr, run_x = [], attr, sx
                run.append(ch)
            if run:
                self.put(sy, run_x, "".join(run), run_attr)
        self.put(vh, 0, "─" * (w - 1), self.c("dark"))
        self.draw_side(vw + 1, side, vh)
        self.draw_log(vh + 1, w, note)
        s.refresh()

    def terrain(self, x, y, water):
        lv = self.g.lv
        t = lv.t(x, y)
        if t == FLOOR:
            if (x, y) in lv.ice:
                return "▒", "ice"
            if water == 2:
                return "≈", "deep"
            if water == 1:
                return "~", "shallow"
            if (x, y) in lv.glow:
                return ".", "cyan"
            return ".", FLOOR_COLOUR[lv.theme]
        if t == KELP:
            return '"', "teal" if water == 2 else "green"
        glyph, colour, _ = TILES[t]
        return glyph, colour

    def cell(self, x, y, mons):
        g, lv, p = self.g, self.g.lv, self.g.player
        i = y * W + x
        pos = (x, y)
        if pos not in g.visible:
            if not lv.seen[i]:
                return " ", 0
            if pos in lv.mem_items:
                return lv.mem_items[pos][0], self.c("dark")
            return self.terrain(x, y, lv.mem_water[i])[0], self.c("dark")
        dim = 0 if pos in g.lit else curses.A_DIM
        if pos == (p.x, p.y):
            colour = "shallow" if g.submerged() else "white" if pos in g.lit else "pale"
            return "@", self.c(colour) | curses.A_BOLD
        m = mons.get(pos)
        if m and g.player_sees(m):
            attr = self.c(m.d["colour"])
            if m.team == "ally":
                attr |= curses.A_UNDERLINE | curses.A_BOLD
            return m.d["glyph"], attr
        cl = lv.clouds.get(pos)
        if cl:
            return "░", self.c("purple" if cl[0] == "ink" else "lime")
        items = lv.items.get(pos)
        if items:
            glyph, colour = g.item_glyph(items[-1])
            return glyph, self.c(colour) | dim
        if pos in g.lures:
            return "*", self.c("yellow") | curses.A_BOLD
        glyph, colour = self.terrain(x, y, lv.wat(x, y))
        return glyph, self.c(colour) | dim

    def bar(self, label, value, top, n=10):
        filled = 0 if top <= 0 else max(0, min(n, round(n * value / top)))
        return f"{label:<7}{'█' * filled}{'░' * (n - filled)}"

    def draw_side(self, x0, width, height):
        g, p, lv = self.g, self.g.player, self.g.lv
        rows = []

        def add(*segs):
            rows.append(segs or (("", 0),))

        add(("SUNKEN", self.c("gold") | curses.A_BOLD), (f"   turn {g.turn}", self.c("gray")))
        add((f"Depth {lv.depth} · {lv.name}", self.c("cyan")))
        add()
        mh = g.maxhp()
        add((self.bar("Health", p.hp, mh), self.c("red" if p.hp * 3 < mh else "lime")), (f" {p.hp}/{mh}", 0))
        if g.submerged() or p.breath < g.max_breath():
            low = p.breath * 3 < g.max_breath()
            add((self.bar("Breath", p.breath, g.max_breath()), self.c("orange" if low else "blue")),
                (f" {p.breath}", 0))
        if "wick" in p.relics:
            lamp = "everburning" + ("" if p.lantern else ", shut")
        elif p.oil <= 0:
            lamp = "out of oil"
        elif not p.lantern:
            lamp = "shuttered"
        else:
            lamp = "dark (under water)" if g.submerged() else "lit"
        add((self.bar("Oil", p.oil, OIL_MAX), self.c("yellow")), (f" {p.oil}", 0))
        add((f"Lantern {lamp}", self.c("yellow" if g.lantern_lit() else "gray")))
        if g.heart_taken:
            add(("Tide   THE SEA IS COMING IN", self.c("red") | curses.A_BOLD))
        else:
            nxt = TIDES[(lv.tide_i + 1) % len(TIDES)]
            way = "rising" if TIDE_SHIFT[nxt] > TIDE_SHIFT[lv.tide] else "falling"
            add((f"Tide   {lv.tide}, {way} in {lv.tide_clock}", self.c("blue")))
        watchers = g.seen_by()
        lit = (p.x, p.y) in g.lit
        if watchers:
            add((f"SEEN by {len(watchers)}", self.c("red") | curses.A_BOLD),
                (f" · {'in light' if lit else 'in shadow'}", self.c("gray")))
        else:
            hidden = "hidden in kelp" if lv.t(p.x, p.y) == KELP else "under water" if g.submerged() else (
                "in light" if lit else "in shadow")
            add(("unseen", self.c("lime")), (f" · {hidden}", self.c("yellow" if lit else "gray")))
        if p.sneaking:
            add(("sneaking", self.c("teal")))
        effects = [f"{STATUS_WORDS[k]} {v}" for k, v in p.status.items() if k in STATUS_WORDS]
        if effects:
            add((", ".join(effects), self.c("magenta")))
        add()
        add(("Weapon ", self.c("gray")), (g.names(p.weapon)[0] if p.weapon else "fists", self.c("pale")))
        add(("Armour ", self.c("gray")), (g.names(p.armor)[0] if p.armor else "none", self.c("pale")),
            (f" [{g.armour()}]", self.c("gray")))
        for ch in p.charms:
            add(("Charm  ", self.c("gray")), (g.names(ch)[0], self.c("gold")))
        if p.relics:
            add(("Relics", self.c("gray")))
            for k in p.relics:
                cd = RELICS[k][2]
                tail = "" if not cd else f" ({p.cooldown[k]})" if p.cooldown.get(k) else " ready"
                add((" ♦ " + RELICS[k][0], self.c("gold")), (tail, self.c("gray")))
        if p.pacts:
            add(("Pacts ", self.c("gray")), (", ".join(PACTS[k][0] for k in p.pacts), self.c("magenta")))
        pets = [m for m in lv.monsters if m.team == "ally"]
        if pets:
            add(("Companions", self.c("gray")))
            for a in pets:
                what = f"{a.charm_turns} turns" if a.charm_turns else a.order
                add((f" {a.d['glyph']} ", self.c(a.d["colour"]) | curses.A_UNDERLINE),
                    (f"{a.pet or a.d['name']} {a.hp}/{a.maxhp} ", 0), (what, self.c("gray")))
        seen = sorted((m for m in lv.monsters if m.team != "ally" and g.player_sees(m)),
                      key=lambda m: cheb(m.x, m.y, p.x, p.y))
        if seen:
            add()
            for m in seen:
                state = STATE_WORDS.get(m.state, m.state)
                hurt = "" if m.hp >= m.maxhp else f" {round(100 * m.hp / m.maxhp)}%"
                add((f" {m.d['glyph']} ", self.c(m.d["colour"])), (m.d["name"], 0),
                    (f" {state}{hurt}", self.c("red" if m.state == "hunt" else "gray")))
        for y, segs in enumerate(rows[:height - 1]):
            x = x0
            for text, attr in segs:
                room = width - (x - x0)
                if room <= 0:
                    break
                self.put(y, x, text[:room], attr)
                x += len(text[:room])
        self.put(height - 1, x0, f"Pearls {p.pearls}", self.c("white"))
        self.put(height - 1, x0 + width - 7, "? help", self.c("gray"))

    def draw_log(self, y0, w, note=None):
        """The latest messages, newest at the bottom; this turn's in colour, older ones grey.
        A note (a prompt, or what the cursor is on) takes the bottom lines."""
        lines = [(t, self.c("cyan")) for t in textwrap.wrap(note, w - 2)][:LOG_LINES] if note else []
        room = LOG_LINES - len(lines)
        older = []
        for idx in range(len(self.g.log) - 1, -1, -1):
            if len(older) >= room:
                break
            _, text, colour, count = self.g.log[idx]
            if count > 1:
                text += f" (×{count})"
            attr = self.c(colour) if idx >= self.mark else self.c("gray")
            older[0:0] = [(part, attr) for part in textwrap.wrap(text, w - 2) or [""]]
        lines = (older[-room:] if room > 0 else []) + lines
        for i, (text, attr) in enumerate(lines):
            self.put(y0 + i, 1, text, attr)

    # -- menus and screens --

    def menu(self, title, rows, footer="Esc to close"):
        """rows: [(key or None, text, attr)]. Returns the key chosen, or None."""
        keys = [r[0] for r in rows if r[0]]
        picks = [i for i, r in enumerate(rows) if r[0]]
        sel = 0
        top = 0
        while True:
            self.draw()
            h, w = self.s.getmaxyx()
            bw = min(w - 2, max([len(title) + 6, len(footer) + 4] + [len(t) + 8 for _, t, _ in rows]))
            bh = min(h - 2, len(rows) + 4)
            y0, x0 = (h - bh) // 2, (w - bw) // 2
            body = bh - 4
            if picks:
                cur = picks[sel]
                if cur < top:
                    top = cur
                elif cur >= top + body:
                    top = cur - body + 1
            box = self.c("pale")
            self.put(y0, x0, "┌" + "─" * (bw - 2) + "┐", box)
            for y in range(y0 + 1, y0 + bh - 1):
                self.put(y, x0, "│" + " " * (bw - 2) + "│", box)
            self.put(y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", box)
            self.put(y0, x0 + 2, f" {title} ", self.c("gold") | curses.A_BOLD)
            for n, (key, text, attr) in enumerate(rows[top:top + body]):
                line = f"{key}) {text}" if key else text
                if picks and top + n == picks[sel]:
                    attr |= curses.A_REVERSE
                self.put(y0 + 2 + n, x0 + 2, line[:bw - 4], attr)
            self.put(y0 + bh - 2, x0 + 2, footer[:bw - 4], self.c("gray"))
            ch = self.s.getch()
            if ch == 27:
                return None
            if ch == curses.KEY_UP and picks:
                sel = (sel - 1) % len(picks)
            elif ch == curses.KEY_DOWN and picks:
                sel = (sel + 1) % len(picks)
            elif ch in (10, 13, curses.KEY_ENTER) and picks:
                return rows[picks[sel]][0]
            elif 0 < ch < 256 and chr(ch) in keys:
                return chr(ch)
            elif ch == ord(" ") and not picks:
                return None

    def pager(self, title, lines, at_end=False):
        """lines: [(text, attr)]; scrolls, any other key closes."""
        top = None
        while True:
            self.s.erase()
            h, w = self.s.getmaxyx()
            body = h - 3
            if top is None:
                top = max(0, len(lines) - body) if at_end else 0
            top = max(0, min(top, max(0, len(lines) - body)))
            self.put(0, 1, title, self.c("gold") | curses.A_BOLD)
            for n, (text, attr) in enumerate(lines[top:top + body]):
                self.put(1 + n, 2, text[:w - 3], attr)
            more = "  ↑↓ to scroll" if len(lines) > body else ""
            self.put(h - 1, 1, f"Any key to close{more}", self.c("gray"))
            self.s.refresh()
            ch = self.s.getch()
            if ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch == curses.KEY_PPAGE:
                top -= body
            elif ch in (curses.KEY_NPAGE, ord(" ")) and top + body < len(lines):
                top += body
            elif ch != curses.KEY_RESIZE:
                return

    def wrap(self, text, attr=0, indent=""):
        h, w = self.s.getmaxyx()
        return [(indent + t, attr) for t in textwrap.wrap(text, max(20, min(w - 6, 90) - len(indent)))] or [("", attr)]

    def intro(self):
        lines = [(t, self.c("pale")) for t in INTRO.split("\n")]
        self.s.erase()
        h, w = self.s.getmaxyx()
        top = max(0, (h - len(lines) - 2) // 2)
        self.put(top, 2, "SUNKEN", self.c("gold") | curses.A_BOLD)
        for n, (text, attr) in enumerate(lines):
            self.put(top + 2 + n, 2, text, attr)
        self.s.refresh()
        self.s.getch()

    def help(self):
        self.pager("Sunken — keys and how it works", [(t, self.c("pale")) for t in HELP.split("\n")])

    def messages(self):
        lines = []
        for turn, text, colour, count in self.g.log:
            lines.append((f"{turn:>5}  {text}" + (f" (×{count})" if count > 1 else ""), self.c(colour)))
        self.pager("Messages", lines, at_end=True)

    def character(self):
        g, p = self.g, self.g.player
        c = self.c
        lines = [(f"Health {p.hp}/{g.maxhp()} · armour {g.armour()} · breath {g.max_breath()} · "
                  f"lantern reach {g.light_radius()} · pearls {p.pearls} · kills {p.kills}", c("white")), ("", 0),
                 ("Skills — they grow as you use them", c("gold"))]
        what = {"melee": "from hitting things: accuracy and damage",
                "stealth": "from moving unseen: quieter steps",
                "swimming": "from deep water: faster strokes, more breath",
                "aim": "from throws and rods: accuracy and damage",
                "kinship": "from taming: easier taming, more companions"}
        for s in SKILLS:
            lvl = g.level(s)
            xp = p.xp[s]
            nxt = SKILL_STEPS[lvl + 1] if lvl + 1 < len(SKILL_STEPS) else None
            prog = f"{int(xp)}/{nxt}" if nxt else "mastered"
            lines.append((f"  {s:<9} {lvl}  {'■' * lvl}{'·' * (9 - lvl)}  {prog:<9} {what[s]}", c("pale")))
        lines.append((f"  You can keep {g.ally_cap()} companion{'s' if g.ally_cap() != 1 else ''}.", c("gray")))
        if p.relics:
            lines += [("", 0), ("Relics", c("gold"))]
            for k in p.relics:
                lines += self.wrap(f"{RELICS[k][0]}: {RELICS[k][1]}", c("pale"), "  ")
        if p.pacts:
            lines += [("", 0), ("Pacts", c("magenta"))]
            for k in p.pacts:
                name, boon, bane = PACTS[k]
                lines += self.wrap(f"{name}: {boon} {bane}", c("pale"), "  ")
        lines += [("", 0), ("Discoveries", c("gold"))]
        for kind, table in (("vial", VIALS), ("shell", SHELLS), ("rod", RODS), ("charm", CHARMS)):
            known = [k for k in table if k in g.known]
            names = ", ".join(table[k][0] for k in known) if known else "none yet"
            lines += self.wrap(f"{kind}s ({len(known)}/{len(table)}): {names}", c("pale"), "  ")
        self.pager("You", lines)

    def records_lines(self, highlight=None):
        lines = [("Hall of records", self.c("gold") | curses.A_BOLD)]
        for i, r in enumerate(load_records()[:10], 1):
            mark = "ESCAPED" if r["won"] else f"depth {r['depth']}"
            text = f"{i:>2}. {r['score']:>5}  {mark:<8}  {r['date']}  {r['cause']}"
            attr = self.c("lime") if r["won"] else self.c("pale")
            if highlight and r.get("seed") == highlight.seed and r.get("turns") == highlight.turn:
                attr |= curses.A_BOLD | curses.A_REVERSE
            lines.append((text, attr))
        return lines

    def end_screen(self):
        g, p = self.g, self.g.player
        won = g.over == "won"
        verdict = "You escaped the Drowned Palace with the Tide-Heart!" if won else (
            "You gave up the run." if g.cause.startswith("gave up") else "You died.")
        lines = [(verdict,
                  self.c("gold" if won else "red") | curses.A_BOLD),
                 (g.cause[0].upper() + g.cause[1:] + ".", self.c("pale")), ("", 0),
                 (f"Score {g.score} · deepest depth {p.depth_max} · {g.turn} turns · {p.kills} kills · "
                  f"{p.pearls} pearls", self.c("white"))]
        if p.relics:
            lines += self.wrap("Relics: " + ", ".join(RELICS[k][0] for k in p.relics), self.c("gold"))
        if p.pacts:
            lines += self.wrap("Pacts: " + ", ".join(PACTS[k][0] for k in p.pacts), self.c("magenta"))
        skills = ", ".join(f"{s} {g.level(s)}" for s in SKILLS)
        lines += self.wrap("Skills: " + skills, self.c("pale"))
        lines += [("", 0)] + self.records_lines(g) + [("", 0), ("n  a new run      q  quit", self.c("cyan"))]
        while True:
            self.s.erase()
            for n, (text, attr) in enumerate(lines):
                self.put(1 + n, 2, text, attr)
            self.s.refresh()
            ch = self.s.getch()
            if ch in (ord("n"), ord("N")):
                return "new"
            if ch in (ord("q"), ord("Q"), 27):
                return "exit"

    def confirm(self, question):
        self.draw(note=question + "  (y/n)")
        return self.s.getch() in (ord("y"), ord("Y"))

    def direction(self, prompt):
        self.draw(note=prompt + " — which direction? (Esc to cancel)")
        return MOVES.get(self.s.getch())

    def target(self, prompt, reach=THROW_RANGE, look=False):
        g, p, lv = self.g, self.g.player, self.g.lv
        cands = sorted((m for m in lv.monsters if m.team == "hostile" and g.player_sees(m)),
                       key=lambda m: cheb(m.x, m.y, p.x, p.y))
        if self.last_target in cands:
            cands.remove(self.last_target)
            cands.insert(0, self.last_target)
        cur = (cands[0].x, cands[0].y) if cands else (p.x, p.y)
        ci = 0
        while True:
            pts = [] if look or cur == (p.x, p.y) else g.projectile(cur[0], cur[1], reach)
            keys = "move the cursor · Tab next creature · Esc to stop" if look else \
                "move the cursor · Tab next creature · Enter to confirm · Esc to cancel"
            self.draw(cursor=cur, path=pts, note=f"{prompt}: {g.describe(*cur)}  [{keys}]")
            ch = self.s.getch()
            if ch in MOVES or ch in RUNS:
                dx, dy = MOVES.get(ch) or RUNS[ch]
                k = 1 if ch in MOVES else 5
                cur = (max(0, min(W - 1, cur[0] + dx * k)), max(0, min(H - 1, cur[1] + dy * k)))
            elif ch == 9 and cands:
                ci = (ci + 1) % len(cands)
                cur = (cands[ci].x, cands[ci].y)
            elif ch in (10, 13, curses.KEY_ENTER, ord("t"), ord("z"), ord(".")) and not look:
                if cur != (p.x, p.y):
                    m = g.monster_at(*cur)
                    if m:
                        self.last_target = m
                    return cur
            elif ch in (27, ord("q"), ord("x")):
                return None

    def choose(self, title, pred):
        p = self.g.player
        rows = [(LETTERS[i], self.inv_line(it), self.c(self.g.item_glyph(it)[1]))
                for i, it in enumerate(p.inv) if pred(it)]
        if not rows:
            self.g.msg("You have nothing for that.", "gray")
            return None
        key = self.menu(title, rows, "a letter, or Esc")
        return p.inv[LETTERS.index(key)] if key else None

    def inv_line(self, it):
        p = self.g.player
        tag = " (wielded)" if it is p.weapon else " (worn)" if it is p.armor or it in p.charms else ""
        return self.g.item_name(it) + tag

    # -- acting --

    def spend(self, cost):
        if cost:
            self.g.spend(cost)
        self.settle()

    def settle(self):
        g = self.g
        while g.pending and not g.over:
            pending, g.pending = g.pending, None
            if pending[0] == "shrine":
                self.shrine(pending[1])
            elif pending[0] == "trade":
                self.trade(pending[1])
            elif pending[0] == "identify":
                self.identify_menu()

    def use(self, action, it):
        g = self.g
        name = g.names(it)[0]
        if action == "q":
            self.spend(g.drink(it))
        elif action == "r":
            self.spend(g.read(it))
        elif action in ("z", "t"):
            if action == "t" and it.kind == "weapon" and it is g.player.weapon:
                if not self.confirm(f"Throw your wielded {name}?"):
                    return
            pos = self.target(("Zap " if action == "z" else "Throw ") + name, 10 if action == "z" else THROW_RANGE)
            if pos:
                self.spend(g.zap(it, *pos) if action == "z" else g.throw(it, *pos))
        elif action == "e":
            self.spend(g.equip(it))
        elif action == "d":
            self.spend(g.drop(it))

    def item_actions(self, it):
        g, p = self.g, self.g.player
        k = it.kind
        acts = []
        if k == "vial":
            acts = [("q", "drink"), ("t", "throw")]
        elif k == "oil":
            acts = [("q", "pour into your lantern")]
        elif k == "shell":
            acts = [("r", "read")]
        elif k == "rod":
            acts = [("z", "zap")]
        elif k == "weapon":
            acts = [("e", "put away" if it is p.weapon else "wield"), ("t", "throw")]
        elif k == "armor":
            acts = [("e", "take off" if it is p.armor else "wear")]
        elif k == "charm":
            acts = [("e", "take off" if it in p.charms else "put on")]
        elif k == "bait":
            acts = [("t", "throw")]
        if k != "heart":
            acts.append(("d", "drop"))
        rows = self.wrap(g.describe_item(it), self.c("pale"))
        rows = [(None, t, a) for t, a in rows] + [(None, "", 0)] + [(key, text, 0) for key, text in acts]
        key = self.menu(g.item_name(it, article=False), rows, "a letter, or Esc")
        if key:
            self.use(key, it)

    def inventory(self):
        g, p = self.g, self.g.player
        if not p.inv:
            g.msg("Your pack is empty.", "gray")
            return
        rows = [(LETTERS[i], self.inv_line(it), self.c(g.item_glyph(it)[1])) for i, it in enumerate(p.inv)]
        key = self.menu(f"Pack {len(p.inv)}/{MAX_INVENTORY} · {p.pearls} pearls", rows, "a letter to use an item, Esc")
        if key:
            self.item_actions(p.inv[LETTERS.index(key)])

    def shrine(self, pos):
        g = self.g
        offers = g.lv.shrines.get(pos)
        if not offers:
            return
        c = self.c
        rows = [(None, "A voice like the tide offers you a pact. Choose one, or leave it.", c("teal")), (None, "", 0)]
        for k, key in zip("ab", offers):
            name, boon, bane = PACTS[key]
            rows += [(k, name, c("magenta")), (None, "   " + boon, c("lime")), (None, "   But: " + bane, c("orange")),
                     (None, "", 0)]
        choice = self.menu("A drowned shrine", rows, "a or b to make the pact, Esc to leave")
        if choice:
            self.spend(g.take_pact(offers["ab".index(choice)], pos))
        else:
            g.msg("You leave the shrine be. Step on it again if you change your mind.", "gray")

    def trade(self, m):
        g, p = self.g, self.g.player
        while m.wares and m in g.lv.monsters:
            rows = [(None, f"“Pearls, pearls. What'll it be?”  You have {p.pearls} pearls.", self.c("green")),
                    (None, "", 0)]
            for i, (it, price) in enumerate(m.wares):
                rows.append((LETTERS[i], f"{g.shop_name(it)} — {price} pearls",
                             self.c("pale" if p.pearls >= price else "gray")))
            choice = self.menu("The hermit's wares", rows, "a letter to buy, Esc to leave")
            if not choice:
                return
            g.buy(m, LETTERS.index(choice))
        if m in g.lv.monsters and not m.wares:
            g.msg("“That's all I had. Off with you.”", "green")

    def identify_menu(self):
        g, p = self.g, self.g.player
        rows = [(LETTERS[i], self.inv_line(it), 0) for i, it in enumerate(p.inv) if g.unknown(it)]
        key = self.menu("Lore: what do you want to know?", rows, "a letter")
        if key:
            g.identify_item(p.inv[LETTERS.index(key)])
        else:
            g.msg("The knowledge drifts away unused.", "gray")

    def activate(self):
        g, p = self.g, self.g.player
        actives = [k for k in p.relics if RELICS[k][2]]
        if not actives:
            g.msg("None of your relics can be activated.", "gray")
            return
        rows = []
        for i, k in enumerate(actives):
            cd = p.cooldown.get(k)
            rows.append((LETTERS[i], f"{RELICS[k][0]} — {'ready' if not cd else f'{cd} turns'}",
                         self.c("gold" if not cd else "gray")))
            rows += [(None, t, a) for t, a in self.wrap(RELICS[k][1], self.c("pale"), "   ")]
        key = self.menu("Activate a relic", rows, "a letter, or Esc")
        if not key:
            return
        relic = actives[LETTERS.index(key)]
        target = None
        if relic == "conch" and not p.cooldown.get(relic):
            target = self.target("Charm which creature", reach=99)
            if not target:
                return
        self.spend(g.activate(relic, target))

    def auto(self, step):
        g, p = self.g, self.g.player
        g.disturb = False
        hp = p.hp
        self.s.nodelay(True)
        try:
            for _ in range(600):
                act, why = step()
                if act is None:
                    if why:
                        g.msg(why, "gray")
                    break
                if act[0] == "move":
                    cost = g.player_move(act[1], act[2])
                elif act[0] == "pickup":
                    cost = g.pickup()
                else:
                    cost = g.wait()
                if not cost:
                    break
                g.spend(cost)
                if g.over or g.pending or g.disturb or p.hp < hp:
                    break
                hp = p.hp
                self.draw()
                curses.napms(8)
                if self.s.getch() != -1:
                    break
        finally:
            self.s.nodelay(False)
        self.settle()

    def run(self, d):
        g, lv = self.g, self.g.lv
        n = [0]

        def step():
            p = g.player
            if n[0] and (lv.items.get((p.x, p.y)) or lv.t(p.x, p.y) in (STAIRS, SHRINE, EXIT)):
                return None, None
            nx, ny = p.x + d[0], p.y + d[1]
            if not inside(nx, ny) or lv.t(nx, ny) not in WALKABLE or g.monster_at(nx, ny):
                return None, None
            if n[0] and lv.wat(nx, ny) != lv.wat(p.x, p.y):
                return None, None
            n[0] += 1
            return ("move",) + d, None
        self.auto(step)

    def rest(self):
        g = self.g
        n = [0]

        def step():
            p = g.player
            n[0] += 1
            if g.threats():
                return None, "Not with something hunting you."
            if (p.hp >= g.maxhp() and p.breath >= g.max_breath()) or n[0] > 200:
                return None, "You feel rested." if n[0] > 1 else "You're already rested."
            return ("wait",), None
        self.auto(step)

    def handle(self, ch):
        g, p = self.g, self.g.player
        self.mark = len(g.log)
        if ch in MOVES:
            self.spend(g.player_move(*MOVES[ch]))
        elif ch in RUNS:
            self.run(RUNS[ch])
        elif ch in (ord("."), ord("5"), curses.KEY_B2):
            self.spend(g.wait())
        elif ch in (ord("g"), ord(",")):
            self.spend(g.pickup())
        elif ch == ord("i"):
            self.inventory()
        elif ch == ord("q"):
            it = self.choose("Drink what?", lambda x: x.kind in ("vial", "oil"))
            if it:
                self.use("q", it)
        elif ch == ord("r"):
            it = self.choose("Read which shell?", lambda x: x.kind == "shell")
            if it:
                self.use("r", it)
        elif ch == ord("z"):
            it = self.choose("Zap which rod?", lambda x: x.kind == "rod")
            if it:
                self.use("z", it)
        elif ch == ord("t"):
            it = self.choose("Throw what?", lambda x: x.kind not in ("heart",) and x not in p.charms
                             and x is not p.armor)
            if it:
                self.use("t", it)
        elif ch in (ord("e"), ord("w")):
            it = self.choose("Wield, wear or take off what?", lambda x: x.kind in ("weapon", "armor", "charm"))
            if it:
                self.use("e", it)
        elif ch == ord("d"):
            it = self.choose("Drop what?", lambda x: x.kind != "heart")
            if it:
                self.use("d", it)
        elif ch == ord("T"):
            d = self.direction("Tame")
            if d:
                self.spend(g.tame(*d))
        elif ch == ord("C"):
            d = self.direction("Close a door")
            if d:
                self.spend(g.close_door(*d))
        elif ch == ord("c"):
            g.command_allies()
        elif ch == ord("a"):
            self.activate()
        elif ch == ord("f"):
            self.spend(g.toggle_lantern())
        elif ch == ord("s"):
            g.toggle_sneak()
        elif ch == ord("R"):
            self.rest()
        elif ch == ord("o"):
            self.auto(g.explore_step)
        elif ch == ord(">"):
            lv = g.lv
            if lv.t(p.x, p.y) == STAIRS:
                g.descend()
            elif lv.stairs and lv.seen[lv.stairs[1] * W + lv.stairs[0]]:
                self.auto(lambda: g.travel_step(lv.stairs))
            elif lv.depth == MAX_DEPTH:
                g.msg("There's no deeper to go. The Tide-Heart is on the altar here.", "gray")
            else:
                g.msg("You haven't found the stairs down yet.", "gray")
        elif ch == ord("x"):
            self.target("Look", look=True)
        elif ch == ord("m"):
            self.messages()
        elif ch == ord("@"):
            self.character()
        elif ch == ord("?"):
            self.help()
        elif ch == ord("S"):
            if self.confirm("Save the run and quit?"):
                save_game(g)
                return "exit"
        elif ch == ord("Q"):
            if self.confirm("Give up this run? It counts as a death."):
                g.over, g.cause = "dead", f"gave up on depth {g.lv.depth}"
                g.record()
        return None

    def loop(self):
        while True:
            self.draw()
            if self.g.over:
                return self.end_screen()
            ch = self.s.getch()
            if ch == curses.KEY_RESIZE:
                continue
            if self.handle(ch) == "exit":
                return "exit"


def run(stdscr, game, seed):
    curses.curs_set(0)
    if curses.has_colors():
        curses.use_default_colors()
    stdscr.keypad(True)
    while True:
        fresh = game is None
        if fresh:
            game = Game(seed)
            seed = None
        ui = UI(stdscr, game)
        if fresh:
            ui.intro()
        else:
            game.msg("You take up the run where you left it.", "cyan")
        try:
            result = ui.loop()
        except KeyboardInterrupt:
            if not game.over:
                save_game(game)
            return
        if result != "new":
            return
        game = None


def print_records():
    records = load_records()
    if not records:
        print("No runs yet.")
        return
    for i, r in enumerate(records[:20], 1):
        mark = "ESCAPED " if r["won"] else f"depth {r['depth']:<2}"
        print(f"{i:>2}. {r['score']:>5}  {mark}  {r['date']}  {r['cause']}")


def main():
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(
        description="Sunken — a roguelike in a drowned city: twelve depths, a tide that moves, darkness for "
                    "cover, and the Tide-Heart at the bottom.",
        epilog=f"The run in progress and the hall of records are kept in {STATE_DIR.replace(home, '~')}.")
    parser.add_argument("--seed", type=int, help="start a new run from this seed (the same city every time)")
    parser.add_argument("--new", action="store_true", help="start a new run even if one is saved (that one is lost)")
    parser.add_argument("--records", action="store_true", help="print the hall of records and exit")
    args = parser.parse_args()
    if args.records:
        print_records()
        return
    game = None if args.new or args.seed is not None else load_game()
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(run, game, args.seed)


if __name__ == "__main__":
    main()
