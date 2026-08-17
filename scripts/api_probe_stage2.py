"""Manual, opt-in, read-only probe for the Stage 2 API assumptions that
Stage 1's smoke test never exercised: CLOB /book, CLOB /prices-history on
an ACTIVE market, the CLOB batch endpoints, and the Data API.

Writes nothing to any database. Places no orders, needs no auth. Run this
BEFORE Stage 2's CLOB-dependent code (order-book-imbalance feature, CLOB
staleness gate condition) gets implemented, so that code can be written
against confirmed shapes instead of guessed ones.

Run with: python scripts/api_probe_stage2.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.polymarket.client import CLOBClient, GammaClient, PolymarketAPIError  # noqa: E402


def _dump(label: str, value) -> None:
    print(f"\n--- {label} ---")
    try:
        print(json.dumps(value, indent=2)[:1500])
    except TypeError:
        print(repr(value)[:1500])


async def find_active_tradeable_market(gamma: GammaClient) -> tuple[str, str] | None:
    """Return (question, token_id) for one active, order-book-enabled market."""
    response = await gamma.get_events(
        active=True, closed=False, limit=20, order="volume24hr", ascending=False
    )
    events = response if isinstance(response, list) else (response.get("data") or [])
    for event in events:
        for market in event.get("markets") or []:
            if not market.get("enableOrderBook"):
                continue
            token_ids_raw = market.get("clobTokenIds")
            try:
                token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
            except json.JSONDecodeError:
                continue
            if token_ids:
                return market.get("question", "?"), token_ids[0]
    return None


async def probe_book(clob: CLOBClient, token_id: str) -> None:
    print("\n=== A. GET /book (order book depth) ===")
    try:
        book = await clob.get_book(token_id)
        _dump("/book response", book)
        if isinstance(book, dict):
            print(f"Top-level keys: {list(book.keys())}")
            bids = book.get("bids")
            asks = book.get("asks")
            if isinstance(bids, list) and bids:
                print(f"Sample bid entry: {bids[0]!r} (type={type(bids[0])})")
            if isinstance(asks, list) and asks:
                print(f"Sample ask entry: {asks[0]!r} (type={type(asks[0])})")
    except PolymarketAPIError as exc:
        print(f"FAILED: {exc}")


async def probe_prices_history(raw_client: httpx.AsyncClient, token_id: str) -> None:
    print("\n=== B. GET /prices-history on an ACTIVE market ===")
    for interval, fidelity in (("1h", 5), ("1d", 60)):
        params = {"market": token_id, "interval": interval, "fidelity": fidelity}
        try:
            resp = await raw_client.get("https://clob.polymarket.com/prices-history", params=params)
            print(f"\ninterval={interval} fidelity={fidelity} -> HTTP {resp.status_code}")
            if resp.status_code < 400:
                data = resp.json()
                if isinstance(data, dict) and "history" in data:
                    points = data["history"]
                elif isinstance(data, list):
                    points = data
                else:
                    points = None
                    print(f"Unexpected shape, top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                if points is not None:
                    print(f"Point count: {len(points)}")
                    if points:
                        print(f"Sample point: {points[0]!r}")
                        print(f"Last point:   {points[-1]!r}")
            else:
                print(f"Body: {resp.text[:500]}")
        except httpx.HTTPError as exc:
            print(f"FAILED: {exc}")


async def probe_batch_endpoints(raw_client: httpx.AsyncClient, token_ids: list[str]) -> None:
    print("\n=== C. Batch endpoints (POST /prices, /midpoints) ===")
    # Two plausible request-body shapes - try both, report which (if either) works.
    body_variants = {
        "list_of_dicts (BUY side)": [{"token_id": t, "side": "BUY"} for t in token_ids],
        "bare_list_of_token_ids": token_ids,
    }
    for endpoint in ("/prices", "/midpoints"):
        for variant_name, body in body_variants.items():
            try:
                resp = await raw_client.post(f"https://clob.polymarket.com{endpoint}", json=body)
                print(f"\nPOST {endpoint} body={variant_name} -> HTTP {resp.status_code}")
                if resp.status_code < 400:
                    _dump(f"{endpoint} ({variant_name}) response", resp.json())
                else:
                    print(f"Body: {resp.text[:300]}")
            except httpx.HTTPError as exc:
                print(f"POST {endpoint} ({variant_name}) FAILED: {exc}")


async def probe_data_api(raw_client: httpx.AsyncClient, condition_id: str | None) -> None:
    print("\n=== D. Data API (data-api.polymarket.com) ===")
    candidate_paths = [
        ("/trades", {"market": condition_id, "limit": 5} if condition_id else {"limit": 5}),
        ("/activity", {"limit": 5}),
    ]
    for path, params in candidate_paths:
        try:
            resp = await raw_client.get(f"https://data-api.polymarket.com{path}", params=params)
            print(f"\nGET {path} params={params} -> HTTP {resp.status_code}")
            if resp.status_code < 400:
                _dump(f"{path} response", resp.json())
            else:
                print(f"Body: {resp.text[:300]}")
        except httpx.HTTPError as exc:
            print(f"GET {path} FAILED: {exc}")


async def main() -> None:
    gamma = GammaClient()
    clob = CLOBClient()
    raw_client = httpx.AsyncClient(timeout=10.0)
    try:
        print("=== Finding one active, order-book-enabled market via Gamma ===")
        found = await find_active_tradeable_market(gamma)
        if found is None:
            print("No active order-book-enabled market found in the first 20 events - re-run later.")
            return
        question, token_id = found
        print(f"Using market: {question!r}")
        print(f"token_id: {token_id}")

        await probe_book(clob, token_id)
        await probe_prices_history(raw_client, token_id)
        await probe_batch_endpoints(raw_client, [token_id])
        await probe_data_api(raw_client, None)
    finally:
        await gamma.aclose()
        await clob.aclose()
        await raw_client.aclose()

    print("\nProbe complete. Paste this entire output back so Stage 2's CLOB-dependent code can be written against confirmed shapes.")


if __name__ == "__main__":
    asyncio.run(main())
