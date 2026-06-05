"""Polymarket automation CLI.

Examples:
  python main.py list                # show configured bots
  python main.py status              # show balance, open orders, positions
  python main.py run wta             # run the WTA tennis bot forever
  python main.py run wta --once      # one cycle, then exit
  python main.py run wta --live      # disable dry_run for this invocation
  python main.py markets wta         # preview markets the wta bot would target

Analytics:
  python main.py backtest wta --days 30      # replay history through the strategy
  python main.py edge wta                    # rank tonight's intents by expected value
  python main.py pnl                         # PnL summary across all bots
  python main.py pnl wta                     # PnL for one bot
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from src import config as cfg_mod
from src import state as state_mod
from src.analytics import backtest as backtest_mod
from src.analytics import edge as edge_mod
from src.analytics import pnl as pnl_mod
from src.bots.runner import BotRunner
from src.client import PolymarketClient
from src.markets import fetch_markets
from src.tracker import Tracker


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_list(app: cfg_mod.AppConfig, _args) -> int:
    for name, b in app.bots.items():
        marker = "ON " if b.enabled else "off"
        print(f"  [{marker}] {name:10s} sport={b.sport:10s} tag={b.polymarket_tag:10s} account={b.account}")
        if b.description:
            print(f"          {b.description}")
    print(f"\nAccounts configured: {', '.join(app.accounts) or '(none — set PRIVATE_KEY/FUNDER_ADDRESS in .env)'}")
    print(f"Dry run: {app.globals.dry_run}   refresh: {app.globals.refresh_interval}s   discount: {app.globals.discount}")
    return 0


def cmd_status(app: cfg_mod.AppConfig, args) -> int:
    account_name = args.account or "primary"
    account = app.accounts.get(account_name)
    if not account:
        print(f"No credentials for account '{account_name}'. Set PRIVATE_KEY/FUNDER_ADDRESS in .env.")
        return 1
    client = PolymarketClient(account=account, host=app.clob_host, chain_id=app.chain_id)
    Tracker(client, label=account_name).snapshot()
    return 0


def cmd_markets(app: cfg_mod.AppConfig, args) -> int:
    bot = app.bots.get(args.bot)
    if not bot:
        print(f"Unknown bot '{args.bot}'. Available: {', '.join(app.bots)}")
        return 1
    markets = fetch_markets(app.gamma_host, bot.polymarket_tag)
    if not markets:
        print("No markets found.")
        return 0
    for m in markets[:50]:
        prices = " / ".join(f"{o}={p:.2f}" for o, p in zip(m.outcomes, m.last_prices))
        end = m.end_date.isoformat(timespec="minutes") if m.end_date else "?"
        print(f"  {end}  {m.question[:70]:70s}  {prices}")
    return 0


def cmd_dashboard(app: cfg_mod.AppConfig, args) -> int:  # noqa: ARG001
    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. `pip install -r requirements.txt`")
        return 1
    print(f"Dashboard: http://{args.host}:{args.port}")
    uvicorn.run("src.api:app", host=args.host, port=args.port, log_level="info")
    return 0


_BACKTEST_CACHE_DIR = Path("state")


def _backtest_cache_path(tag: str, days: int) -> Path:
    _BACKTEST_CACHE_DIR.mkdir(exist_ok=True)
    return _BACKTEST_CACHE_DIR / f"backtest_{tag}_{days}d.json"


def _load_or_run_backtest(
    app: cfg_mod.AppConfig, tag: str, days: int, force: bool,
) -> backtest_mod.BacktestStats:
    cache_path = _backtest_cache_path(tag, days)
    if not force and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            trades = [backtest_mod.BacktestTrade(**t) for t in data.get("trades", [])]
            stats_kwargs = {k: v for k, v in data.items() if k not in {"trades", "generated_at"}}
            return backtest_mod.BacktestStats(trades=trades, **stats_kwargs)
        except Exception:  # noqa: BLE001
            pass
    stats = backtest_mod.run_backtest(
        app.globals,
        gamma_host=app.gamma_host,
        clob_host=app.clob_host,
        tag=tag,
        days=days,
    )
    payload = backtest_mod.stats_to_dict(stats)
    payload["trades"] = [t.__dict__ for t in stats.trades]
    cache_path.write_text(json.dumps(payload, indent=2, default=str))
    return stats


def cmd_backtest(app: cfg_mod.AppConfig, args) -> int:
    bot = app.bots.get(args.bot)
    if not bot:
        print(f"Unknown bot '{args.bot}'. Available: {', '.join(app.bots)}")
        return 1
    print(f"Running backtest for tag={bot.polymarket_tag} over {args.days} days...")
    stats = _load_or_run_backtest(app, bot.polymarket_tag, args.days, force=args.refresh)
    for line in stats.summary_lines():
        print(line)
    if args.top:
        print("\nTop winning fills:")
        winners = sorted([t for t in stats.trades if t.won is True],
                         key=lambda t: t.pnl_usdc, reverse=True)[:args.top]
        for t in winners:
            print(f"  +${t.pnl_usdc:.2f}  bid={t.bid:.2f}  fav={t.favorite_price:.2f}  {t.question[:60]}")
        print("\nTop losing fills:")
        losers = sorted([t for t in stats.trades if t.won is False],
                        key=lambda t: t.pnl_usdc)[:args.top]
        for t in losers:
            print(f"  ${t.pnl_usdc:+.2f}  bid={t.bid:.2f}  fav={t.favorite_price:.2f}  {t.question[:60]}")
    return 0


def cmd_edge(app: cfg_mod.AppConfig, args) -> int:
    bot = app.bots.get(args.bot)
    if not bot:
        print(f"Unknown bot '{args.bot}'. Available: {', '.join(app.bots)}")
        return 1
    if not bot.enabled:
        print(f"Bot '{bot.name}' is disabled — enable it in config.yaml first.")
        return 1
    print(f"Loading backtest for tag={bot.polymarket_tag} ({args.days}d, cached if available)...")
    stats = _load_or_run_backtest(app, bot.polymarket_tag, args.days, force=False)

    print(f"Planning current cycle for {bot.name}...")
    # Force dry-run so planning doesn't accidentally send orders.
    app.globals.dry_run = True
    runner = BotRunner(app, bot)
    markets = fetch_markets(app.gamma_host, bot.polymarket_tag)
    plan = runner.strategy.plan(markets)
    if not plan.intents:
        print("No intents this cycle.")
        return 0

    rows = edge_mod.score_intents(plan.intents, stats)
    print()
    print(f"{'EV $':>8}  {'EV/sh':>7}  {'fill':>5}  {'win|f':>5}  {'bid':>5}  {'fav':>5}  market")
    print("-" * 100)
    for r in rows:
        print(
            f"  {r.ev_dollars:+7.3f}  {r.ev_per_share:+6.3f}  "
            f"{r.fill_rate*100:4.0f}%  {r.win_rate_given_fill*100:4.0f}%  "
            f"{r.bid:.2f}  {r.favorite_price:.2f}  {r.market_slug[:60]}"
        )
    pos_ev = sum(r.ev_dollars for r in rows if r.ev_dollars > 0)
    neg_ev = sum(r.ev_dollars for r in rows if r.ev_dollars <= 0)
    print()
    print(f"Sum positive-EV intents: ${pos_ev:+.2f}   non-positive: ${neg_ev:+.2f}")
    print("Caveat: EV uses historical fill rates that assume queue-front fills. Real EV is lower.")
    return 0


def cmd_pnl(app: cfg_mod.AppConfig, args) -> int:
    bots_to_check = [args.bot] if args.bot else list(app.bots.keys())
    pnls = []
    account_positions: dict[str, list] = {}
    for name in bots_to_check:
        b = app.bots.get(name)
        if not b:
            print(f"Unknown bot '{name}'.")
            continue
        positions = None
        if not args.no_live and b.account in app.accounts:
            if b.account not in account_positions:
                try:
                    client = PolymarketClient(account=app.accounts[b.account],
                                              host=app.clob_host, chain_id=app.chain_id)
                    account_positions[b.account] = client.positions()
                except Exception as e:  # noqa: BLE001
                    print(f"  ({name}: position fetch failed: {e})")
                    account_positions[b.account] = []
            positions = account_positions[b.account]
        pnls.append(pnl_mod.compute_bot_pnl(name, positions=positions))
    for line in pnl_mod.summary_lines(pnls):
        print(line)
    return 0


def cmd_run(app: cfg_mod.AppConfig, args) -> int:
    bot = app.bots.get(args.bot)
    if not bot:
        print(f"Unknown bot '{args.bot}'. Available: {', '.join(app.bots)}")
        return 1
    if not bot.enabled:
        print(f"Bot '{args.bot}' is disabled in config.yaml — enable it first.")
        return 1
    if args.live:
        app.globals.dry_run = False
    runner = BotRunner(app, bot)
    if args.once:
        runner.run_once()
    else:
        runner.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="polymarket-bot")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show configured bots")

    p_status = sub.add_parser("status", help="show balance, open orders, positions")
    p_status.add_argument("--account", default=None)

    p_markets = sub.add_parser("markets", help="preview markets in scope for a bot")
    p_markets.add_argument("bot")

    p_run = sub.add_parser("run", help="run a bot")
    p_run.add_argument("bot")
    p_run.add_argument("--once", action="store_true", help="run one cycle and exit")
    p_run.add_argument("--live", action="store_true", help="override dry_run=true in config")

    p_dash = sub.add_parser("dashboard", help="serve the web dashboard")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8787)

    p_bt = sub.add_parser("backtest", help="replay history through the strategy")
    p_bt.add_argument("bot")
    p_bt.add_argument("--days", type=int, default=30)
    p_bt.add_argument("--refresh", action="store_true", help="ignore cache, refetch history")
    p_bt.add_argument("--top", type=int, default=0, help="show top N winning + losing fills")

    p_edge = sub.add_parser("edge", help="score current planning intents by EV")
    p_edge.add_argument("bot")
    p_edge.add_argument("--days", type=int, default=30, help="backtest lookback window")

    p_pnl = sub.add_parser("pnl", help="PnL summary across bots")
    p_pnl.add_argument("bot", nargs="?", default=None)
    p_pnl.add_argument("--no-live", action="store_true",
                       help="skip live position fetch (state-only)")

    args = parser.parse_args(argv)
    app = cfg_mod.load(args.config)

    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "markets": cmd_markets,
        "run": cmd_run,
        "dashboard": cmd_dashboard,
        "backtest": cmd_backtest,
        "edge": cmd_edge,
        "pnl": cmd_pnl,
    }
    return dispatch[args.cmd](app, args)


if __name__ == "__main__":
    sys.exit(main())
