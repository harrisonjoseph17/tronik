"""Manual, opt-in, read-only probe for the resolution/outcome-tracking
design. Confirms the authoritative Gamma fields for market resolution
before app/polymarket/resolutions.py gets implemented, per this session's
established "verify before coding" pattern.

Specifically checks:
- Full, UNTRUNCATED raw JSON for a resolved/closed market. (Stage 1's
  smoke test captured one resolved-market example but truncated it at
  2000 chars before the object's full field list ended.)
- The same for a currently-active market, for comparison.
- A grep across all keys of both for anything resolution/outcome/winner/
  payout/settlement/status-related, to catch a field never previously
  observed.
- Confirms GammaClient.get_markets(slug=...) actually returns full data for
  a market outside the active&closed=false scan window - markets.py's
  fetch_active_events explicitly excludes closed markets, so this direct
  per-market lookup path is what resolutions.py will depend on.

Writes nothing to any database, places no orders, needs no auth.

Run with: python scripts/api_probe_resolutions.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.polymarket.client import GammaClient, PolymarketAPIError  # noqa: E402

RESOLUTION_KEYWORDS = ("resolv", "outcome", "winner", "payout", "settl", "status")


def _dump_full(label: str, value: dict) -> None:
    print(f"\n--- {label} (full, untruncated) ---")
    print(json.dumps(value, indent=2))


def _grep_keys(label: str, market: dict) -> None:
    matches = [k for k in market.keys() if any(kw in k.lower() for kw in RESOLUTION_KEYWORDS)]
    print(f"\n{label} - keys matching resolution-related keywords: {matches}")
    for key in matches:
        print(f"  {key} = {market.get(key)!r}")


def _find_local_slug_from_analyses() -> str | None:
    """Best-effort: pick the oldest market we've already analyzed (likely
    to have resolved by now, especially fast 5-minute BTC markets), and
    look up its slug from the local markets table. None if no local DB."""
    db_path = Path("data/polymarket.db")
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT m.slug FROM markets m
            JOIN analyses a ON a.market_id = m.market_id
            WHERE m.slug IS NOT NULL
            ORDER BY a.computed_at ASC
            LIMIT 1
            """
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _find_local_active_slug() -> str | None:
    db_path = Path("data/polymarket.db")
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT slug FROM markets WHERE status = 'active' AND slug IS NOT NULL LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


async def probe_market_by_slug(
    gamma: GammaClient, slug: str, label: str, *, extra_params: dict | None = None
) -> dict | None:
    params = {"slug": slug, **(extra_params or {})}
    print(f"\n=== Fetching {label}: params={params} ===")
    try:
        response = await gamma.get_markets(**params)
    except PolymarketAPIError as exc:
        print(f"FAILED: {exc}")
        return None

    if isinstance(response, list):
        market = response[0] if response else None
    elif isinstance(response, dict):
        data = response.get("data")
        market = data[0] if isinstance(data, list) and data else response
    else:
        market = None

    if market is None:
        print("No market returned for these params.")
        return None

    print(f"Top-level keys ({len(market)}): {list(market.keys())}")
    _dump_full(label, market)
    _grep_keys(label, market)
    return market


async def probe_slug_param_variants(gamma: GammaClient, slug: str) -> dict | None:
    """The bare slug=X lookup came back empty for a market outside the
    active&closed=false window in round 1 - test whether /markets, like
    /events, needs an explicit closed=True/archived=True to see it."""
    print(f"\n=== Testing param variants for a market that returned empty: slug={slug!r} ===")
    variants = [
        {},
        {"closed": True},
        {"archived": True},
        {"closed": True, "archived": True},
    ]
    for extra in variants:
        market = await probe_market_by_slug(gamma, slug, f"variant {extra}", extra_params=extra)
        if market is not None:
            print(f"\n*** SUCCESS with extra params: {extra} ***")
            return market
    print("\nAll variants returned empty for this slug - it may simply no longer exist.")
    return None


async def find_a_resolved_market_via_events(gamma: GammaClient) -> str | None:
    """Fallback if nothing locally-analyzed has resolved yet: ask Gamma
    directly for recently-closed events and pull one market's slug."""
    print("\n=== Falling back: searching Gamma directly for a closed event ===")
    try:
        response = await gamma.get_events(
            closed=True, limit=5, order="closed_time", ascending=False
        )
    except PolymarketAPIError as exc:
        print(f"FAILED: {exc}")
        return None
    events = response if isinstance(response, list) else (response.get("data") or [])
    for event in events:
        for market in event.get("markets") or []:
            slug = market.get("slug")
            if slug:
                return slug
    return None


async def main() -> None:
    gamma = GammaClient()
    try:
        local_slug = _find_local_slug_from_analyses()
        found_market = None
        if local_slug:
            print(f"Using a locally-analyzed market's slug (may or may not have resolved yet): {local_slug!r}")
            found_market = await probe_market_by_slug(gamma, local_slug, "candidate resolved/closed market")
            if found_market is None:
                # Bare slug= came back empty - test whether /markets, like
                # /events, needs closed=True/archived=True explicitly.
                found_market = await probe_slug_param_variants(gamma, local_slug)

        if found_market is None:
            print("\nFalling back to a direct Gamma search for any closed event.")
            fallback_slug = await find_a_resolved_market_via_events(gamma)
            if fallback_slug:
                found_market = await probe_market_by_slug(gamma, fallback_slug, "candidate resolved/closed market (fallback)")
                if found_market is None:
                    found_market = await probe_slug_param_variants(gamma, fallback_slug)

        if found_market is None:
            print("Could not find any candidate resolved market via any method - skipping.")

        active_slug = _find_local_active_slug()
        if active_slug:
            await probe_market_by_slug(gamma, active_slug, "known ACTIVE (unresolved) market")
        else:
            print("No local active market found for comparison - skipping.")
    finally:
        await gamma.aclose()

    print(
        "\nProbe complete. Paste this entire output back so resolutions.py "
        "can be written against confirmed fields, not guessed ones."
    )


if __name__ == "__main__":
    asyncio.run(main())
