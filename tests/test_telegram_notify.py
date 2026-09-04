"""Tests for app/notify/telegram.py (requirement #20) - respx-mocked,
mirroring tests/test_client.py's pattern but for a sync httpx.Client since
send_telegram_message never needs to be async (no other caller in this
module does network I/O)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.config.loader import AppConfig, NotificationSettings
from app.notify.telegram import (
    NotifySummary,
    format_new_trade_message,
    format_settlement_message,
    notify_paper_trading_events,
    send_telegram_message,
)
from app.storage.database import Database
from app.storage.models import Market
from app.storage.paper_trade_models import PaperTrade, PaperTradeStatus
from app.storage.repositories import MarketRepository

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)

TELEGRAM_URL = "https://api.telegram.org/bot123:abc/sendMessage"


def _trade(**overrides) -> PaperTrade:
    defaults = dict(
        market_id="m1",
        classification="COMPOUND",
        category="crypto",
        subcategory="btc_up_down",
        signal_at=T1,
        selected_outcome="Yes",
        entry_probability=0.6,
        estimated_probability=0.75,
        raw_edge=0.15,
        adjusted_edge=0.1,
        score=62.83,
    )
    defaults.update(overrides)
    return PaperTrade(**defaults)


# --------------------------------------------------------------------------
# send_telegram_message
# --------------------------------------------------------------------------


@respx.mock
def test_send_telegram_message_success():
    respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert send_telegram_message("123:abc", "chat1", "hello") is True


@respx.mock
def test_send_telegram_message_telegram_reported_failure():
    respx.post(TELEGRAM_URL).mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"})
    )
    assert send_telegram_message("123:abc", "chat1", "hello") is False


@respx.mock
def test_send_telegram_message_non_200_status():
    respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(401, json={"ok": False}))
    assert send_telegram_message("123:abc", "chat1", "hello") is False


@respx.mock
def test_send_telegram_message_network_error_never_raises():
    respx.post(TELEGRAM_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert send_telegram_message("123:abc", "chat1", "hello") is False


@respx.mock
def test_send_telegram_message_timeout_never_raises():
    respx.post(TELEGRAM_URL).mock(side_effect=httpx.TimeoutException("boom"))
    assert send_telegram_message("123:abc", "chat1", "hello") is False


@respx.mock
def test_send_telegram_message_non_json_response_never_raises():
    respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, text="not json"))
    assert send_telegram_message("123:abc", "chat1", "hello") is False


@respx.mock
def test_send_telegram_message_passes_chat_id_and_text():
    route = respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    send_telegram_message("123:abc", "chat1", "hello world")
    request = route.calls.last.request
    import json as _json

    body = _json.loads(request.content)
    assert body == {"chat_id": "chat1", "text": "hello world"}


# --------------------------------------------------------------------------
# format_new_trade_message / format_settlement_message
# --------------------------------------------------------------------------


def test_format_new_trade_message_includes_market_question_when_available():
    market = Market(id="m1", question="Will BTC be up?", category="crypto", subcategory="btc_up_down")
    text = format_new_trade_message(_trade(), market)
    assert "Will BTC be up?" in text
    assert "COMPOUND" in text
    assert "Yes @ 0.600" in text


def test_format_new_trade_message_falls_back_to_market_id_when_market_missing():
    text = format_new_trade_message(_trade(market_id="m99"), None)
    assert "m99" in text


def test_format_settlement_message_includes_status_and_pnl():
    trade = _trade().model_copy(
        update={"status": PaperTradeStatus.WON, "winning_outcome": "Yes", "pnl": 0.4}
    )
    text = format_settlement_message(trade, None)
    assert "WON" in text
    assert "Winner: Yes" in text
    assert "+0.400" in text


def test_format_settlement_message_void_shows_na_winner():
    trade = _trade().model_copy(
        update={"status": PaperTradeStatus.VOID, "winning_outcome": None, "pnl": 0.0}
    )
    text = format_settlement_message(trade, None)
    assert "VOID" in text
    assert "Winner: n/a" in text


# --------------------------------------------------------------------------
# notify_paper_trading_events
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "notify_test.db")
    database.init_schema()
    market_repo = MarketRepository(database)
    try:
        market_repo.upsert_market(
            Market(id="m1", question="Will BTC be up?", category="crypto", subcategory="btc_up_down")
        )
    finally:
        market_repo.close()
    return database


@pytest.fixture
def configured_config() -> AppConfig:
    return AppConfig(notifications=NotificationSettings(telegram_bot_token="123:abc", telegram_chat_id="chat1"))


def test_notify_is_a_no_op_when_unconfigured(db):
    config = AppConfig()  # notifications defaults to NotificationSettings(None, None)
    summary = notify_paper_trading_events(config, db, [_trade()], [])
    assert summary == NotifySummary(sent=0, failed=0, configured=False)


@respx.mock
def test_notify_sends_one_message_per_created_and_settled_trade(configured_config, db):
    route = respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    created = [_trade(market_id="m1")]
    settled = [
        _trade(market_id="m1").model_copy(
            update={"status": PaperTradeStatus.WON, "winning_outcome": "Yes", "pnl": 0.4}
        )
    ]
    summary = notify_paper_trading_events(configured_config, db, created, settled)

    assert summary.sent == 2
    assert summary.failed == 0
    assert summary.configured is True
    assert route.call_count == 2


@respx.mock
def test_notify_no_op_with_no_trades_makes_no_http_calls(configured_config, db):
    route = respx.post(TELEGRAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    summary = notify_paper_trading_events(configured_config, db, [], [])
    assert summary == NotifySummary(sent=0, failed=0, configured=True)
    assert route.call_count == 0


@respx.mock
def test_notify_one_failed_send_does_not_block_the_rest(configured_config, db):
    route = respx.post(TELEGRAM_URL).mock(
        side_effect=[
            httpx.Response(500),  # first message fails
            httpx.Response(200, json={"ok": True}),  # second still goes out
        ]
    )
    created = [_trade(market_id="m1"), _trade(market_id="m1")]  # two "created" notifications
    summary = notify_paper_trading_events(configured_config, db, created, [])

    assert summary.sent == 1
    assert summary.failed == 1
    assert route.call_count == 2
