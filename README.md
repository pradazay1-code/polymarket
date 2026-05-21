# Polymarket Automation

A multi-bot Polymarket trading system + local web dashboard.

**Strategy:** "stink-bid" — post limit orders 30% below market on the favorite
side of binary sports markets, refresh every 15 minutes, hold any fills to
expiration. The idea: in volatile best-of-3 women's tennis (and similar),
in-game panic dumps occasionally hit a deep bid on the eventual winner.

## What's in the box

```
src/
  config.py            # YAML + .env loader, multi-account, signature_type per account
  client.py            # py-clob-client wrapper (orders, cancel, book w/ tick & min size, balance+allowance)
  markets.py           # Gamma API discovery — binary sports markets ending soon (and not already over)
  state.py             # Per-bot JSON snapshots (picks, history) under ./state/
  strategies/
    stink_bid.py       # Planner + executor with tick-size-aware bidding
  bots/runner.py       # tick loop: fetch -> plan -> execute -> snapshot -> sleep
  api.py               # FastAPI dashboard backend
  tracker.py           # Pretty-print balance / open orders / positions for CLI
web/index.html         # Single-page dashboard (no build step)
main.py                # CLI: list / status / markets / run / dashboard
config.yaml            # Bot definitions and global risk knobs
.env.example           # Credentials template
```

## Quick start

1. **Install**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Get your Polymarket credentials** — pick the path that matches your signup:

   **Email / Google signup (most common)**
   - Go to polymarket.com, click your profile → **Settings → Export private key** (requires email verification). That's `PRIVATE_KEY`.
   - On the **Deposit** page, copy the displayed Polygon address. That's `FUNDER_ADDRESS` (your proxy wallet that holds USDC).
   - Set `SIGNATURE_TYPE=1`.
   - No on-chain approvals needed — Polymarket handles them for Magic wallets.

   **MetaMask / browser wallet signup**
   - Export your wallet's private key from MetaMask → `PRIVATE_KEY`.
   - `FUNDER_ADDRESS` is the same EOA's address.
   - Set `SIGNATURE_TYPE=0`.
   - You must approve USDC + ConditionalTokens to the Polymarket exchange contracts (do at least one trade in the UI first to trigger the approvals).

   Copy `.env.example` → `.env` and fill in those three values.

3. **Verify in dry-run** (`dry_run: true` is the default in `config.yaml`):
   ```bash
   python main.py list                    # show configured bots
   python main.py status                  # show balance, open orders, positions
   python main.py markets wta             # preview markets the bot would target
   python main.py run wta --once          # one dry cycle, prints intended bids
   ```

4. **Open the dashboard:**
   ```bash
   python main.py dashboard               # http://127.0.0.1:8787
   ```
   The dashboard shows balance, open orders, positions, and **per-bot picks** —
   the exact bids the bot is planning for the current cycle, with the favorite
   price, your bid, share count, notional, and end time. Buttons let you run a
   single cycle or start/stop the loop.

5. **Go live** when you trust what you see:
   - Flip `dry_run: false` in `config.yaml`, **or** pass `--live` for a single CLI invocation
   - Either start the loop from the dashboard, or run from the CLI:
     ```bash
     python main.py run wta --live
     ```

## Dashboard features

- **Summary cards**: USDC balance & allowance, open-order count, position count,
  cycle activity (planned vs posted), strategy settings.
- **Per-bot tabs**: cycle stats, the picks table (one row per bid the bot plans
  to place this cycle), a collapsible "skipped" log explaining why other markets
  were excluded, and Run / Start / Stop controls.
- **Open Orders tab**: all GTC limit orders currently resting on the CLOB.
- **Positions tab**: every YES/NO holding, with cost basis and live PnL.
- Auto-refresh every 15 seconds.

## Multi-account

Add `PRIVATE_KEY_2`/`FUNDER_ADDRESS_2`/`SIGNATURE_TYPE_2` (and `_3`, `_4`) in
`.env`. In `config.yaml` set a bot's `account:` to `account_2`. You can run
multiple bots on different accounts from the same dashboard.

## Risk controls (see `config.yaml`)

- `dry_run: true` — log-only mode (default).
- `bet_size_usdc` — per-market notional. Start at $5.
- `max_open_notional_usdc` — hard cap across all simultaneous bids in a cycle.
- `min_favorite_price` / `max_favorite_price` — only bid on lopsided markets
  (skip coin-flips and near-certainties).
- `cancel_before_refresh` — wipe stale orders each cycle so bids track the
  current market price.
- Tick size and minimum order size are read **from the live order book** and
  enforced — out-of-tick prices and undersized orders are skipped automatically.

## Adding a new bot

Add an entry under `bots:` in `config.yaml`. As long as Polymarket has a
matching tag (`tennis`, `nba`, `cricket`, `mlb`, `nhl`, `soccer`, `ufc`, `pga`,
etc.), the same stink-bid logic applies.

## Known limits / next steps

- The bot does not auto-redeem winning positions — claim them on
  polymarket.com after a market resolves (or extend with the on-chain
  `redeemPositions` call).
- Market discovery polls Gamma every cycle; for sub-minute reactions, swap to
  the CLOB WSS book stream.
- No automatic backtest harness yet — strategy parameter tuning is manual.

## Disclaimer

For personal automation. Test in dry-run first, start with tiny size, and only
risk what you can afford to lose.
