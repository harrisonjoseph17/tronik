"""Manual, opt-in, read-only smoke test against the LIVE Polymarket APIs.

Never touches the real database (writes to data/smoke_test.db instead), and
never places orders. Confirms:
  1. Gamma API reachability + response shape (dumps raw keys of one market
     so the response-shape/field-name assumptions in markets.py/models.py
     can be corrected before Stage 2).
  2. The full discovery pipeline end-to-end against real data.
  3. CLOB API reachability with a single read-only /price request using a
     token id discovered from step 2 - no authentication, no trading.

Run with: python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.loader import load_config  # noqa: E402
from app.polymarket.client import CLOBClient, GammaClient, PolymarketAPIError  # noqa: E402
from app.polymarket.scanner import run_scan_once  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.repositories import MarketRepository  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smoke_test")

SMOKE_DB_PATH = Path("data/smoke_test.db")


async def dump_raw_sample(client: GammaClient) -> bool:
    print("\n=== 1. Raw Gamma /events response shape ===")
    try:
        response = await client.get_events(active=True, closed=False, limit=1)
    except PolymarketAPIError as exc:
        print(f"FAILED to reach Gamma API: {exc}")
        return False

    if isinstance(response, list):
        print(f"Response is a bare JSON list, length={len(response)}")
        event = response[0] if response else {}
    elif isinstance(response, dict):
        print(f"Response is an envelope with keys: {list(response.keys())}")
        data = response.get("data") or []
        event = data[0] if data else {}
    else:
        print(f"UNEXPECTED response type: {type(response)}")
        event = {}

    print(f"Sample event top-level keys: {list(event.keys())}")
    markets = event.get("markets") or []
    if markets:
        print(f"Sample market top-level keys: {list(markets[0].keys())}")
        print(f"Sample market raw JSON:\n{json.dumps(markets[0], indent=2)[:2000]}")
    else:
        print("Sample event had no nested 'markets' - check field name assumptions.")
    return True


async def run_discovery_and_persist():
    print("\n=== 2. Full discovery pipeline (live) ===")
    config = load_config()
    db = Database(SMOKE_DB_PATH)
    db.init_schema()

    try:
        summary = await run_scan_once(config, db)
    except PolymarketAPIError as exc:
        print(f"FAILED during discovery pipeline: {exc}")
        return None
    print(f"ScanSummary: {summary}")

    repo = MarketRepository(db)
    try:
        print("\nSample of persisted markets (up to 5):")
        for market in repo.list_markets(limit=5):
            print(
                f"  [{market.category}/{market.subcategory}] {market.question!r} "
                f"status={market.status} filter_reason={market.filter_reason} "
                f"mid_price={market.mid_price} liquidity={market.liquidity}"
            )

        target_market = None
        for market in repo.list_markets(limit=200):
            if market.clob_token_ids:
                target_market = market
                break
        return target_market
    finally:
        repo.close()


async def check_clob_connectivity(token_id: str) -> None:
    print("\n=== 3. CLOB API connectivity check (read-only, no auth) ===")
    client = CLOBClient()
    try:
        price = await client.get_price(token_id, side="BUY")
        print(f"GET /price?token_id={token_id}&side=BUY -> {price}")
    except PolymarketAPIError as exc:
        print(f"FAILED to reach CLOB API /price: {exc}")
    finally:
        await client.aclose()


async def main() -> None:
    gamma_client = GammaClient()
    try:
        gamma_ok = await dump_raw_sample(gamma_client)
    finally:
        await gamma_client.aclose()

    if not gamma_ok:
        print(
            "\nGamma API is unreachable from this environment (network egress blocked, or "
            "a real outage). Skipping the full discovery pipeline and CLOB check - re-run "
            "this script once network access to gamma-api.polymarket.com / "
            "clob.polymarket.com is available."
        )
        return

    target_market = await run_discovery_and_persist()
    if target_market is None:
        print("\nCLOB connectivity check skipped: discovery pipeline failed or found no market with clob_token_ids.")
    else:
        await check_clob_connectivity(target_market.clob_token_ids[0])

    print(f"\nSmoke test complete. Data written to {SMOKE_DB_PATH} (isolated from the real DB).")


if __name__ == "__main__":
    asyncio.run(main())
