#!/usr/bin/env python3
"""Look up a Bitcoin transaction by its hash (txid) from the terminal."""

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

USER_AGENT = "btc-tx-check-cli/1.0"
TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def api_base(testnet):
    return "https://blockstream.info/testnet/api" if testnet else "https://blockstream.info/api"


def api_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def fetch_tx(txid, testnet):
    url = f"{api_base(testnet)}/tx/{txid}"
    try:
        return api_get(url), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not found"
        return None, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return None, f"network error: {e.reason}"
    except (json.JSONDecodeError, TimeoutError, OSError) as e:
        return None, str(e)


def fetch_tip_height(testnet):
    url = f"{api_base(testnet)}/blocks/tip/height"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return int(resp.read().decode().strip())


def sats_to_btc(sats):
    return sats / 1e8


def fmt_btc(sats):
    return f"{sats_to_btc(sats):,.8f} BTC"


def fmt_time(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def render(tx, txid, testnet, tip_height=None):
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


def main():
    parser = argparse.ArgumentParser(description="Check a Bitcoin transaction by its hash (txid).")
    parser.add_argument("txid", nargs="?", help="64-character transaction hash")
    parser.add_argument("--testnet", action="store_true", help="query testnet instead of mainnet")
    args = parser.parse_args()

    txid = args.txid
    if not txid:
        try:
            txid = input("Enter transaction hash: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    txid = txid.strip().lower()

    if not TXID_RE.match(txid):
        print(f"{RED}Invalid transaction hash: expected 64 hex characters.{RESET}", file=sys.stderr)
        sys.exit(1)

    tx, error = fetch_tx(txid, args.testnet)
    if error:
        print(f"{RED}Error: {error}{RESET}", file=sys.stderr)
        sys.exit(1)

    tip_height = None
    if tx.get("status", {}).get("confirmed"):
        try:
            tip_height = fetch_tip_height(args.testnet)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            tip_height = None

    print(render(tx, txid, args.testnet, tip_height))


if __name__ == "__main__":
    main()
