"""FastAPI dashboard for the Polymarket automation system.

Endpoints:
    GET  /              -> single-page dashboard UI
    GET  /api/bots      -> bot definitions + latest snapshot summaries
    GET  /api/bots/{name}     -> full snapshot + history for one bot
    GET  /api/account/{name}  -> live balance / allowance / open orders / positions
    POST /api/bots/{name}/tick  -> run one cycle synchronously
    POST /api/bots/{name}/run   -> start the bot loop in a background thread
    POST /api/bots/{name}/stop  -> stop the running loop
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import config as cfg_mod
from . import state
from .bots.runner import BotRunner
from .client import PolymarketClient

log = logging.getLogger(__name__)

_app_cfg: cfg_mod.AppConfig | None = None
_runners: dict[str, BotRunner] = {}
_threads: dict[str, threading.Thread] = {}
_clients: dict[str, PolymarketClient] = {}


def _cfg() -> cfg_mod.AppConfig:
    global _app_cfg
    if _app_cfg is None:
        _app_cfg = cfg_mod.load()
    return _app_cfg


def _get_or_create_runner(bot_name: str) -> BotRunner:
    if bot_name in _runners:
        return _runners[bot_name]
    cfg = _cfg()
    bot = cfg.bots.get(bot_name)
    if not bot:
        raise HTTPException(404, f"unknown bot '{bot_name}'")
    if not bot.enabled:
        raise HTTPException(400, f"bot '{bot_name}' is disabled in config.yaml")
    runner = BotRunner(cfg, bot)
    _runners[bot_name] = runner
    _clients[bot.account] = runner.client
    return runner


def _get_client(account_name: str) -> PolymarketClient:
    if account_name in _clients:
        return _clients[account_name]
    cfg = _cfg()
    account = cfg.accounts.get(account_name)
    if not account:
        raise HTTPException(404, f"no credentials for account '{account_name}'")
    client = PolymarketClient(account=account, host=cfg.clob_host, chain_id=cfg.chain_id)
    _clients[account_name] = client
    return client


app = FastAPI(title="Polymarket Automation Dashboard")


@app.get("/")
def index():
    web_index = Path(__file__).parent.parent / "web" / "index.html"
    if not web_index.exists():
        raise HTTPException(500, "dashboard HTML not found")
    return FileResponse(web_index)


@app.get("/api/bots")
def list_bots() -> dict[str, Any]:
    cfg = _cfg()
    out = []
    for name, b in cfg.bots.items():
        s = state.read_state(name)
        latest = s.get("latest") if s else None
        out.append({
            "name": name,
            "enabled": b.enabled,
            "description": b.description,
            "sport": b.sport,
            "tag": b.polymarket_tag,
            "account": b.account,
            "running": name in _threads and _threads[name].is_alive(),
            "latest": latest,
        })
    return {
        "bots": out,
        "globals": {
            "dry_run": cfg.globals.dry_run,
            "discount": cfg.globals.discount,
            "refresh_interval": cfg.globals.refresh_interval,
            "bet_size_usdc": cfg.globals.bet_size_usdc,
            "max_open_notional_usdc": cfg.globals.max_open_notional_usdc,
            "min_favorite_price": cfg.globals.min_favorite_price,
            "max_favorite_price": cfg.globals.max_favorite_price,
        },
        "accounts": list(cfg.accounts.keys()),
    }


@app.get("/api/bots/{bot_name}")
def bot_detail(bot_name: str) -> dict[str, Any]:
    cfg = _cfg()
    b = cfg.bots.get(bot_name)
    if not b:
        raise HTTPException(404, f"unknown bot '{bot_name}'")
    s = state.read_state(bot_name)
    return {
        "name": bot_name,
        "enabled": b.enabled,
        "description": b.description,
        "tag": b.polymarket_tag,
        "account": b.account,
        "running": bot_name in _threads and _threads[bot_name].is_alive(),
        "state": s,
    }


@app.get("/api/account/{account_name}")
def account_detail(account_name: str) -> dict[str, Any]:
    client = _get_client(account_name)
    ba = client.balance_allowance()
    return {
        "account": account_name,
        "funder": client.account.funder_address,
        "balance": ba["balance"],
        "allowance": ba["allowance"],
        "open_orders": client.open_orders(),
        "positions": client.positions(),
    }


@app.post("/api/bots/{bot_name}/tick")
def bot_tick(bot_name: str) -> dict[str, Any]:
    runner = _get_or_create_runner(bot_name)
    snap = runner.tick()
    return {
        "ts": snap.ts,
        "inspected": snap.inspected,
        "planned": snap.planned,
        "posted": snap.posted,
        "dry_run": snap.dry_run,
        "error": snap.error,
    }


@app.post("/api/bots/{bot_name}/run")
def bot_start(bot_name: str) -> dict[str, Any]:
    if bot_name in _threads and _threads[bot_name].is_alive():
        return {"status": "already running"}
    runner = _get_or_create_runner(bot_name)
    t = threading.Thread(target=runner.run_forever, name=f"bot-{bot_name}", daemon=True)
    t.start()
    _threads[bot_name] = t
    return {"status": "started"}


@app.post("/api/bots/{bot_name}/stop")
def bot_stop(bot_name: str) -> dict[str, Any]:
    runner = _runners.get(bot_name)
    if not runner:
        return {"status": "not running"}
    runner._stop.set()
    return {"status": "stopping"}


@app.exception_handler(Exception)
async def _err(_req, exc):  # noqa: ARG001
    log.exception("api error")
    return JSONResponse(status_code=500, content={"error": str(exc)})
