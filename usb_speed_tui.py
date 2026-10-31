#!/usr/bin/env python3
"""Terminal UI that measures the read and write speed of USB drives (macOS).

A temporary file is written to the drive with the page cache bypassed (F_NOCACHE) and flushed to
the device (F_FULLFSYNC), read back, then used for 4 KiB random reads and writes. The file is
removed afterwards, also when a test is cancelled or fails.
"""

import argparse
import curses
import fcntl
import os
import plistlib
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

SIZES_MIB = [64, 128, 256, 512, 1024, 2048]   # 2048 stays under FAT32's 4 GiB file limit
DEFAULT_SIZE_MIB = 256
BLOCK = 4 * 1024 * 1024                        # sequential transfer size
SMALL = 4096                                   # random transfer size
RANDOM_SECONDS = 5
SAMPLE_SECONDS = 0.25
FREE_MARGIN = 32 * 1024 * 1024
MIN_TEST_BYTES = 16 * 1024 * 1024
TEST_FILE = ".usb_speed_test.tmp"
RESCAN_SECONDS = 2
GRAPH_ROWS = 6

PHASES = [
    ("seqw", "Sequential write"),
    ("seqr", "Sequential read"),
    ("rndr", "4K random read"),
    ("rndw", "4K random write"),
]
LABELS = dict(PHASES)
FULL = [key for key, _ in PHASES]
SEQUENTIAL = ["seqw", "seqr"]

# IOUSBHostDevice "Device Speed" when "UsbLinkSpeed" is missing.
DEVICE_SPEEDS = {0: 1_500_000, 1: 12_000_000, 2: 480_000_000, 3: 5_000_000_000,
                 4: 10_000_000_000, 5: 20_000_000_000}

BARS = " ▁▂▃▄▅▆▇█"


@dataclass
class Drive:
    mount: str
    name: str
    bus: str
    fs: str
    size: int
    free: int
    writable: bool
    whole: str
    model: str
    link_bps: int = 0


def run_plist(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=10, check=True).stdout
        return plistlib.loads(out), None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        return None, str(e)
    except plistlib.InvalidFileException as e:
        return None, f"unexpected output: {e}"


def diskutil_info(target):
    data, _ = run_plist(["diskutil", "info", "-plist", target])
    return data if isinstance(data, dict) else {}


def usb_links():
    """Map BSD disk names to the negotiated link speed (bits/s) of the USB device they sit on."""
    data, _ = run_plist(["ioreg", "-a", "-l", "-r", "-c", "IOUSBHostDevice"])
    links = {}

    def walk(node, speed):
        if not isinstance(node, dict):
            return
        if "idVendor" in node:                  # the nearest USB device above the disk wins
            speed = node.get("UsbLinkSpeed") or DEVICE_SPEEDS.get(node.get("Device Speed"), 0)
        if isinstance(node.get("BSD Name"), str) and speed:
            links[node["BSD Name"]] = int(speed)
        for child in node.get("IORegistryEntryChildren", []):
            walk(child, speed)

    for root in data if isinstance(data, list) else [data]:
        walk(root, 0)
    return links


def scan_drives(show_all=False):
    try:
        names = sorted(os.listdir("/Volumes"), key=str.lower)
    except OSError:
        return []
    links = usb_links()
    drives = []
    for name in names:
        path = os.path.join("/Volumes", name)
        if os.path.islink(path) or not os.path.ismount(path):
            continue
        info = diskutil_info(path)
        bus = info.get("BusProtocol", "")
        if not show_all and (info.get("Internal", True) or bus == "Disk Image"):
            continue
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        whole = info.get("ParentWholeDisk", "")
        model = diskutil_info(whole).get("MediaName", "").strip() if whole else ""
        drives.append(Drive(
            mount=path,
            name=info.get("VolumeName") or name,
            bus=bus or "?",
            fs=info.get("FilesystemUserVisibleName") or info.get("FilesystemType", "?"),
            size=st.f_blocks * st.f_frsize,
            free=st.f_bavail * st.f_frsize,
            writable=bool(info.get("WritableVolume", True)) and os.access(path, os.W_OK),
            whole=whole,
            model=model,
            link_bps=links.get(whole) or links.get(info.get("DeviceIdentifier", ""), 0),
        ))
    return drives


def fmt_bytes(n):
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n / 1e3:.0f} kB"


def fmt_rate(bps):
    mb = bps / 1e6
    return f"{mb:.2f} MB/s" if mb < 10 else f"{mb:.1f} MB/s"


def fmt_link(bps):
    if not bps:
        return ""
    gen = "USB 1" if bps < 480_000_000 else "USB 2" if bps == 480_000_000 else "USB 3"
    speed = f"{bps / 1e9:g} Gb/s" if bps >= 1e9 else f"{bps / 1e6:g} Mb/s"
    return f"{gen} · {speed}"


def no_cache(fd):
    if hasattr(fcntl, "F_NOCACHE"):
        fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)


def full_sync(fd):
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (AttributeError, OSError):          # not every filesystem implements it
        os.fsync(fd)


def test_bytes(drive, size_mib):
    """The test file size: what was asked for, shrunk to fit the free space, whole blocks only."""
    want = size_mib * 1024 * 1024
    fits = (drive.free - FREE_MARGIN) // BLOCK * BLOCK
    return min(want, fits)


class Meter:
    """Counts bytes for one phase and feeds the live rate and graph samples to the Bench."""

    def __init__(self, bench, key, total=None, seconds=None):
        self.bench, self.key, self.total, self.seconds = bench, key, total, seconds
        self.start = self.mark = time.perf_counter()
        self.bytes = self.mark_bytes = self.ops = 0
        bench.current, bench.progress, bench.rate = key, 0.0, 0.0
        bench.samples[key] = []

    def running(self):
        if self.bench.cancel.is_set():
            return False
        if self.seconds is not None:
            return time.perf_counter() - self.start < self.seconds
        return self.bytes < self.total

    def add(self, n):
        self.bytes += n
        self.ops += 1
        now = time.perf_counter()
        if now - self.mark >= SAMPLE_SECONDS:
            rate = (self.bytes - self.mark_bytes) / (now - self.mark)
            self.bench.samples[self.key].append(rate)
            self.bench.rate = rate
            self.mark, self.mark_bytes = now, self.bytes
            if self.seconds is not None:
                self.bench.progress = min(1.0, (now - self.start) / self.seconds)
            else:
                self.bench.progress = self.bytes / self.total

    def finish(self):
        now = time.perf_counter()
        if self.bytes > self.mark_bytes and now > self.mark:   # the last, partial sample
            self.bench.samples[self.key].append((self.bytes - self.mark_bytes) / (now - self.mark))
        elapsed = max(now - self.start, 1e-9)
        self.bench.progress = 1.0
        return self.bytes / elapsed, self.ops / elapsed


class Bench:
    """One test run against one drive, on a worker thread. The UI only reads its fields."""

    def __init__(self, drive, size, phases):
        self.drive, self.size, self.phases = drive, size, phases
        self.path = os.path.join(drive.mount, TEST_FILE)
        self.results = {}                      # key -> (bytes/s, ops/s)
        self.samples = {}                      # key -> rate samples, from when the phase starts
        self.current = None
        self.progress = self.rate = 0.0
        self.error = None
        self.done = False
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.cancel.set()
        self.thread.join()

    @property
    def cancelled(self):
        return self.cancel.is_set()

    def run(self):
        steps = {"seqw": self.seq_write, "seqr": self.seq_read,
                 "rndr": self.random_read, "rndw": self.random_write}
        try:
            for key in self.phases:
                result = steps[key]()
                if self.cancel.is_set():
                    break
                self.results[key] = result
        except OSError as e:
            self.error = e.strerror or str(e)
        finally:
            self.current = None
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as e:
                self.error = self.error or f"could not remove {self.path}: {e.strerror}"
            self.done = True

    def seq_write(self):
        buf = memoryview(os.urandom(BLOCK))
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            no_cache(fd)
            m = Meter(self, "seqw", total=self.size)
            while m.running():
                m.add(os.write(fd, buf[:min(BLOCK, self.size - m.bytes)]))
            full_sync(fd)                      # the time to reach the device counts
            return m.finish()
        finally:
            os.close(fd)

    def seq_read(self):
        buf = bytearray(BLOCK)
        with open(self.path, "rb", buffering=0) as f:
            no_cache(f.fileno())
            m = Meter(self, "seqr", total=self.size)
            while m.running():
                n = f.readinto(buf)
                if not n:
                    break
                m.add(n)
            return m.finish()

    def random_read(self):
        blocks = self.size // SMALL
        rng = random.Random()
        fd = os.open(self.path, os.O_RDONLY)
        try:
            no_cache(fd)
            m = Meter(self, "rndr", seconds=RANDOM_SECONDS)
            while m.running():
                m.add(len(os.pread(fd, SMALL, rng.randrange(blocks) * SMALL)))
            return m.finish()
        finally:
            os.close(fd)

    def random_write(self):
        blocks = self.size // SMALL
        rng = random.Random()
        buf = os.urandom(SMALL)
        fd = os.open(self.path, os.O_WRONLY)
        try:
            no_cache(fd)
            m = Meter(self, "rndw", seconds=RANDOM_SECONDS)
            while m.running():
                m.add(os.pwrite(fd, buf, rng.randrange(blocks) * SMALL))
            full_sync(fd)
            return m.finish()
        finally:
            os.close(fd)


def start_bench(drive, size_mib, phases):
    """A started Bench, or an error message saying why the drive can't be tested."""
    if not drive.writable:
        return None, f"{drive.name} is read-only ({drive.fs}), nothing can be written to it"
    size = test_bytes(drive, size_mib)
    if size < MIN_TEST_BYTES:
        return None, f"{drive.name} has only {fmt_bytes(drive.free)} free"
    bench = Bench(drive, size, phases)
    bench.start()
    return bench, None


def result_text(key, result):
    rate, iops = result
    if key in SEQUENTIAL:
        return fmt_rate(rate)
    return f"{fmt_rate(rate)}  {iops:,.0f} IOPS"


def graph_columns(samples, width):
    """Squeeze the samples into at most `width` columns by averaging neighbours."""
    if len(samples) <= width:
        return samples
    cols = []
    for i in range(width):
        part = samples[i * len(samples) // width:(i + 1) * len(samples) // width]
        cols.append(sum(part) / len(part))
    return cols


def eject(drive):
    try:
        r = subprocess.run(["diskutil", "eject", drive.whole or drive.mount],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as e:
        return str(e)
    if r.returncode:
        lines = (r.stderr or r.stdout).strip().splitlines()
        return lines[-1] if lines else "eject failed"
    return None


def main(stdscr, args):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # title, headings
    curses.init_pair(2, curses.COLOR_GREEN, -1)   # results
    curses.init_pair(3, curses.COLOR_YELLOW, -1)  # running, warnings
    curses.init_pair(4, curses.COLOR_RED, -1)     # errors
    curses.init_pair(5, curses.COLOR_WHITE, -1)   # dim / labels
    dim = curses.color_pair(5) | curses.A_DIM

    stdscr.timeout(200)
    drives = scan_drives(args.all)
    volumes = set(os.listdir("/Volumes"))
    last_scan = time.time()
    selected = 0
    size_mib = args.size
    history = {}          # mount point -> last Bench on it
    bench = None
    message, message_attr = "", 0

    def selected_drive():
        return drives[selected] if 0 <= selected < len(drives) else None

    while True:
        running = bench is not None and not bench.done
        if bench is not None and bench.done and bench is history.get(bench.drive.mount):
            if bench.error:
                message, message_attr = f"Error: {bench.error}", curses.color_pair(4)
            elif bench.cancelled:
                message, message_attr = "Cancelled, test file removed.", curses.color_pair(3)
            else:
                message, message_attr = f"Done, {bench.drive.name} tested.", curses.color_pair(2)
            bench = None

        # Rescan when a volume comes or goes, and now and then for free space; never mid-test.
        if not running and time.time() - last_scan >= RESCAN_SECONDS:
            try:
                now_volumes = set(os.listdir("/Volumes"))
            except OSError:
                now_volumes = volumes
            if now_volumes != volumes:
                current = selected_drive()
                drives = scan_drives(args.all)
                volumes = now_volumes
                mounts = [d.mount for d in drives]
                selected = mounts.index(current.mount) if current and current.mount in mounts else 0
            last_scan = time.time()

        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, text, attr=0, h=h, w=w):
            if 0 <= y < h and 0 <= x < w - 1:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)

        title = " USB Drive Speed Test "
        put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(1) | curses.A_BOLD)

        if h < 18 or w < 64:
            put(2, 2, f"Window too small ({w}x{h}), needs 64x18.", curses.color_pair(3))
            put(h - 1, 2, "q: quit", dim)
            stdscr.refresh()
            if stdscr.getch() in (ord("q"), 27) and not running:
                break
            continue

        name_w = max(10, min(28, w - 62))
        y = 2
        put(y, 4, f"{'Drive':<{name_w}} {'Bus':<10} {'Link':<14} {'Size':>9} {'Free':>9}  Format",
            curses.color_pair(1) | curses.A_BOLD)
        y += 1
        if not drives:
            hint = "No USB drives mounted. Plug one in, it shows up here by itself."
            put(y, 4, hint, dim)
            if not args.all:
                put(y + 1, 4, "(--all lists every mounted volume, disk images included)", dim)
            y += 2
        list_rows = max(1, min(len(drives), h - 17))   # the graph below gives way first
        top = max(0, min(selected - list_rows + 1, len(drives) - list_rows)) if drives else 0
        for i, d in enumerate(drives[top:top + list_rows], start=top):
            marker = ">" if i == selected else " "
            attr = curses.A_REVERSE if i == selected else 0
            link = fmt_link(d.link_bps) or "-"
            line = (f"{d.name[:name_w]:<{name_w}} {d.bus[:10]:<10} {link:<14} "
                    f"{fmt_bytes(d.size):>9} {fmt_bytes(d.free):>9}  {d.fs}")
            put(y, 2, marker, curses.color_pair(1) | curses.A_BOLD)
            put(y, 4, line, attr)
            y += 1

        drive = bench.drive if running else selected_drive()
        if drive:
            detail = " · ".join(x for x in (drive.mount, drive.model, drive.whole) if x)
            put(y, 4, detail, dim)
            y += 1
            if drive.link_bps and drive.link_bps <= 480_000_000:
                put(y, 4, "USB 2 link: around 40 MB/s is the most this port or cable allows.",
                    curses.color_pair(3))
            elif not drive.writable:
                put(y, 4, f"Read-only volume ({drive.fs}), it can't be tested.", curses.color_pair(3))
        y += 2

        shown = bench if running else (history.get(drive.mount) if drive else None)
        size_note = f"Test file {size_mib} MiB"
        if shown and shown.size != size_mib * 1024 * 1024:
            size_note += f" (ran with {shown.size // (1024 * 1024)} MiB, all that fit)"
        put(y, 4, size_note, curses.A_BOLD)
        put(y, 4 + len(size_note), f"   random tests run {RANDOM_SECONDS}s each", dim)
        y += 1

        bar_w = max(10, min(30, w - 50))
        for key, label in PHASES:
            y += 1
            put(y, 4, f"{label:<18}", curses.A_BOLD)
            x = 23
            if shown is None or key not in shown.phases:
                put(y, x, "-", dim)
            elif key in shown.results:
                put(y, x, result_text(key, shown.results[key]), curses.color_pair(2) | curses.A_BOLD)
            elif running and shown.current == key:
                filled = round(bar_w * shown.progress)
                put(y, x, "█" * filled + "░" * (bar_w - filled), curses.color_pair(3))
                live = f"{shown.progress * 100:3.0f}%  {fmt_rate(shown.rate)}" if shown.rate else "starting"
                put(y, x + bar_w + 2, live, curses.color_pair(3))
            elif running:
                put(y, x, "waiting", dim)
            else:
                started = key in shown.samples
                put(y, x, "-" if not started else "cancelled" if shown.cancelled else "failed", dim)
        y += 2

        # Graph of the running phase, or of the sequential write once the run is over:
        # that is where a drive's write cache filling up shows as a cliff.
        if shown:
            key = shown.current if running and shown.current else "seqw"
            samples = list(shown.samples.get(key, []))
            graph_w = w - 16
            rows = min(GRAPH_ROWS, h - 4 - y - 1)       # stay clear of the message line
            if samples and rows >= 3:
                cols = graph_columns(samples, graph_w)
                peak = max(cols) or 1
                put(y, 4, f"{LABELS[key]} over time", curses.color_pair(1))
                put(y, 4 + len(LABELS[key]) + 11,
                    f"  peak {fmt_rate(max(samples))}  low {fmt_rate(min(samples))}", dim)
                for row in range(rows):
                    line_y = y + 1 + row
                    floor = rows - 1 - row                # rows counted from the bottom
                    if row == 0:
                        put(line_y, 4, f"{peak / 1e6:>7.0f}", dim)
                    elif row == rows - 1:
                        put(line_y, 4, f"{0:>7}", dim)
                    chars = []
                    for value in cols:
                        eighths = round(value / peak * rows * 8)
                        chars.append(BARS[max(0, min(8, eighths - floor * 8))])
                    put(line_y, 12, "".join(chars), curses.color_pair(2))

        if message:
            put(h - 3, 4, message, message_attr)
        if running:
            footer = "c: cancel  ·  q: cancel and quit"
        else:
            footer = "↑↓ drive · enter test · s seq only · +/- size · e eject · r rescan · q quit"
        put(h - 1, 2, footer, dim)
        stdscr.refresh()

        key = stdscr.getch()
        if key == -1:
            continue
        if running:
            if key in (ord("c"), ord("q")):
                put(h - 3, 4, "Cancelling, removing the test file...".ljust(w - 6), curses.color_pair(3))
                stdscr.refresh()
                bench.stop()
                if key == ord("q"):
                    break
            continue
        if key == ord("q"):
            break
        elif key in (curses.KEY_UP, ord("k")) and drives:
            selected = (selected - 1) % len(drives)
        elif key in (curses.KEY_DOWN, ord("j")) and drives:
            selected = (selected + 1) % len(drives)
        elif key in (ord("+"), ord("=")):
            size_mib = SIZES_MIB[min(len(SIZES_MIB) - 1, SIZES_MIB.index(size_mib) + 1)]
        elif key in (ord("-"), ord("_")):
            size_mib = SIZES_MIB[max(0, SIZES_MIB.index(size_mib) - 1)]
        elif key in (ord("r"), curses.KEY_RESIZE):
            if key == ord("r"):
                current = selected_drive()
                drives = scan_drives(args.all)
                mounts = [d.mount for d in drives]
                selected = mounts.index(current.mount) if current and current.mount in mounts else 0
                message = ""
        elif key in (10, 13, curses.KEY_ENTER, ord("s")) and selected_drive():
            d = selected_drive()
            # Free space moves between runs; take a fresh look before sizing the file.
            fresh = [x for x in scan_drives(args.all) if x.mount == d.mount]
            if fresh:
                d = drives[selected] = fresh[0]
            bench, error = start_bench(d, size_mib, SEQUENTIAL if key == ord("s") else FULL)
            if error:
                message, message_attr = error, curses.color_pair(4)
            else:
                history[d.mount] = bench
                message = ""
        elif key == ord("e") and selected_drive():
            d = selected_drive()
            put(h - 3, 4, f"Ejecting {d.name}...".ljust(w - 6), curses.color_pair(3))
            stdscr.refresh()
            error = eject(d)
            if error:
                message, message_attr = f"Eject failed: {error}", curses.color_pair(4)
            else:
                message, message_attr = f"{d.name} ejected, safe to unplug.", curses.color_pair(2)
                history.pop(d.mount, None)
                drives = scan_drives(args.all)
                volumes = set(os.listdir("/Volumes"))
                selected = min(selected, max(0, len(drives) - 1))


def run_once(args):
    """Test one volume without the TUI and print the results."""
    drives = scan_drives(show_all=True)
    target = os.path.realpath(args.once)
    drive = next((d for d in drives if os.path.realpath(d.mount) == target), None)
    if drive is None:
        print(f"{args.once} is not a mounted volume under /Volumes", file=sys.stderr)
        return 1
    bench, error = start_bench(drive, args.size, SEQUENTIAL if args.seq_only else FULL)
    if error:
        print(error, file=sys.stderr)
        return 1
    link = fmt_link(drive.link_bps)
    print(f"{drive.name}  ({drive.mount}, {drive.bus}{', ' + link if link else ''}, {drive.fs})")
    print(f"test file {bench.size // (1024 * 1024)} MiB")
    printed = set()
    try:
        while not bench.done:
            time.sleep(0.2)
            for key in bench.phases:
                if key in bench.results and key not in printed:
                    print(f"\r  {LABELS[key]:<18} {result_text(key, bench.results[key]):<40}")
                    printed.add(key)
            if bench.current and sys.stderr.isatty():
                print(f"\r  {LABELS[bench.current]:<18} {bench.progress * 100:3.0f}%  "
                      f"{fmt_rate(bench.rate):<20}", end="", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("\ncancelling...", file=sys.stderr)
        bench.stop()
    for key in bench.phases:
        if key in bench.results and key not in printed:
            print(f"\r  {LABELS[key]:<18} {result_text(key, bench.results[key]):<40}")
    if bench.error:
        print(f"error: {bench.error}", file=sys.stderr)
        return 1
    return 130 if bench.cancelled else 0


def parse_args():
    p = argparse.ArgumentParser(description="Measure the read and write speed of USB drives.")
    p.add_argument("--size", type=int, default=DEFAULT_SIZE_MIB, choices=SIZES_MIB,
                   help=f"test file size in MiB (default {DEFAULT_SIZE_MIB})")
    p.add_argument("--all", action="store_true",
                   help="list every mounted volume, not only external drives (disk images too)")
    p.add_argument("--list", action="store_true", help="print the drives found and exit")
    p.add_argument("--once", metavar="VOLUME", help="test this volume without the TUI, e.g. /Volumes/STICK")
    p.add_argument("--seq-only", action="store_true", help="with --once: skip the random 4K tests")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.list:
        for d in scan_drives(args.all):
            link = fmt_link(d.link_bps) or "-"
            print(f"{d.mount:<28} {d.bus:<10} {link:<16} {fmt_bytes(d.size):>9} "
                  f"{fmt_bytes(d.free):>9} free  {d.fs}{'' if d.writable else '  read-only'}")
    elif args.once:
        sys.exit(run_once(args))
    else:
        curses.wrapper(main, args)
