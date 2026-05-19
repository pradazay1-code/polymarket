"""Bot runner: one tick = fetch markets, plan, execute, sleep."""
from __future__ import annotations

import logging
import signal
import time

from ..client import PolymarketClient
from ..config import AppConfig, BotConfig
from ..markets import fetch_markets
from ..strategies.stink_bid import StinkBidStrategy
from ..tracker import Tracker

log = logging.getLogger(__name__)


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
        self.strategy = StinkBidStrategy(self.client, app.globals, label=bot.name)
        self.tracker = Tracker(self.client, label=bot.name)
        self._stop = False
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, *_):
        log.info("[%s] shutdown requested", self.bot.name)
        self._stop = True

    def tick(self) -> None:
        log.info("[%s] --- cycle start ---", self.bot.name)
        markets = fetch_markets(self.app.gamma_host, self.bot.polymarket_tag)
        if not markets:
            log.info("[%s] no markets in scope this cycle", self.bot.name)
            return
        intents = self.strategy.plan(markets)
        log.info("[%s] planned %d / %d markets", self.bot.name, len(intents), len(markets))
        self.strategy.execute(intents)
        self.tracker.snapshot()

    def run_once(self) -> None:
        self.tick()

    def run_forever(self) -> None:
        interval = self.app.globals.refresh_interval
        log.info("[%s] starting loop, every %ds (dry_run=%s)", self.bot.name, interval, self.app.globals.dry_run)
        while not self._stop:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] tick failed: %s", self.bot.name, e)
            # Sleep in small slices so SIGINT is responsive.
            slept = 0
            while slept < interval and not self._stop:
                time.sleep(min(2, interval - slept))
                slept += 2
        log.info("[%s] stopped", self.bot.name)
