from datetime import datetime, timedelta, timezone

import pytest

from app.polymarket.categorize import CategoryRule
from app.polymarket.markets import (
    FILTER_REASON_CLOSED,
    FILTER_REASON_DUPLICATE,
    FILTER_REASON_LOW_LIQUIDITY,
    FILTER_REASON_NEAR_RESOLUTION,
    FILTER_REASON_WIDE_SPREAD,
    categorize_and_normalize,
    extract_markets_from_events,
    fetch_active_events,
    filter_markets,
    flag_duplicates,
)
from app.storage.models import Market


class _StubGammaClient:
    """Fake GammaClient returning a canned sequence of /events responses."""

    def __init__(self, responses: list):
        self._responses = responses
        self.calls: list[dict] = []

    async def get_events(self, **params):
        self.calls.append(params)
        return self._responses[len(self.calls) - 1]


def _market(id_, **overrides):
    base = {"id": id_, "question": "Q?", "markets": []}
    base.update(overrides)
    return base


# ---- pagination ----


@pytest.mark.asyncio
async def test_pagination_bare_list_stops_when_page_smaller_than_page_size():
    client = _StubGammaClient([[{"id": "e1"}, {"id": "e2"}]])
    events = [e async for e in fetch_active_events(client, page_size=5, max_pages=10)]
    assert [e["id"] for e in events] == ["e1", "e2"]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_pagination_envelope_stops_on_has_more_false():
    client = _StubGammaClient(
        [
            {"data": [{"id": "e1"}], "has_more": True},
            {"data": [{"id": "e2"}], "has_more": False},
        ]
    )
    events = [e async for e in fetch_active_events(client, page_size=1, max_pages=10)]
    assert [e["id"] for e in events] == ["e1", "e2"]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_pagination_stops_at_max_pages_safety_cap():
    full_page = [{"id": f"e{i}"} for i in range(5)]
    client = _StubGammaClient([full_page] * 10)
    events = [e async for e in fetch_active_events(client, page_size=5, max_pages=3)]
    assert len(events) == 15
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_pagination_empty_first_page_stops_immediately():
    client = _StubGammaClient([[]])
    events = [e async for e in fetch_active_events(client, page_size=5, max_pages=10)]
    assert events == []
    assert len(client.calls) == 1


# ---- extraction ----


def test_extract_markets_from_events_flattens_and_attaches_context():
    events = [
        {
            "id": "e1",
            "title": "Event One",
            "tags": [{"label": "Crypto"}],
            "markets": [{"id": "m1", "question": "Will BTC pump?"}],
        }
    ]
    markets = extract_markets_from_events(events)
    assert len(markets) == 1
    assert markets[0]["event_id"] == "e1"
    assert markets[0]["_event_title"] == "Event One"
    assert markets[0]["_event_tags"] == ["Crypto"]


def test_extract_markets_handles_missing_markets_key():
    events = [{"id": "e1", "title": "No markets field"}]
    assert extract_markets_from_events(events) == []


def test_extract_markets_handles_non_list_markets_key():
    events = [{"id": "e1", "markets": "not-a-list"}]
    assert extract_markets_from_events(events) == []


def test_categorize_and_normalize_uses_event_context():
    rules = [CategoryRule("crypto", "btc_updown", ("will bitcoin",))]
    raw_markets = [
        {"id": "m1", "question": "Will Bitcoin be up?", "_event_title": None, "_event_tags": []}
    ]
    markets = categorize_and_normalize(raw_markets, rules)
    assert markets[0].category == "crypto"
    assert markets[0].subcategory == "btc_updown"


# ---- filters ----


def _built_market(**overrides) -> Market:
    defaults = dict(
        id="m1",
        question="Q",
        status="active",
        active=True,
        liquidity=5000.0,
        best_bid=0.5,
        best_ask=0.51,
    )
    defaults.update(overrides)
    market = Market(**defaults)
    market.spread = market.best_ask - market.best_bid if market.best_bid and market.best_ask else None
    return market


def test_filter_excludes_closed_markets():
    market = _built_market(status="closed", active=False)
    filtered, counts = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert filtered[0].filter_reason == FILTER_REASON_CLOSED
    assert counts[FILTER_REASON_CLOSED] == 1


def test_filter_excludes_low_liquidity():
    market = _built_market(liquidity=50.0)
    filtered, counts = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert filtered[0].filter_reason == FILTER_REASON_LOW_LIQUIDITY
    assert counts[FILTER_REASON_LOW_LIQUIDITY] == 1


def test_filter_excludes_wide_spread():
    market = _built_market(best_bid=0.30, best_ask=0.60)
    filtered, counts = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert filtered[0].filter_reason == FILTER_REASON_WIDE_SPREAD
    assert counts[FILTER_REASON_WIDE_SPREAD] == 1


def test_filter_excludes_near_resolution():
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    market = _built_market(end_date=soon)
    filtered, counts = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert filtered[0].filter_reason == FILTER_REASON_NEAR_RESOLUTION
    assert counts[FILTER_REASON_NEAR_RESOLUTION] == 1


def test_filter_keeps_healthy_market_included():
    far = datetime.now(timezone.utc) + timedelta(days=5)
    market = _built_market(end_date=far)
    filtered, counts = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert filtered[0].filter_reason is None
    assert counts == {}


def test_filter_never_removes_markets_from_the_list():
    market = _built_market(status="closed", active=False)
    filtered, _ = filter_markets(
        [market], min_liquidity=1000, max_spread=0.1, min_hours_to_resolution=2
    )
    assert len(filtered) == 1


# ---- duplicates ----


def test_flag_duplicates_flags_same_event_same_tokens():
    m1 = Market(id="m1", question="Q", event_id="e1", clob_token_ids=["t1", "t2"], volume=100.0)
    m2 = Market(id="m2", question="Q dup", event_id="e1", clob_token_ids=["t1", "t2"], volume=50.0)
    result = flag_duplicates([m1, m2])
    assert len(result) == 2  # not removed
    lower_volume = next(m for m in result if m.id == "m2")
    assert lower_volume.is_duplicate is True
    assert lower_volume.duplicate_of == "m1"
    assert lower_volume.filter_reason == FILTER_REASON_DUPLICATE
    higher_volume = next(m for m in result if m.id == "m1")
    assert higher_volume.is_duplicate is False


def test_flag_duplicates_does_not_flag_distinct_markets():
    m1 = Market(id="m1", question="Q1", event_id="e1", clob_token_ids=["t1"])
    m2 = Market(id="m2", question="Q2", event_id="e2", clob_token_ids=["t2"])
    result = flag_duplicates([m1, m2])
    assert all(not m.is_duplicate for m in result)


def test_flag_duplicates_hard_dedups_same_id():
    m1 = Market(id="m1", question="Q")
    m1_again = Market(id="m1", question="Q")
    result = flag_duplicates([m1, m1_again])
    assert len(result) == 1


def test_flag_duplicates_does_not_overwrite_existing_filter_reason():
    m1 = Market(id="m1", question="Q", event_id="e1", clob_token_ids=["t1"], volume=100.0)
    m2 = Market(
        id="m2",
        question="Q dup",
        event_id="e1",
        clob_token_ids=["t1"],
        volume=50.0,
        filter_reason=FILTER_REASON_CLOSED,
    )
    result = flag_duplicates([m1, m2])
    dup = next(m for m in result if m.id == "m2")
    assert dup.filter_reason == FILTER_REASON_CLOSED
    assert dup.is_duplicate is True
