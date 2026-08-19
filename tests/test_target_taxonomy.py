"""Categorization tests against the REAL config/markets.yaml - i.e. the
actual four-family target taxonomy (politics/elon_musk_tweets,
politics/white_house_tweets, crypto/btc_up_down, sports/basketball), not an
abstract illustrative rule set. See test_categorize.py for generic
mechanism tests.
"""

from pathlib import Path

from app.config.loader import load_config
from app.polymarket.categorize import (
    OTHER_CATEGORY,
    UNCATEGORIZED_SUBCATEGORY,
    categorize_market,
    load_category_rules,
)

RULES = load_category_rules(Path("config/markets.yaml"))
NOT_CATEGORIZED = (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY)


def _categorize(question: str, event_title: str | None = None, tags=None):
    return categorize_market(question, event_title, tags, RULES)


# ---- politics/elon_musk_tweets: positive ----


def test_elon_musk_tweet_market_matches():
    assert _categorize("Will Elon Musk tweet 100+ times this week?") == (
        "politics",
        "elon_musk_tweets",
    )


def test_how_many_times_will_elon_musk_tweet_matches():
    assert _categorize("How many times will Elon Musk tweet?") == ("politics", "elon_musk_tweets")


def test_how_many_posts_will_elon_musk_make_matches():
    assert _categorize("How many posts will Elon Musk make this week?") == (
        "politics",
        "elon_musk_tweets",
    )


def test_elonmusk_handle_matches():
    assert _categorize("@elonmusk tweets this week?") == ("politics", "elon_musk_tweets")


def test_elon_musk_post_market_matches_via_musk_post():
    assert _categorize("Will Musk post about Tesla before Friday?") == (
        "politics",
        "elon_musk_tweets",
    )


def test_elon_musk_matches_via_tags():
    assert _categorize("How many times?", "Elon Tweet Count", ["Elon Musk"]) == (
        "politics",
        "elon_musk_tweets",
    )


# ---- politics/elon_musk_tweets: negative (generic Elon Musk news) ----


def test_elon_musk_buy_market_is_not_categorized():
    assert _categorize("Will Elon Musk buy TikTok?") == NOT_CATEGORIZED


def test_elon_musk_ceo_market_is_not_categorized():
    assert _categorize("Will Elon Musk become CEO of OpenAI?") == NOT_CATEGORIZED


def test_elon_musk_launch_market_is_not_categorized():
    assert _categorize("Will Elon Musk launch a new Tesla model this year?") == NOT_CATEGORIZED


def test_elon_musk_legal_market_is_not_categorized():
    assert _categorize("Will Elon Musk settle the SEC lawsuit by year end?") == NOT_CATEGORIZED


# ---- politics/white_house_tweets: positive ----


def test_white_house_tweet_market_matches():
    assert _categorize("White House tweet about tariffs?") == ("politics", "white_house_tweets")


def test_how_many_times_will_white_house_tweet_matches():
    assert _categorize("How many times will the White House tweet today?") == (
        "politics",
        "white_house_tweets",
    )


def test_whitehouse_handle_post_x_times_matches():
    assert _categorize("Will @WhiteHouse post 10+ times today?") == (
        "politics",
        "white_house_tweets",
    )


def test_how_many_posts_will_white_house_make_matches():
    assert _categorize("How many posts will the White House make this week?") == (
        "politics",
        "white_house_tweets",
    )


def test_potus_tweet_matches():
    assert _categorize("Will POTUS tweet about the economy?") == (
        "politics",
        "white_house_tweets",
    )


# ---- politics/white_house_tweets: negative (generic White House news) ----


def test_white_house_general_market_is_not_categorized():
    assert _categorize("Will the White House confirm the meeting?") == NOT_CATEGORIZED


def test_white_house_press_secretary_market_is_not_categorized():
    assert _categorize("Who will be the next White House press secretary?") == NOT_CATEGORIZED


def test_white_house_appoint_market_is_not_categorized():
    assert _categorize("Will the White House appoint a new Chief of Staff?") == NOT_CATEGORIZED


def test_white_house_personnel_market_is_not_categorized():
    assert _categorize("White House personnel changes expected this month?") == NOT_CATEGORIZED


def test_white_house_policy_market_is_not_categorized():
    assert _categorize("Will the White House announce a new tariff policy?") == NOT_CATEGORIZED


def test_white_house_briefing_market_is_not_categorized():
    assert _categorize("Will the White House hold a press briefing today?") == NOT_CATEGORIZED


def test_white_house_visit_market_is_not_categorized():
    assert _categorize("Will X visit the White House this month?") == NOT_CATEGORIZED


# ---- crypto/btc_up_down: positive ----


def test_bitcoin_up_or_down_matches():
    assert _categorize("Bitcoin Up or Down - August 17, 6:30PM-6:35PM ET") == (
        "crypto",
        "btc_up_down",
    )


def test_will_bitcoin_be_up_matches():
    assert _categorize("Will Bitcoin be up today?") == ("crypto", "btc_up_down")


def test_btc_above_matches():
    assert _categorize("Will BTC be above $120,000 on Friday?") == ("crypto", "btc_up_down")


# ---- crypto/btc_up_down: negative (other coins / generic crypto) ----


def test_ethereum_up_or_down_does_not_match_btc():
    assert _categorize("Ethereum Up or Down - August 17, 6:30PM-6:35PM ET") == NOT_CATEGORIZED


def test_solana_price_market_does_not_match_btc():
    assert _categorize("Will Solana be above $200 by Friday?") == NOT_CATEGORIZED


def test_xrp_market_does_not_match_btc():
    assert _categorize("Will XRP be above $3 by Friday?") == NOT_CATEGORIZED


def test_generic_crypto_market_does_not_match_btc():
    assert _categorize("Will the total crypto market cap exceed $3T?") == NOT_CATEGORIZED


def test_crypto_regulation_market_does_not_match_btc():
    assert _categorize("Will Congress pass new crypto regulation this year?") == NOT_CATEGORIZED


def test_crypto_etf_news_market_does_not_match_btc():
    assert _categorize("Will a new crypto ETF be approved this quarter?") == NOT_CATEGORIZED


# ---- sports/basketball: positive ----


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


# ---- sports/basketball: negative (other sports) ----


def test_football_market_does_not_match_basketball():
    assert _categorize("Arsenal vs Chelsea", "Premier League matchday") == NOT_CATEGORIZED


def test_nfl_market_does_not_match_basketball():
    assert _categorize("Chiefs vs Bills - Moneyline", "NFL") == NOT_CATEGORIZED


def test_soccer_world_cup_does_not_match_basketball():
    assert _categorize("Who wins the World Cup?") == NOT_CATEGORIZED


def test_tennis_market_does_not_match_basketball():
    assert _categorize("Will Djokovic win the Australian Open?") == NOT_CATEGORIZED


def test_baseball_market_does_not_match_basketball():
    assert _categorize("Yankees vs Red Sox - Moneyline", "MLB") == NOT_CATEGORIZED


def test_hockey_market_does_not_match_basketball():
    assert _categorize("Will the Oilers win the Stanley Cup?", "NHL") == NOT_CATEGORIZED


# ---- politics that must not be categorized at all ----


def test_zelenskyy_tweet_market_is_not_categorized():
    assert _categorize("Will Zelenskyy tweet before Friday?") == NOT_CATEGORIZED


def test_unrelated_politics_market_is_not_categorized():
    assert _categorize("Who wins the 2028 presidential election?") == NOT_CATEGORIZED


def test_generic_congress_market_is_not_categorized():
    assert _categorize("Will the Senate pass the bill this month?") == NOT_CATEGORIZED


# ---- regression: exactly four target subcategories are active ----


def test_only_four_target_subcategories_are_configured():
    config = load_config(
        profile_path=Path("config/profile.yaml"), markets_path=Path("config/markets.yaml")
    )
    pairs = {
        (category, subcategory)
        for category, subcategories in config.target_subcategories.items()
        for subcategory in subcategories
    }
    assert pairs == {
        ("politics", "elon_musk_tweets"),
        ("politics", "white_house_tweets"),
        ("crypto", "btc_up_down"),
        ("sports", "basketball"),
    }


def test_no_zelenskyy_or_football_rule_exists_in_markets_yaml():
    rule_pairs = {(r.category, r.subcategory) for r in RULES}
    assert ("politics", "zelenskyy_tweets") not in rule_pairs
    assert ("politics", "ukraine_president_tweets") not in rule_pairs
    assert ("sports", "football") not in rule_pairs
    assert rule_pairs == {
        ("politics", "elon_musk_tweets"),
        ("politics", "white_house_tweets"),
        ("crypto", "btc_up_down"),
        ("sports", "basketball"),
    }
