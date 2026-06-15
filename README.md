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

## Climate / weather strategy

A second strategy targets Polymarket's climate markets ("Will [city]'s high
be above X°F on [date]?") using free NOAA forecast data.

```bash
python main.py markets climate           # preview Polymarket climate markets
python main.py run climate --once        # dry cycle: forecast vs market price
```

How it decides:
1. Parse the question for (city, threshold °F, target date).
2. Pull the NWS daily forecast for that location.
3. Compute model P(high > threshold) using a normal CDF with sigma
   sized to days-out (NWS hits ~2°F RMSE 1 day out, ~6°F at a week).
4. If model probability disagrees with the market price by more than
   `min_edge` (default 5%), post a small limit on the underpriced side.

Config knobs (under `bots.climate` in `config.yaml`):
- `min_edge`: minimum probability gap to bet (default 0.05)
- `max_days_out`: refuse bets > N days out (default 7 — forecasts get noisy)
- `bid_discount`: how far below market to post the limit (default 0.03)

Honest scope: the parser handles common US-city daily-high questions.
Long-horizon ("hottest year on record") and non-temperature climate
markets are skipped. Markets are small so keep `bet_size_usdc` tiny.

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

## Analytics

Three commands sit on top of the bot to answer "does this strategy actually
have edge before I risk money?"

```bash
# Replay 30 days of resolved markets through the strategy. Results cached in
# state/backtest_<tag>_<days>d.json — pass --refresh to refetch.
python main.py backtest wta --days 30 --top 10

# Plan the next live cycle, then rank intents by expected value using the
# historical hit rates from the backtest. Needs Polymarket credentials.
python main.py edge wta

# Aggregate PnL across all bots: live posts, dry-run posts, open positions,
# realized + unrealized. --no-live skips the position fetch.
python main.py pnl
python main.py pnl wta --no-live
```

The backtest works **without Polymarket credentials** — only public Gamma +
CLOB price-history endpoints — so you can evaluate edge before signing up.

### Honest caveats baked into the output

- Backtest fills assume our limit is at the front of the queue. In reality,
  many "filled" prints would have been someone else's order first. Real
  fill counts will be a fraction of simulated.
- Only markets that resolved cleanly are scored — survivorship bias.
- No slippage, no fees, no partial fills modelled.
- EV from edge scoring uses bucketed historical rates; bucket sizes are
  printed so you can see when sample size is too small to trust.
- Past performance is not predictive. Use these numbers to **rule strategies
  out**, not to size up.

## Known limits / next steps

- The bot does not auto-redeem winning positions — claim them on
  polymarket.com after a market resolves (or extend with the on-chain
  `redeemPositions` call).
- Market discovery polls Gamma every cycle; for sub-minute reactions, swap to
  the CLOB WSS book stream.
- Backtester models GTC fills naively (next-cycle window); add a queue-
  position model for tighter realism.

## Disclaimer

For personal automation. Test in dry-run first, start with tiny size, and only
risk what you can afford to lose.
