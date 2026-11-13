---
paths:
  - "termimage.py"
  - "wiki_tui.py"
---

# termimage.py

Displays an image in the terminal two different ways, and is importable so `wiki_tui` could use it
later — but `wiki_tui.py` does not depend on it and must stay that way unless asked.

Usage is documented in `termimage.1`, a roff man page (`man ./termimage.1`, or `mandoc -T lint`
after editing it — it currently lints clean). Keep it in step with the argparse options.

- **Approach A, characters** (works anywhere): decode to RGBA, box-filter resample, then print `▀`
  per cell with the foreground set to the top pixel and the background to the bottom — two pixels per
  cell. `render_ascii()` is the further fallback for terminals without 24-bit colour.
- **Approach B, real pixels** (kitty graphics protocol, which Ghostty and kitty both speak): send the
  PNG itself and let the terminal draw it. `t=t` hands over a *temp file path* instead of the pixels
  (~100 byte escape instead of megabytes; the terminal deletes the file), and `t=d` base64s the image
  inline in 4096-byte chunks for when the terminal is on the far end of an SSH connection.

`kitty_supported()` asks the terminal rather than guessing from `$TERM`: it sends a graphics query with
a DA1 query (`ESC[c`) chasing it, so there is always a reply to wait for — if DA1 answers and no `_G`
reply arrived, the protocol is unsupported. Read the reply with `os.read` on the raw fd;
`sys.stdin.read(1)` blocks filling its text buffer and the probe hangs.

The PNG decoder is complete for still images and deliberately does not lean on external tools: all five
colour types, bit depths 1/2/4/8/16, all five scanline filters, and Adam7 interlacing. That matters
because **`sips` preserves interlacing and bit depth on a PNG→PNG conversion**, so "convert it with
sips first" does *not* rescue those two cases — it returns a file that fails the same way. `sips` is
used only to transcode non-PNG formats (JPEG, WebP) and to downscale before transmission.

Testing without a graphical terminal: fork a pty, answer the graphics query with `ESC_Gi=31;OK ESC\`,
and assert on the escapes the program emits (one `a=T` placement header, `m=1` on every chunk but the
last, base64 payload reassembling into a valid PNG). Both the answering and silent cases must be
tested, since the silent one is what exercises the fallback.
