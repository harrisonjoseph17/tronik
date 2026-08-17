"""Manual, opt-in, read-only probe for the Stage 2 API assumptions that
Stage 1's smoke test never exercised: CLOB /book, CLOB /prices-history on
an ACTIVE market, the CLOB batch endpoints, and the Data API.

Writes nothing to any database. Places no orders, needs no auth. Run this
BEFORE Stage 2's CLOB-dependent code (order-book-imbalance feature, CLOB
staleness gate condition) gets implemented, so that code can be written
against confirmed shapes instead of guessed ones.

Round 1 of this probe picked a market via Gamma's `enableOrderBook` flag
and got a 404 from /book - Gamma flagging a market as order-book-enabled
does not mean that specific token has live orders right now (e.g. a paused
esports sub-market between games). Round 2 instead picks a token from the
Data API's /trades feed, which shows what's *actually* trading this
instant (e.g. 5-minute BNB/ETH up-down markets) - a far more reliable
signal that /book will return something real.

Run with: python scripts/api_probe_stage2.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.polymarket.client import CLOBClient, PolymarketAPIError  # noqa: E402


def _dump(label: str, value) -> None:
    print(f"\n--- {label} ---")
    try:
        print(json.dumps(value, indent=2)[:1500])
    except TypeError:
        print(repr(value)[:1500])


async def find_actively_trading_token(raw_client: httpx.AsyncClient) -> tuple[str, str, str] | None:
    """Return (title, token_id, condition_id) for a market with a real,
    recent trade per the Data API's /trades feed."""
    resp = await raw_client.get("https://data-api.polymarket.com/trades", params={"limit": 20})
    resp.raise_for_status()
    trades = resp.json()
    if not trades:
        return None
    trade = trades[0]
    return trade.get("title", "?"), trade.get("asset"), trade.get("conditionId")


async def probe_trades_filtered_by_market(raw_client: httpx.AsyncClient, condition_id: str) -> None:
    print("\n=== E. GET /trades filtered by market (confirming the query param name) ===")
    for param_name in ("market", "asset", "conditionId"):
        try:
            resp = await raw_client.get(
                "https://data-api.polymarket.com/trades",
                params={param_name: condition_id, "limit": 5},
            )
            print(f"\nparam={param_name} -> HTTP {resp.status_code}")
            if resp.status_code < 400:
                data = resp.json()
                if isinstance(data, list) and data:
                    all_match = all(t.get("conditionId") == condition_id for t in data)
                    print(f"Returned {len(data)} trades, all match requested market: {all_match}")
                else:
                    print(f"Returned {len(data) if isinstance(data, list) else data!r} trades")
            else:
                print(f"Body: {resp.text[:300]}")
        except httpx.HTTPError as exc:
            print(f"param={param_name} FAILED: {exc}")


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
    clob = CLOBClient()
    raw_client = httpx.AsyncClient(timeout=10.0)
    try:
        print("=== Finding a token that is ACTUALLY trading right now (via Data API /trades) ===")
        found = await find_actively_trading_token(raw_client)
        if found is None:
            print("No recent trades found - re-run in a moment.")
            return
        title, token_id, condition_id = found
        print(f"Using market: {title!r}")
        print(f"token_id: {token_id}")
        print(f"condition_id: {condition_id}")

        await probe_book(clob, token_id)
        await probe_prices_history(raw_client, token_id)
        await probe_batch_endpoints(raw_client, [token_id])
        await probe_data_api(raw_client, condition_id)
        if condition_id:
            await probe_trades_filtered_by_market(raw_client, condition_id)
    finally:
        await clob.aclose()
        await raw_client.aclose()

    print("\nProbe complete. Paste this entire output back so Stage 2's CLOB-dependent code can be written against confirmed shapes.")


if __name__ == "__main__":
    asyncio.run(main())
