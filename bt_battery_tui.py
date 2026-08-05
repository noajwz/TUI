#!/usr/bin/env python3
"""Terminal UI showing battery levels for Bluetooth devices (macOS)."""

import curses
import json
import subprocess
import time

REFRESH_SECONDS = 5

BATTERY_LABELS = {
    "Main": "Battery",
    "Case": "Case",
    "Left": "Left",
    "Right": "Right",
}


def fetch_devices():
    try:
        out = subprocess.run(
            ["system_profiler", "-json", "SPBluetoothDataType"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except Exception as e:
        return [], str(e)

    try:
        data = json.loads(out)["SPBluetoothDataType"][0]
    except Exception as e:
        return [], f"unexpected output: {e}"

    devices = []
    for connected, key in ((True, "device_connected"), (False, "device_not_connected")):
        for entry in data.get(key, []):
            for name, attrs in entry.items():
                batteries = []
                for field, value in attrs.items():
                    if "batteryLevel" in field:
                        suffix = field.replace("device_batteryLevel", "")
                        label = BATTERY_LABELS.get(suffix, suffix or "Battery")
                        try:
                            pct = int(str(value).rstrip("%"))
                        except ValueError:
                            continue
                        batteries.append((label, pct))
                devices.append({
                    "name": name,
                    "connected": connected,
                    "minor_type": attrs.get("device_minorType", ""),
                    "batteries": batteries,
                })
    return devices, None


def battery_color_pair(pct):
    if pct < 20:
        return 4
    if pct < 50:
        return 3
    return 2


def draw_bar(width, pct):
    filled = max(0, min(width, round(width * pct / 100)))
    return "#" * filled + "-" * (width - filled)


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)    # title
    curses.init_pair(2, curses.COLOR_GREEN, -1)   # good battery
    curses.init_pair(3, curses.COLOR_YELLOW, -1)  # medium battery
    curses.init_pair(4, curses.COLOR_RED, -1)     # low battery
    curses.init_pair(5, curses.COLOR_WHITE, -1)   # dim / labels

    stdscr.timeout(1000)
    devices, error = fetch_devices()
    last_fetch = time.time()

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        def put(y, x, text, attr=0):
            if 0 <= y < h and x < w:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)

        title = " Bluetooth Battery Monitor "
        put(0, max(0, (w - len(title)) // 2), title, curses.color_pair(1) | curses.A_BOLD)

        if error:
            put(2, 2, f"Error: {error}", curses.color_pair(4))
        elif not devices:
            put(2, 2, "No Bluetooth devices found.", curses.color_pair(5))
        else:
            connected = [d for d in devices if d["connected"]]
            not_connected = [d for d in devices if not d["connected"]]

            y = 2
            for section_title, section in (("Connected", connected), ("Not Connected", not_connected)):
                if not section:
                    continue
                put(y, 2, section_title, curses.color_pair(1) | curses.A_BOLD)
                y += 1
                for d in section:
                    if y >= h - 2:
                        break
                    name = d["name"]
                    put(y, 4, name, curses.A_BOLD)
                    y += 1
                    if d["batteries"]:
                        for label, pct in d["batteries"]:
                            if y >= h - 2:
                                break
                            bar = draw_bar(20, pct)
                            attr = curses.color_pair(battery_color_pair(pct))
                            put(y, 6, f"{label:8s} [{bar}] {pct:3d}%", attr)
                            y += 1
                    else:
                        put(y, 6, "(no battery data)", curses.color_pair(5))
                        y += 1
                y += 1

        age = int(time.time() - last_fetch)
        footer = f"updated {age}s ago  ·  r: refresh  ·  q: quit"
        put(h - 1, 2, footer, curses.color_pair(5))

        stdscr.refresh()
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            break
        elif key in (ord("r"), ord("R")):
            devices, error = fetch_devices()
            last_fetch = time.time()
        elif key == -1 and time.time() - last_fetch >= REFRESH_SECONDS:
            devices, error = fetch_devices()
            last_fetch = time.time()


if __name__ == "__main__":
    curses.wrapper(main)
