"""Bot runner: one tick = fetch markets, plan, execute, persist, sleep."""
from __future__ import annotations

import logging
import signal
import time
import threading

from ..client import PolymarketClient
from ..config import AppConfig, BotConfig
from ..markets import fetch_markets
from ..state import CycleSnapshot, now_iso, write_snapshot
from ..strategies.climate_forecast import ClimateForecastStrategy
from ..strategies.stink_bid import StinkBidStrategy

log = logging.getLogger(__name__)


def _build_strategy(bot: BotConfig, client, cfg):
    name = bot.strategy
    if name == "stink_bid":
        return StinkBidStrategy(client, cfg, label=bot.name)
    if name == "climate_forecast":
        return ClimateForecastStrategy(client, cfg, label=bot.name, extra=bot.extra)
    raise ValueError(f"unknown strategy '{name}' for bot '{bot.name}'")


class BotRunner:
    def __init__(self, app: AppConfig, bot: BotConfig):
        self.app = app
        self.bot = bot
        account = app.accounts.get(bot.account)
        if not account:
            raise RuntimeError(
                f"Bot '{bot.name}' needs account '{bot.account}' but no matching "
                f"PRIVATE_KEY/FUNDER_ADDRESS pair was found in .env"
            )
        self.client = PolymarketClient(account=account, host=app.clob_host, chain_id=app.chain_id)
        self.strategy = _build_strategy(bot, self.client, app.globals)
        self._stop = threading.Event()

    def _shutdown(self, *_):
        log.info("[%s] shutdown requested", self.bot.name)
        self._stop.set()

    def tick(self) -> CycleSnapshot:
        snap = CycleSnapshot(bot=self.bot.name, ts=now_iso(), dry_run=self.app.globals.dry_run,
                             inspected=0, planned=0, posted=0)
        try:
            log.info("[%s] --- cycle start ---", self.bot.name)
            markets = fetch_markets(self.app.gamma_host, self.bot.polymarket_tag)
            snap.inspected = len(markets)
            if not markets:
                log.info("[%s] no markets in scope", self.bot.name)
            else:
                result = self.strategy.plan(markets)
                snap.planned = len(result.intents)
                snap.intents = [i.to_dict() for i in result.intents]
                snap.skipped = result.skipped
                log.info("[%s] planned %d / %d markets", self.bot.name, snap.planned, snap.inspected)
                posts = self.strategy.execute(result.intents)
                snap.posts = posts
                snap.posted = len(posts)
            ba = self.client.balance_allowance()
            snap.balance = ba["balance"]
            snap.allowance = ba["allowance"]
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] tick failed", self.bot.name)
            snap.error = str(e)
        finally:
            write_snapshot(snap)
        return snap

    def run_once(self) -> CycleSnapshot:
        return self.tick()

    def run_forever(self) -> None:
        # Only install signal handlers when we own the main thread.
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._shutdown)
            signal.signal(signal.SIGTERM, self._shutdown)
        interval = self.app.globals.refresh_interval
        log.info("[%s] starting loop every %ds (dry_run=%s)", self.bot.name, interval, self.app.globals.dry_run)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(timeout=interval)
        log.info("[%s] stopped", self.bot.name)
