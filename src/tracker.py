"""Track open orders + positions; pretty-print to stdout via rich."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from rich.console import Console
from rich.table import Table

from .client import PolymarketClient

log = logging.getLogger(__name__)
_console = Console()


class Tracker:
    def __init__(self, client: PolymarketClient, label: str):
        self.client = client
        self.label = label

    def snapshot(self) -> None:
        orders = self.client.open_orders()
        positions = self.client.positions()
        bal = self.client.usdc_balance()

        header = f"[{self.label}] {datetime.now(timezone.utc).isoformat(timespec='seconds')}  USDC=${bal:.2f}"
        _console.rule(header)

        if orders:
            t = Table(title="Open Orders", show_lines=False)
            for col in ("market", "side", "price", "size", "filled"):
                t.add_column(col)
            for o in orders[:50]:
                t.add_row(
                    str(o.get("market", ""))[:18],
                    str(o.get("side", "")),
                    f"{float(o.get('price', 0)):.2f}",
                    f"{float(o.get('original_size', o.get('size', 0))):.2f}",
                    f"{float(o.get('size_matched', 0)):.2f}",
                )
            _console.print(t)
        else:
            _console.print("[dim]no open orders[/dim]")

        if positions:
            t = Table(title="Positions", show_lines=False)
            for col in ("market", "outcome", "size", "avg", "cur", "pnl"):
                t.add_column(col)
            for p in positions[:50]:
                t.add_row(
                    str(p.get("title", p.get("slug", "")))[:32],
                    str(p.get("outcome", "")),
                    f"{float(p.get('size', 0)):.2f}",
                    f"{float(p.get('avgPrice', 0)):.2f}",
                    f"{float(p.get('curPrice', 0)):.2f}",
                    f"{float(p.get('cashPnl', 0)):.2f}",
                )
            _console.print(t)
        else:
            _console.print("[dim]no positions[/dim]")
