#!/usr/bin/env python3
"""Look up a Bitcoin, Dogecoin or Ethereum transaction by its hash from the terminal."""

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

USER_AGENT = "btc-tx-check-cli/1.0"
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


def ask(text):
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Check a Bitcoin, Dogecoin or Ethereum transaction by its hash.",
        epilog="Without --coin, a hash starting with 0x is taken as Ethereum and any other as Bitcoin.")
    parser.add_argument("txid", nargs="?", help="transaction hash: 64 hex characters, after 0x for Ethereum")
    parser.add_argument("-c", "--coin", choices=COINS, help="which chain the hash belongs to")
    parser.add_argument("--testnet", action="store_true",
                        help="query testnet instead of mainnet (Sepolia for Ethereum; not for Dogecoin)")
    args = parser.parse_args()

    if args.coin == "doge" and args.testnet:
        parser.error("--testnet is not available for Dogecoin")

    txid, coin = args.txid, args.coin
    prompted = not txid
    if prompted:
        txid = ask("Enter transaction hash: ")
        if txid is None:
            return
    txid = txid.strip().lower()
    has_prefix = txid.startswith("0x")
    digits = txid[2:] if has_prefix else txid

    # 0x is optional when Ethereum was asked for, and wrong for the other two.
    if not TXID_RE.match(digits) or (has_prefix and coin not in (None, "eth")):
        print(f"{RED}Invalid transaction hash: expected 64 hex characters"
              f"{' without 0x' if has_prefix and coin in ('btc', 'doge') else ''}.{RESET}", file=sys.stderr)
        sys.exit(1)

    # Bitcoin and Dogecoin hashes look identical, so only the user can tell them apart.
    if coin is None and has_prefix:
        coin = "eth"
    elif coin is None and prompted:
        answer = ask(f"Coin ({'/'.join(COINS)}) [btc]: ")
        if answer is None:
            return
        coin = answer.lower() or "btc"
        if coin not in COINS:
            print(f"{RED}Unknown coin: expected one of {', '.join(COINS)}.{RESET}", file=sys.stderr)
            sys.exit(1)
        if coin == "doge" and args.testnet:
            parser.error("--testnet is not available for Dogecoin")
    elif coin is None:
        coin = "btc"

    if coin == "eth":
        txid = "0x" + digits
        tx, error = fetch_eth(txid, args.testnet)
    elif coin == "doge":
        tx, error = fetch_doge(txid)
    else:
        tx, error = fetch_btc(txid, args.testnet)

    if error:
        print(f"{RED}Error: {error}{RESET}", file=sys.stderr)
        if error == "not found":
            others = " or ".join(f"--coin {c}" for c in COINS if c != coin)
            print(f"{DIM}Looked on {COINS[coin]}; for another chain use {others}.{RESET}", file=sys.stderr)
        sys.exit(1)

    if coin == "eth":
        print(render_eth(tx, txid, args.testnet))
        return
    if coin == "doge":
        print(render_doge(tx, txid))
        return

    tip_height = None
    if tx.get("status", {}).get("confirmed"):
        try:
            tip_height = fetch_tip_height(args.testnet)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            tip_height = None

    print(render_btc(tx, txid, args.testnet, tip_height))


if __name__ == "__main__":
    main()
