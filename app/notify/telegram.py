"""Telegram notifications for paper trading (requirement #20).

Read-driven, same architecture as paper trading itself (requirement #19):
never touches app/analysis/* or app/polymarket/*, never writes to
paper_trades/signal_journal/resolutions - it only reads the PaperTrade
objects handed to it by app/paper/paper_trading.py's summaries and looks
up each one's Market for a human-readable message.

Best-effort, never fatal: if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't
set, notify_paper_trading_events() is a silent no-op (this is the VPS's
default state until the user opts in). If Telegram's API is unreachable
or returns an error, that's logged as a warning and skipped - a bad send
must never crash scan/analyze/resolve/paper, and one failed send must
never block the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.config.loader import AppConfig
from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTrade
from app.storage.repositories import MarketRepository

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_telegram_message(
    bot_token: str, chat_id: str, text: str, *, client: httpx.Client | None = None
) -> bool:
    """Best-effort send. Returns True only on a real Telegram-confirmed
    delivery (HTTP 200 and {"ok": true} in the response body) - never
    raises, so a network failure or bad token degrades to a logged
    warning instead of taking down the pipeline."""
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=10.0)

    try:
        response = client.post(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    except httpx.HTTPError as exc:
        logger.warning("failed to send Telegram message: %s", exc)
        return False
    finally:
        if owns_client:
            client.close()

    if response.status_code != 200:
        logger.warning(
            "Telegram API returned status %d: %s", response.status_code, response.text
        )
        return False

    try:
        body = response.json()
    except ValueError:
        logger.warning("Telegram API returned a non-JSON response")
        return False

    if not body.get("ok"):
        logger.warning("Telegram API reported failure: %s", body.get("description"))
        return False

    return True


def format_new_trade_message(trade: PaperTrade, market: Market | None) -> str:
    question = market.question if market is not None else trade.market_id
    return (
        "New paper trade opened\n"
        f"Market: {question}\n"
        f"Category: {trade.category}/{trade.subcategory}\n"
        f"Classification: {trade.classification}\n"
        f"Selected: {trade.selected_outcome} @ {trade.entry_probability:.3f}\n"
        f"Model estimate: {_fmt(trade.estimated_probability)} | "
        f"Edge: {_fmt(trade.adjusted_edge, signed=True)} | Score: {_fmt(trade.score, digits=2)}"
    )


def format_settlement_message(trade: PaperTrade, market: Market | None) -> str:
    question = market.question if market is not None else trade.market_id
    return (
        f"Paper trade settled: {trade.status.value}\n"
        f"Market: {question}\n"
        f"Selected: {trade.selected_outcome} | Winner: {trade.winning_outcome or 'n/a'}\n"
        f"P&L: {_fmt(trade.pnl, signed=True)}"
    )


def _fmt(value: float | None, *, digits: int = 3, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    sign = "+" if signed else ""
    return f"{value:{sign}.{digits}f}"


@dataclass
class NotifySummary:
    sent: int = 0
    failed: int = 0
    configured: bool = True


def notify_paper_trading_events(
    config: AppConfig,
    db: Database,
    created_trades: list[PaperTrade],
    settled_trades: list[PaperTrade],
) -> NotifySummary:
    bot_token = config.notifications.telegram_bot_token
    chat_id = config.notifications.telegram_chat_id
    if not bot_token or not chat_id:
        return NotifySummary(configured=False)

    summary = NotifySummary()
    if not created_trades and not settled_trades:
        return summary

    market_repo = MarketRepository(db)
    client = httpx.Client(timeout=10.0)
    try:
        for trade in created_trades:
            market = market_repo.get_market(trade.market_id)
            text = format_new_trade_message(trade, market)
            if send_telegram_message(bot_token, chat_id, text, client=client):
                summary.sent += 1
            else:
                summary.failed += 1

        for trade in settled_trades:
            market = market_repo.get_market(trade.market_id)
            text = format_settlement_message(trade, market)
            if send_telegram_message(bot_token, chat_id, text, client=client):
                summary.sent += 1
            else:
                summary.failed += 1
    finally:
        market_repo.close()
        client.close()

    return summary
