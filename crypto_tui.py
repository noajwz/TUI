#!/usr/bin/env python3
"""Terminal UI with live prices and charts for Bitcoin, Ethereum and Monero (CoinGecko API)."""

import argparse
import curses
import json
import locale
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://api.coingecko.com/api/v3"
USER_AGENT = "crypto-tui/1.0 (terminal price checker)"

COINS = [("bitcoin", "BTC", "Bitcoin"), ("ethereum", "ETH", "Ethereum"), ("monero", "XMR", "Monero")]
CURRENCIES = ["eur", "usd"]                    # `c` cycles these; --currency accepts any vs_currency
SYMBOLS = {"eur": "€", "usd": "$", "gbp": "£", "jpy": "¥", "btc": "₿"}

# (key, label, days, x-axis label format, seconds a fetched chart is kept)
RANGES = [
    ("1", "1D", 1, "%H:%M", 300),
    ("2", "7D", 7, "%a %d", 900),
    ("3", "30D", 30, "%d %b", 1800),
    ("4", "90D", 90, "%d %b", 3600),
    ("5", "1Y", 365, "%b '%y", 3600),
]
DEFAULT_RANGE = 1                              # 7D

REFRESH_SECONDS = 60                           # market data; the free API allows a few calls a minute
RETRY_SECONDS = 30                             # after a failed chart fetch, so a 429 isn't prolonged
MIN_W, MIN_H = 60, 20
VOLUME_ROWS = 2
Y_LABEL_W = 11

BARS = " ▁▂▃▄▅▆▇█"
# Braille dot bits by [row][column] inside a 2×4 cell.
DOTS = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]


def api_get(path, params):
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp), None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "rate limited by CoinGecko, try again in a minute"
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"network error: {getattr(e, 'reason', e)}"
    except json.JSONDecodeError:
        return None, "bad JSON from CoinGecko"


def fetch_markets(currency):
    data, err = api_get("/coins/markets", {
        "vs_currency": currency,
        "ids": ",".join(cid for cid, _, _ in COINS),
        "sparkline": "true",
        "price_change_percentage": "1h,24h,7d,30d,1y",
    })
    if err:
        return None, err
    if not isinstance(data, list):
        return None, "unexpected reply from CoinGecko"
    return {m["id"]: m for m in data if isinstance(m, dict) and "id" in m}, None


def fetch_chart(coin_id, currency, days):
    """Returns ([(t, price)], [(t, volume)]) with t in seconds."""
    data, err = api_get(f"/coins/{coin_id}/market_chart", {"vs_currency": currency, "days": days})
    if err:
        return None, err
    try:
        prices = [(t / 1000, p) for t, p in data["prices"] if p is not None]
        volumes = [(t / 1000, v) for t, v in data["total_volumes"] if v is not None]
    except (KeyError, TypeError, ValueError):
        return None, "unexpected chart data from CoinGecko"
    if len(prices) < 2:
        return None, "not enough chart data"
    return (prices, volumes), None


def sparkline_chart(market):
    """The hourly 7-day sparkline that comes with the market data, as a chart without volumes —
    shown for 7D until (or if never) its own chart arrives, since it costs no extra API call."""
    prices = ((market or {}).get("sparkline_in_7d") or {}).get("price") or []
    if len(prices) < 2:
        return None
    try:
        end = datetime.fromisoformat(market["last_updated"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        end = time.time()
    n = len(prices)
    return [(end - (n - 1 - i) * 3600, p) for i, p in enumerate(prices)], []


# ---------------------------------------------------------------- formatting


def symbol(currency):
    return SYMBOLS.get(currency, currency.upper() + " ")


def fmt_price(value, currency):
    if value is None:
        return "—"
    if value < 0:
        return "−" + fmt_price(-value, currency)
    if abs(value) >= 1000:
        text = f"{value:,.0f}" if abs(value) >= 100_000 else f"{value:,.2f}"
    elif abs(value) >= 1:
        text = f"{value:,.2f}"
    else:
        text = f"{value:.6f}" if currency == "btc" else f"{value:.4f}"
    return symbol(currency) + text


def fmt_big(value, currency=None):
    if value is None:
        return "—"
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            text = f"{value / size:.2f}{suffix}"
            break
    else:
        text = f"{value:.2f}"
    return symbol(currency) + text if currency else text


def fmt_pct(value):
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '−'}{abs(value):.2f}%"


def fmt_axis(value, currency):
    """A short price for the y axis, at most Y_LABEL_W - 2 columns."""
    if abs(value) >= 10_000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.1f}"
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.5f}"


# ---------------------------------------------------------------- charts


def resample(points, n, start, end):
    """Bucket (t, v) points into n equal time slots from start to end; the last value of each
    slot wins and empty slots carry the previous value forward."""
    if n <= 0 or not points:
        return []
    span = (end - start) or 1
    out = [None] * n
    for t, v in points:
        i = min(n - 1, max(0, int((t - start) / span * n)))
        out[i] = v
    prev = next((v for v in out if v is not None), points[0][1])
    for i, v in enumerate(out):
        if v is None:
            out[i] = prev
        prev = out[i]
    return out


def bucket_means(points, n, start, end):
    """Like resample(), but averages each slot. Used for volumes, which CoinGecko gives as a
    rolling 24h figure rather than per interval, so averaging is right and summing is not."""
    if n <= 0 or not points:
        return []
    span = (end - start) or 1
    sums, counts = [0.0] * n, [0] * n
    for t, v in points:
        i = min(n - 1, max(0, int((t - start) / span * n)))
        sums[i] += v
        counts[i] += 1
    return [s / c if c else 0.0 for s, c in zip(sums, counts)]


def braille_chart(values, width, height, lo, hi):
    """A line through `values` (2 per cell column) drawn in braille, `height` rows tall."""
    dots_h = height * 4
    grid = [[0] * width for _ in range(height)]
    span = (hi - lo) or 1

    def dot(x, y):
        grid[y // 4][x // 2] |= DOTS[y % 4][x % 2]

    prev = None
    for x, v in enumerate(values[: width * 2]):
        y = min(dots_h - 1, max(0, round((hi - v) / span * (dots_h - 1))))
        if prev is None:
            dot(x, y)
        else:
            # A jump is drawn half in the previous column and half in this one, so it stays joined.
            mid = (prev + y) // 2
            for yy in range(min(prev, mid), max(prev, mid) + 1):
                dot(x - 1, yy)
            for yy in range(min(mid, y), max(mid, y) + 1):
                dot(x, yy)
        prev = y
    return ["".join(chr(0x2800 + c) if c else " " for c in row) for row in grid]


def bar_rows(values, height):
    """Vertical bars with eighth-block resolution, from just under the smallest value to the
    largest — a rolling 24h volume barely moves, and bars from zero would all look the same."""
    peak = max(values, default=0)
    base = min(values, default=0)
    base -= (peak - base) * 0.1 or peak * 0.1
    span = (peak - base) or 1
    rows = []
    for r in range(height):
        floor = (height - 1 - r) * 8
        line = []
        for v in values:
            eighths = round((v - base) / span * height * 8)
            line.append(BARS[max(0, min(8, eighths - floor))])
        rows.append("".join(line))
    return rows


def sparkline(values, width):
    if not values or width <= 0:
        return ""
    cols = resample(list(enumerate(values)), width, 0, len(values))
    lo, hi = min(cols), max(cols)
    span = (hi - lo) or 1
    return "".join(BARS[1 + round((v - lo) / span * 7)] for v in cols)


def nice_ticks(lo, hi, count):
    """Evenly spaced y-axis values between lo and hi, top first."""
    if count < 2 or hi <= lo:
        return [hi]
    return [hi - (hi - lo) * i / (count - 1) for i in range(count)]


# ---------------------------------------------------------------- app


class App:
    def __init__(self, currency):
        self.currencies = CURRENCIES if currency in CURRENCIES else [currency] + CURRENCIES
        self.currency = currency
        self.selected = 0
        self.range_i = DEFAULT_RANGE
        self.markets = {}            # currency -> {coin_id: market}
        self.markets_at = {}         # currency -> time of the last fetch, failed or not
        self.updated = {}            # currency -> time of the last successful fetch
        self.charts = {}             # (coin_id, currency, days) -> (fetched_at, (prices, volumes))
        self.failed = {}             # same key -> (time of the failed fetch, error)
        self.cursor = None           # column index of the crosshair, or None
        self.message = ""
        self.loading = ""

    @property
    def coin(self):
        return COINS[self.selected]

    @property
    def range(self):
        return RANGES[self.range_i]

    def market(self, coin_id=None):
        return self.markets.get(self.currency, {}).get(coin_id or self.coin[0])

    def chart(self):
        entry = self.charts.get((self.coin[0], self.currency, self.range[2]))
        if entry:
            return entry[1]
        if self.range[2] == 7:
            return sparkline_chart(self.market())
        return None

    def refresh_markets(self, stdscr, force=False):
        at = self.markets_at.get(self.currency, 0)
        if not force and time.time() - at < REFRESH_SECONDS:
            return
        self.paint_loading(stdscr, "prices")
        data, err = fetch_markets(self.currency)
        self.markets_at[self.currency] = time.time()   # also on failure, so errors don't hammer
        if err:
            self.message = err
        else:
            self.markets[self.currency] = data
            self.updated[self.currency] = time.time()
            self.message = ""

    def refresh_chart(self, stdscr, force=False):
        key = (self.coin[0], self.currency, self.range[2])
        entry = self.charts.get(key)
        if not force and entry and time.time() - entry[0] < self.range[4]:
            return
        if not force and key in self.failed and time.time() - self.failed[key][0] < RETRY_SECONDS:
            return
        self.paint_loading(stdscr, f"{self.coin[1]} {self.range[1]} chart")
        data, err = fetch_chart(*key)
        if err:
            self.message = err
            self.failed[key] = (time.time(), err)     # an older chart, if any, stays on screen
        else:
            self.charts[key] = (time.time(), data)
            self.failed.pop(key, None)
            self.message = ""

    def paint_loading(self, stdscr, what):
        self.loading = f"loading {what}…"
        self.draw(stdscr)
        stdscr.refresh()
        self.loading = ""

    # ------------------------------------------------------------ drawing

    def draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            put(stdscr, 0, 0, f"Window too small ({w}×{h}), need {MIN_W}×{MIN_H}.", w)
            return
        cur = self.currency
        row = 0

        title = f" ₿ Crypto prices · {cur.upper()}"
        put(stdscr, row, 0, title, w, curses.color_pair(1) | curses.A_BOLD)
        at = self.updated.get(cur)
        status = self.loading or (f"updated {datetime.fromtimestamp(at):%H:%M:%S}" if at else "")
        put(stdscr, row, w - len(status) - 1, status, w, curses.color_pair(5))
        row += 1

        # Coin table.
        periods = ["1h", "24h", "7d"] if w >= 66 else ["24h", "7d"]
        spark_w = max(0, w - 36 - 10 * len(periods))
        header = f"   {'coin':<15}{'price':>14}" + "".join(f"{p:>10}" for p in periods)
        if spark_w >= 6:
            header += "   " + "7 days".ljust(spark_w)
        put(stdscr, row + 1, 0, header, w, curses.color_pair(5))
        row += 2
        for i, (cid, sym, name) in enumerate(COINS):
            m = self.markets.get(cur, {}).get(cid)
            sel = i == self.selected
            attr = curses.A_BOLD if sel else curses.A_NORMAL
            put(stdscr, row, 0, (" ▶ " if sel else "   ") + f"{sym:<5}{name:<10}", w,
                curses.color_pair(1) | attr if sel else attr)
            if m:
                put(stdscr, row, 18, f"{fmt_price(m.get('current_price'), cur):>14}", w, attr)
                col = 32
                for key in periods:
                    pct = m.get(f"price_change_percentage_{key}_in_currency")
                    put(stdscr, row, col, f"{fmt_pct(pct):>10}", w, change_attr(pct))
                    col += 10
                spark = (m.get("sparkline_in_7d") or {}).get("price") or []
                if spark_w >= 6 and spark:
                    first, last = spark[0], spark[-1]
                    put(stdscr, row, col + 3, sparkline(spark, spark_w), w,
                        change_attr(last - first))
            else:
                put(stdscr, row, 18, f"{'—':>14}", w, curses.color_pair(5))
            row += 1
        row += 1
        put(stdscr, row, 0, "─" * w, w, curses.color_pair(5))
        row += 1

        # Chart heading: coin, range tabs, and the change over the range.
        cid, sym, name = self.coin
        put(stdscr, row, 1, f"{name} ({sym}/{cur.upper()})", w, curses.A_BOLD)
        col = len(name) + len(sym) + len(cur) + 7
        for i, (_, label, *_rest) in enumerate(RANGES):
            text = f" {label} "
            put(stdscr, row, col, text, w, curses.A_REVERSE if i == self.range_i else curses.color_pair(5))
            col += len(text) + 1
        chart = self.chart()

        footer_rows = 4                               # crosshair, stats ×2, keys
        chart_h = h - row - 1 - 1 - footer_rows       # minus heading row and x axis
        show_volume = chart_h - VOLUME_ROWS >= 5 and bool(chart and chart[1])
        if show_volume:
            chart_h -= VOLUME_ROWS
        plot_w = w - Y_LABEL_W - 2
        row += 1

        if not chart:
            failed = self.failed.get((cid, cur, self.range[2]))
            text = "no chart data yet"
            if failed:
                wait = max(0, round(RETRY_SECONDS - (time.time() - failed[0])))
                text = f"chart unavailable: {failed[1]} — retrying in {wait}s"
            put(stdscr, row + chart_h // 2, Y_LABEL_W, text, w, curses.color_pair(5))
        else:
            row = self.draw_chart(stdscr, chart, row, chart_h, plot_w, show_volume, w)
        row = h - footer_rows
        self.draw_stats(stdscr, row, w, chart)
        if self.message:
            put_runs(stdscr, h - 1, 0, [(f" {self.message} ", curses.color_pair(4) | curses.A_REVERSE),
                                        ("  r retry  q quit", curses.color_pair(5))], w)
        else:
            keys = ["↑↓ coin", "1-5 range", "←→ inspect", "space hide", "c currency", "r refresh", "q quit"]
            put_runs(stdscr, h - 1, 0, [(f" {k} ", curses.color_pair(5)) for k in keys], w)

    def draw_chart(self, stdscr, chart, row, chart_h, plot_w, show_volume, w):
        prices, volumes = chart
        cur = self.currency
        start, end = prices[0][0], prices[-1][0]
        cols = resample(prices, plot_w * 2, start, end)
        lo, hi = min(cols), max(cols)
        pad = (hi - lo) * 0.04 or hi * 0.01 or 1
        lo_ax, hi_ax = lo - pad, hi + pad
        up = cols[-1] >= cols[0]
        line_attr = curses.color_pair(2 if up else 4)

        lines = braille_chart(cols, plot_w, chart_h, lo_ax, hi_ax)
        tick_step = max(2, chart_h // 4)
        tick_rows = set(range(0, chart_h, tick_step))
        for r, text in enumerate(lines):
            y = row + r
            if r in tick_rows:
                value = hi_ax - (hi_ax - lo_ax) * (r * 4 + 1.5) / (chart_h * 4 - 1)   # cell middle
                put(stdscr, y, 0, f"{fmt_axis(value, cur):>{Y_LABEL_W - 2}} ┤", w, curses.color_pair(5))
            else:
                put(stdscr, y, Y_LABEL_W - 1, "│", w, curses.color_pair(5))
            put(stdscr, y, Y_LABEL_W + 1, text, w, line_attr)

        cursor = None if self.cursor is None else min(self.cursor, plot_w - 1)
        if cursor is not None:
            cx = Y_LABEL_W + 1 + cursor
            for r in range(chart_h):
                ch = lines[r][cursor]
                put(stdscr, row + r, cx, "│" if ch == " " else ch, w,
                    curses.color_pair(3) | (curses.A_REVERSE if ch != " " else 0))
        row += chart_h

        # X axis with time labels.
        put(stdscr, row, Y_LABEL_W - 1, "└" + "─" * (plot_w + 1), w, curses.color_pair(5))
        fmt = self.range[3]
        label_w = len(datetime.fromtimestamp(end).strftime(fmt))
        step = max(label_w + 4, 14)
        x = 0
        while x + label_w <= plot_w:
            t = start + (end - start) * (x + label_w / 2) / plot_w
            put(stdscr, row, Y_LABEL_W + 1 + x, f" {datetime.fromtimestamp(t).strftime(fmt)} ", w,
                curses.color_pair(5))
            x += step
        row += 1

        if show_volume:
            vols = bucket_means(volumes, plot_w, start, end)
            bars = bar_rows(vols, VOLUME_ROWS)
            put(stdscr, row, 0, f"{'24h vol':>{Y_LABEL_W - 2}}", w, curses.color_pair(5))
            for r, text in enumerate(bars):
                put(stdscr, row + r, Y_LABEL_W + 1, text, w, curses.color_pair(6))
                if cursor is not None:
                    put(stdscr, row + r, Y_LABEL_W + 1 + cursor, text[cursor], w, curses.color_pair(3))
            row += VOLUME_ROWS
        return row

    def draw_stats(self, stdscr, row, w, chart):
        cur = self.currency
        m = self.market()
        dim = curses.color_pair(5)
        if chart:
            prices, _ = chart
            first, last = prices[0][1], prices[-1][1]
            hi = max(p for _, p in prices)
            lo = min(p for _, p in prices)
            plot_w = w - Y_LABEL_W - 2
            if self.cursor is not None:
                start, end = prices[0][0], prices[-1][0]
                c = min(self.cursor, plot_w - 1)
                t = start + (end - start) * (c + 0.5) / plot_w
                vals = resample(prices, plot_w, start, end)
                v = vals[c]
                line = [(f" {datetime.fromtimestamp(t):%a %d %b %Y %H:%M}  ", curses.color_pair(3)),
                        (fmt_price(v, cur), curses.A_BOLD),
                        (f"  {fmt_pct((v / first - 1) * 100)} from start of range", change_attr(v - first)),
                        (f"   {fmt_pct((last / v - 1) * 100)} since", change_attr(last - v))]
            else:
                line = [(f" {self.range[1]} change ", dim),
                        (fmt_pct((last / first - 1) * 100), change_attr(last - first) | curses.A_BOLD),
                        (f" ({fmt_price(last - first, cur)})", change_attr(last - first)),
                        ("   high ", dim), (fmt_price(hi, cur), curses.A_NORMAL),
                        ("   low ", dim), (fmt_price(lo, cur), curses.A_NORMAL)]
            put_runs(stdscr, row, 0, line, w)
        if m:
            ath = m.get("ath")
            ath_date = (m.get("ath_date") or "")[:10]
            supply = m.get("circulating_supply")
            max_supply = m.get("max_supply")
            runs = [(" rank ", dim), (f"#{m.get('market_cap_rank') or '—'}", curses.A_NORMAL),
                    ("   mcap ", dim), (fmt_big(m.get("market_cap"), cur), curses.A_NORMAL),
                    ("   24h vol ", dim), (fmt_big(m.get("total_volume"), cur), curses.A_NORMAL),
                    ("   supply ", dim), (fmt_big(supply), curses.A_NORMAL),
                    (f" / {fmt_big(max_supply)}" if max_supply else " (no cap)", dim)]
            put_runs(stdscr, row + 1, 0, runs, w)
            runs = [(" ATH ", dim), (fmt_price(ath, cur), curses.A_NORMAL),
                    (f" {fmt_pct(m.get('ath_change_percentage'))} ({ath_date})", dim)]
            for key in ("30d", "1y"):
                pct = m.get(f"price_change_percentage_{key}_in_currency")
                runs += [(f"   {key} ", dim), (fmt_pct(pct), change_attr(pct))]
            put_runs(stdscr, row + 2, 0, runs, w)


def change_attr(value):
    if value is None:
        return curses.color_pair(5)
    return curses.color_pair(2 if value >= 0 else 4)


def put(stdscr, y, x, text, w, attr=0):
    h, _ = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w or x < 0:
        return
    text = text[: w - x - (1 if y == h - 1 else 0)]
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def put_runs(stdscr, y, x, runs, w):
    """Draws runs left to right, stopping at the first one that doesn't fit whole."""
    for text, attr in runs:
        if x + len(text) > w - 1:
            return
        put(stdscr, y, x, text, w, attr)
        x += len(text)


def run(stdscr, args):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, selection
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # up
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # crosshair
    curses.init_pair(4, curses.COLOR_RED, -1)      # down, errors
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # volume
    stdscr.timeout(1000)

    app = App(args.currency)
    app.range_i = next(i for i, r in enumerate(RANGES) if r[1].lower() == args.range.lower())
    app.selected = next(i for i, c in enumerate(COINS) if c[1].lower() == args.coin.lower())

    while True:
        app.refresh_markets(stdscr)
        app.refresh_chart(stdscr)
        app.draw(stdscr)
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            app.selected = (app.selected - 1) % len(COINS)
        elif key in (curses.KEY_DOWN, ord("j")):
            app.selected = (app.selected + 1) % len(COINS)
        elif key in (ord("\t"), ord("]")):
            app.range_i = (app.range_i + 1) % len(RANGES)
        elif key in (curses.KEY_BTAB, ord("[")):
            app.range_i = (app.range_i - 1) % len(RANGES)
        elif key in (ord(r[0]) for r in RANGES):
            app.range_i = next(i for i, r in enumerate(RANGES) if ord(r[0]) == key)
        elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l")):
            plot_w = stdscr.getmaxyx()[1] - Y_LABEL_W - 2
            step = -1 if key in (curses.KEY_LEFT, ord("h")) else 1
            if app.cursor is None:
                app.cursor = plot_w - 1
            else:
                app.cursor = max(0, min(plot_w - 1, app.cursor + step))
        elif key in (curses.KEY_SLEFT, ord("H")):
            if app.cursor is not None:
                app.cursor = max(0, app.cursor - 10)
        elif key in (curses.KEY_SRIGHT, ord("L")):
            if app.cursor is not None:
                app.cursor = min(stdscr.getmaxyx()[1] - Y_LABEL_W - 3, app.cursor + 10)
        elif key == ord(" "):
            app.cursor = None
        elif key == ord("c"):
            app.currency = app.currencies[(app.currencies.index(app.currency) + 1) % len(app.currencies)]
        elif key == ord("r"):
            app.message = ""
            app.refresh_markets(stdscr, force=True)
            app.refresh_chart(stdscr, force=True)


def print_once(currency):
    bold, dim, green, red, reset = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"
    markets, err = fetch_markets(currency)
    if err:
        print(f"{red}error:{reset} {err}")
        return 1
    print(f"{dim}{'coin':<16}{'price':>14}{'1h':>10}{'24h':>10}{'7d':>10}   7 days{reset}")
    for cid, sym, name in COINS:
        m = markets.get(cid)
        if not m:
            print(f"{sym:<5}{name:<11}{'—':>14}")
            continue
        cells = []
        for key in ("1h", "24h", "7d"):
            pct = m.get(f"price_change_percentage_{key}_in_currency")
            color = dim if pct is None else green if pct >= 0 else red
            cells.append(f"{color}{fmt_pct(pct):>10}{reset}")
        spark = (m.get("sparkline_in_7d") or {}).get("price") or []
        color = green if spark and spark[-1] >= spark[0] else red
        print(f"{bold}{sym:<5}{reset}{name:<11}{bold}{fmt_price(m.get('current_price'), currency):>14}"
              f"{reset}{''.join(cells)}   {color}{sparkline(spark, 28)}{reset}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Live BTC, ETH and XMR prices and charts.")
    parser.add_argument("--currency", default="eur", type=str.lower,
                        help="quote currency, e.g. eur, usd, gbp (default eur)")
    parser.add_argument("--coin", default="BTC", choices=[c[1] for c in COINS], type=str.upper,
                        help="coin selected at start")
    parser.add_argument("--range", default=RANGES[DEFAULT_RANGE][1],
                        choices=[r[1] for r in RANGES], type=str.upper, help="chart range at start")
    parser.add_argument("--once", action="store_true", help="print the prices once and exit")
    args = parser.parse_args()
    if args.once:
        raise SystemExit(print_once(args.currency))
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
