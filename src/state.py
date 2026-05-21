"""Persistent state for the bot loop + dashboard.

Single-file JSON store under `state/<bot>.json`. The runner writes a snapshot
at the end of every cycle; the dashboard reads them.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
STATE_DIR = Path("state")


@dataclass
class CycleSnapshot:
    bot: str
    ts: str
    dry_run: bool
    inspected: int
    planned: int
    posted: int
    intents: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    balance: float = 0.0
    allowance: float = 0.0
    error: str | None = None


def _path(bot: str) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{bot}.json"


def write_snapshot(snap: CycleSnapshot) -> None:
    p = _path(snap.bot)
    with _LOCK:
        existing = read_state(snap.bot)
        history = existing.get("history", [])
        history.append({
            "ts": snap.ts,
            "inspected": snap.inspected,
            "planned": snap.planned,
            "posted": snap.posted,
            "dry_run": snap.dry_run,
            "error": snap.error,
        })
        # Keep last 200 cycles.
        history = history[-200:]
        payload = {
            "bot": snap.bot,
            "latest": {
                "ts": snap.ts,
                "dry_run": snap.dry_run,
                "inspected": snap.inspected,
                "planned": snap.planned,
                "posted": snap.posted,
                "intents": snap.intents,
                "skipped": snap.skipped,
                "posts": snap.posts,
                "balance": snap.balance,
                "allowance": snap.allowance,
                "error": snap.error,
            },
            "history": history,
        }
        p.write_text(json.dumps(payload, indent=2, default=str))


def read_state(bot: str) -> dict[str, Any]:
    p = _path(bot)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def list_bots() -> list[str]:
    if not STATE_DIR.exists():
        return []
    return sorted(p.stem for p in STATE_DIR.glob("*.json"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
