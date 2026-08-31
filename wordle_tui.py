#!/usr/bin/env python3
"""Terminal UI Wordle: guess the hidden five-letter word in six tries."""

import argparse
import curses
import datetime
import random
import subprocess

WORD_LEN = 5
MAX_GUESSES = 6
DICT_PATH = "/usr/share/dict/words"

HIT, NEAR, MISS, BLANK = 3, 2, 1, 0

# (tile width, tile height, gap between grid rows), roomiest first. draw() takes the
# first that fits, so a short window loses the row gaps before it loses the boxes.
LAYOUTS = ((5, 3, 1), (5, 3, 0), (3, 1, 0))

KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")

# Answers are common words only. Guesses are checked against these *plus* the system
# dictionary, so obscure-but-real words are accepted without ever being the answer.
ANSWERS = """
abide about above abuse actor acute adapt admit adopt adult after again agent agree
ahead alarm album alert alien align alike alive allow alone along aloud alpha alter
amber amble amend among ample angel anger angle angry ankle apart apple apply apron
arena argue arise armor aroma arrow aside asset atlas audio audit avoid await awake
award aware awful bacon badge badly baker balmy banjo barge basic basil basin basis
batch beach beard beast beech began begin begun being belly below bench berry birch
birth black blade blame bland blank blast blaze bleak blend bless blind blink bliss
block blood bloom blown blues blunt blush board boast bonus boost booth bound brace
braid brain brake brand brass brave bread break breed brick bride brief bring brink
brisk broad broke brook broom brown brush build built bunch bunny burnt burst buyer
cabin cable cameo canal candy canoe canon cargo carol carry carve catch cause cease
cedar chain chair chalk charm chart chase cheap cheat check cheek cheer chess chest
chief child chill china chime choir chord chose chunk cider cigar civic civil claim
clamp clash clasp class clean clear clerk click cliff climb cling cloak clock clone
close cloth cloud clown coach coast cobra cocoa colon color comet comic coral corps
couch cough could count court cover crack craft crane crash crate crawl crazy cream
creek creep crest crime crisp cross crowd crown crude cruel crumb crush crust curve
cycle daily dairy daisy dance dated dealt death debit debut decay decor delay delta
dense depth derby devil diary digit dimly diner dirty ditch diver dizzy dodge doing
donor doubt dough dozen draft drain drama drank drape drawn dread dream dress dried
drift drill drink drive drone drove drown drunk dryer dusty dwarf dwell eager eagle
early earth eaten ebony edict eight elbow elder elect elite ember empty enact ended
enemy enjoy enter entry equal equip erase error essay ether event every exact exalt
excel exert exile exist extra fable faced facet faint fairy faith false fancy fatal
fault favor feast fence ferry fetch fever fiber field fiery fifth fifty fight final
finch first flame flash fleet flesh flick fling float flock flood floor flour fluid
flush flute focal focus foggy force forge forth forty forum found frame fraud fresh
fried frost frown fruit fudge fully funny gauge ghost giant giddy given giver glade
gland glare glass glaze gleam glide globe gloom glory glove gnome going grace grade
grain grand grant grape graph grasp grass grave gravy graze great greed green greet
grief grill grind groan groom grove growl grown gruff guard guess guest guide guild
guilt gully gusto habit hairy handy happy hardy harsh haste hatch haunt haven havoc
hazel heard heart heavy hedge hefty hello hence heron hinge hippo hobby hoist holly
honey honor horde horse hotel hound house hover human humid humor hurry husky hyena
ideal image imply index inert infer inlet inner input irony issue ivory jazzy jelly
jewel joint joker jolly joust judge juice jumbo juror karma kayak kneel knife knock
knoll known koala label labor laden lance lapse large larva laser latch later laugh
layer lease leash least leave ledge legal lemon level lever light lilac limbo limit
linen liner lingo liver llama loath lobby local lodge lofty logic loose loser lotus
lousy loyal lucky lunar lunch lunge lying lyric macro madam magic magma maize major
maker mango manor maple march marsh mason match maybe mayor meant medal media melon
mercy merge merit merry metal meter midst might mimic mince miner minor mirth mixed
model moist molar money month moral motel motif motor mound mount mourn mouse mouth
movie mower muddy mural music musty naive naked nasal nasty naval navel needy nerve
never newly niche niece night ninth noble noise north notch noted novel nudge nurse
nylon oasis occur ocean offer often olive onion onset opera orbit order organ other
otter ought ounce outer owing owner oxide ozone paint panel panic paper parka party
pasta paste patch patio pause peace peach pearl pedal penny perch peril petal petty
phase phone photo piano piece pilot pinch pitch pivot pixel pizza place plaid plain
plane plank plant plate plaza plead pluck plumb plume plush point polar polka porch
pouch pound power prank press price pride prime print prior prism prize probe prone
proof proud prove prune pulse punch pupil puppy purge purse quack quail quake quart
queen query quest queue quick quiet quill quilt quirk quite quota quote racer radar
radio rainy raise rally ranch range rapid ratio raven razor reach react ready realm
rebel refer regal reign relax relay renew repay reply resin reuse revel rhino rhyme
rider ridge rifle right rigid rinse ripen risen risky rival river roast robin robot
rocky rogue roman rough round route royal rugby ruler rumor rural rusty saint salad
salon salty sandy satin sauce scald scale scalp scarf scare scene scent scoop scope
score scorn scout scrap screw scrub sedan seize sense serve seven sever shade shaft
shake shall shame shape share shark sharp shave sheep sheer sheet shelf shell shift
shine shiny shirt shock shone shore short shout shove shown shrub shrug siege sight
silly since siren skate skill skirt skull slain slant slash slate slave sleek sleep
sleet slice slide slime slope sloth slump small smart smash smell smile smirk smoke
smoky snack snail snake sneak sniff snore snout snowy sober solar solid solve sonic
sorry sound south space spade spare spark speak spear speed spell spend spent spice
spicy spike spill spine spite split spoke spoon sport spout spray spree squad squat
squid stack staff stage stain stair stake stale stalk stall stamp stand stare stark
start state steal steam steel steep steer stern stick stiff still sting stink stock
stoic stole stomp stone stony stood stool stoop store storm story stout stove strap
straw stray strip stuck study stuff stump stung style suave sugar suite sunny super
surge surly swamp swarm swear sweat sweep sweet swell swept swift swine swing sword
sworn syrup table taboo tacit taken tally talon tango tapir tardy taste tasty teach
tempo tenor tense tenth thank theft their theme there these thick thief thigh thing
think third thorn those three threw throw thumb tiger tight timer timid tipsy title
toast today token tonic tooth topaz topic torch total touch tough towel tower toxic
trace track trade trail train trait tramp trash tread treat trend trial tribe trick
tried tries troop trout truce truck truly trump trunk trust truth tulip tumor tutor
twice twine twist udder ultra uncle under undue unfit union unite unity until upper
upset urban urged usage usher usual utter vague valet valid valor value valve vapor
vault venom venue verge verse vicar video vigil vigor villa vinyl viola viper viral
virus visit vital vivid vocal vodka vogue voice vouch vowel wager wagon waist waltz
waste watch water weary weave wedge weigh weird whale wheat wheel where which while
whirl whisk white whole whose widen widow width wield wince winch windy wiper wired
witch witty woken woman women world worry worse worst worth would wound woven wrath
wreck wrist write wrong wrote yacht yearn yeast yield young youth yummy zebra zesty
zonal
""".split()


def load_dictionary():
    """Five-letter words from the system dictionary, unioned with the answer list."""
    words = set(ANSWERS)
    try:
        with open(DICT_PATH, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                word = line.strip().lower()
                if len(word) == WORD_LEN and word.isalpha() and word.isascii():
                    words.add(word)
    except OSError:
        pass  # No system dictionary: the answer list alone is the vocabulary.
    return words


def pick_answer(daily=False, seed=None):
    if seed is not None:
        return sorted(ANSWERS)[seed % len(ANSWERS)]
    if daily:
        day = datetime.date.today().toordinal()
        return sorted(ANSWERS)[day % len(ANSWERS)]
    return random.choice(ANSWERS)


def score_guess(guess, answer):
    """Mark each position HIT/NEAR/MISS, spending each answer letter at most once."""
    marks = [MISS] * WORD_LEN
    unmatched = {}
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            marks[i] = HIT
        else:
            unmatched[a] = unmatched.get(a, 0) + 1
    for i, g in enumerate(guess):
        if marks[i] != HIT and unmatched.get(g, 0):
            marks[i] = NEAR
            unmatched[g] -= 1
    return marks


def hard_mode_error(guess, history):
    """Why `guess` fails to reuse everything earlier guesses revealed, or None."""
    ordinals = ("1st", "2nd", "3rd", "4th", "5th")
    for word, marks in history:
        for i, (ch, mark) in enumerate(zip(word, marks)):
            if mark == HIT and guess[i] != ch:
                return f"{ordinals[i]} letter must be {ch.upper()}"
        needed = {}
        for ch, mark in zip(word, marks):
            if mark in (HIT, NEAR):
                needed[ch] = needed.get(ch, 0) + 1
        for ch, count in needed.items():
            if guess.count(ch) < count:
                plural = "" if count == 1 else f" x{count}"
                return f"Guess must use {ch.upper()}{plural}"
    return None


def keyboard_state(history):
    """Best mark seen per letter; HIT outranks NEAR outranks MISS."""
    state = {}
    for word, marks in history:
        for ch, mark in zip(word, marks):
            if mark > state.get(ch, BLANK):
                state[ch] = mark
    return state


def share_text(history, won, label, hard):
    squares = {HIT: "\U0001f7e9", NEAR: "\U0001f7e8", MISS: "⬛"}
    tally = f"{len(history)}/{MAX_GUESSES}" if won else f"X/{MAX_GUESSES}"
    head = f"wordle_tui {label} {tally}{'*' if hard else ''}"
    grid = "\n".join("".join(squares[m] for m in marks) for _, marks in history)
    return f"{head}\n\n{grid}\n"


def copy_to_clipboard(text):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def init_colors():
    """Pairs 1-3 are the tile fills, 4-6 the chrome. Falls back to 8 colours."""
    curses.start_color()
    curses.use_default_colors()
    if curses.COLORS >= 256:
        white = 231
        curses.init_pair(1, white, 71)    # hit   ~#6aaa64
        curses.init_pair(2, white, 179)   # near  ~#c9b458
        curses.init_pair(3, white, 242)   # miss  ~#787c7e
    else:
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(4, curses.COLOR_CYAN, -1)     # title
    curses.init_pair(5, curses.COLOR_WHITE, -1)    # dim chrome
    curses.init_pair(6, curses.COLOR_YELLOW, -1)   # messages


def mark_attr(mark):
    if mark == HIT:
        return curses.color_pair(1) | curses.A_BOLD
    if mark == NEAR:
        return curses.color_pair(2) | curses.A_BOLD
    if mark == MISS:
        return curses.color_pair(3)
    return curses.color_pair(5) | curses.A_DIM


class Game:
    def __init__(self, answer, words, hard, label):
        self.answer = answer
        self.words = words
        self.hard = hard
        self.label = label
        self.history = []      # [(word, marks)]
        self.typed = ""
        self.message = ""
        self.over = False
        self.won = False

    def type_letter(self, ch):
        if len(self.typed) < WORD_LEN:
            self.typed += ch
            self.message = ""

    def backspace(self):
        self.typed = self.typed[:-1]
        self.message = ""

    def submit(self):
        guess = self.typed
        if len(guess) < WORD_LEN:
            self.message = "Not enough letters"
            return
        if guess not in self.words:
            self.message = "Not in word list"
            return
        if self.hard:
            problem = hard_mode_error(guess, self.history)
            if problem:
                self.message = problem
                return
        marks = score_guess(guess, self.answer)
        self.history.append((guess, marks))
        self.typed = ""
        self.message = ""
        if guess == self.answer:
            self.over = self.won = True
            self.message = ("Genius!", "Magnificent!", "Impressive!",
                            "Splendid!", "Great!", "Phew!")[len(self.history) - 1]
        elif len(self.history) == MAX_GUESSES:
            self.over = True
            self.message = f"The word was {self.answer.upper()}"


def rows_needed(tile_h, vgap):
    """Title, blank, grid, blank, message, blank, three keyboard rows, footer."""
    return MAX_GUESSES * (tile_h + vgap) + 9


def choose_layout(h, w):
    for tile_w, tile_h, vgap in LAYOUTS:
        if h >= rows_needed(tile_h, vgap) and w >= WORD_LEN * tile_w + WORD_LEN + 1:
            return tile_w, tile_h, vgap
    return None


def draw(stdscr, game, copied):
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    def put(y, x, text, attr=0):
        if 0 <= y < h and 0 <= x < w:
            stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)

    layout = choose_layout(h, w)
    if layout is None:
        put(0, 0, "Terminal too small", curses.color_pair(6))
        stdscr.refresh()
        return
    tile_w, tile_h, vgap = layout
    tall = tile_h > 1
    gap = 1
    grid_w = WORD_LEN * tile_w + (WORD_LEN - 1) * gap

    left = (w - grid_w) // 2
    title = "W O R D L E" + ("  (hard)" if game.hard else "")
    put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(4) | curses.A_BOLD)

    y = 2
    for row in range(MAX_GUESSES):
        if row < len(game.history):
            word, marks = game.history[row]
        elif row == len(game.history) and not game.over:
            word = game.typed.ljust(WORD_LEN)
            marks = [BLANK] * WORD_LEN
        else:
            word, marks = " " * WORD_LEN, [BLANK] * WORD_LEN
        for col in range(WORD_LEN):
            ch = word[col].upper()
            x = left + col * (tile_w + gap)
            scored = marks[col] != BLANK
            attr = mark_attr(marks[col])
            if scored:
                attr_body = attr
                lines = ["     ", f"  {ch}  ", "     "] if tall else [f" {ch} "]
            else:
                bright = ch != " "
                attr_body = curses.color_pair(5) | (curses.A_BOLD if bright else curses.A_DIM)
                lines = (["┌───┐", f"│ {ch} │",
                          "└───┘"] if tall else [f"[{ch}]"])
            for dy, line in enumerate(lines):
                put(y + dy, x, line, attr_body)
        y += tile_h + vgap

    y += 1
    if game.message:
        colour = curses.color_pair(4) if game.won else curses.color_pair(6)
        put(y, max(0, (w - len(game.message)) // 2), game.message, colour | curses.A_BOLD)
    y += 2

    state = keyboard_state(game.history)
    for row_i, letters in enumerate(KEY_ROWS):
        row_w = len(letters) * 3 + (len(letters) - 1)
        x = max(0, (w - row_w) // 2)
        for ch in letters:
            put(y, x, f" {ch.upper()} ", mark_attr(state.get(ch, BLANK)))
            x += 4
        y += 1
        if y >= h - 1:
            break

    if game.over:
        footer = "n new game   c copy result   q quit"
        if copied:
            footer = "Result copied to clipboard!   n new game   q quit"
    else:
        footer = "type letters   enter guess   backspace delete   esc quit"
    put(h - 1, max(0, (w - len(footer)) // 2), footer, curses.color_pair(5) | curses.A_DIM)
    stdscr.refresh()


def main(stdscr, args):
    curses.curs_set(0)
    init_colors()
    if hasattr(curses, "set_escdelay"):
        curses.set_escdelay(25)

    words = load_dictionary()
    label = str(datetime.date.today()) if args.daily else "random"

    def new_game():
        answer = args.word or pick_answer(daily=args.daily, seed=args.seed)
        return Game(answer, words, args.hard, label)

    game = new_game()
    copied = False

    while True:
        draw(stdscr, game, copied)
        key = stdscr.getch()

        if key == curses.KEY_RESIZE:
            continue
        if key == 27:  # esc
            return
        if game.over:
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("n"), ord("N")):
                game, copied = new_game(), False
            elif key in (ord("c"), ord("C")):
                text = share_text(game.history, game.won, game.label, game.hard)
                copied = copy_to_clipboard(text)
                game.message = "" if copied else "Copy failed (pbcopy unavailable)"
            continue

        copied = False
        if key in (curses.KEY_BACKSPACE, curses.KEY_DC, 127, 8):
            game.backspace()
        elif key in (10, 13, curses.KEY_ENTER):
            game.submit()
        elif 0 <= key < 256 and chr(key).isalpha():
            game.type_letter(chr(key).lower())


def parse_args():
    parser = argparse.ArgumentParser(description="Wordle in the terminal.")
    parser.add_argument("--daily", action="store_true",
                        help="today's word, the same for every run today")
    parser.add_argument("--hard", action="store_true",
                        help="hard mode: revealed hints must be reused in later guesses")
    parser.add_argument("--seed", type=int, help="pick the answer by number, for a repeatable game")
    parser.add_argument("--word", help="force the answer (for testing)")
    args = parser.parse_args()
    if args.word:
        args.word = args.word.strip().lower()
        if len(args.word) != WORD_LEN or not args.word.isalpha():
            parser.error("--word must be five letters")
    return args


if __name__ == "__main__":
    try:
        curses.wrapper(main, parse_args())
    except KeyboardInterrupt:
        pass
