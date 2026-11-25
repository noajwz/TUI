---
paths:
  - "tx_check.py"
---

# tx_check.py

Bitcoin, Dogecoin and Ethereum transactions by hash. It was `btc_tx_check.py`, a Bitcoin-only CLI
script, until it became a curses TUI. All APIs are keyless: Blockstream (BTC), BlockCypher (DOGE,
rate limited), Blockscout plus a publicnode JSON-RPC node (ETH).

- **Recognising the coin**: `0x` means Ethereum and goes nowhere else. BTC and DOGE hashes are
  indistinguishable, so `find()` asks the chains in turn, the open tab's first. `--coin` pins one
  chain, for when the hash shouldn't be sent to the others. A chain that errors is reported as
  such, never folded into "not found".
- **One tab per coin**, each keeping its own transaction. A found hash switches to its coin's tab.
- **No key may use 0-9 or a-f**: those start the hash prompt, which is what makes pasting work.
- **Refreshing is manual** (`r`) and keeps the last answer on screen when it fails.
- **The `render_*` functions return ANSI text** used by both modes: printed as is by `--once` (or
  without colours when piped) and turned into curses attributes by `styled()`.
- **Blockscout's 404 is not trusted.** It can say "not found" for minutes about a mined
  transaction. `fetch_eth` then asks a node, reshaped to the Blockscout fields `render_eth`
  reads. Without a receipt that fallback reports an error rather than guess success or failure.
- **ETH, DOGE and token amounts are formatted from integers** (`fmt_units`); floats lose digits at
  wei size. `fmt_btc` still uses floats, which is safe at Bitcoin's supply.
- **`clean()` everything chosen by strangers** (token symbols, contract and ENS names, methods):
  they can contain terminal escape codes. Token lines always show the contract, as symbols are
  easily faked.
- DOGE totals come from `total` and `fees`, not from summing the lists, which the API truncates.
- No `--testnet` for Dogecoin; BlockCypher has none. For Ethereum it means Sepolia.
