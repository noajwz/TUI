#!/usr/bin/env python3
"""Markdown reader — renders a .md file in the terminal the way a browser would show it: headings,
emphasis, lists and task lists, block quotes and GitHub's note/warning boxes, code blocks, tables
drawn as grids, footnotes and links you can follow, to other Markdown files and back.

The file is watched while it's open and drawn again when it changes, so it works as a live preview
next to an editor. A folder opens its README. `-` reads standard input; `--once` prints the
rendered text instead (plain when piped).
"""

import argparse
import curses
import html
import locale
import os
import re
import subprocess
import sys
import time
import unicodedata

MAX_WIDTH = 100          # text is never wider than this unless `w` lifts it
CHECK_SECONDS = 1        # how often the file is checked for changes
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 30, 6
MD_EXTS = (".md", ".markdown", ".mdown", ".mkd", ".mdx")
BULLETS = "•◦▪"

# inline styles, as bit flags on a span
BOLD, ITALIC, CODE, STRIKE, LINK, IMAGE, NOTE = 1, 2, 4, 8, 16, 32, 64

ALERTS = {"NOTE": ("ⓘ Note", "note"), "TIP": ("✓ Tip", "tip"), "IMPORTANT": ("! Important", "important"),
          "WARNING": ("⚠ Warning", "warning"), "CAUTION": ("✖ Caution", "caution")}
LINE_COMMENTS = {"#": "python py sh bash zsh shell fish yaml yml toml ruby rb r perl make makefile dockerfile "
                      "conf ini nix elixir julia powershell ps1 cmake",
                 "//": "c cpp c++ h hpp cs java js javascript ts typescript jsx tsx go rust rs swift kotlin kt "
                       "scala dart php zig groovy jsonc",
                 "--": "sql lua haskell hs elm ada applescript",
                 ";": "lisp clojure scheme asm nasm ahk autohotkey ini"}
COMMENT_OF = {lang: mark for mark, langs in LINE_COMMENTS.items() for lang in langs.split()}

FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$")
ATX = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT = re.compile(r" {0,3}(=+|-+)[ \t]*$")
HR = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
BULLET = re.compile(r"( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)")
QUOTE = re.compile(r" {0,3}> ?")
TABLE_SEP = re.compile(r" {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
HTML_START = re.compile(r" {0,3}<(/?[A-Za-z][A-Za-z0-9-]*[\s/>]|/?[A-Za-z][A-Za-z0-9-]*$|!--)")
REF_DEF = re.compile(r" {0,3}\[([^\]^][^\]]*)\]:[ \t]*<?([^\s>]+)>?(?:[ \t]+[\"'(].*[\"')])?[ \t]*$")
FOOT_DEF = re.compile(r" {0,3}\[\^([^\]]+)\]:[ \t]?(.*)")
TASK = re.compile(r"\[([ xX])\](?:[ \t]+|$)")
AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^\s<>]+|[\w.+-]+@[\w-]+\.[\w.-]+)>")
BARE_URL = re.compile(r"(?:https?://|www\.)[^\s<>]*[^\s<>.,:;!?\"')\]*_~]")
TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^>]*)?)/?>")
PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


# -- parsing: text -> blocks --

class Block:
    def __init__(self, kind, **fields):
        self.kind = kind
        self.__dict__.update(fields)


class Context:
    """What the whole file shares: link reference definitions and footnotes."""
    def __init__(self):
        self.refs = {}         # label (casefolded) -> url
        self.notes = {}        # label -> [Block]
        self.note_order = []   # labels in order of first reference


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def starts_block(line):
    """Whether a line interrupts a paragraph."""
    m = BULLET.match(line)
    if m and line[m.end():].strip() and (not m[2][0].isdigit() or m[2][:-1] == "1"):
        return True
    return bool(FENCE.match(line) or ATX.match(line) or HR.match(line) or QUOTE.match(line)
                or HTML_START.match(line))


def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, buf, code = [], [], False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if c == "`":
            code = not code
        if c == "|" and not code:
            cells.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def join_para(lines):
    """Paragraph lines into one string: soft breaks become spaces, hard breaks (two trailing spaces
    or a backslash) a newline."""
    out = []
    for k, line in enumerate(lines):
        s = line.strip() if k else line.lstrip()
        last = k == len(lines) - 1
        if not last and line.endswith("  "):
            out.append(s + "\n")
        elif not last and s.endswith("\\"):
            out.append(s[:-1] + "\n")
        else:
            out.append(s + ("" if last else " "))
    return "".join(out)


def html_attr(attrs, name):
    m = re.search(r"\b" + name + r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", attrs, re.I)
    return m[1].strip("\"'") if m else ""


def html_to_md(text):
    """An HTML block as Markdown text: images and links kept, block tags as line breaks, the rest
    of the tags dropped."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<img\b([^>]*)>", lambda m: f"![{html_attr(m[1], 'alt') or 'image'}]({html_attr(m[1], 'src')})",
                  text, flags=re.I)
    text = re.sub(r"<a\b([^>]*)>(.*?)</a>",
                  lambda m: f"[{m[2].strip()}]({html_attr(m[1], 'href')})" if html_attr(m[1], "href") else m[2],
                  text, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?(?:p|div|h\d|details|summary|li|ul|ol|table|tr|td|th|thead|tbody|picture|source|center)\b[^>]*>",
                  "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(s.strip() for s in text.split("\n") if s.strip())


def parse(text):
    """Markdown text -> ([Block], Context)."""
    ctx = Context()
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    blocks = []
    if lines and lines[0].strip() == "---":           # YAML front matter
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                blocks.append(Block("code", lang="front matter", text="\n".join(lines[1:j])))
                lines = lines[j + 1:]
                break
    blocks += parse_blocks(lines, ctx)
    return blocks, ctx


def parse_blocks(lines, ctx):
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = FENCE.match(line)
        if m:
            indent, fence, info = len(m[1]), m[2], m[3]
            close = re.compile(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
            body = []
            i += 1
            while i < n and not close.match(lines[i]):
                body.append(lines[i][min(indent, indent_of(lines[i])):])
                i += 1
            i += 1
            out.append(Block("code", lang=info, text="\n".join(body)))
            continue
        if line.startswith("    "):
            body = []
            while i < n and (lines[i].startswith("    ") or not lines[i].strip()):
                body.append(lines[i][4:])
                i += 1
            while body and not body[-1].strip():
                body.pop()
            out.append(Block("code", lang="", text="\n".join(body)))
            continue
        m = ATX.match(line)
        if m:
            out.append(Block("heading", level=len(m[1]), text=(m[2] or "").strip()))
            i += 1
            continue
        if HR.match(line):
            out.append(Block("hr"))
            i += 1
            continue
        if QUOTE.match(line):
            body = []
            while i < n and lines[i].strip():
                m = QUOTE.match(lines[i])
                if not m and (starts_block(lines[i]) or not body[-1].strip()):
                    break
                body.append(lines[i][m.end():] if m else lines[i])     # or a lazy continuation
                i += 1
            out.append(Block("quote", children=parse_blocks(body, ctx)))
            continue
        if BULLET.match(line):
            i = parse_list(lines, i, ctx, out)
            continue
        m = FOOT_DEF.match(line)
        if m:
            body = [m[2]]
            i += 1
            while i < n and (lines[i].startswith("    ") or (not lines[i].strip() and i + 1 < n
                                                              and lines[i + 1].startswith("    "))):
                body.append(lines[i][4:])
                i += 1
            ctx.notes[m[1]] = parse_blocks(body, ctx)
            continue
        m = REF_DEF.match(line)
        if m:
            ctx.refs.setdefault(m[1].strip().casefold(), m[2])
            i += 1
            continue
        if HTML_START.match(line):
            body = []
            comment = line.lstrip().startswith("<!--")
            while i < n and (lines[i].strip() or comment):
                body.append(lines[i])
                i += 1
                if comment and "-->" in body[-1]:
                    break
            text = html_to_md("\n".join(body))
            if text:
                out.append(Block("para", text=text))
            continue
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and TABLE_SEP.match(lines[i + 1]) \
                and len(split_row(line)) == len(split_row(lines[i + 1])):
            head = split_row(line)
            aligns = []
            for cell in split_row(lines[i + 1]):
                c = cell.strip()
                aligns.append("center" if c.startswith(":") and c.endswith(":") else
                              "right" if c.endswith(":") else "left")
            rows = []
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i] and not starts_block(lines[i]):
                cells = split_row(lines[i])
                rows.append((cells + [""] * len(head))[:len(head)])
                i += 1
            out.append(Block("table", head=head, aligns=aligns, rows=rows))
            continue
        body = [line]
        i += 1
        level = 0
        while i < n and lines[i].strip():
            m = SETEXT.match(lines[i])
            if m and not lines[i].startswith("    "):
                level = 1 if m[1][0] == "=" else 2
                i += 1
                break
            if starts_block(lines[i]):
                break
            body.append(lines[i])
            i += 1
        if level:
            out.append(Block("heading", level=level, text=" ".join(s.strip() for s in body)))
        else:
            out.append(Block("para", text=join_para(body)))
    return out


def parse_list(lines, i, ctx, out):
    n = len(lines)
    first = BULLET.match(lines[i])
    ordered = first[2][0].isdigit()
    mark = first[2][-1]
    start = int(first[2][:-1]) if ordered else 1
    items, loose = [], False
    while i < n:
        m = BULLET.match(lines[i])
        if not m or m[2][0].isdigit() != ordered or m[2][-1] != mark:
            break
        spaces = len(m[3])
        content = m.end() if 1 <= spaces <= 4 else len(m[1]) + len(m[2]) + 1
        body = [lines[i][content:]]
        i += 1
        while i < n:
            line = lines[i]
            if not line.strip():
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and indent_of(lines[j]) >= content:
                    body += [""] * (j - i)
                    i = j
                    continue
                break
            if indent_of(line) >= content:
                body.append(line[content:])
            elif body[-1].strip() and not starts_block(line) and not BULLET.match(line):
                body.append(line.strip())                # lazy continuation of a paragraph
            else:
                break
            i += 1
        task = None
        t = TASK.match(body[0])
        if t:
            task = t[1] != " "
            body[0] = body[0][t.end():]
        inner = body[:]
        while inner and not inner[-1].strip():
            inner.pop()
        if "" in inner:
            loose = True
        items.append(Block("item", children=parse_blocks(body, ctx), task=task))
        if i < n and not lines[i].strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            m = BULLET.match(lines[j]) if j < n else None
            if m and m[2][0].isdigit() == ordered and m[2][-1] == mark and indent_of(lines[j]) < 4:
                loose = True
                i = j
                continue
            break
    out.append(Block("list", ordered=ordered, start=start, items=items, loose=loose))
    return i


# -- inline: text -> spans --

class Link:
    def __init__(self, kind, target, label=""):
        self.kind, self.target, self.label = kind, target, label
        self.rects = []        # (line, col0, col1) where it's drawn


def find_close(text, i):
    """Index of the ] that closes the [ at i, skipping nested brackets, escapes and code."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            end = text.find("`", j + 1)
            j = end + 1 if end != -1 else j + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def parse_link(text, i, ctx):
    """[label](url "title"), [label][ref], [label][] or [label] at i -> (label, url, end) or None."""
    j = find_close(text, i)
    if j == -1:
        return None
    label = text[i + 1:j]
    if text.startswith("(", j + 1):
        depth, k = 0, j + 1
        while k < len(text):
            if text[k] == "\\":
                k += 2
                continue
            if text[k] == "(":
                depth += 1
            elif text[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= len(text):
            return None
        inner = text[j + 2:k].strip()
        if inner.startswith("<") and ">" in inner:
            url = inner[1:inner.index(">")]
        else:
            url = inner.split()[0] if inner else ""
        return label, url, k + 1
    if text.startswith("[", j + 1):
        k = text.find("]", j + 1)
        if k != -1:
            ref = (text[j + 2:k] or label).strip().casefold()
            if ref in ctx.refs:
                return label, ctx.refs[ref], k + 1
    if label.strip().casefold() in ctx.refs:
        return label, ctx.refs[label.strip().casefold()], j + 1
    return None


def emphasis(text, i):
    """**bold**, __bold__, *italic*, _italic_ or ~~struck~~ starting at i -> (inner, flag, end) or None."""
    for delim, flag in (("**", BOLD), ("__", BOLD), ("~~", STRIKE), ("*", ITALIC), ("_", ITALIC)):
        if not text.startswith(delim, i):
            continue
        if delim[0] == "_" and i > 0 and text[i - 1].isalnum():
            return None                                  # snake_case
        start = i + len(delim)
        if start >= len(text) or text[start].isspace():
            continue
        j = start
        while True:
            j = text.find(delim, j + 1)
            if j == -1:
                break
            if len(delim) == 1 and (text.startswith(delim, j + 1) or text[j - 1] == delim):
                j += 1                                   # part of a ** run, not ours
                continue
            if text[j - 1].isspace():
                continue
            while text.startswith(delim, j + 1):
                j += 1                                   # ***x*** closes on the last of the run
            if delim[0] == "_" and j + len(delim) < len(text) and text[j + len(delim)].isalnum():
                continue
            return text[start:j], flag, j + len(delim)
    return None


def inline(text, ctx, flags=0, link=None):
    """Inline Markdown -> [(text, flags, Link or None)]; "\n" spans are hard line breaks."""
    out, buf = [], []
    i, n = 0, len(text)

    def flush():
        if buf:
            out.append((html.unescape("".join(buf)), flags, link))
            buf.clear()
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in PUNCT:
            buf.append(text[i + 1])
            i += 2
            continue
        if c == "\n":
            flush()
            out.append(("\n", flags, link))
            i += 1
            continue
        if c == "`":
            ticks = re.match(r"`+", text[i:])[0]
            m = re.compile(r"(?<!`)" + ticks + r"(?!`)").search(text, i + len(ticks))
            if m:
                flush()
                code = text[i + len(ticks):m.start()].replace("\n", " ")
                if code.startswith(" ") and code.endswith(" ") and code.strip():
                    code = code[1:-1]
                out.append((code, flags | CODE, link))
                i = m.end()
                continue
            buf.append(ticks)
            i += len(ticks)
            continue
        if c == "!" and text.startswith("[", i + 1):
            r = parse_link(text, i + 1, ctx)
            if r:
                flush()
                alt = re.sub(r"[*_`]", "", r[0]).strip() or os.path.basename(r[1]) or "image"
                out.append(("▣ " + alt, flags | IMAGE, Link("image", r[1], alt) if link is None else link))
                i = r[2]
                continue
        if c == "[":
            m = re.match(r"\[\^([^\]\s]+)\]", text[i:])
            if m and m[1] in ctx.notes:
                flush()
                if m[1] not in ctx.note_order:
                    ctx.note_order.append(m[1])
                out.append((f"[{ctx.note_order.index(m[1]) + 1}]", flags | NOTE, Link("anchor", "#fn-" + m[1])))
                i += m.end()
                continue
            r = parse_link(text, i, ctx) if link is None else None
            if r:
                flush()
                inner = Link("link", r[1], r[0])
                out += inline(r[0], ctx, flags | LINK, inner) or [(r[1], flags | LINK, inner)]
                i = r[2]
                continue
        if c == "<":
            if text.startswith("<!--", i):
                end = text.find("-->", i)
                if end != -1:
                    i = end + 3
                    continue
            m = AUTOLINK.match(text, i)
            if m and link is None:
                flush()
                url = m[1] if ":" in m[1] else "mailto:" + m[1]
                out.append((m[1], flags | LINK, Link("link", url)))
                i = m.end()
                continue
            m = TAG.match(text, i)
            if m:
                flush()
                name = m[2].lower()
                if name == "br":
                    out.append(("\n", flags, link))
                elif name == "img":
                    alt = html_attr(m[3], "alt") or "image"
                    out.append(("▣ " + alt, flags | IMAGE, Link("image", html_attr(m[3], "src"), alt) if link is None else link))
                i = m.end()
                continue
        if c in "*_~":
            r = emphasis(text, i)
            if r:
                flush()
                out += inline(r[0], ctx, flags | r[1], link)
                i = r[2]
                continue
            run = re.match(r"[*_~]+", text[i:])[0]   # an unmatched run stays as it is
            buf.append(run)
            i += len(run)
            continue
        if c in "hw" and link is None and (i == 0 or not text[i - 1].isalnum()):
            m = BARE_URL.match(text, i)
            if m:
                flush()
                url = m[0] if m[0].startswith("http") else "https://" + m[0]
                out.append((m[0], flags | LINK, Link("link", url)))
                i = m.end()
                continue
        buf.append(c)
        i += 1
    flush()
    return out


def plain(spans):
    return "".join(t for t, _, _ in spans).replace("\n", " ")


def slugify(text):
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    s = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return s.replace(" ", "-")


# -- layout: blocks -> lines of segments --

def cwidth(text):
    if text.isascii():
        return len(text)
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


def chop(text, width):
    """Split a word that's wider than the line into pieces that fit."""
    pieces, cur, used = [], [], 0
    for ch in text:
        w = cwidth(ch)
        if used + w > width and cur:
            pieces.append("".join(cur))
            cur, used = [], 0
        cur.append(ch)
        used += w
    pieces.append("".join(cur))
    return pieces


class Line:
    """One screen line: segments of (text, kind, flags, Link or None), plus what it starts."""
    __slots__ = ("segs", "heading", "anchors", "block")

    def __init__(self, segs=None, heading=None, anchors=None):
        self.segs = segs or []
        self.heading = heading         # (level, title) of a heading's first line
        self.anchors = anchors or []   # anchor names that land here
        self.block = 0                 # the top-level block it belongs to

    def width(self):
        return sum(cwidth(s[0]) for s in self.segs)

    def text(self):
        return "".join(s[0] for s in self.segs)


def wrap_spans(spans, width, kind):
    """Word-wrap spans to `width`, keeping each word's styles; returns [Line]."""
    width = max(1, width)
    tokens, word = [], []                # ("w", pieces) | ("s", piece) | ("br",)
    for text, flags, link in spans:
        if text == "\n":
            if word:
                tokens.append(("w", word))
                word = []
            tokens.append(("br",))
            continue
        for part in re.split(r"( +)", text):
            if not part:
                continue
            if part.startswith(" "):
                if word:
                    tokens.append(("w", word))
                    word = []
                tokens.append(("s", (" ", kind, flags, link)))
            else:
                word.append((part, kind, flags, link))
    if word:
        tokens.append(("w", word))
    lines, cur, used, space = [], [], 0, None
    for tok in tokens:
        if tok[0] == "br":
            lines.append(Line(cur))
            cur, used, space = [], 0, None
        elif tok[0] == "s":
            space = tok[1] if cur else None
        else:
            ww = sum(cwidth(p[0]) for p in tok[1])
            if cur and used + 1 + ww > width:
                lines.append(Line(cur))
                cur, used, space = [], 0, None
            if space:
                cur.append(space)
                used += 1
            space = None
            if ww <= width - used:
                cur += tok[1]
                used += ww
                continue
            for p in tok[1]:                              # a word too long for any line
                for piece in chop(p[0], width):
                    pw = cwidth(piece)
                    if used + pw > width and cur:
                        lines.append(Line(cur))
                        cur, used = [], 0
                    cur.append((piece,) + p[1:])
                    used += pw
    if cur or not lines:
        lines.append(Line(cur))
    return lines


def prefixed(lines, first, rest):
    """Put `first` segments before the first line and `rest` before the others."""
    for k, line in enumerate(lines):
        line.segs = list(first if k == 0 else rest) + line.segs
    return lines


class Doc:
    def __init__(self, lines):
        self.lines = lines
        self.links, self.headings, self.anchors = [], [], {}
        self.block_start = {}
        for n, line in enumerate(lines):
            self.block_start.setdefault(line.block, n)
            if line.heading:
                self.headings.append((n,) + line.heading)
            for a in line.anchors:
                self.anchors.setdefault(a, n)
            col = 0
            for text, _, _, link in line.segs:
                w = cwidth(text)
                if link is not None:
                    if not link.rects:
                        self.links.append(link)
                    if link.rects and link.rects[-1][0] == n and link.rects[-1][2] == col:
                        link.rects[-1] = (n, link.rects[-1][1], col + w)
                    else:
                        link.rects.append((n, col, col + w))
                col += w
        self.texts = [line.text() for line in lines]


def layout(blocks, ctx, width):
    """Blocks -> Doc at a text width."""
    lines = []
    slugs = {}
    for b, block in enumerate(blocks):
        if lines:
            lines.append(Line())
        for line in render(block, ctx, width, 0, slugs):
            line.block = b
            lines.append(line)
    if ctx.note_order:
        lines.append(Line())
        lines.append(Line([("─" * min(20, width), "rule", 0, None)]))
        for k, label in enumerate(ctx.note_order, 1):
            mark = f"[{k}] "
            body = []
            for child in ctx.notes.get(label, []):
                body += render(child, ctx, width - len(mark), 0, slugs)
            body = prefixed(body or [Line()], [(mark, "note", 0, None)], [(" " * len(mark), "p", 0, None)])
            body[0].anchors.append("fn-" + label)
            for line in body:
                line.block = len(blocks)
            lines += body
    return Doc(lines)


def render(block, ctx, width, depth, slugs):
    """One block -> [Line] at `width`; `depth` is the list nesting for bullet shapes."""
    width = max(4, width)
    k = block.kind
    if k == "para":
        return wrap_spans(inline(block.text, ctx), width, "p")
    if k == "heading":
        spans = inline(block.text, ctx)
        kind = f"h{min(block.level, 4)}"
        out = wrap_spans(spans, width, kind)
        title = plain(spans).strip()
        slug = slugify(title)
        if slug in slugs:
            slugs[slug] += 1
            slug = f"{slug}-{slugs[slug]}"
        else:
            slugs[slug] = 0
        out[0].heading = (block.level, title)
        out[0].anchors.append(slug)
        if block.level <= 2:
            out.append(Line([(("═" if block.level == 1 else "─") * width, "h1rule" if block.level == 1 else "rule", 0, None)]))
        return out
    if k == "hr":
        return [Line([("─" * width, "rule", 0, None)])]
    if k == "code":
        return render_code(block, width)
    if k == "quote":
        children = block.children
        bar = "quote"
        out = []
        if children and children[0].kind == "para":
            m = re.match(r"\[!(\w+)\][ \t]*\n?", children[0].text)
            if m and m[1].upper() in ALERTS:
                label, bar = ALERTS[m[1].upper()]
                out.append(Line([(label, bar, BOLD, None)]))
                rest = children[0].text[m.end():].strip()
                children = ([Block("para", text=rest)] if rest else []) + children[1:]
        for n, child in enumerate(children):
            if n or out and child.kind != "para":
                out.append(Line())
            out += render(child, ctx, width - 2, depth, slugs)
        return prefixed(out or [Line()], [("│ ", bar, 0, None)], [("│ ", bar, 0, None)])
    if k == "list":
        out = []
        last = block.start + len(block.items) - 1
        mark_w = len(f"{last}.") + 1 if block.ordered else 2
        for n, item in enumerate(block.items):
            if n and block.loose:
                out.append(Line())
            if block.ordered:
                mark = (f"{block.start + n}." if block.start + n >= 0 else "-").rjust(mark_w - 1) + " "
                marks = [(mark, "bullet", 0, None)]
            else:
                marks = [(BULLETS[depth % len(BULLETS)] + " ", "bullet", 0, None)]
            if item.task is not None:
                marks.append(("☑ " if item.task else "☐ ", "done" if item.task else "bullet", 0, None))
            mw = sum(cwidth(m[0]) for m in marks)
            body = []
            for c, child in enumerate(item.children):
                if c and block.loose:
                    body.append(Line())
                body += render(child, ctx, width - mw, depth + 1, slugs)
            if item.task:
                for line in body:
                    line.segs = [(t, kind, flags | STRIKE, lk) if kind == "p" else (t, kind, flags, lk)
                                 for t, kind, flags, lk in line.segs]
            out += prefixed(body or [Line()], marks, [(" " * mw, "p", 0, None)])
        return out
    if k == "table":
        return render_table(block, ctx, width)
    return []


def render_code(block, width):
    mark = COMMENT_OF.get(block.lang.lower())
    out = []
    if block.lang:
        out.append(Line([("│ ", "codebar", 0, None), (block.lang, "codelang", 0, None)]))
    link = Link("code", block.text, block.lang or "code")
    for raw in block.text.split("\n") or [""]:
        kind = "comment" if mark and raw.lstrip().startswith(mark) else "code"
        pieces = chop(raw, width - 2) if cwidth(raw) > width - 2 else [raw]
        for n, piece in enumerate(pieces):
            out.append(Line([("│ " if n == 0 else "┆ ", "codebar", 0, link), (piece, kind, 0, None)]))
    return out


def render_table(block, ctx, width):
    ncols = len(block.head)
    cells = [[inline(c, ctx) for c in block.head]] + [[inline(c, ctx) for c in row] for row in block.rows]
    natural = [max(1, max(cwidth(plain(row[c])) for row in cells)) for c in range(ncols)]
    room = width - 3 * ncols - 1
    widths = natural[:]
    while sum(widths) > room and max(widths) > 3:
        widest = widths.index(max(widths))
        widths[widest] -= 1
    out = []

    def border(left, mid, right):
        return Line([(left + mid.join("─" * (w + 2) for w in widths) + right, "rule", 0, None)])

    def row_lines(row, kind):
        wrapped = [wrap_spans(row[c], widths[c], kind) for c in range(ncols)]
        height = max(len(w) for w in wrapped)
        lines = []
        for r in range(height):
            segs = [("│ ", "rule", 0, None)]
            for c in range(ncols):
                cell = wrapped[c][r] if r < len(wrapped[c]) else Line()
                pad = max(0, widths[c] - cell.width())
                left = pad // 2 if block.aligns[c] == "center" else pad if block.aligns[c] == "right" else 0
                if left:
                    segs.append((" " * left, kind, 0, None))
                segs += cell.segs
                segs.append((" " * (pad - left) + (" │ " if c < ncols - 1 else " │"), "rule", 0, None))
            lines.append(Line(segs))
        return lines
    out.append(border("┌", "┬", "┐"))
    out += row_lines(cells[0], "th")
    out.append(border("├", "┼", "┤"))
    body = [row_lines(row, "p") for row in cells[1:]]
    tall = any(len(r) > 1 for r in body)
    for n, r in enumerate(body):
        if n and tall:
            out.append(border("├", "┼", "┤"))
        out += r
    out.append(border("└", "┴", "┘"))
    return out


# -- the app --

HELP = [
    ("Reading", None),
    ("↑↓ jk", "scroll a line"),
    ("Space PgDn f  b PgUp", "a page down or up"),
    ("d u", "half a page down or up"),
    ("g G  Home End", "top and bottom"),
    ("] [", "next and previous heading"),
    ("t", "contents — the headings, ⏎ to jump"),
    ("w", "full window width, or at most %d columns" % MAX_WIDTH),
    ("Links and code", None),
    ("Tab ⇧Tab", "select the next or previous link or code block"),
    ("⏎  click", "follow the link (Markdown files open here) or copy the code block"),
    ("Backspace ←", "back to where you were"),
    ("y", "copy the link's address, or the code block"),
    ("Finding", None),
    ("/  n N", "search, next and previous match"),
    ("Other", None),
    ("e", "edit the file in $EDITOR"),
    ("o", "open another file"),
    ("r", "read the file again (it also does so on its own when the file changes)"),
    ("q", "quit, or back from a page"),
]


def resolve(path):
    """A folder means its README (or its only Markdown file)."""
    if os.path.isdir(path):
        names = sorted(os.listdir(path))
        for name in names:
            if name.lower() in ("readme.md", "readme.markdown", "index.md"):
                return os.path.join(path, name)
        md = [n for n in names if n.lower().endswith(MD_EXTS)]
        if len(md) == 1:
            return os.path.join(path, md[0])
        return None
    return path


def read_text(path, data=None):
    if data is None:
        with open(path, "rb") as f:
            data = f.read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


class App:
    def __init__(self, stdscr, path, data=None):
        self.stdscr = stdscr
        self.mode = "read"                 # read, toc, help
        self.full = False
        self.history = []                  # (path, top) to go back to
        self.query = ""
        self.cursor_line = -1              # the line of the current search match
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        self.page = 1
        self.text_x = 2
        self.data = data
        self.open(path)

    # -- files --

    def open(self, path, keep=None):
        """Read and parse a file; `keep` is a (block, line in block) to stay at, for reloads."""
        text = read_text(path, self.data if path == "-" else None)
        self.path = path
        self.stamp = self.file_stamp()
        self.blocks, self.ctx = parse(text)
        self.doc_width = None
        self.top = 0
        self.sel = None
        self.toc_sel = 0
        self.matches = None
        self.relayout(keep)

    def file_stamp(self):
        if self.path == "-":
            return None
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def position(self):
        if not self.doc.lines:
            return (0, 0)
        b = self.doc.lines[min(self.top, len(self.doc.lines) - 1)].block
        return (b, self.top - self.doc.block_start.get(b, 0))

    def relayout(self, keep=None):
        h, w = self.stdscr.getmaxyx()
        width = max(10, w - 4 if self.full else min(MAX_WIDTH, w - 4))
        if width == self.doc_width and keep is None:
            return
        keep = keep or (self.position() if self.doc_width else None)
        self.ctx.note_order = []
        self.doc = layout(self.blocks, self.ctx, width)
        self.doc_width = width
        self.sel = None
        self.matches = None
        if keep:
            b, k = keep
            starts = self.doc.block_start
            if b in starts:
                self.top = starts[b] + k
            else:
                self.top = len(self.doc.lines)
        self.clamp()

    def check_file(self):
        stamp = self.file_stamp()
        if stamp and stamp != self.stamp:
            self.reload(quiet=True)

    def reload(self, quiet=False):
        if self.path == "-":
            self.say("standard input can't be read again", curses.color_pair(4))
            return
        keep = self.position()
        try:
            self.open(self.path, keep)
        except OSError as e:
            self.stamp = self.file_stamp()
            self.say(f"couldn't read: {e.strerror}", curses.color_pair(4))
            return
        self.say("changed on disk — drawn again" if quiet else "read again")

    def go(self, path, anchor=None):
        """Open another Markdown file, remembering where we were."""
        try:
            self.history.append((self.path, self.top))
            self.open(path)
        except OSError as e:
            self.history.pop()
            self.say(f"couldn't open {os.path.basename(path)}: {e.strerror}", curses.color_pair(4))
            return
        if anchor:
            self.jump_anchor(anchor, remember=False)

    def back(self):
        if not self.history:
            self.say("nothing to go back to")
            return
        path, top = self.history.pop()
        if path != self.path:
            try:
                self.open(path)
            except OSError as e:
                self.say(f"couldn't open {path}: {e.strerror}", curses.color_pair(4))
                return
        self.top = top
        self.clamp()

    # -- moving --

    def body_height(self):
        return self.stdscr.getmaxyx()[0] - 2

    def clamp(self):
        self.top = max(0, min(self.top, len(self.doc.lines) - self.body_height()))

    def scroll(self, n):
        self.top += n
        self.clamp()

    def show_line(self, n, margin=3):
        h = self.body_height()
        if n < self.top or n >= self.top + h:
            self.top = max(0, n - margin)
            self.clamp()

    def section(self):
        """The heading the top of the screen is under."""
        current = None
        for line, level, title in self.doc.headings:
            if line > self.top + 1:
                break
            current = title
        return current

    def next_heading(self, step):
        heads = [line for line, _, _ in self.doc.headings]
        if step > 0:
            later = [n for n in heads if n > self.top]
            if later:
                self.top = later[0]
        else:
            earlier = [n for n in heads if n < self.top]
            self.top = earlier[-1] if earlier else 0
        self.clamp()

    def jump_anchor(self, anchor, remember=True):
        doc = self.doc
        name = anchor.lstrip("#")
        line = doc.anchors.get(name, doc.anchors.get(slugify(name), doc.anchors.get(name.lower())))
        if line is None:
            self.say(f"no heading #{name} here", curses.color_pair(4))
            return
        if remember:
            self.history.append((self.path, self.top))
        self.top = line
        self.clamp()

    # -- links --

    def visible(self, link):
        h = self.body_height()
        return any(self.top <= r[0] < self.top + h for r in link.rects)

    def select(self, step):
        links = self.doc.links
        if not links:
            self.say("no links or code on this page")
            return
        h = self.body_height()
        if self.sel is None or not self.visible(links[self.sel]):
            on = [i for i, l in enumerate(links) if self.visible(l)]
            if on:
                self.sel = on[0] if step > 0 else on[-1]
            else:
                after = [i for i, l in enumerate(links) if l.rects[0][0] >= self.top + h]
                before = [i for i, l in enumerate(links) if l.rects[-1][0] < self.top]
                self.sel = (after[0] if after else 0) if step > 0 else (before[-1] if before else len(links) - 1)
        else:
            self.sel = (self.sel + step) % len(links)
        link = links[self.sel]
        if not self.visible(link):
            self.show_line(link.rects[0][0], h // 3)

    def follow(self, link):
        t = link.target
        if link.kind == "code":
            self.copy(t, "the code block")
            return
        if t.startswith("#"):
            self.jump_anchor(t)
            return
        if re.match(r"[a-z][a-z0-9+.-]*:", t, re.I) and not re.match(r"file:", t, re.I):
            self.open_external(t)
            return
        if self.path == "-":
            self.say("relative links need a file, not standard input", curses.color_pair(4))
            return
        target, _, anchor = t.removeprefix("file://").partition("#")
        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(self.path)),
                                             os.path.expanduser(target.replace("%20", " "))))
        md = resolve(path)
        if md and os.path.isfile(md) and md.lower().endswith(MD_EXTS) and link.kind != "image":
            self.go(md, anchor)
        elif os.path.exists(path):
            self.open_external(path)
        else:
            self.say(f"not found: {target}", curses.color_pair(4))

    def open_external(self, target):
        try:
            subprocess.run(["open", target], check=True, capture_output=True)
            self.say(f"opened {target}")
        except (OSError, subprocess.CalledProcessError):
            self.say(f"couldn't open {target}", curses.color_pair(4))

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {what}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))

    # -- search --

    def find_all(self):
        if self.matches is None:
            q = self.query.casefold()
            self.matches = [(n, i) for n, text in enumerate(self.doc.texts)
                            for i in (m.start() for m in re.finditer(re.escape(q), text.casefold()))] if q else []
        return self.matches

    def find(self, step):
        found = self.find_all()
        if not found:
            if self.query:
                self.say(f"“{self.query}” isn't here", curses.color_pair(4))
            return
        lines = [n for n, _ in found]
        h = self.body_height()
        if step > 0:
            after = [n for n in lines if n > self.cursor_line]
            n = after[0] if after else lines[0]
        else:
            before = [n for n in lines if n < self.cursor_line]
            n = before[-1] if before else lines[-1]
        if (step > 0 and n <= self.cursor_line) or (step < 0 and n >= self.cursor_line):
            self.say("search wrapped around")
        self.cursor_line = n
        if not (self.top <= n < self.top + h):
            self.top = max(0, n - h // 3)
            self.clamp()

    def start_search(self):
        text = self.prompt(" search: ")
        if text is None or not text.strip():
            return
        self.query = text.strip()
        self.matches = None
        self.cursor_line = self.top - 1
        found = self.find_all()
        self.find(1)
        if found:
            self.say(f"{len(found):,} matches on {len({n for n, _ in found}):,} lines")

    # -- drawing --

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h or x < 0:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    def init_attrs(self):
        c = curses.color_pair
        italic = getattr(curses, "A_ITALIC", 0)
        self.kinds = {
            "p": 0, "h1": c(1) | curses.A_BOLD, "h2": c(1) | curses.A_BOLD, "h3": c(3) | curses.A_BOLD,
            "h4": curses.A_BOLD, "h1rule": c(1), "rule": c(5), "th": curses.A_BOLD,
            "code": c(2), "comment": c(5) | italic, "codebar": c(5), "codelang": c(5) | italic,
            "quote": c(5), "note": c(1), "tip": c(2), "important": c(7), "warning": c(3), "caution": c(4),
            "bullet": c(1), "done": c(2),
        }
        self.italic = italic

    def seg_attr(self, kind, flags, link):
        a = self.kinds.get(kind, 0)
        if flags & BOLD:
            a |= curses.A_BOLD
        if flags & ITALIC:
            a |= self.italic or curses.A_DIM
        if flags & STRIKE:
            a |= curses.A_DIM
        if flags & CODE:
            a = (a & ~curses.A_COLOR) | curses.color_pair(2)
        if flags & (LINK | NOTE):
            a = (a & ~curses.A_COLOR) | curses.color_pair(6) | curses.A_UNDERLINE
        if flags & IMAGE:
            a = (a & ~curses.A_COLOR) | curses.color_pair(7)
        if link is not None and self.sel is not None and link is self.doc.links[self.sel]:
            a = (a & ~curses.A_COLOR) | curses.color_pair(1) | curses.A_REVERSE
        return a

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.relayout()
        self.draw_title(w)
        if self.mode == "read":
            self.draw_text(h, w)
        else:
            getattr(self, "draw_" + self.mode)(h, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_title(self, w):
        name = "stdin" if self.path == "-" else os.path.basename(self.path)
        self.put(0, 0, " Markdown ", curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE)
        here = self.section() if self.mode == "read" else None
        right = f"§ {here}" if here else ""
        right_w = min(cwidth(right), w // 2)
        self.put(0, 11, name, curses.A_BOLD, w - 13 - right_w)
        if right:
            self.put(0, w - right_w - 1, right, curses.color_pair(5), right_w)

    def draw_text(self, h, w):
        doc = self.doc
        x0 = self.text_x
        height = h - 2
        self.page = max(1, height - 2)
        q = self.query.casefold()
        found = self.find_all() if q else []
        by_line = {}
        for n, i in found:
            by_line.setdefault(n, []).append(i)
        for k in range(height):
            n = self.top + k
            if n >= len(doc.lines):
                break
            y = 1 + k
            x = x0
            for text, kind, flags, link in doc.lines[n].segs:
                cw = cwidth(text)
                self.put(y, x, text, self.seg_attr(kind, flags, link), cw)
                x += cw
            for i in by_line.get(n, ()):
                text = doc.texts[n]
                col = x0 + cwidth(text[:i])
                here = n == self.cursor_line
                self.put(y, col, text[i:i + len(q)], curses.color_pair(3) | curses.A_REVERSE | (curses.A_BOLD if here else 0),
                         cwidth(text[i:i + len(q)]))
        if not doc.lines or all(not line.segs for line in doc.lines):
            self.put(2, x0, "(nothing to show — the file is empty)", curses.color_pair(5))

    def draw_toc(self, h, w):
        heads = self.doc.headings
        if not heads:
            self.put(2, 2, "no headings", curses.color_pair(5))
            return
        height = h - 3
        self.page = max(1, height - 1)
        self.toc_sel = max(0, min(self.toc_sel, len(heads) - 1))
        top = max(0, min(self.toc_sel - height // 2, len(heads) - height))
        self.put(1, 2, "Contents", curses.color_pair(1) | curses.A_BOLD)
        low = min(level for _, level, _ in heads)
        for k, (line, level, title) in enumerate(heads[top:top + height]):
            i = top + k
            attr = curses.A_BOLD if level == low else 0
            if i == self.toc_sel:
                attr = curses.color_pair(1) | curses.A_REVERSE | curses.A_BOLD
            indent = 2 + 2 * (level - low)
            self.put(2 + k, indent, " " + title + " ", attr, w - indent - 1)

    def draw_help(self, h, w):
        lines = [("", None)] + HELP
        height = h - 2
        self.page = max(1, height - 1)
        self.help_top = max(0, min(getattr(self, "help_top", 0), len(lines) - height))
        for k, (keys, what) in enumerate(lines[self.help_top:self.help_top + height]):
            y = 1 + k
            if what is None:
                self.put(y, 1, keys, curses.color_pair(1) | curses.A_BOLD)
            else:
                self.put(y, 3, keys, curses.A_BOLD, 20)
                self.put(y, 24, what, curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        doc = self.doc
        n = len(doc.lines)
        h = self.body_height()
        where = "all" if n <= h else "top" if self.top == 0 else "end" if self.top + h >= n else \
            f"{100 * (self.top + h) // n}%"
        if self.mode == "read" and self.sel is not None and self.visible(doc.links[self.sel]):
            link = doc.links[self.sel]
            if link.kind == "code":
                text = f" ⏎/y copy the {link.label} block ({link.target.count(chr(10)) + 1} lines)"
            else:
                text = f" → {link.target}   ⏎ open  y copy"
            self.put(y, 0, text, curses.color_pair(1), w - len(where) - 2)
        else:
            keys = {"read": ["Tab link", "⏎ follow", "t contents", "/ search", "] heading", "⌫ back", "w width",
                             "e edit", "? help", "q quit"],
                    "toc": ["↑↓ move", "⏎ jump", "q back"],
                    "help": ["↑↓ scroll", "q back"]}[self.mode]
            room = w - 2 - len(where) - 2
            while len(keys) > 2 and cwidth("  ".join(keys)) > room:
                keys.pop(-2)
            self.put(y, 0, " " + "  ".join(keys), curses.color_pair(5))
        if self.mode == "read":
            self.put(y, w - len(where) - 1, where, curses.color_pair(1))

    # -- input --

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
                        return "".join(buf)
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":
                        buf.clear()
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf)
        finally:
            curses.curs_set(0)

    def edit(self):
        if self.path == "-":
            self.say("standard input can't be edited", curses.color_pair(4))
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.run(editor.split() + [self.path])
        except OSError:
            self.say(f"couldn't start {editor}", curses.color_pair(4))
        curses.reset_prog_mode()
        self.stdscr.refresh()
        self.check_file()

    def open_prompt(self):
        text = self.prompt(" open: ")
        if not text or not text.strip():
            return
        base = os.path.dirname(os.path.abspath(self.path)) if self.path != "-" else os.getcwd()
        path = resolve(os.path.join(base, os.path.expanduser(text.strip())))
        if not path or not os.path.isfile(path):
            self.say(f"no Markdown file at {text.strip()}", curses.color_pair(4))
            return
        self.go(path)

    def click(self, mx, my):
        n = self.top + my - 1
        if self.mode != "read" or not 0 <= n < len(self.doc.lines):
            return
        col = mx - self.text_x
        for i, link in enumerate(self.doc.links):
            if any(r[0] == n and r[1] <= col < r[2] for r in link.rects):
                self.sel = i
                self.follow(link)
                return

    def handle(self, ch):
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                return True
            if bstate & curses.BUTTON4_PRESSED:
                ch = 1000
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000):
                ch = 1001
            elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                self.click(mx, my)
                return True
            else:
                return True
        if self.mode == "toc":
            return self.handle_toc(ch)
        if self.mode == "help":
            steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, 1000: -3, 1001: 3,
                     curses.KEY_PPAGE: -self.page, curses.KEY_NPAGE: self.page, ord(" "): self.page}
            if ch in steps:
                self.help_top = max(0, getattr(self, "help_top", 0) + steps[ch])
            elif ch in (ord("q"), ord("?"), 27, 10, 13, curses.KEY_ENTER, curses.KEY_BACKSPACE, 127):
                self.mode = "read"
            return True
        page, half = self.page, max(1, self.page // 2)
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, 1000: -3, 1001: 3,
                 curses.KEY_PPAGE: -page, ord("b"): -page, curses.KEY_NPAGE: page, ord(" "): page, ord("f"): page,
                 ord("d"): half, ord("u"): -half,
                 curses.KEY_HOME: -10**9, ord("g"): -10**9, curses.KEY_END: 10**9, ord("G"): 10**9}
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in steps:
            self.scroll(steps[ch])
        elif ch == ord("\t"):
            self.select(1)
        elif ch == curses.KEY_BTAB:
            self.select(-1)
        elif ch in (10, 13, curses.KEY_ENTER):
            if self.sel is not None and self.visible(self.doc.links[self.sel]):
                self.follow(self.doc.links[self.sel])
            else:
                self.scroll(1)
        elif ch == ord("y") and self.sel is not None:
            link = self.doc.links[self.sel]
            self.copy(link.target, "the code block" if link.kind == "code" else "the address")
        elif ch in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
            self.back()
        elif ch == ord("]"):
            self.next_heading(1)
        elif ch == ord("["):
            self.next_heading(-1)
        elif ch == ord("t"):
            if self.doc.headings:
                cur = [i for i, (line, _, _) in enumerate(self.doc.headings) if line <= self.top + 1]
                self.toc_sel = cur[-1] if cur else 0
                self.mode = "toc"
            else:
                self.say("no headings in this file")
        elif ch == ord("/"):
            self.start_search()
        elif ch == ord("n"):
            self.find(1)
        elif ch == ord("N"):
            self.find(-1)
        elif ch == 27:
            self.query, self.matches, self.sel = "", None, None
        elif ch == ord("w"):
            self.full = not self.full
            self.relayout(self.position())
        elif ch == ord("r"):
            self.reload()
        elif ch == ord("e"):
            self.edit()
        elif ch == ord("o"):
            self.open_prompt()
        elif ch == ord("?"):
            self.mode, self.help_top = "help", 0
        return True

    def handle_toc(self, ch):
        heads = self.doc.headings
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, 1000: -1, 1001: 1,
                 curses.KEY_PPAGE: -self.page, curses.KEY_NPAGE: self.page,
                 ord("g"): -10**9, curses.KEY_HOME: -10**9, ord("G"): 10**9, curses.KEY_END: 10**9}
        if ch in steps:
            self.toc_sel = max(0, min(len(heads) - 1, self.toc_sel + steps[ch]))
        elif ch in (10, 13, curses.KEY_ENTER, ord("l"), curses.KEY_RIGHT):
            self.history.append((self.path, self.top))
            self.top = heads[self.toc_sel][0]
            self.clamp()
            self.mode = "read"
        elif ch in (ord("q"), ord("t"), 27, curses.KEY_BACKSPACE, 127, curses.KEY_LEFT, ord("h")):
            self.mode = "read"
        return True


def run(stdscr, path, data):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # headings, bullets, selection
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # code
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # h3, search, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # links
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)  # images
    stdscr.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    app = App(stdscr, path, data)
    app.init_attrs()
    last_check = time.monotonic()
    while True:
        stdscr.timeout(CHECK_SECONDS * 1000 if path != "-" else -1)
        app.draw()
        ch = stdscr.getch()
        if time.monotonic() - last_check >= CHECK_SECONDS:
            app.check_file()
            last_check = time.monotonic()
        if ch in (-1, curses.KEY_RESIZE):
            continue
        if not app.handle(ch):
            return


ANSI = {"h1": "\033[1;36m", "h2": "\033[1;36m", "h3": "\033[1;33m", "h4": "\033[1m", "h1rule": "\033[36m",
        "rule": "\033[2m", "th": "\033[1m", "code": "\033[32m", "comment": "\033[2;3m", "codebar": "\033[2m",
        "codelang": "\033[2;3m", "quote": "\033[2m", "note": "\033[36m", "tip": "\033[32m", "important": "\033[35m",
        "warning": "\033[33m", "caution": "\033[31m", "bullet": "\033[36m", "done": "\033[32m"}


def print_once(path, data, width):
    blocks, ctx = parse(read_text(path, data))
    doc = layout(blocks, ctx, width)
    color = sys.stdout.isatty()
    for line in doc.lines:
        if not color:
            print(line.text().rstrip())
            continue
        out = []
        for text, kind, flags, link in line.segs:
            codes = ANSI.get(kind, "")
            if flags & BOLD:
                codes += "\033[1m"
            if flags & ITALIC:
                codes += "\033[3m"
            if flags & STRIKE:
                codes += "\033[9m"
            if flags & CODE:
                codes += "\033[32m"
            if flags & (LINK | NOTE):
                codes += "\033[34;4m"
            if flags & IMAGE:
                codes += "\033[35m"
            out.append(f"{codes}{text}\033[0m" if codes else text)
        print("".join(out).rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Read a Markdown file in the terminal, rendered — links, tables, code and all.")
    parser.add_argument("path", nargs="?", default=".",
                        help="a Markdown file, a folder (opens its README) or - for standard input (default: .)")
    parser.add_argument("--once", action="store_true", help="print the rendered text and exit")
    parser.add_argument("--width", type=int, metavar="N", help=f"text width for --once (default: the terminal's, at most {MAX_WIDTH})")
    args = parser.parse_args()
    data = None
    if args.path == "-":
        data = sys.stdin.buffer.read()
        path = "-"
    else:
        path = resolve(os.path.expanduser(args.path))
        if path is None:
            parser.error(f"no README or single Markdown file in {args.path}")
        if not os.path.isfile(path):
            parser.error(f"no such file: {args.path}")
    if args.once:
        width = args.width or min(MAX_WIDTH, os.get_terminal_size().columns - 1 if sys.stdout.isatty() else 80)
        raise SystemExit(print_once(path, data, max(20, width)))
    if path == "-":
        os.dup2(os.open("/dev/tty", os.O_RDONLY), 0)     # keys come from the terminal, not the pipe
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, path, data)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
