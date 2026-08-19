"""Tests for determine_outcome() against fixture data modeled on the real
confirmed Gamma responses from scripts/api_probe_resolutions.py (see
app/polymarket/resolutions.py's module docstring for the full findings)."""

from datetime import datetime

import pytest

from app.polymarket.client import PolymarketAPIError
from app.polymarket.resolutions import determine_outcome, fetch_market_state
from app.storage.resolution_models import ResolutionStatus

# Modeled on a real confirmed unresolved market ("Game 1: Any Player Quadra
# Kill?") - note umaResolutionStatus (singular) is genuinely ABSENT as a
# key, not just empty, on a real unresolved market.
UNRESOLVED_MARKET = {
    "id": "3563932",
    "question": "Game 1: Any Player Quadra Kill?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.5", "0.5"]',
    "active": True,
    "closed": False,
    "umaResolutionStatuses": "[]",
}

# Modeled on a real confirmed resolved market ("Will Elon Musk post 140-159
# tweets from August 11 to August 18, 2026?" - resolved "No").
RESOLVED_MARKET = {
    "id": "3414461",
    "question": "Will Elon Musk post 140-159 tweets from August 11 to August 18, 2026?",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0", "1"]',
    "active": True,
    "closed": True,
    "closedTime": "2026-08-18 16:57:10+00",
    "umaEndDate": "2026-08-18T16:57:10Z",
    "umaResolutionStatus": "resolved",
    # Confirmed unreliable - tracks raw UMA oracle stages, not Gamma's
    # final determination. Must never be used as the resolution signal.
    "umaResolutionStatuses": '["proposed"]',
}

EXPECTED_RESOLVED_AT = datetime.fromisoformat("2026-08-18T16:57:10+00:00")


def test_unresolved_market_returns_unresolved_status():
    result = determine_outcome(UNRESOLVED_MARKET)
    assert result.status == ResolutionStatus.UNRESOLVED
    assert result.winning_outcome_index is None
    assert result.winning_outcome is None
    assert result.resolved_at is None


def test_resolved_market_extracts_correct_winning_outcome():
    result = determine_outcome(RESOLVED_MARKET)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.winning_outcome_index == 1
    assert result.winning_outcome == "No"
    assert result.resolved_at == EXPECTED_RESOLVED_AT


def test_plural_uma_resolution_statuses_field_is_never_used_as_the_signal():
    """A market where the plural array claims resolution but the singular
    field (the confirmed-authoritative one) is absent must NOT be treated
    as resolved."""
    market = dict(UNRESOLVED_MARKET, umaResolutionStatuses='["resolved"]')
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.UNRESOLVED


def test_closed_without_uma_resolution_status_is_not_resolved():
    """closed=true alone (e.g. mid-dispute) must not be treated as resolved."""
    market = dict(RESOLVED_MARKET)
    del market["umaResolutionStatus"]
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.UNRESOLVED


def test_uma_resolution_status_without_closed_is_not_resolved():
    market = dict(RESOLVED_MARKET, closed=False)
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.UNRESOLVED


def test_fifty_fifty_settlement_is_invalid_not_guessed():
    market = dict(RESOLVED_MARKET, outcomePrices='["0.5", "0.5"]')
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID
    assert result.winning_outcome_index is None
    assert result.winning_outcome is None
    assert result.resolved_at == EXPECTED_RESOLVED_AT  # still know when Gamma says it resolved


def test_missing_outcome_prices_is_invalid_not_fatal():
    market = dict(RESOLVED_MARKET)
    del market["outcomePrices"]
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


def test_malformed_outcome_prices_is_invalid_not_fatal():
    market = dict(RESOLVED_MARKET, outcomePrices="not valid json")
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


def test_mismatched_outcomes_and_prices_length_is_invalid():
    market = dict(RESOLVED_MARKET, outcomes='["Yes", "No", "Maybe"]')
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


def test_multiple_winning_prices_is_invalid_not_guessed():
    market = dict(RESOLVED_MARKET, outcomePrices='["1", "1"]')
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


def test_zero_winning_prices_is_invalid_not_guessed():
    market = dict(RESOLVED_MARKET, outcomePrices='["0.3", "0.3"]')
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


def test_resolved_at_falls_back_to_closed_time_when_uma_end_date_missing():
    market = dict(RESOLVED_MARKET)
    del market["umaEndDate"]
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.RESOLVED
    assert result.resolved_at == EXPECTED_RESOLVED_AT


def test_empty_outcomes_is_invalid_not_fatal():
    market = dict(RESOLVED_MARKET, outcomes="[]", outcomePrices="[]")
    result = determine_outcome(market)
    assert result.status == ResolutionStatus.INVALID


# ---- fetch_market_state: confirmed two-attempt behavior ----


class _StubGamma:
    """Records calls, returns a canned response per call number - confirms
    fetch_market_state's confirmed-live two-attempt sequence (bare slug=
    finds active markets; closed=True finds closed/resolved ones)."""

    def __init__(self, responses: list):
        self._responses = responses
        self.calls: list[dict] = []

    async def get_markets(self, **params):
        self.calls.append(params)
        return self._responses[len(self.calls) - 1]


@pytest.mark.asyncio
async def test_fetch_market_state_finds_active_market_on_first_bare_call():
    gamma = _StubGamma([[UNRESOLVED_MARKET]])
    market = await fetch_market_state(gamma, "some-slug")
    assert market == UNRESOLVED_MARKET
    assert gamma.calls == [{"slug": "some-slug"}]  # never needed the closed=True retry


@pytest.mark.asyncio
async def test_fetch_market_state_retries_with_closed_true_when_bare_call_is_empty():
    gamma = _StubGamma([[], [RESOLVED_MARKET]])
    market = await fetch_market_state(gamma, "some-slug")
    assert market == RESOLVED_MARKET
    assert gamma.calls == [{"slug": "some-slug"}, {"slug": "some-slug", "closed": True}]


@pytest.mark.asyncio
async def test_fetch_market_state_returns_none_when_both_attempts_empty():
    gamma = _StubGamma([[], []])
    market = await fetch_market_state(gamma, "some-slug")
    assert market is None


@pytest.mark.asyncio
async def test_fetch_market_state_handles_envelope_response_shape():
    gamma = _StubGamma([{"data": [UNRESOLVED_MARKET], "has_more": False}])
    market = await fetch_market_state(gamma, "some-slug")
    assert market == UNRESOLVED_MARKET


@pytest.mark.asyncio
async def test_fetch_market_state_returns_none_on_api_error():
    class _ErrorGamma:
        async def get_markets(self, **params):
            raise PolymarketAPIError("boom", status_code=500)

    market = await fetch_market_state(_ErrorGamma(), "some-slug")
    assert market is None
