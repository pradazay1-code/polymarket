"""PnL aggregation from state snapshots + live positions.

Two modes coexist depending on whether the bot is live or dry-run:

  LIVE:    posted trades end up in `state/<bot>.json.history[*].posts`.
           Realized + unrealized PnL is read straight from Polymarket's
           data-api positions response (`cashPnl`, `curPrice`, etc.).

  DRY-RUN: posted trades are paper trades. Realized PnL is unknown until
           the underlying market resolves. We mark dry-run trades and
           report counts + notional, deferring resolution to the
           backtester (which can re-simulate them against actual prices).

We DO NOT silently inflate dry-run "would have made" numbers. They're
shown separately so you can tell what's real and what's hypothetical.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .. import state as state_mod


@dataclass
class BotPnL:
    bot: str
    cycles: int = 0
    dry_run_cycles: int = 0
    live_cycles: int = 0
    intents_planned: int = 0
    posts_dry: int = 0
    posts_live: int = 0
    posted_notional_dry: float = 0.0
    posted_notional_live: float = 0.0
    # Position-derived PnL (only meaningful for live).
    open_positions: int = 0
    position_cost: float = 0.0
    position_value: float = 0.0
    cash_pnl: float = 0.0
    realized_pnl: float = 0.0
    # Errors observed (most recent).
    last_error: str | None = None

    def total_value(self) -> float:
        return self.cash_pnl + self.realized_pnl

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "total_pnl": round(self.total_value(), 2),
        }


def _sum_intent_notional(intents: list[dict[str, Any]]) -> float:
    return sum(float(i.get("notional", 0)) for i in intents)


def compute_bot_pnl(bot: str, positions: list[dict[str, Any]] | None = None) -> BotPnL:
    """Aggregate per-bot PnL across all recorded cycles + current positions."""
    s = state_mod.read_state(bot)
    pnl = BotPnL(bot=bot)
    if not s:
        return pnl
    history = s.get("history", []) or []
    pnl.cycles = len(history)
    for h in history:
        if h.get("dry_run"):
            pnl.dry_run_cycles += 1
        else:
            pnl.live_cycles += 1
        if h.get("error"):
            pnl.last_error = h["error"]
    pnl.intents_planned = sum(int(h.get("planned", 0)) for h in history)

    latest = s.get("latest") or {}
    for post in latest.get("posts", []) or []:
        notional = float(post.get("intent", {}).get("notional", 0))
        if post.get("dry_run"):
            pnl.posts_dry += 1
            pnl.posted_notional_dry += notional
        else:
            pnl.posts_live += 1
            pnl.posted_notional_live += notional

    if positions:
        pnl.open_positions = len(positions)
        for p in positions:
            pnl.position_cost += float(p.get("initialValue", p.get("totalBought", 0)) or 0)
            pnl.position_value += float(p.get("currentValue", 0) or 0)
            pnl.cash_pnl += float(p.get("cashPnl", 0) or 0)
            pnl.realized_pnl += float(p.get("realizedPnl", 0) or 0)
    pnl.posted_notional_dry = round(pnl.posted_notional_dry, 2)
    pnl.posted_notional_live = round(pnl.posted_notional_live, 2)
    pnl.position_cost = round(pnl.position_cost, 2)
    pnl.position_value = round(pnl.position_value, 2)
    pnl.cash_pnl = round(pnl.cash_pnl, 2)
    pnl.realized_pnl = round(pnl.realized_pnl, 2)
    return pnl


def summary_lines(pnls: list[BotPnL]) -> list[str]:
    if not pnls:
        return ["No bots with recorded state yet — run a cycle first."]
    out = []
    grand_dry_n = grand_dry_notional = 0
    grand_live_n = grand_live_notional = 0.0
    grand_cash = grand_real = 0.0
    for p in pnls:
        out.append(f"[{p.bot}]  cycles={p.cycles}  (live={p.live_cycles}  dry={p.dry_run_cycles})")
        out.append(f"   planned this run: {p.intents_planned}")
        if p.posts_live or p.posts_dry:
            out.append(
                f"   posts (latest cycle): live={p.posts_live} (${p.posted_notional_live:.2f})  "
                f"dry={p.posts_dry} (${p.posted_notional_dry:.2f})"
            )
        if p.open_positions:
            out.append(
                f"   positions: {p.open_positions}  cost=${p.position_cost:.2f}  "
                f"value=${p.position_value:.2f}  cashPnl=${p.cash_pnl:+.2f}  "
                f"realized=${p.realized_pnl:+.2f}"
            )
        if p.last_error:
            out.append(f"   last error: {p.last_error}")
        grand_dry_n += p.posts_dry
        grand_dry_notional += p.posted_notional_dry
        grand_live_n += p.posts_live
        grand_live_notional += p.posted_notional_live
        grand_cash += p.cash_pnl
        grand_real += p.realized_pnl
    out.append("")
    out.append(
        f"TOTAL  live posts={int(grand_live_n)} (${grand_live_notional:.2f})  "
        f"dry posts={int(grand_dry_n)} (${grand_dry_notional:.2f})  "
        f"cashPnl=${grand_cash:+.2f}  realized=${grand_real:+.2f}"
    )
    out.append("")
    out.append("Note: dry-run posts have no realized PnL until you backtest them against actual prices.")
    out.append("      Use `python main.py backtest <bot>` for hit/win rates on this tag.")
    return out
