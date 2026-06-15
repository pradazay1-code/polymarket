"""FastAPI dashboard for the Polymarket automation system.

Endpoints:
    GET  /              -> single-page dashboard UI
    GET  /api/bots      -> bot definitions + latest snapshot summaries
    GET  /api/bots/{name}     -> full snapshot + history for one bot
    GET  /api/account/{name}  -> live balance / allowance / open orders / positions
    POST /api/bots/{name}/tick  -> run one cycle synchronously
    POST /api/bots/{name}/run   -> start the bot loop in a background thread
    POST /api/bots/{name}/stop  -> stop the running loop
    POST /api/settings          -> update mutable globals (dry_run, bet_size_usdc)
    POST /api/analytics/backtest/{name}  -> kick off backtest (async)
    GET  /api/analytics/backtest/{name}  -> current backtest result + job status
    GET  /api/analytics/pnl              -> aggregated PnL across bots
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import config as cfg_mod
from . import state
from .analytics import backtest as backtest_mod
from .analytics import pnl as pnl_mod
from .bots.runner import BotRunner
from .client import PolymarketClient

log = logging.getLogger(__name__)

_app_cfg: cfg_mod.AppConfig | None = None
_runners: dict[str, BotRunner] = {}
_threads: dict[str, threading.Thread] = {}
_clients: dict[str, PolymarketClient] = {}
_backtest_jobs: dict[str, dict[str, Any]] = {}  # bot_name -> {"thread", "started_at", "days", "error"}
_BACKTEST_CACHE_DIR = Path("state")


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
            "strategy": b.strategy,
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


# --- settings ---------------------------------------------------------------


@app.post("/api/settings")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Mutate at-runtime globals. Persisted to config.yaml requires editing
    the file; this only changes the in-process AppConfig until restart."""
    cfg = _cfg()
    changed = {}
    if "dry_run" in payload:
        cfg.globals.dry_run = bool(payload["dry_run"])
        changed["dry_run"] = cfg.globals.dry_run
    if "bet_size_usdc" in payload:
        try:
            v = float(payload["bet_size_usdc"])
            if 0 < v <= 1000:
                cfg.globals.bet_size_usdc = v
                changed["bet_size_usdc"] = v
        except (TypeError, ValueError):
            pass
    if "max_open_notional_usdc" in payload:
        try:
            v = float(payload["max_open_notional_usdc"])
            if 0 < v <= 100000:
                cfg.globals.max_open_notional_usdc = v
                changed["max_open_notional_usdc"] = v
        except (TypeError, ValueError):
            pass
    return {"updated": changed, "note": "in-memory only; restart reloads config.yaml"}


# --- analytics --------------------------------------------------------------


def _backtest_cache_path(tag: str, days: int) -> Path:
    _BACKTEST_CACHE_DIR.mkdir(exist_ok=True)
    return _BACKTEST_CACHE_DIR / f"backtest_{tag}_{days}d.json"


def _run_backtest_async(bot_name: str, tag: str, days: int) -> None:
    job = _backtest_jobs.get(bot_name)
    if job is None:
        return
    cfg = _cfg()
    try:
        stats = backtest_mod.run_backtest(
            cfg.globals,
            gamma_host=cfg.gamma_host,
            clob_host=cfg.clob_host,
            tag=tag,
            days=days,
        )
        payload = backtest_mod.stats_to_dict(stats)
        payload["trades"] = [t.__dict__ for t in stats.trades]
        _backtest_cache_path(tag, days).write_text(json.dumps(payload, indent=2, default=str))
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        log.exception("backtest failed bot=%s", bot_name)
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/api/analytics/backtest/{bot_name}")
def start_backtest(bot_name: str, days: int = 30) -> dict[str, Any]:
    cfg = _cfg()
    bot = cfg.bots.get(bot_name)
    if not bot:
        raise HTTPException(404, f"unknown bot '{bot_name}'")
    existing = _backtest_jobs.get(bot_name)
    if existing and existing.get("thread") and existing["thread"].is_alive():
        return {"status": "already running", "started_at": existing.get("started_at"), "days": existing.get("days")}
    job: dict[str, Any] = {
        "status": "running",
        "started_at": state.now_iso(),
        "days": days,
        "tag": bot.polymarket_tag,
        "error": None,
    }
    _backtest_jobs[bot_name] = job
    t = threading.Thread(
        target=_run_backtest_async,
        args=(bot_name, bot.polymarket_tag, days),
        name=f"backtest-{bot_name}",
        daemon=True,
    )
    job["thread"] = t
    t.start()
    return {"status": "started", "started_at": job["started_at"], "days": days}


@app.get("/api/analytics/backtest/{bot_name}")
def get_backtest(bot_name: str, days: int = 30) -> dict[str, Any]:
    cfg = _cfg()
    bot = cfg.bots.get(bot_name)
    if not bot:
        raise HTTPException(404, f"unknown bot '{bot_name}'")
    job = _backtest_jobs.get(bot_name)
    job_status = None
    if job:
        thread = job.get("thread")
        alive = thread is not None and thread.is_alive()
        job_status = {
            "status": "running" if alive else (job.get("status") or "idle"),
            "started_at": job.get("started_at"),
            "days": job.get("days"),
            "error": job.get("error"),
        }
    cache = _backtest_cache_path(bot.polymarket_tag, days)
    result: dict[str, Any] | None = None
    if cache.exists():
        try:
            result = json.loads(cache.read_text())
        except json.JSONDecodeError:
            result = None
    return {"bot": bot_name, "tag": bot.polymarket_tag, "days": days, "job": job_status, "result": result}


@app.get("/api/analytics/pnl")
def get_pnl() -> dict[str, Any]:
    cfg = _cfg()
    account_positions: dict[str, list] = {}
    rows = []
    for name, b in cfg.bots.items():
        positions = None
        if b.account in cfg.accounts:
            if b.account not in account_positions:
                try:
                    client = _get_client(b.account)
                    account_positions[b.account] = client.positions()
                except Exception as e:  # noqa: BLE001
                    log.warning("position fetch failed for %s: %s", b.account, e)
                    account_positions[b.account] = []
            positions = account_positions[b.account]
        rows.append(pnl_mod.compute_bot_pnl(name, positions=positions).to_dict())
    return {"bots": rows}


@app.exception_handler(Exception)
async def _err(_req, exc):  # noqa: ARG001
    log.exception("api error")
    return JSONResponse(status_code=500, content={"error": str(exc)})
