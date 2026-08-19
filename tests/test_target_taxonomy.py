"""Categorization tests against the REAL config/markets.yaml - i.e. the
actual four-family target taxonomy (politics/elon_musk, politics/white_house,
crypto/btc_up_down, sports/basketball), not an abstract illustrative rule
set. See test_categorize.py for generic mechanism tests.
"""

from pathlib import Path

from app.polymarket.categorize import (
    OTHER_CATEGORY,
    UNCATEGORIZED_SUBCATEGORY,
    categorize_market,
    load_category_rules,
)

RULES = load_category_rules(Path("config/markets.yaml"))


def _categorize(question: str, event_title: str | None = None, tags=None):
    return categorize_market(question, event_title, tags, RULES)


# ---- politics/elon_musk ----


def test_elon_musk_tweet_market_matches():
    assert _categorize("Will Elon Musk tweet 100+ times this week?") == ("politics", "elon_musk")


def test_elon_musk_post_market_matches_via_musk_post():
    assert _categorize("Will Musk post about Tesla before Friday?") == ("politics", "elon_musk")


def test_elon_musk_matches_via_tags():
    assert _categorize("How many times?", "Elon Tweet Count", ["Elon Musk"]) == (
        "politics",
        "elon_musk",
    )


# ---- politics/white_house ----


def test_white_house_tweet_market_matches():
    assert _categorize("White House tweet about tariffs?") == ("politics", "white_house")


def test_white_house_general_market_matches():
    assert _categorize("Will the White House confirm the meeting?") == ("politics", "white_house")


# ---- crypto/btc_up_down ----


def test_bitcoin_up_or_down_matches():
    assert _categorize("Bitcoin Up or Down - August 17, 6:30PM-6:35PM ET") == (
        "crypto",
        "btc_up_down",
    )


def test_will_bitcoin_be_up_matches():
    assert _categorize("Will Bitcoin be up today?") == ("crypto", "btc_up_down")


def test_btc_above_matches():
    assert _categorize("Will BTC be above $120,000 on Friday?") == ("crypto", "btc_up_down")


# ---- crypto: other coins must NOT match btc_up_down ----


def test_ethereum_up_or_down_does_not_match_btc():
    result = _categorize("Ethereum Up or Down - August 17, 6:30PM-6:35PM ET")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_solana_price_market_does_not_match_btc():
    result = _categorize("Will Solana be above $200 by Friday?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_generic_crypto_market_does_not_match_btc():
    result = _categorize("Will the total crypto market cap exceed $3T?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


# ---- sports/basketball ----


def test_nba_market_matches_basketball():
    assert _categorize("Lakers vs Celtics - Moneyline", "NBA") == ("sports", "basketball")


def test_basketball_keyword_matches():
    assert _categorize("Who wins the college basketball championship?") == (
        "sports",
        "basketball",
    )


def test_wnba_matches_basketball():
    assert _categorize("WNBA Finals: who wins Game 1?") == ("sports", "basketball")


def test_euroleague_matches_basketball():
    assert _categorize("EuroLeague: Real Madrid vs Barcelona - Winner") == (
        "sports",
        "basketball",
    )


# ---- sports: other sports must NOT match basketball ----


def test_football_market_does_not_match_basketball():
    result = _categorize("Arsenal vs Chelsea", "Premier League matchday")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_soccer_world_cup_does_not_match_basketball():
    result = _categorize("Who wins the World Cup?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_tennis_market_does_not_match_basketball():
    result = _categorize("Will Djokovic win the Australian Open?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_baseball_market_does_not_match_basketball():
    result = _categorize("Yankees vs Red Sox - Moneyline", "MLB")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_hockey_market_does_not_match_basketball():
    result = _categorize("Will the Oilers win the Stanley Cup?", "NHL")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


# ---- politics that must NOT be categorized ----


def test_zelenskyy_tweet_market_is_not_categorized():
    result = _categorize("Will Zelenskyy tweet before Friday?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_unrelated_politics_market_is_not_categorized():
    result = _categorize("Who wins the 2028 presidential election?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def test_generic_congress_market_is_not_categorized():
    result = _categorize("Will the Senate pass the bill this month?")
    assert result == (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)
