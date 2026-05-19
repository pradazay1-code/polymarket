"""Configuration loading: YAML bot config + .env account credentials."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Account:
    name: str
    private_key: str
    funder_address: str


@dataclass
class GlobalConfig:
    discount: float
    refresh_interval: int
    bet_size_usdc: float
    max_open_notional_usdc: float
    max_favorite_price: float
    min_favorite_price: float
    cancel_before_refresh: bool
    dry_run: bool


@dataclass
class BotConfig:
    name: str
    enabled: bool
    description: str
    sport: str
    espn_league: str | None
    polymarket_tag: str
    account: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    globals: GlobalConfig
    bots: dict[str, BotConfig]
    accounts: dict[str, Account]
    clob_host: str
    gamma_host: str
    chain_id: int


def _load_accounts() -> dict[str, Account]:
    """Read PRIVATE_KEY / FUNDER_ADDRESS (+ _2, _3, _4) from env."""
    accounts: dict[str, Account] = {}
    suffixes = [("", "primary"), ("_2", "account_2"), ("_3", "account_3"), ("_4", "account_4")]
    for suffix, name in suffixes:
        pk = os.getenv(f"PRIVATE_KEY{suffix}", "").strip()
        funder = os.getenv(f"FUNDER_ADDRESS{suffix}", "").strip()
        if pk and funder:
            accounts[name] = Account(name=name, private_key=pk, funder_address=funder)
    return accounts


def load(path: str | Path = "config.yaml") -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    g = raw["global"]
    globals_ = GlobalConfig(
        discount=float(g["discount"]),
        refresh_interval=int(g["refresh_interval"]),
        bet_size_usdc=float(g["bet_size_usdc"]),
        max_open_notional_usdc=float(g["max_open_notional_usdc"]),
        max_favorite_price=float(g["max_favorite_price"]),
        min_favorite_price=float(g["min_favorite_price"]),
        cancel_before_refresh=bool(g["cancel_before_refresh"]),
        dry_run=bool(g["dry_run"]),
    )
    bots: dict[str, BotConfig] = {}
    for name, b in raw["bots"].items():
        bots[name] = BotConfig(
            name=name,
            enabled=bool(b.get("enabled", False)),
            description=b.get("description", ""),
            sport=b["sport"],
            espn_league=b.get("espn_league"),
            polymarket_tag=b["polymarket_tag"],
            account=b.get("account", "primary"),
            extra={k: v for k, v in b.items() if k not in {"enabled", "description", "sport", "espn_league", "polymarket_tag", "account"}},
        )
    return AppConfig(
        globals=globals_,
        bots=bots,
        accounts=_load_accounts(),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
    )
