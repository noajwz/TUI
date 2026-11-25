#!/usr/bin/env python3
"""Transaction checker — paste a Bitcoin, Dogecoin or Ethereum transaction hash and see its status,
amounts, fee and confirmations. The coin is found by looking the hash up, each coin has its own tab,
and `r` checks again, so a transaction can be kept open until it has enough confirmations.

With --once, or when the output is piped, it prints the transaction and exits instead.
"""

import argparse
import curses
import datetime
import json
import locale
import re
import sys
import unicodedata
import urllib.error
import urllib.request

USER_AGENT = "tx-check/1.0"
TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")

COINS = {"btc": "Bitcoin", "doge": "Dogecoin", "eth": "Ethereum"}
DOGE_API = "https://api.blockcypher.com/v1/doge/main"
DOGE_LIST_LIMIT = 50  # inputs/outputs asked for per transaction; the API defaults to 20

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
ANSI_RE = re.compile(r"\033\[(\d+)m")

MIN_W, MIN_H = 50, 10
CHROME_ROWS = 4            # tabs and a rule above the transaction, status and keys below it


def api_base(testnet):
    return "https://blockstream.info/testnet/api" if testnet else "https://blockstream.info/api"


def api_get(url, body=None):
    headers = {"User-Agent": USER_AGENT}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def fetch_json(url, body=None):
    try:
        return api_get(url, body), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not found"
        if e.code == 429:
            return None, "rate limited by the API, try again later"
        return None, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except (json.JSONDecodeError, TimeoutError, OSError) as e:
        return None, str(e)


def fetch_btc(txid, testnet):
    return fetch_json(f"{api_base(testnet)}/tx/{txid}")


def fetch_doge(txid):
    return fetch_json(f"{DOGE_API}/txs/{txid}?limit={DOGE_LIST_LIMIT}")


def eth_base(testnet):
    return "https://eth-sepolia.blockscout.com" if testnet else "https://eth.blockscout.com"


def fetch_eth(txid, testnet):
    """Blockscout, or a plain node when it has no answer.

    Blockscout knows token names and keeps all history, but can say 404 for minutes about a
    transaction that is already mined, so its "not found" is never taken on its own.
    """
    tx, error = fetch_json(f"{eth_base(testnet)}/api/v2/transactions/{txid}")
    if not error:
        return tx, None
    tx, node_error = fetch_eth_node(txid, testnet)
    if tx:
        return tx, None
    if error == "not found" and node_error:
        return None, f"not on Blockscout, and the fallback node failed: {node_error}"
    return None, error


def eth_rpc(method, params, testnet):
    url = "https://ethereum-sepolia-rpc.publicnode.com" if testnet else "https://ethereum-rpc.publicnode.com"
    reply, error = fetch_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if error:
        return None, error
    if reply.get("error"):
        return None, clean(reply["error"].get("message", "RPC error"))
    return reply.get("result"), None


def fetch_eth_node(txid, testnet):
    """The same transaction from a node, reshaped to the Blockscout fields render_eth reads."""
    raw, error = eth_rpc("eth_getTransactionByHash", [txid], testnet)
    if error or not raw:
        return None, error

    def num(source, key):
        return int(source[key], 16) if source.get(key) else None

    tx = {
        "from": {"hash": raw.get("from")},
        "to": {"hash": raw["to"]} if raw.get("to") else None,
        "value": num(raw, "value"),
        "nonce": num(raw, "nonce"),
        "gas_limit": num(raw, "gas"),
        "gas_price": num(raw, "gasPrice"),
        "block_number": num(raw, "blockNumber"),
        "from_node": True,
    }
    if tx["block_number"] is None:
        # A pending fee is only a ceiling, which is what Blockscout calls "maximum".
        price = num(raw, "maxFeePerGas") or tx["gas_price"]
        if price is not None and tx["gas_limit"] is not None:
            tx["gas_price"] = price
            tx["fee"] = {"type": "maximum", "value": price * tx["gas_limit"]}
        return tx, None

    stamp = num(raw, "blockTimestamp")
    if stamp:
        tx["timestamp"] = datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc).isoformat()
    receipt, error = eth_rpc("eth_getTransactionReceipt", [txid], testnet)
    if error or not receipt:
        # Without a receipt there is no telling success from failure, so don't claim either.
        return None, error or "no receipt yet"
    tx["status"] = "error" if receipt.get("status") == "0x0" else "ok"
    tx["gas_used"] = num(receipt, "gasUsed")
    tx["gas_price"] = num(receipt, "effectiveGasPrice") or tx["gas_price"]
    if receipt.get("contractAddress"):
        tx["created_contract"] = {"hash": receipt["contractAddress"]}
    if tx["gas_used"] is not None and tx["gas_price"] is not None:
        # A blob transaction pays for its blobs on top of the gas.
        blob = (num(receipt, "blobGasUsed") or 0) * (num(receipt, "blobGasPrice") or 0)
        tx["fee"] = {"type": "actual", "value": tx["gas_used"] * tx["gas_price"] + blob}
    tip, _ = eth_rpc("eth_blockNumber", [], testnet)
    if tip:
        tx["confirmations"] = max(0, int(tip, 16) - tx["block_number"] + 1)
    return tx, None


def fetch_tip_height(testnet):
    url = f"{api_base(testnet)}/blocks/tip/height"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return int(resp.read().decode().strip())


def sats_to_btc(sats):
    return sats / 1e8


def fmt_btc(sats):
    return f"{sats_to_btc(sats):,.8f} BTC"


def fmt_units(value, decimals, trim=True):
    """Exact decimal string for an integer amount; floats lose digits at wei/koinu sizes."""
    value = int(value)
    whole, frac = divmod(abs(value), 10 ** decimals)
    frac = str(frac).zfill(decimals) if decimals else ""
    if trim:
        frac = frac.rstrip("0")
    return f"{'-' if value < 0 else ''}{whole:,}{'.' + frac if frac else ''}"


def fmt_doge(koinu):
    return f"{fmt_units(koinu, 8, trim=False)} DOGE"


def fmt_eth(wei):
    return f"{fmt_units(wei, 18)} ETH"


def fmt_gwei(wei):
    return f"{fmt_units(wei, 9)} gwei"


def fmt_iso(stamp):
    return f"{stamp[:19].replace('T', ' ')} UTC"


def fmt_confirmations(count):
    if count is None:
        return "confirmations unknown"
    return f"{count:,} confirmation{'' if count == 1 else 's'}"


def clean(text):
    """Token names and contract text are chosen by strangers; keep escape codes off the terminal."""
    return "".join(c for c in str(text) if c.isprintable())


def fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render_btc(tx, txid, testnet, tip_height=None):
    out = []
    out.append(f"{BOLD}{CYAN}Transaction {txid}{RESET}")
    net = "testnet" if testnet else "mainnet"
    out.append(f"{DIM}{net} · https://blockstream.info/{'testnet/' if testnet else ''}tx/{txid}{RESET}")
    out.append("=" * min(78, max(10, len(txid))))
    out.append("")

    status = tx.get("status", {})
    confirmed = status.get("confirmed", False)
    if confirmed:
        height = status.get("block_height")
        block_time = status.get("block_time")
        confirmations = tip_height - height + 1 if tip_height is not None and height is not None else None
        conf_str = f"{confirmations:,} confirmation{'s' if confirmations != 1 else ''}" if confirmations is not None else "confirmations unknown"
        out.append(f"{GREEN}{BOLD}✔ Confirmed{RESET} in block {height}  ({conf_str})")
        if block_time:
            out.append(f"  {fmt_time(block_time)}")
    else:
        out.append(f"{YELLOW}{BOLD}⧗ Unconfirmed{RESET} (in mempool)")
    out.append("")

    is_coinbase = any(v.get("is_coinbase") for v in tx.get("vin", []))
    vin = tx.get("vin", [])
    vout = tx.get("vout", [])
    total_in = sum(v.get("prevout", {}).get("value", 0) or 0 for v in vin) if not is_coinbase else 0
    total_out = sum(v.get("value", 0) or 0 for v in vout)
    fee = tx.get("fee")

    out.append(f"{BOLD}Size:{RESET}   {tx.get('size', '?')} bytes    "
               f"{BOLD}Weight:{RESET} {tx.get('weight', '?')} WU    "
               f"{BOLD}vsize:{RESET} {tx.get('weight', 0) // 4 if tx.get('weight') else '?'} vB")
    if is_coinbase:
        out.append(f"{BOLD}Type:{RESET}   coinbase (newly generated coins)")
    else:
        out.append(f"{BOLD}Fee:{RESET}    {fmt_btc(fee) if fee is not None else 'unknown'}"
                    + (f"  ({fee / (tx.get('weight', 1) / 4):.1f} sat/vB)"
                       if fee is not None and tx.get("weight") else ""))
    out.append("")

    out.append(f"{BOLD}Inputs{RESET} ({len(vin)}){'' if is_coinbase else f', total {fmt_btc(total_in)}'}")
    for i, v in enumerate(vin):
        if v.get("is_coinbase"):
            out.append(f"  [{i}] {DIM}coinbase{RESET}")
            continue
        prevout = v.get("prevout", {})
        addr = prevout.get("scriptpubkey_address", "(no address / script)")
        value = prevout.get("value")
        out.append(f"  [{i}] {addr}  {fmt_btc(value) if value is not None else ''}")
    out.append("")

    out.append(f"{BOLD}Outputs{RESET} ({len(vout)}), total {fmt_btc(total_out)}")
    for i, v in enumerate(vout):
        addr = v.get("scriptpubkey_address", "(unspendable / OP_RETURN)")
        value = v.get("value", 0)
        spent_marker = ""
        out.append(f"  [{i}] {addr}  {fmt_btc(value)}{spent_marker}")

    return "\n".join(out)


def render_doge(tx, txid):
    out = []
    out.append(f"{BOLD}{CYAN}Transaction {txid}{RESET}")
    out.append(f"{DIM}Dogecoin mainnet · https://live.blockcypher.com/doge/tx/{txid}/{RESET}")
    out.append("=" * min(78, len(txid)))
    out.append("")

    height = tx.get("block_height", -1)
    if height is not None and height >= 0:
        out.append(f"{GREEN}{BOLD}✔ Confirmed{RESET} in block {height}  "
                   f"({fmt_confirmations(tx.get('confirmations'))})")
        if tx.get("confirmed"):
            out.append(f"  {fmt_iso(tx['confirmed'])}")
    else:
        out.append(f"{YELLOW}{BOLD}⧗ Unconfirmed{RESET} (in mempool)")
        if tx.get("received"):
            out.append(f"  first seen {fmt_iso(tx['received'])}")
    if tx.get("double_spend"):
        out.append(f"{RED}{BOLD}! Double spend{RESET} (another transaction spends the same coins)")
    out.append("")

    inputs = tx.get("inputs") or []
    outputs = tx.get("outputs") or []
    is_coinbase = any(v.get("output_index") == -1 for v in inputs)
    total_out = tx.get("total", 0)
    fee = tx.get("fees")
    size = tx.get("size")

    out.append(f"{BOLD}Size:{RESET}   {size or '?'} bytes")
    if is_coinbase:
        out.append(f"{BOLD}Type:{RESET}   coinbase (newly generated coins)")
    elif fee is not None:
        rate = f"  ({fmt_units(fee * 1000 // size, 8)} DOGE/kB)" if size else ""
        out.append(f"{BOLD}Fee:{RESET}    {fmt_doge(fee)}{rate}")
    else:
        out.append(f"{BOLD}Fee:{RESET}    unknown")
    out.append("")

    # Both lists can be cut short by the API, so the totals come from the
    # transaction itself rather than from adding up what was listed.
    n_in = tx.get("vin_sz", len(inputs))
    n_out = tx.get("vout_sz", len(outputs))
    total_in = "" if is_coinbase or fee is None else f", total {fmt_doge(total_out + fee)}"
    out.append(f"{BOLD}Inputs{RESET} ({n_in}){total_in}")
    for i, v in enumerate(inputs):
        if v.get("output_index") == -1:
            out.append(f"  [{i}] {DIM}coinbase{RESET}")
            continue
        addr = ", ".join(v.get("addresses") or []) or "(no address / script)"
        value = v.get("output_value")
        out.append(f"  [{i}] {addr}  {fmt_doge(value) if value is not None else ''}")
    if n_in > len(inputs):
        out.append(f"  {DIM}… {n_in - len(inputs)} more not shown{RESET}")
    out.append("")

    out.append(f"{BOLD}Outputs{RESET} ({n_out}), total {fmt_doge(total_out)}")
    for i, v in enumerate(outputs):
        addr = ", ".join(v.get("addresses") or []) or "(unspendable / OP_RETURN)"
        out.append(f"  [{i}] {addr}  {fmt_doge(v.get('value', 0))}")
    if n_out > len(outputs):
        out.append(f"  {DIM}… {n_out - len(outputs)} more not shown{RESET}")

    return "\n".join(out)


def eth_address(party):
    """An address, followed by its ENS or contract name when it has one."""
    if not party:
        return "(none)"
    name = party.get("ens_domain_name") or party.get("name")
    return party.get("hash", "?") + (f"  {DIM}{clean(name)}{RESET}" if name else "")


def eth_token_amount(transfer):
    token = transfer.get("token") or {}
    total = transfer.get("total") or {}
    symbol = clean(token.get("symbol") or token.get("name") or "unknown token")
    token_id = total.get("token_id")
    value = total.get("value")
    if token_id is not None:
        count = f"{value} × " if value not in (None, "1") else ""
        return f"{count}{symbol} #{token_id}"
    if value is None:
        return symbol
    decimals = total.get("decimals") or token.get("decimals") or 0
    return f"{fmt_units(value, int(decimals))} {symbol}"


def render_eth(tx, txid, testnet):
    out = []
    out.append(f"{BOLD}{CYAN}Transaction {txid}{RESET}")
    net = "Sepolia testnet" if testnet else "mainnet"
    out.append(f"{DIM}Ethereum {net} · {eth_base(testnet)}/tx/{txid}{RESET}")
    out.append("=" * min(78, len(txid)))
    out.append("")

    block = tx.get("block_number")
    result = str(tx.get("result") or "")
    if block is None and "dropped" in result:
        out.append(f"{RED}{BOLD}✘ Dropped{RESET} (never included, or replaced by another transaction)")
    elif block is None:
        out.append(f"{YELLOW}{BOLD}⧗ Pending{RESET} (not in a block yet)")
    else:
        where = f"in block {block}  ({fmt_confirmations(tx.get('confirmations'))})"
        if tx.get("status") == "error":
            # A failed transaction is still mined and still pays its fee, but moves nothing.
            out.append(f"{RED}{BOLD}✘ Failed{RESET} {where}")
            if result and not result.startswith("awaiting"):
                out.append(f"  {clean(result)}")
        else:
            out.append(f"{GREEN}{BOLD}✔ Confirmed{RESET} {where}")
        if tx.get("timestamp"):
            out.append(f"  {fmt_iso(tx['timestamp'])}")
    out.append("")

    out.append(f"{BOLD}From:{RESET}   {eth_address(tx.get('from'))}")
    if tx.get("to"):
        out.append(f"{BOLD}To:{RESET}     {eth_address(tx['to'])}")
    else:
        out.append(f"{BOLD}To:{RESET}     contract creation → {eth_address(tx.get('created_contract'))}")
    out.append(f"{BOLD}Value:{RESET}  {fmt_eth(tx.get('value') or 0)}")
    if tx.get("method"):
        out.append(f"{BOLD}Method:{RESET} {clean(tx['method'])}")
    out.append("")

    fee = tx.get("fee") or {}
    if fee.get("value") is not None:
        upto = "up to " if fee.get("type") == "maximum" else ""
        out.append(f"{BOLD}Fee:{RESET}    {upto}{fmt_eth(fee['value'])}")
    else:
        out.append(f"{BOLD}Fee:{RESET}    unknown")
    used, limit = tx.get("gas_used"), tx.get("gas_limit")
    gas = f"{int(used):,} used of " if used is not None else "limit "
    gas += f"{int(limit):,}" if limit is not None else "?"
    if tx.get("gas_price") is not None:
        gas += f"  at {fmt_gwei(tx['gas_price'])}"
    out.append(f"{BOLD}Gas:{RESET}    {gas}")
    out.append(f"{BOLD}Nonce:{RESET}  {tx.get('nonce', '?')}")

    if tx.get("from_node"):
        out.append("")
        out.append(f"{DIM}Blockscout gave no answer, so this came straight from a node: no token "
                   f"transfers or names. Try again in a few minutes for those.{RESET}")

    transfers = tx.get("token_transfers") or []
    if transfers:
        out.append("")
        more = "+" if tx.get("token_transfers_overflow") else ""
        out.append(f"{BOLD}Token transfers{RESET} ({len(transfers)}{more})")
        for i, t in enumerate(transfers):
            # Symbols are free text and easily faked, so the contract is always shown.
            contract = (t.get("token") or {}).get("address_hash", "?")
            out.append(f"  [{i}] {eth_token_amount(t)}  {DIM}contract {contract}{RESET}")
            out.append(f"      {eth_address(t.get('from'))}")
            out.append(f"      → {eth_address(t.get('to'))}")
        if more:
            out.append(f"  {DIM}… more not shown{RESET}")

    return "\n".join(out)


# -- looking up --

class Check:
    """One answer from the chain: the rendered transaction and what a refresh is compared on."""

    def __init__(self, text, mined, confirmations):
        self.text = text
        self.mined = mined
        self.confirmations = confirmations
        self.at = datetime.datetime.now()


def check(coin, txid, testnet):
    """Look `txid` up on one chain. Returns (Check, None) or (None, error)."""
    if coin == "eth":
        tx, error = fetch_eth(txid, testnet)
        if error:
            return None, error
        return Check(render_eth(tx, txid, testnet), tx.get("block_number") is not None,
                     tx.get("confirmations")), None
    if coin == "doge":
        tx, error = fetch_doge(txid)
        if error:
            return None, error
        return Check(render_doge(tx, txid), (tx.get("block_height") or -1) >= 0, tx.get("confirmations")), None

    tx, error = fetch_btc(txid, testnet)
    if error:
        return None, error
    status = tx.get("status", {})
    mined = status.get("confirmed", False)
    tip_height, confirmations = None, None
    if mined:
        try:
            tip_height = fetch_tip_height(testnet)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            tip_height = None
        if tip_height is not None and status.get("block_height") is not None:
            # The tip can come from a server one block behind the one that gave the transaction.
            tip_height = max(tip_height, status["block_height"])
            confirmations = tip_height - status["block_height"] + 1
    return Check(render_btc(tx, txid, testnet, tip_height), mined, confirmations), None


def parse_hash(text):
    """(64 hex digits, whether 0x came before them), or (None, False) if it isn't a hash."""
    text = text.strip().lower()
    has_prefix = text.startswith("0x")
    digits = text[2:] if has_prefix else text
    return (digits, has_prefix) if TXID_RE.match(digits) else (None, False)


def chain_id(coin, digits):
    return "0x" + digits if coin == "eth" else digits


def coins_for(testnet):
    return [c for c in COINS if not (testnet and c == "doge")]


def find(digits, has_prefix, first, testnet, progress=lambda coin: None):
    """Look a hash up chain by chain, `first` first. Returns (coin, Check, None) or (None, None, error).

    Bitcoin and Dogecoin hashes look identical, so which chain knows the hash is the only way to
    tell them apart. 0x settles it without asking the others.
    """
    if has_prefix:
        coins = ["eth"]
    else:
        coins = sorted(coins_for(testnet), key=lambda c: c != first)
    missing, failed = [], []
    for coin in coins:
        progress(coin)
        result, error = check(coin, chain_id(coin, digits), testnet)
        if result:
            return coin, result, None
        if error == "not found":
            missing.append(COINS[coin])
        else:
            failed.append(f"{COINS[coin]}: {error}")
    parts = [f"not found on {', '.join(missing)}"] if missing else []
    return None, None, "; ".join(parts + failed)      # a chain that didn't answer isn't a "not found"


# -- full screen --

def cwidth(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1
               for c in text)


def styled(line):
    """Split a rendered line into (character, ANSI codes in force) pairs."""
    out, codes = [], frozenset()
    for i, part in enumerate(ANSI_RE.split(line)):
        if i % 2:
            codes = frozenset() if part == "0" else codes | {part}
        else:
            out += [(c, codes) for c in part if c.isprintable()]
    return out


def wrap(text, width):
    """Rendered text as screen rows of (character, codes) pairs; continuation rows are indented."""
    rows = []
    for line in text.split("\n"):
        cells = styled(line)
        if line and not line.strip("="):
            rows.append(cells[:width])
            continue
        row, used, indent = [], 0, 0
        for c, codes in cells:
            size = cwidth(c)
            if used + size > width:
                # Break at the last space when there is one past the indent, so that an address
                # or an amount isn't split unless it is wider than the window by itself.
                cut = next((i for i in range(len(row) - 1, indent, -1) if row[i][0] == " "), None)
                if c == " ":
                    rest = []
                elif cut is None or cut < width // 4:
                    rest = []
                else:
                    rest, row = row[cut + 1:], row[:cut]
                rows.append(row)
                indent = 6
                row = [(" ", frozenset())] * indent + rest
                used = cwidth("".join(x for x, _ in row))
                if c == " ":
                    continue
            row.append((c, codes))
            used += size
        rows.append(row)
    return rows


def short_count(check_):
    if not check_.mined:
        return "⧗"
    if check_.confirmations is None:
        return "✔"
    return "999+" if check_.confirmations > 999 else str(check_.confirmations)


def describe_change(old, new):
    count = fmt_confirmations(new.confirmations)
    if not new.mined:
        return "was in a block, now unconfirmed again" if old.mined else "still unconfirmed"
    if not old.mined:
        return f"now in a block, {count}"
    if old.confirmations is None or new.confirmations is None:
        return count
    gained = new.confirmations - old.confirmations
    return f"{count} ({gained:+,})" if gained else f"still {count}"


class Tab:
    def __init__(self, coin):
        self.coin = coin
        self.txid = None
        self.check = None
        self.top = 0


class App:
    def __init__(self, stdscr, testnet):
        self.stdscr = stdscr
        self.testnet = testnet
        self.tabs = [Tab(c) for c in coins_for(testnet)]
        self.current = 0
        self.message = ""
        self.style = "dim"
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)
        self.styles = {"36": curses.color_pair(1), "32": curses.color_pair(2), "33": curses.color_pair(3),
                       "31": curses.color_pair(4), "2": curses.color_pair(5), "1": curses.A_BOLD}
        self.styles.update(dim=self.styles["2"], good=self.styles["32"], bad=self.styles["31"])

    @property
    def tab(self):
        return self.tabs[self.current]

    def say(self, message, style="dim"):
        self.message, self.style = message, style

    # -- drawing --

    def put(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if not 0 <= y < h or x >= w:
            return
        while cwidth(text) > w - x:
            text = text[:-1]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:                   # the bottom-right cell raises, which is harmless
            pass

    def rows(self):
        return wrap(self.tab.check.text, self.stdscr.getmaxyx()[1] - 2) if self.tab.check else []

    def page(self):
        return max(1, self.stdscr.getmaxyx()[0] - CHROME_ROWS)

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"Window too small, needs {MIN_W}×{MIN_H}")
            self.stdscr.refresh()
            return
        x = 1
        for i, tab in enumerate(self.tabs):
            label = f" {COINS[tab.coin]}{' ' + short_count(tab.check) if tab.check else ''} "
            self.put(0, x, label, curses.A_REVERSE | curses.A_BOLD if i == self.current else 0)
            x += cwidth(label) + 1
        if self.testnet:
            self.put(0, w - 9, "testnet", self.styles["33"])
        self.put(1, 0, "─" * w, self.styles["dim"])

        tab = self.tab
        rows, page = self.rows(), self.page()
        tab.top = max(0, min(tab.top, len(rows) - page))
        for y, row in enumerate(rows[tab.top:tab.top + page]):
            x = 1
            for c, codes in row:
                attr = 0
                for code in codes:
                    attr |= self.styles.get(code, 0)
                self.put(2 + y, x, c, attr)
                x += cwidth(c)
        if not tab.check:
            self.put(3, 2, f"No {COINS[tab.coin]} transaction open.")
            self.put(5, 2, "Paste a transaction hash and press Enter.", self.styles["dim"])
            self.put(6, 2, "It opens in the tab of the coin it belongs to.", self.styles["dim"])

        more = ""
        if len(rows) > page:
            more = f"{tab.top + 1}-{min(len(rows), tab.top + page)}/{len(rows)}"
        checked = f"checked {tab.check.at:%H:%M:%S}" if tab.check else ""
        right = "  ".join(t for t in (checked, more) if t)
        message = self.message if len(self.message) <= w - 2 else self.message[:w - 3] + "…"
        self.put(h - 2, 1, message, self.styles[self.style])
        if len(message) + len(right) + 4 <= w:
            self.put(h - 2, w - len(right) - 1, right, self.styles["dim"])
        keys = "paste or n new hash  r refresh  ←→ coin  ↑↓ scroll  q quit"
        if w < 62:
            keys = "n new  r refresh  ←→ coin  ↑↓ scroll  q quit"
        self.put(h - 1, 1, keys, self.styles["dim"])
        self.stdscr.refresh()

    # -- actions --

    def busy(self, message):
        self.say(message)
        self.draw()

    def open(self, text, only=None):
        """Look a hash up and show it in its coin's tab; `only` limits it to one chain."""
        digits, has_prefix = parse_hash(text)
        if digits is None or (has_prefix and only not in (None, "eth")):
            self.say("Not a transaction hash: expected 64 hex characters, after 0x for Ethereum", "bad")
            return
        if only:
            self.busy(f"Looking on {COINS[only]}…")
            coin = only
            result, error = check(only, chain_id(only, digits), self.testnet)
            error = f"{COINS[only]}: {error}" if error else None
        else:
            coin, result, error = find(digits, has_prefix, self.tab.coin, self.testnet,
                                       lambda c: self.busy(f"Looking on {COINS[c]}…"))
        curses.flushinp()
        if error:
            self.say(error, "bad")
            return
        self.current = next(i for i, t in enumerate(self.tabs) if t.coin == coin)
        self.tab.txid, self.tab.check, self.tab.top = chain_id(coin, digits), result, 0
        self.say(f"Found on {COINS[coin]}", "good")

    def refresh(self):
        tab = self.tab
        if not tab.check:
            self.say("Nothing to refresh in this tab yet")
            return
        self.busy("Refreshing…")
        result, error = check(tab.coin, tab.txid, self.testnet)
        curses.flushinp()                      # impatient extra presses would only hit the rate limit
        if error == "not found" and not tab.check.mined:
            self.say("No longer found: it may have been dropped or replaced (showing the last answer)", "bad")
        elif error:
            self.say(f"Refresh failed, showing the last answer: {error}", "bad")
        else:
            change = describe_change(tab.check, result)
            better = result.mined and (result.confirmations or 0) > (tab.check.confirmations or 0)
            tab.check = result
            self.say(change[0].upper() + change[1:], "good" if better else "dim")

    def prompt(self, label, initial=""):
        """A line of text typed on the status row, or None on Esc."""
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                self.draw()
                h, w = self.stdscr.getmaxyx()
                room = max(1, w - len(label) - 3)
                text = label + "".join(buf)[-room:]
                self.put(h - 2, 0, " " * w)
                self.put(h - 2, 1, text, self.styles["36"])
                try:
                    self.stdscr.move(h - 2, min(w - 1, 1 + cwidth(text)))
                except curses.error:
                    pass
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf)
                if key == "\x1b":
                    return None
                if key in ("\x7f", "\b", curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == "\x15":            # Ctrl-U
                    buf.clear()
                elif isinstance(key, str) and key.isprintable():
                    buf.append(key)
        finally:
            curses.curs_set(0)

    def ask_hash(self, initial=""):
        text = self.prompt("Transaction hash: ", initial)
        if text and text.strip():
            self.open(text)

    def handle(self, ch):
        """Returns False to quit. No command may use 0-9 or a-f: those start a pasted hash."""
        if ch == curses.KEY_RESIZE:
            return True
        self.say("")
        if ch in (ord("q"), ord("Q")):
            return False
        if 0 <= ch < 128 and chr(ch) in "0123456789abcdefABCDEF":
            self.ask_hash(chr(ch))
        elif ch in (ord("n"), ord("N"), ord("/"), curses.KEY_ENTER, 10, 13):
            self.ask_hash()
        elif ch in (ord("r"), ord("R")):
            self.refresh()
        elif ch in (curses.KEY_RIGHT, ord("\t"), ord("l")):
            self.current = (self.current + 1) % len(self.tabs)
        elif ch in (curses.KEY_LEFT, curses.KEY_BTAB, ord("h")):
            self.current = (self.current - 1) % len(self.tabs)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.tab.top += 1                  # draw() keeps it within the text
        elif ch in (curses.KEY_UP, ord("k")):
            self.tab.top -= 1
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.tab.top += self.page()
        elif ch == curses.KEY_PPAGE:
            self.tab.top -= self.page()
        elif ch in (curses.KEY_HOME, ord("g")):
            self.tab.top = 0
        elif ch in (curses.KEY_END, ord("G")):
            self.tab.top = len(self.rows())
        return True


def run(stdscr, text, coin, testnet):
    app = App(stdscr, testnet)
    if coin:
        app.current = coins_for(testnet).index(coin)
    if text:
        app.open(text, only=coin)
    while True:
        app.draw()
        if not app.handle(stdscr.getch()):
            return


def print_once(text, coin, testnet):
    if not text:
        try:
            text = input("Enter transaction hash: ")
        except (EOFError, KeyboardInterrupt):
            return 0
    tty = sys.stdout.isatty(), sys.stderr.isatty()

    def fail(message):
        print(f"{RED}{message}{RESET}" if tty[1] else message, file=sys.stderr)
        return 1

    digits, has_prefix = parse_hash(text)
    if digits is None:
        return fail("Invalid transaction hash: expected 64 hex characters, after 0x for Ethereum.")
    if has_prefix and coin not in (None, "eth"):
        return fail(f"Invalid transaction hash: {COINS[coin]} hashes don't start with 0x.")
    if coin:
        result, error = check(coin, chain_id(coin, digits), testnet)
        error = f"{COINS[coin]}: {error}" if error else None
    else:
        coin, result, error = find(digits, has_prefix, "btc", testnet)
    if error:
        return fail(f"Error: {error}")
    print(result.text if tty[0] else ANSI_RE.sub("", result.text))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check a Bitcoin, Dogecoin or Ethereum transaction by its hash.",
        epilog="Without --coin the hash is looked up on each chain in turn; one starting with 0x only on Ethereum.")
    parser.add_argument("txid", nargs="?", help="transaction hash: 64 hex characters, after 0x for Ethereum")
    parser.add_argument("-c", "--coin", choices=COINS, help="look on this chain only")
    parser.add_argument("--testnet", action="store_true",
                        help="query testnet instead of mainnet (Sepolia for Ethereum; not for Dogecoin)")
    parser.add_argument("--once", action="store_true", help="print the transaction and exit")
    args = parser.parse_args()
    if args.coin == "doge" and args.testnet:
        parser.error("--testnet is not available for Dogecoin")
    if args.once or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(print_once(args.txid, args.coin, args.testnet))
    locale.setlocale(locale.LC_ALL, "")
    curses.set_escdelay(25)
    try:
        curses.wrapper(run, args.txid, args.coin, args.testnet)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
