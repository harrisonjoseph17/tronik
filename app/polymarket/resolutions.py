"""Market resolution/outcome tracking.

Stage 1's discovery scanner only ever queries Gamma for active, non-closed
events (fetch_active_events in markets.py), so a market that has since
resolved permanently drops out of every future scan. This module is the
only place that looks a specific, already-known market up again to see
whether it has resolved.

Confirmed live before this was written (see scripts/api_probe_resolutions.py,
two rounds - do not change this logic without re-verifying against real
data first):

- GET /markets?slug=X with a bare slug param only returns a market that's
  still active; a closed/resolved market needs slug=X&closed=true
  explicitly - the same pattern Stage 1 already found applies to /events.
- The authoritative "is this actually resolved" signal is the singular
  `umaResolutionStatus == "resolved"` field, confirmed present (with
  exactly that value) on a real resolved market and absent as a key
  entirely on an unresolved one. The plural `umaResolutionStatuses` array
  is NOT reliable for this - it showed `["proposed"]` even on the
  confirmed-resolved market, since it tracks raw UMA oracle stages, not
  Gamma's final determination.
- The winning outcome is read from outcomePrices/outcomes ONLY once the
  above confirms resolution - this reads the final on-chain CTF settlement
  record (a resolved conditional token is redeemable 1:1 for the winning
  outcome, 0 for the rest), not a behavioral inference from price movement.
  If the settled prices don't cleanly single out one outcome (e.g. a 50-50
  "invalid" resolution for a canceled/postponed market - several observed
  market descriptions specify this), the result is INVALID, never a
  guessed winner.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config.loader import AppConfig
from app.polymarket.client import GammaClient, PolymarketAPIError
from app.storage.database import Database
from app.storage.repositories import MarketRepository, ResolutionRepository
from app.storage.resolution_models import Resolution, ResolutionStatus

logger = logging.getLogger(__name__)

WINNING_PRICE_THRESHOLD = 0.99


def _extract_first_market(response: Any) -> dict | None:
    if isinstance(response, list):
        return response[0] if response else None
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            return data[0] if data else None
        if "id" in response:
            return response
    return None


async def fetch_market_state(gamma: GammaClient, slug: str) -> dict | None:
    """Bare slug= finds a still-active market; a closed/resolved one needs
    closed=True explicitly (confirmed live - see module docstring). Tries
    both so this works regardless of the market's current state."""
    try:
        response = await gamma.get_markets(slug=slug)
    except PolymarketAPIError as exc:
        logger.warning("failed to fetch market state for slug=%s: %s", slug, exc)
        return None
    market = _extract_first_market(response)
    if market is not None:
        return market

    try:
        response = await gamma.get_markets(slug=slug, closed=True)
    except PolymarketAPIError as exc:
        logger.warning("failed to fetch closed market state for slug=%s: %s", slug, exc)
        return None
    return _extract_first_market(response)


def _parse_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_floats(values: list) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            result.append(float("nan"))
    return result


def _parse_gamma_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class OutcomeDetermination:
    status: ResolutionStatus
    resolved_at: datetime | None
    winning_outcome_index: int | None
    winning_outcome: str | None


def determine_outcome(raw: dict) -> OutcomeDetermination:
    """Pure - no I/O. raw is one market dict as returned by Gamma."""
    is_resolved = bool(raw.get("closed")) and raw.get("umaResolutionStatus") == "resolved"
    if not is_resolved:
        return OutcomeDetermination(
            status=ResolutionStatus.UNRESOLVED,
            resolved_at=None,
            winning_outcome_index=None,
            winning_outcome=None,
        )

    resolved_at = _parse_gamma_datetime(raw.get("umaEndDate")) or _parse_gamma_datetime(
        raw.get("closedTime")
    )

    outcomes = _parse_json_list(raw.get("outcomes"))
    prices = _safe_floats(_parse_json_list(raw.get("outcomePrices")))

    winners = [i for i, p in enumerate(prices) if p >= WINNING_PRICE_THRESHOLD]
    if len(winners) != 1 or len(prices) != len(outcomes) or not outcomes:
        # Confirmed resolved by Gamma, but the settlement record itself is
        # inconclusive - never guess a winner.
        return OutcomeDetermination(
            status=ResolutionStatus.INVALID,
            resolved_at=resolved_at,
            winning_outcome_index=None,
            winning_outcome=None,
        )

    index = winners[0]
    return OutcomeDetermination(
        status=ResolutionStatus.RESOLVED,
        resolved_at=resolved_at,
        winning_outcome_index=index,
        winning_outcome=outcomes[index],
    )


@dataclass
class ResolutionCheckSummary:
    checked: int = 0
    newly_resolved: int = 0
    still_unresolved: int = 0
    invalid: int = 0
    not_found: int = 0


async def check_pending_resolutions(
    config: AppConfig, db: Database, *, gamma: GammaClient | None = None, limit: int = 500
) -> ResolutionCheckSummary:
    """Only ever looks at markets with at least one analyses row that
    aren't already marked resolved (list_market_ids_pending_check excludes
    resolved markets entirely) - so "newly_resolved" here is exactly that:
    a market that just transitioned this run, never a re-confirmation."""
    summary = ResolutionCheckSummary()
    now = datetime.now(timezone.utc)

    owns_gamma = gamma is None
    if gamma is None:
        gamma = GammaClient(
            timeout=config.filters.request_timeout_seconds,
            max_retries=config.filters.max_retries,
            rate_limit_per_10s=config.filters.gamma_rate_limit_per_10s,
        )

    market_repo = MarketRepository(db)
    resolution_repo = ResolutionRepository(db)
    try:
        market_ids = resolution_repo.list_market_ids_pending_check(limit=limit)
        for market_id in market_ids:
            market = market_repo.get_market(market_id)
            if market is None or not market.slug:
                summary.not_found += 1
                continue

            raw = await fetch_market_state(gamma, market.slug)
            summary.checked += 1
            if raw is None:
                summary.not_found += 1
                continue

            outcome = determine_outcome(raw)
            resolution_repo.upsert(
                Resolution(
                    market_id=market_id,
                    resolution_status=outcome.status,
                    resolved_at=outcome.resolved_at,
                    winning_outcome_index=outcome.winning_outcome_index,
                    winning_outcome=outcome.winning_outcome,
                    raw_resolution_json=json.dumps(raw),
                    checked_at=now,
                )
            )

            if outcome.status == ResolutionStatus.RESOLVED:
                summary.newly_resolved += 1
            elif outcome.status == ResolutionStatus.INVALID:
                summary.invalid += 1
            else:
                summary.still_unresolved += 1
    finally:
        market_repo.close()
        resolution_repo.close()
        if owns_gamma:
            await gamma.aclose()

    logger.info(
        "resolution check complete: checked=%d newly_resolved=%d still_unresolved=%d "
        "invalid=%d not_found=%d",
        summary.checked,
        summary.newly_resolved,
        summary.still_unresolved,
        summary.invalid,
        summary.not_found,
    )
    return summary
