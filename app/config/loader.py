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

# Analysis-universe allowlist: (category, subcategory) pairs fed into Stage
# 2. config/markets.yaml can define classification rules beyond this without
# expanding what actually gets analyzed - this is the deliberate narrowing
# step. Used as the default when profile.yaml has no target_subcategories
# section, so a missing/malformed config fails toward the narrow universe,
# not the broad one.
#
# Sports/basketball removed (scope reduction, requirement #17) - see the
# matching comment in config/profile.yaml.
DEFAULT_TARGET_SUBCATEGORIES: dict[str, frozenset[str]] = {
    "politics": frozenset({"elon_musk_tweets", "white_house_tweets"}),
    "crypto": frozenset({"btc_up_down"}),
}


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
class AnalysisSettings:
    min_snapshots_for_history: int = 3
    min_history_hours: float = 1.0

    max_probability_adjustment: float = 0.15
    partial_data_max_adjustment: float = 0.05
    momentum_adjustment_weight: float = 0.3

    category_reliability: dict[str, float] = field(
        default_factory=lambda: {"politics": 0.7, "crypto": 0.8, "sports": 0.6, "other": 0.3}
    )
    slippage_estimate: float = 0.01

    risk_gate_max_spread: float = 0.08
    risk_gate_min_liquidity: float = 2000.0
    risk_gate_min_hours_to_resolution: float = 4.0
    risk_gate_min_edge: float = 0.05

    score_weight_edge: float = 35.0
    score_weight_confidence: float = 20.0
    score_weight_liquidity: float = 15.0
    score_weight_data_quality: float = 15.0
    score_weight_asymmetric_bonus: float = 15.0
    # Normalization anchors: a sub-score reaches 1.0 (full weight) once the
    # underlying value hits this point, not a hard cap on the raw value.
    score_edge_normalization: float = 0.30
    score_liquidity_normalization_multiplier: float = 5.0

    compound_min_score: float = 60.0
    watch_min_score: float = 35.0
    asymmetric_edge_threshold: float = 0.25

    # Caps how many included markets per target category get analyzed in one
    # `analyze` run, keeping runtime/memory bounded on a small VPS - the
    # analysis pipeline does no network I/O itself, but SQLite work still
    # scales with market count.
    max_markets_per_category: int = 200


@dataclass
class NotificationSettings:
    # Secrets - read from environment/.env only, never from profile.yaml
    # (which is checked into git). Both None means notifications are
    # unconfigured and app/notify/telegram.py silently no-ops.
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@dataclass
class AppConfig:
    filters: FilterSettings = field(default_factory=FilterSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    category_rules: list[CategoryRule] = field(default_factory=list)
    # (category, subcategory) allowlist - see DEFAULT_TARGET_SUBCATEGORIES.
    target_subcategories: dict[str, frozenset[str]] = field(
        default_factory=lambda: dict(DEFAULT_TARGET_SUBCATEGORIES)
    )
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
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

    analysis_raw = profile.get("analysis") or {}
    analysis_defaults = AnalysisSettings()
    analysis = AnalysisSettings(
        min_snapshots_for_history=int(
            analysis_raw.get("min_snapshots_for_history", analysis_defaults.min_snapshots_for_history)
        ),
        min_history_hours=float(
            analysis_raw.get("min_history_hours", analysis_defaults.min_history_hours)
        ),
        max_probability_adjustment=float(
            analysis_raw.get("max_probability_adjustment", analysis_defaults.max_probability_adjustment)
        ),
        partial_data_max_adjustment=float(
            analysis_raw.get("partial_data_max_adjustment", analysis_defaults.partial_data_max_adjustment)
        ),
        momentum_adjustment_weight=float(
            analysis_raw.get("momentum_adjustment_weight", analysis_defaults.momentum_adjustment_weight)
        ),
        category_reliability={
            str(k): float(v)
            for k, v in (analysis_raw.get("category_reliability") or analysis_defaults.category_reliability).items()
        },
        slippage_estimate=float(
            analysis_raw.get("slippage_estimate", analysis_defaults.slippage_estimate)
        ),
        risk_gate_max_spread=float(
            analysis_raw.get("risk_gate_max_spread", analysis_defaults.risk_gate_max_spread)
        ),
        risk_gate_min_liquidity=float(
            analysis_raw.get("risk_gate_min_liquidity", analysis_defaults.risk_gate_min_liquidity)
        ),
        risk_gate_min_hours_to_resolution=float(
            analysis_raw.get(
                "risk_gate_min_hours_to_resolution", analysis_defaults.risk_gate_min_hours_to_resolution
            )
        ),
        risk_gate_min_edge=float(
            analysis_raw.get("risk_gate_min_edge", analysis_defaults.risk_gate_min_edge)
        ),
        score_weight_edge=float(
            analysis_raw.get("score_weight_edge", analysis_defaults.score_weight_edge)
        ),
        score_weight_confidence=float(
            analysis_raw.get("score_weight_confidence", analysis_defaults.score_weight_confidence)
        ),
        score_weight_liquidity=float(
            analysis_raw.get("score_weight_liquidity", analysis_defaults.score_weight_liquidity)
        ),
        score_weight_data_quality=float(
            analysis_raw.get("score_weight_data_quality", analysis_defaults.score_weight_data_quality)
        ),
        score_weight_asymmetric_bonus=float(
            analysis_raw.get("score_weight_asymmetric_bonus", analysis_defaults.score_weight_asymmetric_bonus)
        ),
        score_edge_normalization=float(
            analysis_raw.get("score_edge_normalization", analysis_defaults.score_edge_normalization)
        ),
        score_liquidity_normalization_multiplier=float(
            analysis_raw.get(
                "score_liquidity_normalization_multiplier",
                analysis_defaults.score_liquidity_normalization_multiplier,
            )
        ),
        compound_min_score=float(
            analysis_raw.get("compound_min_score", analysis_defaults.compound_min_score)
        ),
        watch_min_score=float(
            analysis_raw.get("watch_min_score", analysis_defaults.watch_min_score)
        ),
        asymmetric_edge_threshold=float(
            analysis_raw.get("asymmetric_edge_threshold", analysis_defaults.asymmetric_edge_threshold)
        ),
        max_markets_per_category=int(
            analysis_raw.get("max_markets_per_category", analysis_defaults.max_markets_per_category)
        ),
    )

    category_rules = load_category_rules(markets_path) if markets_path.exists() else []

    target_subcategories_raw = profile.get("target_subcategories")
    if target_subcategories_raw:
        target_subcategories = {
            str(category): frozenset(str(sub) for sub in (subs or []))
            for category, subs in target_subcategories_raw.items()
        }
    else:
        target_subcategories = dict(DEFAULT_TARGET_SUBCATEGORIES)

    db_path = Path(os.environ.get("DB_PATH", storage_raw.get("db_path", "data/polymarket.db")))
    log_level = os.environ.get("LOG_LEVEL", logging_raw.get("level", "INFO"))

    # Secrets - env/.env only, no profile.yaml fallback (see NotificationSettings).
    notifications = NotificationSettings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
    )

    return AppConfig(
        filters=filters,
        analysis=analysis,
        category_rules=category_rules,
        target_subcategories=target_subcategories,
        notifications=notifications,
        db_path=db_path,
        log_level=log_level,
    )
