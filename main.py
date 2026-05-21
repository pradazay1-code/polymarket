"""Polymarket automation CLI.

Examples:
  python main.py list                # show configured bots
  python main.py status              # show balance, open orders, positions
  python main.py run wta             # run the WTA tennis bot forever
  python main.py run wta --once      # one cycle, then exit
  python main.py run wta --live      # disable dry_run for this invocation
  python main.py markets wta         # preview markets the wta bot would target
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from src import config as cfg_mod
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

    args = parser.parse_args(argv)
    app = cfg_mod.load(args.config)

    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "markets": cmd_markets,
        "run": cmd_run,
        "dashboard": cmd_dashboard,
    }
    return dispatch[args.cmd](app, args)


if __name__ == "__main__":
    sys.exit(main())
