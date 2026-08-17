"""Loads config/profile.yaml + config/markets.yaml + .env into a typed AppConfig.

No secrets are required for Stage 1 (no Telegram/LLM yet), but .env is loaded
now so later stages don't need a config-loading refactor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app.polymarket.categorize import CategoryRule, load_category_rules

DEFAULT_PROFILE_PATH = Path("config/profile.yaml")
DEFAULT_MARKETS_PATH = Path("config/markets.yaml")


@dataclass
class FilterSettings:
    min_liquidity: float = 1000.0
    max_spread: float = 0.10
    min_hours_to_resolution: float = 2.0
    scan_interval_seconds: int = 900
    request_timeout_seconds: float = 10.0
    max_retries: int = 4
    events_page_size: int = 100
    events_max_pages: int = 20
    events_order: str = "volume24hr"
    events_ascending: bool = False
    gamma_rate_limit_per_10s: int = 3500
    clob_rate_limit_per_10s: int = 1500


@dataclass
class AppConfig:
    filters: FilterSettings = field(default_factory=FilterSettings)
    category_rules: list[CategoryRule] = field(default_factory=list)
    db_path: Path = Path("data/polymarket.db")
    log_level: str = "INFO"


def load_config(
    profile_path: Path = DEFAULT_PROFILE_PATH,
    markets_path: Path = DEFAULT_MARKETS_PATH,
) -> AppConfig:
    load_dotenv()

    profile: dict = {}
    if profile_path.exists():
        profile = yaml.safe_load(profile_path.read_text()) or {}

    filters_raw = profile.get("filters") or {}
    discovery_raw = profile.get("discovery") or {}
    http_raw = profile.get("http") or {}
    storage_raw = profile.get("storage") or {}
    logging_raw = profile.get("logging") or {}

    defaults = FilterSettings()
    filters = FilterSettings(
        min_liquidity=float(filters_raw.get("min_liquidity", defaults.min_liquidity)),
        max_spread=float(filters_raw.get("max_spread", defaults.max_spread)),
        min_hours_to_resolution=float(
            filters_raw.get("min_hours_to_resolution", defaults.min_hours_to_resolution)
        ),
        scan_interval_seconds=int(
            discovery_raw.get("scan_interval_seconds", defaults.scan_interval_seconds)
        ),
        request_timeout_seconds=float(
            http_raw.get("timeout_seconds", defaults.request_timeout_seconds)
        ),
        max_retries=int(http_raw.get("max_retries", defaults.max_retries)),
        events_page_size=int(discovery_raw.get("events_page_size", defaults.events_page_size)),
        events_max_pages=int(discovery_raw.get("events_max_pages", defaults.events_max_pages)),
        events_order=str(discovery_raw.get("order", defaults.events_order)),
        events_ascending=bool(discovery_raw.get("ascending", defaults.events_ascending)),
        gamma_rate_limit_per_10s=int(
            http_raw.get("gamma_rate_limit_per_10s", defaults.gamma_rate_limit_per_10s)
        ),
        clob_rate_limit_per_10s=int(
            http_raw.get("clob_rate_limit_per_10s", defaults.clob_rate_limit_per_10s)
        ),
    )

    category_rules = load_category_rules(markets_path) if markets_path.exists() else []

    db_path = Path(os.environ.get("DB_PATH", storage_raw.get("db_path", "data/polymarket.db")))
    log_level = os.environ.get("LOG_LEVEL", logging_raw.get("level", "INFO"))

    return AppConfig(
        filters=filters,
        category_rules=category_rules,
        db_path=db_path,
        log_level=log_level,
    )
