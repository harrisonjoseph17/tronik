from app.polymarket.categorize import OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY
from app.storage.models import Market


def _raw_market(**overrides):
    base = {
        "id": "123",
        "event_id": "e1",
        "question": "Will BTC be up today?",
        "slug": "btc-up-today",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.62", "0.38"]',
        "clobTokenIds": '["111", "222"]',
        "volume": "5000.5",
        "volume24hr": "1200",
        "liquidity": "3000",
        "bestBid": "0.61",
        "bestAsk": "0.63",
        "active": True,
        "closed": False,
        "archived": False,
        "enableOrderBook": True,
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T23:59:59Z",
    }
    base.update(overrides)
    return base


def test_parses_stringified_price_and_token_fields():
    market = Market.from_gamma(_raw_market())
    assert market.outcomes == ["Yes", "No"]
    assert market.outcome_prices == [0.62, 0.38]
    assert market.clob_token_ids == ["111", "222"]


def test_missing_optional_fields_do_not_raise():
    raw = _raw_market()
    for key in ("bestBid", "bestAsk", "volume", "liquidity", "slug"):
        raw.pop(key)
    market = Market.from_gamma(raw)
    assert market.best_bid is None
    assert market.best_ask is None
    assert market.volume is None
    assert market.liquidity is None
    assert market.slug is None


def test_malformed_json_degrades_to_empty_list():
    market = Market.from_gamma(_raw_market(outcomePrices="not valid json"))
    assert market.outcome_prices == []


def test_non_list_json_degrades_to_empty_list():
    market = Market.from_gamma(_raw_market(outcomePrices='{"a": 1}'))
    assert market.outcome_prices == []


def test_mid_price_from_best_bid_ask():
    market = Market.from_gamma(_raw_market(bestBid="0.40", bestAsk="0.60"))
    assert market.mid_price == 0.5
    assert market.spread == 0.2


def test_mid_price_falls_back_to_outcome_prices_when_no_book():
    raw = _raw_market(outcomePrices='["0.71", "0.29"]')
    raw.pop("bestBid")
    raw.pop("bestAsk")
    market = Market.from_gamma(raw)
    assert market.mid_price == 0.71
    assert market.spread is None


def test_mid_price_none_when_no_data_at_all():
    raw = _raw_market(outcomePrices="[]")
    raw.pop("bestBid")
    raw.pop("bestAsk")
    market = Market.from_gamma(raw)
    assert market.mid_price is None


def test_status_derivation_active():
    market = Market.from_gamma(_raw_market(active=True, closed=False, archived=False))
    assert market.status == "active"


def test_status_derivation_closed():
    market = Market.from_gamma(_raw_market(active=False, closed=True, archived=False))
    assert market.status == "closed"


def test_status_derivation_archived():
    market = Market.from_gamma(_raw_market(active=False, closed=True, archived=True))
    assert market.status == "archived"


def test_status_derivation_invalid_when_nothing_set():
    market = Market.from_gamma(_raw_market(active=False, closed=False, archived=False))
    assert market.status == "invalid"


def test_dates_parsed_to_utc():
    market = Market.from_gamma(_raw_market())
    assert market.start_date is not None
    assert market.end_date is not None
    assert market.start_date.year == 2026


def test_malformed_date_degrades_to_none():
    market = Market.from_gamma(_raw_market(startDate="not-a-date"))
    assert market.start_date is None


def test_default_category_is_other_uncategorized_when_not_provided():
    market = Market.from_gamma(_raw_market())
    assert market.category == OTHER_CATEGORY
    assert market.subcategory == UNCATEGORIZED_SUBCATEGORY


def test_category_passed_through_when_provided():
    market = Market.from_gamma(_raw_market(), category="crypto", subcategory="btc_updown")
    assert market.category == "crypto"
    assert market.subcategory == "btc_updown"
