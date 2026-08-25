#!/usr/bin/env python3
"""Show images in the terminal, two ways: real pixels (kitty protocol) or half-block characters."""

import argparse
import base64
import fcntl
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import tty
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = "termimage/1.0 (terminal image viewer)"
TIMEOUT = 15

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ESC = "\033"
UPPER_HALF = "▀"                      # ▀ : foreground is the top pixel, background the bottom
ASCII_RAMP = " .:-=+*#%@"

# A cell is roughly twice as tall as it is wide; used when the terminal won't tell us.
DEFAULT_CELL_ASPECT = 2.0
DEFAULT_MAX_COLS = 60
# Adam7 interlace passes: (x offset, y offset, x step, y step)
ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
         (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
QUERY_ID = 31                              # image id used only for the "do you support this?" probe


class Unsupported(Exception):
    """Raised for PNG variants the built-in decoder does not handle."""


# ------------------------------------------------------------------ images


class Image:
    """8-bit RGBA pixels, one bytes object per row."""

    __slots__ = ("width", "height", "rows")

    def __init__(self, width, height, rows):
        self.width = width
        self.height = height
        self.rows = rows

    def pixel(self, x, y):
        i = x * 4
        row = self.rows[y]
        return row[i], row[i + 1], row[i + 2], row[i + 3]


def png_size(data):
    """Width and height straight from the IHDR chunk, without decoding pixels."""
    if data[:8] != PNG_MAGIC:
        raise Unsupported("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def decode_png(data):
    """Decode a non-interlaced 8-bit PNG to an Image, using only zlib and struct."""
    if data[:8] != PNG_MAGIC:
        raise Unsupported("not a PNG")

    pos = 8
    header = None
    palette = b""
    trns = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    if header is None:
        raise Unsupported("no IHDR")
    width, height, depth, color, _comp, _filt, interlace = header
    if depth not in (1, 2, 4, 8, 16):
        raise Unsupported(f"{depth}-bit samples")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise Unsupported(f"colour type {color}")

    raw = zlib.decompress(bytes(idat))
    try:
        if interlace:
            rows = deinterlace(raw, width, height, channels, depth, color)
        else:
            rows, _ = read_pass(raw, 0, width, height, channels, depth, color)
    except IndexError:
        raise Unsupported("truncated image data") from None

    return to_rgba(rows, width, height, channels, color, palette, trns)


def read_pass(raw, at, width, height, channels, depth, color):
    """Unfilter one pass and normalise it to one byte per sample."""
    stride = (width * channels * depth + 7) // 8
    bpp = max(1, channels * depth // 8)          # filter offset, per the PNG spec
    if len(raw) < at + height * (stride + 1):
        raise IndexError("short image data")

    rows = []
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        if filter_type:
            unfilter(line, previous, filter_type, bpp)
        previous = bytearray(line)
        if depth == 16:
            line = line[0::2]                     # keep the high byte of each sample
        elif depth < 8:
            line = expand_bits(line, width * channels, depth, color)
        rows.append(bytes(line))
    return rows, at


def expand_bits(line, samples, depth, color):
    """Unpack 1/2/4-bit samples into one byte each, scaling greyscale up to 0-255."""
    out = bytearray(samples)
    mask = (1 << depth) - 1
    per_byte = 8 // depth
    for i in range(samples):
        byte = line[i // per_byte]
        shift = 8 - depth * (i % per_byte + 1)
        value = (byte >> shift) & mask
        out[i] = value if color == 3 else value * 255 // mask
    return out


def deinterlace(raw, width, height, channels, depth, color):
    """Reassemble the seven Adam7 passes into ordinary top-to-bottom scanlines."""
    full = [bytearray(width * channels) for _ in range(height)]
    at = 0
    for x_start, y_start, x_step, y_step in ADAM7:
        xs = range(x_start, width, x_step)
        ys = range(y_start, height, y_step)
        if not len(xs) or not len(ys):
            continue
        rows, at = read_pass(raw, at, len(xs), len(ys), channels, depth, color)
        for row_index, y in enumerate(ys):
            source = rows[row_index]
            for col_index, x in enumerate(xs):
                full[y][x * channels:(x + 1) * channels] = \
                    source[col_index * channels:(col_index + 1) * channels]
    return [bytes(row) for row in full]


def unfilter(line, previous, filter_type, bpp):
    """Undo one PNG scanline filter in place (see RFC 2083 section 6)."""
    for i in range(len(line)):
        left = line[i - bpp] if i >= bpp else 0
        up = previous[i]
        upleft = previous[i - bpp] if i >= bpp else 0
        if filter_type == 1:
            line[i] = (line[i] + left) & 0xFF
        elif filter_type == 2:
            line[i] = (line[i] + up) & 0xFF
        elif filter_type == 3:
            line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
        elif filter_type == 4:
            estimate = left + up - upleft
            da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - upleft)
            nearest = left if da <= db and da <= dc else up if db <= dc else upleft
            line[i] = (line[i] + nearest) & 0xFF


def to_rgba(rows, width, height, channels, color, palette, trns):
    """Expand greyscale / palette / RGB rows into flat RGBA rows."""
    out = []
    for row in rows:
        buf = bytearray(width * 4)
        for x in range(width):
            src = x * channels
            if color == 0:                                     # greyscale
                grey = row[src]
                red = green = blue = grey
                alpha = 255
            elif color == 2:                                   # truecolour
                red, green, blue = row[src], row[src + 1], row[src + 2]
                alpha = 255
            elif color == 3:                                   # palette index
                index = row[src]
                base = index * 3
                if base + 2 >= len(palette):
                    red = green = blue = 0
                else:
                    red, green, blue = palette[base], palette[base + 1], palette[base + 2]
                alpha = trns[index] if index < len(trns) else 255
            elif color == 4:                                   # greyscale + alpha
                red = green = blue = row[src]
                alpha = row[src + 1]
            else:                                              # RGBA
                red, green, blue, alpha = row[src], row[src + 1], row[src + 2], row[src + 3]
            out_at = x * 4
            buf[out_at] = red
            buf[out_at + 1] = green
            buf[out_at + 2] = blue
            buf[out_at + 3] = alpha
        out.append(bytes(buf))
    return Image(width, height, out)


def resize(image, width, height):
    """Box-filter resample. Averaging keeps downscaled photographs from going speckly."""
    if width == image.width and height == image.height:
        return image
    rows = []
    for y in range(height):
        y0 = y * image.height // height
        y1 = max(y0 + 1, (y + 1) * image.height // height)
        buf = bytearray(width * 4)
        for x in range(width):
            x0 = x * image.width // width
            x1 = max(x0 + 1, (x + 1) * image.width // width)
            red = green = blue = alpha = count = 0
            for sy in range(y0, y1):
                row = image.rows[sy]
                for sx in range(x0, x1):
                    at = sx * 4
                    red += row[at]
                    green += row[at + 1]
                    blue += row[at + 2]
                    alpha += row[at + 3]
                    count += 1
            at = x * 4
            buf[at] = red // count
            buf[at + 1] = green // count
            buf[at + 2] = blue // count
            buf[at + 3] = alpha // count
        rows.append(bytes(buf))
    return Image(width, height, rows)


# ------------------------------------------------------- loading and sips


def sips_convert(data, max_dim=None):
    """Hand the bytes to macOS's built-in sips to get a plain 8-bit PNG back."""
    if not shutil.which("sips"):
        raise Unsupported("sips is not available to convert this image")
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "in")
        dst = os.path.join(work, "out.png")
        with open(src, "wb") as handle:
            handle.write(data)
        command = ["sips", "-s", "format", "png"]
        if max_dim:
            command += ["-Z", str(max_dim)]
        command += [src, "--out", dst]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(dst):
            raise Unsupported(f"sips could not convert this image: {result.stderr.strip()}")
        with open(dst, "rb") as handle:
            return handle.read()


def ensure_png(data, max_dim=None):
    """PNG bytes suitable for the kitty protocol, converting other formats if needed."""
    already_png = data[:8] == PNG_MAGIC
    if already_png and (max_dim is None or not shutil.which("sips")):
        return data                      # shrinking is only an optimisation; sending it whole works
    return sips_convert(data, max_dim)


def load_image(data):
    """An Image, falling back to sips when the built-in decoder meets an exotic PNG."""
    try:
        return decode_png(data)
    except Unsupported:
        return decode_png(sips_convert(data))


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def read_source(source):
    """Bytes for a local path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        return fetch_bytes(source)
    with open(source, "rb") as handle:
        return handle.read()


def wiki_lead_image(lang, topic, size=640):
    """The article's lead image, as (url, article title)."""
    params = {
        "action": "query", "titles": topic, "prop": "pageimages",
        "pithumbsize": size, "redirects": 1, "format": "json", "formatversion": 2,
    }
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.load(response)
    pages = payload.get("query", {}).get("pages", [])
    if not pages or "missing" in pages[0]:
        return None, None
    page = pages[0]
    thumbnail = page.get("thumbnail", {}).get("source")
    return thumbnail, page.get("title")


# ---------------------------------------------------------------- geometry


def terminal_size():
    """(columns, rows, cell_width_px, cell_height_px); pixel sizes are 0 when unknown."""
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, xpixel, ypixel = struct.unpack("HHHH", packed)
    except (OSError, ValueError):
        return 80, 24, 0, 0
    cell_w = xpixel // cols if cols and xpixel else 0
    cell_h = ypixel // rows if rows and ypixel else 0
    return cols or 80, rows or 24, cell_w, cell_h


def fit_cells(image_width, image_height, max_cols):
    """Cell box that preserves the picture's aspect ratio."""
    _cols, _rows, cell_w, cell_h = terminal_size()
    aspect = (cell_h / cell_w) if cell_w and cell_h else DEFAULT_CELL_ASPECT
    cols = max(1, max_cols)
    rows = max(1, round(cols * (image_height / image_width) / aspect))
    return cols, rows


# --------------------------------------------- approach A: block characters


def composite(red, green, blue, alpha, background):
    if alpha >= 250:
        return red, green, blue
    scale = alpha / 255.0
    return (round(red * scale + background[0] * (1 - scale)),
            round(green * scale + background[1] * (1 - scale)),
            round(blue * scale + background[2] * (1 - scale)))


def render_blocks(image, cols, rows, background=(0, 0, 0)):
    """Two pixels per cell using ▀ : foreground paints the top half, background the bottom."""
    small = resize(image, cols, rows * 2)
    lines = []
    for row in range(rows):
        parts = []
        previous = None
        for col in range(cols):
            top = composite(*small.pixel(col, row * 2), background)
            bottom = composite(*small.pixel(col, row * 2 + 1), background)
            if (top, bottom) != previous:                      # only re-emit colour when it changes
                parts.append(f"{ESC}[38;2;{top[0]};{top[1]};{top[2]}m"
                             f"{ESC}[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m")
                previous = (top, bottom)
            parts.append(UPPER_HALF)
        parts.append(f"{ESC}[0m")
        lines.append("".join(parts))
    return lines


def render_ascii(image, cols, rows):
    """Last-resort rendering for terminals without 24-bit colour: brightness to characters."""
    small = resize(image, cols, rows * 2)
    lines = []
    for row in range(rows):
        line = []
        for col in range(cols):
            total = 0
            for half in (0, 1):
                red, green, blue, alpha = small.pixel(col, row * 2 + half)
                total += (0.299 * red + 0.587 * green + 0.114 * blue) * (alpha / 255.0)
            level = total / 2 / 255
            line.append(ASCII_RAMP[min(len(ASCII_RAMP) - 1, int(level * len(ASCII_RAMP)))])
        lines.append("".join(line))
    return lines


# ------------------------------------------- approach B: kitty graphics


def kitty_escape(control, payload=b""):
    return f"{ESC}_G{control};{payload.decode('ascii')}{ESC}\\"


def kitty_supported(timeout=0.4):
    """Ask the terminal directly; a DA1 query rides along so there is always a reply to wait for."""
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return False
    probe = kitty_escape(f"i={QUERY_ID},s=1,v=1,a=q,t=d,f=24", base64.b64encode(b"\0\0\0"))
    try:
        old = termios.tcgetattr(sys.stdin)
    except termios.error:
        return False
    try:
        tty.setraw(sys.stdin.fileno())
        sys.stdout.write(probe + f"{ESC}[c")
        sys.stdout.flush()
        reply = ""
        fd = sys.stdin.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            # read the raw fd: sys.stdin.read(1) blocks filling its text buffer
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            reply += chunk.decode("latin-1")
            if reply.endswith("c") and f"{ESC}[" in reply:     # DA1 answered: nothing more is coming
                break
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    return "_G" in reply


def kitty_show(png, cols, rows, image_id=1, transfer="auto"):
    """Place a PNG into a cols x rows box at the cursor."""
    if transfer == "auto":
        transfer = "direct" if os.environ.get("SSH_CONNECTION") else "file"

    placement = f"a=T,f=100,i={image_id},c={cols},r={rows}"

    if transfer == "file":
        # t=t hands over a path instead of the pixels: a ~100 byte escape rather than megabytes.
        # The terminal deletes the file once it has read it.
        handle = tempfile.NamedTemporaryFile(prefix="termimage-", suffix=".png", delete=False)
        try:
            handle.write(png)
            handle.close()
            sys.stdout.write(kitty_escape(f"{placement},t=t",
                                          base64.b64encode(handle.name.encode())))
            sys.stdout.flush()
        except OSError:
            os.unlink(handle.name)
            raise
        return

    payload = base64.b64encode(png)
    chunk = 4096
    pieces = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
    for index, piece in enumerate(pieces):
        more = 1 if index < len(pieces) - 1 else 0
        control = f"{placement},t=d,m={more}" if index == 0 else f"m={more}"
        sys.stdout.write(kitty_escape(control, piece))
    sys.stdout.flush()


def kitty_delete(image_id=1):
    sys.stdout.write(kitty_escape(f"a=d,d=i,i={image_id}"))
    sys.stdout.flush()


# -------------------------------------------------------------------- CLI


def choose_mode(requested):
    if requested != "auto":
        return requested
    if kitty_supported():
        return "kitty"
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return "blocks"
    return "ascii"


def show(data, mode, max_cols, background, transfer, image_id=1):
    """Render the image bytes and return the mode actually used."""
    if mode == "kitty":
        png = ensure_png(data, max_dim=max(320, max_cols * 16))
        width, height = png_size(png)
        cols, rows = fit_cells(width, height, max_cols)
        kitty_show(png, cols, rows, image_id, transfer)
        sys.stdout.write("\n" * rows)          # step the cursor past the picture
        sys.stdout.flush()
        return "kitty"

    image = load_image(data)
    cols, rows = fit_cells(image.width, image.height, max_cols)
    lines = render_blocks(image, cols, rows, background) if mode == "blocks" \
        else render_ascii(image, cols, rows)
    print("\n".join(lines))
    return mode


def parse_colour(text):
    text = text.lstrip("#")
    if len(text) != 6:
        raise argparse.ArgumentTypeError("colour must be six hex digits, e.g. 101010")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def main():
    parser = argparse.ArgumentParser(
        description="Show an image in the terminal, as real pixels or as coloured characters.")
    parser.add_argument("source", nargs="?", help="image file or http(s) URL")
    parser.add_argument("--wiki", metavar="TOPIC", help="use a Wikipedia article's lead image")
    parser.add_argument("--lang", default="en", help="Wikipedia language code (default: en)")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "kitty", "blocks", "ascii", "both"],
                        help="auto detects the terminal; both shows kitty and blocks together")
    parser.add_argument("--width", type=int, default=0,
                        help=f"width in cells (default: {DEFAULT_MAX_COLS}, capped to the terminal)")
    parser.add_argument("--bg", type=parse_colour, default=(0, 0, 0),
                        help="background colour composited under transparency (default: 000000)")
    parser.add_argument("--transfer", default="auto", choices=["auto", "direct", "file"],
                        help="how kitty receives the image: inline base64, or a temp file path")
    parser.add_argument("--probe", action="store_true",
                        help="report what this terminal supports and exit")
    args = parser.parse_args()

    if args.probe:
        cols, rows, cell_w, cell_h = terminal_size()
        print(f"terminal      : {cols}x{rows} cells", end="")
        print(f", cell {cell_w}x{cell_h}px" if cell_w else ", cell size unreported")
        print(f"TERM          : {os.environ.get('TERM', '-')}")
        print(f"COLORTERM     : {os.environ.get('COLORTERM', '-')}")
        graphics = kitty_supported()
        print(f"kitty graphics: {'yes' if graphics else 'no'}")
        print(f"sips          : {'yes' if shutil.which('sips') else 'no'}")
        if graphics:
            mode = "kitty"
        elif os.environ.get("COLORTERM") in ("truecolor", "24bit"):
            mode = "blocks"
        else:
            mode = "ascii"
        print(f"chosen mode   : {mode}")
        return

    if not args.source and not args.wiki:
        parser.error("give an image path or URL, or --wiki TOPIC")

    title = None
    try:
        if args.wiki:
            url, title = wiki_lead_image(args.lang, args.wiki)
            if not url:
                print(f"No lead image for '{args.wiki}'.", file=sys.stderr)
                sys.exit(1)
            data = fetch_bytes(url)
        else:
            data = read_source(args.source)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"Could not read the image: {exc}", file=sys.stderr)
        sys.exit(1)

    cols = args.width or DEFAULT_MAX_COLS
    cols = min(cols, terminal_size()[0] - 1)

    if title:
        print(f"{ESC}[1m{title}{ESC}[0m")

    try:
        if args.mode == "both":
            print(f"{ESC}[2m-- approach B: kitty graphics protocol --{ESC}[0m")
            if kitty_supported():
                show(data, "kitty", cols, args.bg, args.transfer)
            else:
                print("   (this terminal did not answer the kitty graphics query)")
            print(f"{ESC}[2m-- approach A: half-block characters --{ESC}[0m")
            show(data, "blocks", cols, args.bg, args.transfer)
        else:
            show(data, choose_mode(args.mode), cols, args.bg, args.transfer)
    except Unsupported as exc:
        print(f"Cannot display this image: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
