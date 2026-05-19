# Polymarket Automation

A multi-bot Polymarket trading system built around the **stink-bid** strategy:
post limit orders 30% below market on the favorite side of binary sports
markets, refresh every 15 minutes, and hold any fills to expiration.

The idea comes from the WTA-tennis bot walkthrough — best-of-3 women's
tennis matches are volatile enough that an in-game panic dump occasionally
hits a deep bid on the favorite, then resolves at $1.

## What's in the box

```
src/
  config.py            # YAML + .env loader, multi-account
  client.py            # py-clob-client wrapper (order placement, cancel, book, positions)
  markets.py           # Gamma API discovery — find binary sports markets ending soon
  tracker.py           # Pretty-print balance / open orders / positions
  strategies/
    stink_bid.py       # The "30% below favorite" planner + executor
  bots/
    runner.py          # tick loop: fetch -> plan -> execute -> sleep
main.py                # CLI: list / status / markets / run
config.yaml            # Bot definitions and global risk knobs
.env.example           # Credentials template
```

## Quick start

1. **Install**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Fund a Polygon wallet** with USDC.e, approve the CLOB (do this once via
   polymarket.com), then copy `.env.example` to `.env` and fill in
   `PRIVATE_KEY` (the EOA that signs) and `FUNDER_ADDRESS` (the Polymarket
   proxy wallet that holds the USDC).
3. **Peek before you bet** — `dry_run: true` is the default in
   `config.yaml`. Run a market preview and a dry cycle first:
   ```bash
   python main.py list
   python main.py markets wta
   python main.py run wta --once         # dry — just logs intentions
   ```
4. **Go live** when you trust it:
   ```bash
   # flip dry_run to false in config.yaml, OR pass --live for one invocation
   python main.py run wta --live
   ```

## Multi-account

The video author runs several accounts to test ideas in parallel.
Add `PRIVATE_KEY_2`/`FUNDER_ADDRESS_2` (and `_3`, `_4`) in `.env`, then in
`config.yaml` set a bot's `account:` to `account_2`. Run separate bots in
separate processes.

## Risk controls (read `config.yaml`)

- `dry_run: true` — log-only mode (default).
- `bet_size_usdc` — per-market notional. Start at $5 like the video does.
- `max_open_notional_usdc` — hard cap across all simultaneous bids.
- `min_favorite_price` / `max_favorite_price` — only bid on lopsided markets
  (skip coin-flips and ~certainties).
- `cancel_before_refresh` — wipe stale orders each cycle so the bid tracks
  the current market price.

## Adding a new bot

Add an entry under `bots:` in `config.yaml` — that's usually it, as long as
Polymarket has a tag for the sport (try `tennis`, `nba`, `cricket`, `mlb`,
`nhl`, `soccer`, `ufc`, `pga`). The same stink-bid strategy applies to any
binary sports market.

## Disclaimer

This is for personal automation. Test in dry-run first, start with tiny
size, and only risk what you can afford to lose.
