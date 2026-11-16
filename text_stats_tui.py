#!/usr/bin/env python3
"""Text stats — what a folder of text is made of: lines of code, comments and blanks per programming
language, word frequency, and a page of other metrics (line endings, indentation, encodings,
readability, TODOs, the longest line …).

In a git repository the files are the ones git knows about, so .gitignore is respected; elsewhere
the tree is walked, skipping hidden folders, dependencies and build output. Binary files are
skipped. A line is a comment line when it holds a comment and no code, as tokei and cloc count it;
strings are followed so a `#` inside quotes doesn't start a comment. The scan runs on a thread and
the views fill in while it goes.
"""

import argparse
import curses
import keyword
import locale
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter

MAX_FILE_BYTES = 10_000_000    # bigger files are skipped as "too big"
LONG_LINES = (80, 120)         # lines longer than these are counted
TICK_MS = 200
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 60, 12
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", "build", "dist", "target", "vendor",
             "site-packages", "Pods", "DerivedData"}   # and hidden folders; only used outside git
BARS = " ▏▎▍▌▋▊▉█"

# name -> (extensions and file names, line comments, block comments, string quotes, kind)
# A block comment written (start, end, True) only counts at the start of a line (Python docstrings);
# elsewhere it's a string. kind is code, markup, prose (readability applies) or data.
C_LIKE = (("//",), (("/*", "*/"),), ('"', "'"))
HASH = (("#",), (), ('"', "'"))
LANGUAGES = {
    "Python": (".py .pyw .pyi", ("#",), (('"""', '"""', True), ("'''", "'''", True)), ('"""', "'''", '"', "'"), "code"),
    "JavaScript": (".js .mjs .cjs .jsx", *C_LIKE[:2], ('"', "'", "`"), "code"),
    "TypeScript": (".ts .tsx .mts .cts", *C_LIKE[:2], ('"', "'", "`"), "code"),
    "Java": (".java", *C_LIKE, "code"),
    "Kotlin": (".kt .kts", *C_LIKE[:2], ('"""', '"', "'"), "code"),
    "Swift": (".swift", *C_LIKE[:2], ('"""', '"'), "code"),
    "Objective-C": (".m .mm", *C_LIKE, "code"),
    "C": (".c", *C_LIKE, "code"),
    "C header": (".h", *C_LIKE, "code"),
    "C++": (".cpp .cc .cxx .hpp .hh .hxx", *C_LIKE, "code"),
    "C#": (".cs", *C_LIKE, "code"),
    "Go": (".go", *C_LIKE[:2], ('"', "'", "`"), "code"),
    "Rust": (".rs", *C_LIKE[:2], ('"',), "code"),
    "Zig": (".zig", ("//",), (), ('"',), "code"),
    "Dart": (".dart", *C_LIKE[:2], ("'''", '"""', '"', "'"), "code"),
    "Scala": (".scala .sc", *C_LIKE[:2], ('"""', '"'), "code"),
    "Groovy": (".groovy .gradle", *C_LIKE, "code"),
    "PHP": (".php", ("//", "#"), (("/*", "*/"),), ('"', "'"), "code"),
    "Ruby": (".rb .rake Gemfile Rakefile", ("#",), (("=begin", "=end", True),), ('"', "'"), "code"),
    "Perl": (".pl .pm", ("#",), (("=pod", "=cut", True),), ('"', "'"), "code"),
    "Lua": (".lua", ("--",), (("--[[", "]]"),), ('"', "'"), "code"),
    "R": (".r .R", ("#",), (), ('"', "'"), "code"),
    "Julia": (".jl", ("#",), (("#=", "=#"),), ('"""', '"'), "code"),
    "Haskell": (".hs", ("--",), (("{-", "-}"),), ('"',), "code"),
    "Elixir": (".ex .exs", ("#",), (), ('"""', '"', "'"), "code"),
    "Erlang": (".erl .hrl", ("%",), (), ('"',), "code"),
    "Clojure": (".clj .cljs .cljc .edn", (";",), (), ('"',), "code"),
    "Lisp": (".lisp .lsp .el .scm .rkt", (";",), (("#|", "|#"),), ('"',), "code"),
    "OCaml": (".ml .mli", (), (("(*", "*)"),), ('"',), "code"),
    "Nim": (".nim", ("#",), (("#[", "]#"),), ('"""', '"'), "code"),
    "Fortran": (".f .f90 .f95 .for", ("!",), (), ('"', "'"), "code"),
    "Pascal": (".pas .pp", ("//",), (("{", "}"), ("(*", "*)")), ("'",), "code"),
    "Visual Basic": (".vb .vbs .bas", ("'",), (), ('"',), "code"),
    "Assembly": (".s .S .asm", (";", "#"), (("/*", "*/"),), ('"',), "code"),
    "Shell": (".sh .bash .zsh .ksh .bashrc .zshrc .profile .bash_profile .zprofile", *HASH, "code"),
    "Fish": (".fish", *HASH, "code"),
    "PowerShell": (".ps1 .psm1 .psd1", ("#",), (("<#", "#>"),), ('"', "'"), "code"),
    "Batch": (".bat .cmd", ("REM ", "rem ", "Rem ", "::", "@REM ", "@rem "), (), ('"',), "code"),
    "AutoHotkey": (".ahk", (";",), (("/*", "*/", True),), ('"',), "code"),
    "AppleScript": (".applescript .scpt", ("--", "#"), (("(*", "*)"),), ('"',), "code"),
    "Vim script": (".vim .vimrc", ('"',), (), ("'",), "code"),
    "SQL": (".sql", ("--",), (("/*", "*/"),), ("'", '"'), "code"),
    "Makefile": (".mk Makefile makefile GNUmakefile", ("#",), (), (), "code"),
    "Dockerfile": ("Dockerfile .dockerfile Containerfile", ("#",), (), (), "code"),
    "CMake": (".cmake CMakeLists.txt", ("#",), (), ('"',), "code"),
    "Nix": (".nix", ("#",), (("/*", "*/"),), ('"',), "code"),
    "Terraform": (".tf .tfvars .hcl", ("#", "//"), (("/*", "*/"),), ('"',), "code"),
    "Protobuf": (".proto", *C_LIKE[:2], ('"', "'"), "code"),
    "GraphQL": (".graphql .gql", ("#",), (), ('"""', '"'), "code"),
    "HTML": (".html .htm .xhtml", (), (("<!--", "-->"),), (), "markup"),
    "XML": (".xml .xsl .xslt .svg .plist .xib .storyboard .csproj", (), (("<!--", "-->"),), (), "markup"),
    "Vue": (".vue", ("//",), (("<!--", "-->"), ("/*", "*/")), ('"', "'", "`"), "markup"),
    "Svelte": (".svelte", ("//",), (("<!--", "-->"), ("/*", "*/")), ('"', "'", "`"), "markup"),
    "CSS": (".css", (), (("/*", "*/"),), ('"', "'"), "markup"),
    "SCSS": (".scss .sass .less", ("//",), (("/*", "*/"),), ('"', "'"), "markup"),
    "TeX": (".tex .sty .cls .bib", ("%",), (), (), "prose"),
    "Markdown": (".md .markdown .mdx", (), (("<!--", "-->"),), (), "prose"),
    "reStructuredText": (".rst", (), (), (), "prose"),
    "Org": (".org", (), (), (), "prose"),
    "Roff": (".1 .2 .3 .4 .5 .6 .7 .8 .9 .man .roff .mdoc", ('.\\"', "'\\\"", '.\\#'), (), (), "markup"),
    "Text": (".txt .text README LICENSE COPYING AUTHORS CHANGELOG NOTICE", (), (), (), "prose"),
    "JSON": (".json .jsonc .json5 .geojson .ipynb", ("//",), (("/*", "*/"),), ('"',), "data"),
    "YAML": (".yaml .yml", ("#",), (), ('"', "'"), "data"),
    "TOML": (".toml", ("#",), (), ('"""', '"', "'"), "data"),
    "INI": (".ini .cfg .conf .properties .editorconfig .gitconfig", ("#", ";"), (), (), "data"),
    "CSV": (".csv .tsv", (), (), (), "data"),
    "Git config": (".gitignore .gitattributes .gitmodules .dockerignore .npmignore", ("#",), (), (), "data"),
    "Other": ("", (), (), (), "data"),
}
SHEBANGS = {"python": "Python", "python3": "Python", "bash": "Shell", "sh": "Shell", "zsh": "Shell",
            "ksh": "Shell", "fish": "Fish", "node": "JavaScript", "deno": "TypeScript", "ruby": "Ruby",
            "perl": "Perl", "lua": "Lua", "php": "PHP", "Rscript": "R", "osascript": "AppleScript",
            "pwsh": "PowerShell", "make": "Makefile"}

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having
he her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not
now of off on once only or other our ours ourselves out over own same she should so some such than that the their
theirs them themselves then there these they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours yourself yourselves s t don isn doesn it's i'm
you're can't won't didn't don't isn't let's that's there's what's""".split())
# hidden from the word list with `c`: Python's own keywords plus the common ones of other languages
KEYWORDS = {k.lower() for k in keyword.kwlist + keyword.softkwlist} | set("""self cls this super new delete
null nil undefined true false void int char long short float double bool boolean string str var let const func
function fn def end elif elsif else then fi done esac do while for foreach switch case default break continue
return goto try catch finally throw throws raise public private protected static final abstract virtual override
struct enum class interface extends implements package import include using namespace typedef sizeof unsigned
signed auto extern register volatile inline async await yield echo local export typeof instanceof mut impl pub
crate use mod match where trait dyn ref println printf print len range dict list tuple set get none""".split())
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
VOWELS = re.compile(r"[aeiouy]+")
EOL = re.compile(r"\r\n|\r|\n")
TODO = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
VIEWS = ["languages", "files", "words", "metrics"]


class Lang:
    """A LANGUAGES entry compiled for counting: one regex finds the next comment or string start."""

    def __init__(self, name, spec):
        names, line, blocks, quotes, kind = spec
        self.name, self.kind = name, kind
        self.exts = names.split()
        self.tokens = {}                   # token -> (what, end, start-of-line only)
        for q in quotes:
            self.tokens[q] = ("string", q, False)
        for block in blocks:
            self.tokens[block[0]] = ("block", block[1], len(block) > 2)
        for lc in line:
            self.tokens[lc] = ("line", None, False)
        ordered = sorted(self.tokens, key=len, reverse=True)   # """ before "
        self.rx = re.compile("|".join(map(re.escape, ordered))) if ordered else None
        self.ends = {q: re.compile(r"\\.|" + re.escape(q)) for q in quotes}


LANGS = {name: Lang(name, spec) for name, spec in LANGUAGES.items()}
BY_NAME = {}
for _lang in LANGS.values():
    for _e in _lang.exts:
        BY_NAME[_e if _e.startswith(".") else "=" + _e] = _lang


def prose(lang):
    """Keywords are left in when the scope is a prose language: "use" and "end" are words there."""
    return lang is not None and LANGS[lang].kind == "prose"


def detect(path, first_line):
    base = os.path.basename(path)
    lang = BY_NAME.get("=" + base)
    if lang is None:
        ext = os.path.splitext(base)[1]
        lang = BY_NAME.get(ext) or BY_NAME.get(ext.lower())
    if lang is None and first_line.startswith("#!"):
        parts = first_line[2:].split()
        if parts:
            prog = os.path.basename(parts[0])
            if prog == "env" and len(parts) > 1:
                prog = parts[1] if not parts[1].startswith("-") else parts[-1]
            lang = LANGS.get(SHEBANGS.get(re.sub(r"[\d.]+$", "", prog)) or SHEBANGS.get(prog, ""))
    if lang is None and "." not in base.lstrip("."):
        lang = LANGS["Text"]               # LICENSE-like names without an extension
    return lang or LANGS["Other"]


def classify(lines, lang):
    """(code, comment, blank) line counts."""
    code = comment = blank = 0
    in_block = in_str = None
    for raw in lines:
        s = raw.strip()
        if not s:
            blank += 1
            continue
        if lang.rx is None:
            code += 1
            continue
        has_code = has_comment = False
        i, n = 0, len(s)
        while i < n:
            if in_block:
                has_comment = True
                j = s.find(in_block, i)
                if j < 0:
                    break
                i, in_block = j + len(in_block), None
            elif in_str:
                has_code = True
                for m in lang.ends[in_str].finditer(s, i):
                    if m.group() == in_str:
                        i, in_str = m.end(), None
                        break
                else:
                    break
            else:
                m = lang.rx.search(s, i)
                if (s[i:m.start()] if m else s[i:]).strip():
                    has_code = True
                if not m:
                    break
                what, end, line_start = lang.tokens[m.group()]
                i = m.end()
                if what == "line":
                    has_comment = True
                    break
                if what == "block" and (not line_start or (m.start() == 0 and not has_code)):
                    in_block, has_comment = end, True
                elif what == "block" and m.group() not in lang.ends:
                    has_code = True        # a start-of-line-only marker mid-line is just code
                else:
                    in_str, has_code = m.group(), True
        if in_str and len(in_str) == 1 and in_str != "`":
            in_str = None                  # ordinary quotes don't run past the line
        if has_code:
            code += 1
        elif has_comment:
            comment += 1
        else:
            code += 1
    return code, comment, blank


def syllables(word):
    word = word.lower()
    count = len(VOWELS.findall(word))
    if word.endswith("e") and not word.endswith(("le", "ee")) and count > 1:
        count -= 1
    return max(1, count)


def indent_width(counts):
    """The indent step of a file from how many lines start with n spaces."""
    total = sum(counts.values())
    if not total:
        return None
    for width in (8, 4, 3, 2):
        if sum(c for n, c in counts.items() if n % width == 0) >= 0.9 * total:
            return width
    return 1


class FileStat:
    __slots__ = ("path", "lang", "bytes", "chars", "lines", "code", "comment", "blank", "words",
                 "longest", "longest_at", "long", "eol", "final_newline", "trailing", "indent",
                 "width", "encoding", "todos", "sentences", "syllables", "non_ascii")


def analyse(path, rel):
    """A FileStat and the file's words, or (None, reason) for a skipped file."""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            return None, "too big"
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, "unreadable"
    if b"\0" in data[:8192]:
        return None, "binary"
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")
        encoding = "ASCII" if data.isascii() else "UTF-8 with BOM" if bom else "UTF-8"
    except UnicodeDecodeError:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return None, "binary"          # UTF-16 is rare enough in source to treat as binary
        text, encoding = data.decode("latin-1"), "Latin-1"
    st = FileStat()
    st.path, st.bytes, st.chars, st.encoding = rel, size, len(text), encoding
    crlf = text.count("\r\n")
    cr, lf = text.count("\r") - crlf, text.count("\n") - crlf
    kinds = [name for name, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
    st.eol = kinds[0] if len(kinds) == 1 else "mixed" if kinds else "none"
    st.final_newline = text.endswith(("\n", "\r")) or not text
    lines = EOL.split(text)                # not splitlines(): that also breaks at form feeds and \x85
    if lines[-1] == "":
        lines.pop()
    st.lang = detect(rel, lines[0] if lines else "")
    st.lines = len(lines)
    st.code, st.comment, st.blank = classify(lines, st.lang)
    st.longest, st.longest_at = 0, 0
    st.long = [0] * len(LONG_LINES)
    st.trailing = 0
    tabs = spaces = 0
    widths = Counter()
    for no, line in enumerate(lines, 1):
        n = len(line)
        if n > st.longest:
            st.longest, st.longest_at = n, no
        for k, limit in enumerate(LONG_LINES):
            if n > limit:
                st.long[k] += 1
        if line and line[-1] in " \t":
            st.trailing += 1
        if line[:1] == "\t":
            tabs += 1
        elif line[:1] == " " and line.strip():
            spaces += 1
            widths[n - len(line.lstrip(" "))] += 1
    st.indent = None if not tabs and not spaces else "tabs" if not spaces else "spaces" if not tabs else "mixed"
    st.width = indent_width(widths) if st.indent == "spaces" else None
    words = WORD.findall(text)
    st.words = len(words)
    st.todos = Counter(TODO.findall(text))
    st.non_ascii = 0 if encoding == "ASCII" else len(text) - len(text.encode("ascii", "ignore"))
    if st.lang.kind == "prose":
        st.sentences = len(SENTENCE_END.findall(text))
        st.syllables = sum(syllables(w) for w in words)
    else:
        st.sentences = st.syllables = 0
    return st, Counter(w.lower() for w in words)


def list_files(root):
    """(paths relative to root, through git) — or None when root isn't in a git work tree."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def walk(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(files):
            if name != ".DS_Store":
                yield os.path.relpath(os.path.join(folder, name), root)


class Scan:
    def __init__(self, root, use_git=True):
        self.root = root
        self.lock = threading.Lock()
        self.files = []
        self.words = {}                    # language name -> Counter
        self.skipped = Counter()           # reason -> files
        self.source = "file"
        self.busy = True
        self.started = time.monotonic()
        self.elapsed = 0.0
        self.seen = 0
        self.use_git = use_git
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.collect()
        finally:                           # an unexpected error mustn't leave --once waiting forever
            self.elapsed = time.monotonic() - self.started
            self.busy = False

    def collect(self):
        if os.path.isfile(self.root):
            paths, base = [os.path.basename(self.root)], os.path.dirname(self.root)
        else:
            listed = list_files(self.root) if self.use_git else None
            if not listed:
                listed = None              # not a repository, or a folder it ignores (Homebrew's Cellar)
            self.source = "git" if listed is not None else "walk"
            paths, base = (listed if listed is not None else walk(self.root)), self.root
        for rel in paths:
            full = os.path.join(base, rel)
            self.seen += 1
            if os.path.islink(full) or not os.path.isfile(full):
                continue                   # symlinks, submodules, deleted-but-tracked
            st, words = analyse(full, rel)
            with self.lock:
                if st is None:
                    self.skipped[words] += 1
                    continue
                self.files.append(st)
                self.words.setdefault(st.lang.name, Counter()).update(words)

    def snapshot(self):
        with self.lock:
            return list(self.files)

    def word_counts(self, lang):
        with self.lock:
            if lang is not None:
                return Counter(self.words.get(lang, ()))
            total = Counter()
            for c in self.words.values():
                total.update(c)
            return total


def languages(files):
    """[(name, kind, files, lines, code, comment, blank, bytes)]"""
    rows = {}
    for f in files:
        r = rows.setdefault(f.lang.name, [f.lang.name, f.lang.kind, 0, 0, 0, 0, 0, 0])
        r[2] += 1
        r[3] += f.lines
        r[4] += f.code
        r[5] += f.comment
        r[6] += f.blank
        r[7] += f.bytes
    return [tuple(r) for r in rows.values()]


def metrics(files, words):
    """The Metrics page as [(section, [(label, value)])]."""
    if not files:
        return []
    n = len(files)
    lines = sum(f.lines for f in files)
    code, comment, blank = (sum(getattr(f, k) for f in files) for k in ("code", "comment", "blank"))
    total_words = sum(words.values())
    unique = len(words)
    once = sum(1 for c in words.values() if c == 1)
    letters = sum(len(w) * c for w, c in words.items())
    nonblank = lines - blank
    chars = sum(f.chars for f in files)
    longest = max(files, key=lambda f: f.longest)
    biggest = max(files, key=lambda f: f.lines)
    pct = lambda a, b: f"{a / b:.1%}" if b else "–"
    out = [("Size", [
        ("files", f"{n:,}"),
        ("lines", f"{lines:,}  ({code:,} code · {comment:,} comment · {blank:,} blank)"),
        ("comment density", f"{pct(comment, code + comment)} of non-blank lines"),
        ("bytes", fmt_size(sum(f.bytes for f in files))),
        ("characters", f"{chars:,}  ({sum(f.non_ascii for f in files):,} outside ASCII)"),
        ("average file", f"{lines / n:,.0f} lines"),
        ("largest file", f"{biggest.path}  {biggest.lines:,} lines"),
    ]), ("Words", [
        ("words", f"{total_words:,}"),
        ("distinct words", f"{unique:,}"),
        ("lexical diversity", f"{pct(unique, total_words)}  (distinct ÷ total; lower means more repetition)"),
        ("used only once", f"{once:,}  ({pct(once, unique)} of distinct words: the hapax legomena)"),
        ("average word length", f"{letters / total_words:.1f} letters" if total_words else "–"),
    ]), ("Lines", [
        ("average line length", f"{(chars - lines) / nonblank:.0f} characters (non-blank)" if nonblank else "–"),
        ("longest line", f"{longest.longest:,} characters  {longest.path}:{longest.longest_at}"),
    ] + [(f"longer than {limit}", f"{sum(f.long[k] for f in files):,} lines in "
          f"{sum(1 for f in files if f.long[k]):,} files") for k, limit in enumerate(LONG_LINES)] + [
        ("trailing whitespace", f"{sum(f.trailing for f in files):,} lines in {sum(1 for f in files if f.trailing):,} files"),
        ("no newline at the end", f"{sum(1 for f in files if not f.final_newline):,} files"),
    ])]

    def tally(key, order=None):
        c = Counter(key(f) for f in files)
        c.pop(None, None)
        items = sorted(c.items(), key=lambda kv: (order.index(kv[0]) if order and kv[0] in order else 99, -kv[1]))
        return "  ·  ".join(f"{k} {v:,}" for k, v in items) or "–"

    out.append(("Format (files)", [
        ("line endings", tally(lambda f: f.eol, ["LF", "CRLF", "CR", "mixed", "none"])),
        ("encoding", tally(lambda f: f.encoding, ["ASCII", "UTF-8", "UTF-8 with BOM", "Latin-1"])),
        ("indentation", tally(lambda f: f.indent, ["spaces", "tabs", "mixed"])),
        ("indent width", tally(lambda f: f"{f.width} spaces" if f.width and f.width > 1 else
                               "irregular" if f.width else None)),
    ]))
    todos = Counter()
    for f in files:
        todos.update(f.todos)
    out.append(("Markers", [("TODO · FIXME · XXX · HACK",
                             "  ".join(f"{todos.get(k, 0):,}" for k in ("TODO", "FIXME", "XXX", "HACK")))]))
    prose = [f for f in files if f.lang.kind == "prose" and f.sentences]
    if prose:
        pw, ps, psyl = sum(f.words for f in prose), sum(f.sentences for f in prose), sum(f.syllables for f in prose)
        ease = 206.835 - 1.015 * pw / ps - 84.6 * psyl / pw if pw else 0
        grade = 0.39 * pw / ps + 11.8 * psyl / pw - 15.59 if pw else 0
        out.append((f"Readability ({plural(len(prose), 'prose file')}, English formulas)", [
            ("sentences", f"{ps:,}  ({pw / ps:.1f} words each)"),
            ("Flesch reading ease", f"{ease:.0f}  ({ease_label(round(ease))})"),
            ("Flesch–Kincaid grade", f"{grade:.1f}  (US school years)"),
            ("reading time", f"{fmt_minutes(pw / 238)} at 238 words a minute"),
        ]))
    return out


def ease_label(score):
    for limit, label in ((90, "very easy"), (80, "easy"), (70, "fairly easy"), (60, "plain English"),
                         (50, "fairly difficult"), (30, "difficult")):
        if score >= limit:
            return label
    return "very difficult"


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def fmt_minutes(m):
    if m < 1:
        return "under a minute"
    return f"{m:.0f} min" if m < 60 else f"{int(m) // 60} h {int(m) % 60:02d} min"


def fmt_size(n):
    """Decimal units, as Finder shows them."""
    for unit, limit in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= limit * 0.9995:
            v = n / limit
            return f"{v:.1f} {unit}" if v < 99.95 else f"{v:.0f} {unit}"
    return f"{n} B"


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
    cells = max(0.0, min(1.0, fraction)) * width
    full = int(cells)
    part = BARS[int((cells - full) * 8)] if full < width else ""
    return ("█" * full + part.strip()).ljust(width)


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


LANG_SORTS = [("code", 4), ("lines", 3), ("files", 2), ("comment", 5), ("name", 0)]
FILE_SORTS = [("lines", "lines"), ("code", "code"), ("comment", "comment"), ("size", "bytes"), ("path", "path")]


class App:
    def __init__(self, stdscr, scan):
        self.stdscr, self.scan = stdscr, scan
        self.view = "languages"
        self.lang = None                   # the language filter of the files, words and metrics views
        self.sel = dict.fromkeys(VIEWS, 0)
        self.top = dict.fromkeys(VIEWS, 0)
        self.lang_sort = 0
        self.file_sort = 0
        self.stop = True                   # hide English stop words
        self.keywords = True               # hide programming-language keywords
        self.min_len = 3
        self.cache = {}                    # (what, key) -> (files seen, value)
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.page = 1

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    def cached(self, what, key, make):
        """Recomputed when more files are in, at most once a second while the scan runs."""
        stamp = len(self.scan.files)
        hit = self.cache.get((what, key))
        if hit and (hit[0] == stamp or (self.scan.busy and time.monotonic() - hit[1] < 1)):
            return hit[2]
        value = make()
        self.cache[(what, key)] = (stamp, time.monotonic(), value)
        return value

    def files(self):
        files = self.scan.snapshot()
        return files if self.lang is None else [f for f in files if f.lang.name == self.lang]

    # -- rows of each view --

    def lang_rows(self):
        name, col = LANG_SORTS[self.lang_sort]
        rows = self.cached("langs", None, lambda: languages(self.scan.snapshot()))
        return sorted(rows, key=lambda r: r[0].lower() if col == 0 else -r[col])

    def file_rows(self):
        name, attr = FILE_SORTS[self.file_sort]
        files = self.files()
        return sorted(files, key=lambda f: f.path.lower() if attr == "path" else -getattr(f, attr))

    def word_rows(self):
        counts = self.cached("words", self.lang, lambda: self.scan.word_counts(self.lang))
        keywords = self.keywords and not prose(self.lang)
        return self.cached("wordrows", (self.lang, self.stop, keywords, self.min_len), lambda: [
            (w, c) for w, c in counts.most_common() if len(w) >= self.min_len
            and not (self.stop and w in STOPWORDS) and not (keywords and w in KEYWORDS)])

    def metric_rows(self):
        def make():
            rows = []
            for section, items in metrics(self.files(), self.scan.word_counts(self.lang)):
                rows.append((section, None))
                rows += items
                rows.append(("", None))
            return rows
        return self.cached("metrics", self.lang, make)

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.draw_header(w)
        self.page = h - 5
        self.shown = ""
        getattr(self, "draw_" + self.view)(3, h - 4, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_header(self, w):
        sc = self.scan
        self.put(0, 0, " Text stats ", curses.color_pair(1) | curses.A_BOLD)
        if sc.busy:
            state, pair = f"scanning… {len(sc.files):,} files", 3
        else:
            skipped = sum(sc.skipped.values())
            state = f"{plural(len(sc.files), 'file')} in {sc.elapsed:.1f}s" + (f" · {skipped:,} skipped" if skipped else "")
            pair = 5
        self.put(0, w - cwidth(state) - 1, state, curses.color_pair(pair))
        label = short_path(sc.root) + ("  (git)" if sc.source == "git" else "")
        self.put(0, 12, fit_left(label, w - 15 - cwidth(state)), curses.A_BOLD, w - 15 - cwidth(state))
        x = 1
        for i, v in enumerate(VIEWS, 1):
            text = f" {i} {v.capitalize()} "
            on = v == self.view
            self.put(1, x, text, (curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD) if on else curses.color_pair(5),
                     len(text))
            x += len(text) + 1
        if self.lang and self.view != "languages":
            self.put(1, x + 1, f"only {self.lang}  (Esc: all)", curses.color_pair(3))

    def scrolled(self, view, rows, height):
        n = len(rows)
        self.sel[view] = max(0, min(self.sel[view], n - 1))
        sel, top = self.sel[view], self.top[view]
        if sel < top:
            top = sel
        elif sel >= top + height:
            top = sel - height + 1
        top = max(0, min(top, n - height))
        self.top[view] = top
        if n > height:
            self.shown = f"{top + 1}–{min(n, top + height)} of {n:,}"
        return top

    def draw_languages(self, y0, height, w):
        rows = self.lang_rows()
        cols = [("Files", 7, 2), ("Lines", 11, 3), ("Code", 11, 4), ("Comment", 9, 5), ("Blank", 8, 6)]
        size_w = 10 if w >= 80 else 0      # a narrow window drops the size and the bar
        name_w = max(10, min(22, w - 2 - sum(c[1] for c in cols) - size_w - 20))
        bar_w = w - 2 - name_w - sum(c[1] for c in cols) - size_w if size_w else 0
        sort_col = LANG_SORTS[self.lang_sort][1]
        x = 1 + name_w
        self.put(y0, 1, "Language", curses.color_pair(5) | (curses.A_BOLD if sort_col == 0 else 0), name_w)
        for title, cw, col in cols:
            self.put(y0, x, title.rjust(cw), curses.color_pair(5) | (curses.A_BOLD if col == sort_col else 0), cw)
            x += cw
        self.put(y0, x, "Size".rjust(size_w), curses.color_pair(5), size_w)
        total_code = sum(r[4] for r in rows) or 1
        body = height - 2
        top = self.scrolled("languages", rows, body)
        for i, r in enumerate(rows[top:top + body]):
            y = y0 + 1 + i
            chosen = top + i == self.sel["languages"]
            base = curses.A_REVERSE if chosen else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 1, r[0], base | curses.A_BOLD, name_w)
            x = 1 + name_w
            for _, cw, col in cols:
                self.put(y, x, f"{r[col]:,}".rjust(cw), base | (curses.color_pair(5) if col in (5, 6) else 0), cw)
                x += cw
            self.put(y, x, fmt_size(r[7]).rjust(size_w), base | curses.color_pair(5), size_w)
            if bar_w > 3:
                self.put(y, x + 11, bar(r[4] / total_code, bar_w - 1), base | curses.color_pair(1), bar_w - 1)
        if rows:
            y = y0 + 1 + min(body, len(rows))
            tot = [sum(r[c] for r in rows) for c in range(2, 8)]
            self.put(y, 1, "Total", curses.A_BOLD | curses.color_pair(1), name_w)
            x = 1 + name_w
            for (_, cw, col), v in zip(cols, tot):
                self.put(y, x, f"{v:,}".rjust(cw), curses.A_BOLD, cw)
                x += cw
            self.put(y, x, fmt_size(tot[5]).rjust(size_w), curses.A_BOLD, size_w)
        elif not self.scan.busy:
            self.put(y0 + 2, 2, "no text files here", curses.color_pair(5))

    def draw_files(self, y0, height, w):
        rows = self.file_rows()
        cols = [("Lines", 8, "lines"), ("Code", 8, "code"), ("Comment", 8, "comment"), ("Blank", 7, "blank")]
        lang_w = 12 if self.lang is None and w >= 80 else 0
        path_w = w - 2 - lang_w - sum(c[1] for c in cols) - 9
        sort_attr = FILE_SORTS[self.file_sort][1]
        self.put(y0, 1, "Path", curses.color_pair(5) | (curses.A_BOLD if sort_attr == "path" else 0), path_w)
        x = 1 + path_w
        if lang_w:
            self.put(y0, x + 1, "Language", curses.color_pair(5), lang_w - 1)
            x += lang_w
        for title, cw, attr in cols:
            self.put(y0, x, title.rjust(cw), curses.color_pair(5) | (curses.A_BOLD if attr == sort_attr else 0), cw)
            x += cw
        self.put(y0, x, "Size".rjust(9), curses.color_pair(5) | (curses.A_BOLD if sort_attr == "bytes" else 0), 9)
        body = height - 1
        top = self.scrolled("files", rows, body)
        for i, f in enumerate(rows[top:top + body]):
            y = y0 + 1 + i
            base = curses.A_REVERSE if top + i == self.sel["files"] else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 1, fit_left(f.path, path_w - 1), base, path_w - 1)
            x = 1 + path_w
            if lang_w:
                self.put(y, x + 1, f.lang.name, base | curses.color_pair(5), lang_w - 1)
                x += lang_w
            for _, cw, attr in cols:
                self.put(y, x, f"{getattr(f, attr):,}".rjust(cw), base | (curses.color_pair(5) if attr in ("comment", "blank") else 0), cw)
                x += cw
            self.put(y, x, fmt_size(f.bytes).rjust(9), base | curses.color_pair(5), 9)

    def draw_words(self, y0, height, w):
        rows = self.word_rows()
        counts = self.cached("words", self.lang, lambda: self.scan.word_counts(self.lang))
        total = sum(counts.values())
        keywords = self.keywords and not prose(self.lang)
        hidden = " and ".join(filter(None, ["stop words" if self.stop else "", "keywords" if keywords else ""]))
        info = (f"{total:,} words · {len(counts):,} distinct · shown: {self.min_len}+ letters"
                f"{', without ' + hidden if hidden else ''}")
        self.put(y0, 1, info, curses.color_pair(5))
        body = height - 1
        top = self.scrolled("words", rows, body)
        if not rows:
            if not self.scan.busy:
                self.put(y0 + 2, 2, "no words", curses.color_pair(5))
            return
        peak = rows[0][1]
        word_w = min(24, max(10, max(cwidth(r[0]) for r in rows[top:top + body]) + 1))
        bar_w = w - 1 - 7 - word_w - 10 - 8 - 2
        for i, (word, c) in enumerate(rows[top:top + body]):
            y = y0 + 1 + i
            rank = top + i
            base = curses.A_REVERSE if rank == self.sel["words"] else 0
            self.put(y, 0, " " * w, base)
            self.put(y, 1, f"{rank + 1:>5}", base | curses.color_pair(5), 6)
            self.put(y, 8, word, base | curses.A_BOLD, word_w)
            self.put(y, 8 + word_w, f"{c:,}".rjust(9), base, 9)
            self.put(y, 8 + word_w + 10, f"{c / total:.2%}".rjust(7) if total else "", base | curses.color_pair(5), 7)
            if bar_w > 3:
                self.put(y, 8 + word_w + 19, bar(c / peak, bar_w), base | curses.color_pair(1), bar_w)

    def draw_metrics(self, y0, height, w):
        rows = self.metric_rows()
        if not rows:
            if not self.scan.busy:
                self.put(y0 + 1, 2, "no text files here", curses.color_pair(5))
            return
        self.sel["metrics"] = max(0, min(self.sel["metrics"], max(0, len(rows) - height)))
        top = self.sel["metrics"]
        if len(rows) > height:
            self.shown = f"{top + 1}–{min(len(rows), top + height)} of {len(rows)}"
        label_w = max(cwidth(r[0]) for r in rows if r[1] is not None) + 3
        for i, (label, value) in enumerate(rows[top:top + height]):
            y = y0 + i
            if value is None:
                self.put(y, 1, label, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.put(y, 3, label, curses.color_pair(5), label_w)
                self.put(y, 3 + label_w, value, 0)

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = {"languages": ["↑↓ move", "⏎ files", "w words", "m metrics", f"s sort:{LANG_SORTS[self.lang_sort][0]}"],
                "files": ["↑↓ move", f"s sort:{FILE_SORTS[self.file_sort][0]}", "l language", "o Finder", "Esc all"],
                "words": ["↑↓ move", "l language", f"x stop words:{'hidden' if self.stop else 'shown'}",
                          f"c keywords:{'hidden' if self.keywords else 'shown'}", f"+- min {self.min_len}", "/ find",
                          "Esc all"],
                "metrics": ["↑↓ scroll", "l language", "Esc all"]}[self.view] + ["Tab view"]
        room = w - 2 - (len(self.shown) + 2 if self.shown else 0)
        while len(keys) > 1 and cwidth("  ".join(keys + ["q quit"])) > room:
            keys.pop()                     # the least needed go first in a narrow window
        self.put(y, 0, " " + "  ".join(keys + ["q quit"]), curses.color_pair(5))
        if self.shown:
            self.put(y, w - len(self.shown) - 1, self.shown, curses.color_pair(1))

    # -- actions --

    def prompt(self, label):
        curses.curs_set(1)
        buf = []
        try:
            while True:
                h, w = self.stdscr.getmaxyx()
                text = label + "".join(buf)
                self.put(h - 1, 0, text, curses.color_pair(1) | curses.A_REVERSE)
                self.stdscr.move(h - 1, min(cwidth(text), w - 2))
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
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf).strip()
        finally:
            curses.curs_set(0)

    def find_word(self):
        text = self.prompt(" find word: ")
        if not text:
            return
        word = text.lower()
        rows = self.word_rows()
        for i, (w, c) in enumerate(rows):
            if w == word:
                self.sel["words"] = i
                self.say(f"“{w}”: {c:,} times, #{i + 1}")
                return
        counts = self.cached("words", self.lang, lambda: self.scan.word_counts(self.lang))
        if counts.get(word):
            self.say(f"“{word}”: {counts[word]:,} times — hidden by the stop-word or length filter")
        else:
            self.say(f"“{word}” doesn't occur", curses.color_pair(4))

    def cycle_lang(self):
        names = [r[0] for r in sorted(self.lang_rows(), key=lambda r: -r[4])]
        order = [None] + names
        self.lang = order[(order.index(self.lang) + 1) % len(order)] if self.lang in order else None
        self.sel.update(files=0, words=0, metrics=0)

    def reveal(self, f):
        path = os.path.join(self.scan.root if os.path.isdir(self.scan.root) else os.path.dirname(self.scan.root), f.path)
        try:
            subprocess.run(["open", "-R", path], check=True, capture_output=True)
            self.say(f"shown in Finder: {f.path}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't open Finder", curses.color_pair(4))

    def handle(self, ch):
        v = self.view
        page = max(1, self.page)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                 curses.KEY_PPAGE: -page, curses.KEY_NPAGE: page,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in steps:
            self.sel[v] = max(0, self.sel[v] + steps[ch])
        elif ch in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.view = VIEWS[ch - ord("1")]
        elif ch == ord("\t"):
            self.view = VIEWS[(VIEWS.index(v) + 1) % len(VIEWS)]
        elif ch == curses.KEY_BTAB:
            self.view = VIEWS[(VIEWS.index(v) - 1) % len(VIEWS)]
        elif ch == 27:
            self.lang = None
        elif v == "languages":
            rows = self.lang_rows()
            chosen = rows[min(self.sel[v], len(rows) - 1)][0] if rows else None
            if ch in (curses.KEY_ENTER, 10, 13, ord("w"), ord("m")) and chosen:
                self.lang = chosen
                self.view = {ord("w"): "words", ord("m"): "metrics"}.get(ch, "files")
                self.sel.update(files=0, words=0, metrics=0)
            elif ch == ord("s"):
                self.lang_sort = (self.lang_sort + 1) % len(LANG_SORTS)
        elif ch == ord("l"):
            self.cycle_lang()
        elif v == "files":
            if ch == ord("s"):
                self.file_sort = (self.file_sort + 1) % len(FILE_SORTS)
            elif ch in (ord("o"), curses.KEY_ENTER, 10, 13):
                rows = self.file_rows()
                if rows:
                    self.reveal(rows[min(self.sel[v], len(rows) - 1)])
        elif v == "words":
            if ch == ord("x"):
                self.stop = not self.stop
            elif ch == ord("c"):
                self.keywords = not self.keywords
            elif ch in (ord("+"), ord("=")):
                self.min_len = min(15, self.min_len + 1)
            elif ch in (ord("-"), ord("_")):
                self.min_len = max(1, self.min_len - 1)
            elif ch == ord("/"):
                self.find_word()
        return True


def run(stdscr, scan):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, bars, sections
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # scanning, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    stdscr.keypad(True)
    app = App(stdscr, scan)
    while True:
        stdscr.timeout(TICK_MS if scan.busy else -1)
        app.draw()
        ch = stdscr.getch()
        if ch != -1 and ch != curses.KEY_RESIZE and not app.handle(ch):
            return


def print_once(scan, top, lang):
    tty = sys.stdout.isatty()
    bold, dim, cyan, reset = ("\033[1m", "\033[2m", "\033[36m", "\033[0m") if tty else ("", "", "", "")
    while scan.busy:
        time.sleep(0.05)
    files = scan.files if lang is None else [f for f in scan.files if f.lang.name == lang]
    rows = sorted(languages(files), key=lambda r: -r[4])
    skipped = ", ".join(f"{n} {why}" for why, n in scan.skipped.most_common())
    print(f"{dim}{short_path(scan.root)} · {plural(len(scan.files), 'file')} ({scan.source}) in {scan.elapsed:.2f}s"
          + (f" · skipped {skipped}" if skipped else "") + reset)
    print(f"\n{bold}{'Language':<18}{'Files':>7}{'Lines':>11}{'Code':>11}{'Comment':>10}{'Blank':>9}{'Size':>10}{reset}")
    for r in rows:
        print(f"{r[0]:<18}{r[2]:>7,}{r[3]:>11,}{r[4]:>11,}{r[5]:>10,}{r[6]:>9,}{fmt_size(r[7]):>10}")
    tot = [sum(r[c] for r in rows) for c in range(2, 8)]
    print(f"{bold}{'Total':<18}{tot[0]:>7,}{tot[1]:>11,}{tot[2]:>11,}{tot[3]:>10,}{tot[4]:>9,}{fmt_size(tot[5]):>10}{reset}")
    counts = scan.word_counts(lang)
    keywords = KEYWORDS if not prose(lang) else set()
    words = [(w, c) for w, c in counts.most_common()
             if len(w) >= 3 and w not in STOPWORDS and w not in keywords][:top]
    if words:
        print(f"\n{bold}Top words{reset} {dim}(3+ letters, without stop words{' and keywords' if keywords else ''}){reset}")
        width = max(len(w) for w, _ in words)
        for w, c in words:
            print(f"  {w:<{width}}  {c:>8,}")
    for section, items in metrics(files, counts):
        print(f"\n{cyan}{bold}{section}{reset}")
        label_w = max(len(k) for k, _ in items)
        for k, v in items:
            print(f"  {dim}{k:<{label_w}}{reset}  {v}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Lines of code per language, word frequency and other metrics of a folder of text files.")
    parser.add_argument("path", nargs="?", default=".", help="folder or file (default: the current folder)")
    parser.add_argument("--no-git", action="store_true",
                        help="walk the folder even in a git repository, instead of using git's file list")
    parser.add_argument("--once", action="store_true", help="print the tables and metrics and exit")
    parser.add_argument("--top", type=int, default=20, metavar="N", help="words to print with --once")
    parser.add_argument("--lang", metavar="NAME", help="with --once, only this language (e.g. Python)")
    args = parser.parse_args()
    path = os.path.realpath(os.path.expanduser(args.path))
    if not os.path.exists(path):
        parser.error(f"no such file or folder: {args.path}")
    if args.lang and args.lang not in LANGS:
        match = [n for n in LANGS if n.lower() == args.lang.lower()]
        if not match:
            parser.error(f"unknown language {args.lang!r}; one of: {', '.join(LANGS)}")
        args.lang = match[0]
    scan = Scan(path, use_git=not args.no_git)
    if args.once:
        raise SystemExit(print_once(scan, args.top, args.lang))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, scan)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
