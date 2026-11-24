---
paths:
  - "btc_tx_check.py"
---

# btc_tx_check.py

Looks up one transaction on Bitcoin, Dogecoin or Ethereum. The name dates from when it was
Bitcoin only. All APIs are keyless: Blockstream (BTC), BlockCypher (DOGE, rate limited),
Blockscout plus a publicnode JSON-RPC node (ETH).

- **Choosing the coin**: `--coin` wins; otherwise `0x` means Ethereum and anything else Bitcoin.
  BTC and DOGE hashes are indistinguishable, so a hash typed at the prompt is followed by a coin
  question, and "not found" names the other chains. Don't probe every chain automatically: that
  would hand the hash to services the user didn't ask about.
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
