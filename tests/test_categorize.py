from pathlib import Path

from app.polymarket.categorize import (
    OTHER_CATEGORY,
    UNCATEGORIZED_SUBCATEGORY,
    CategoryRule,
    categorize_market,
    load_category_rules,
)

RULES = [
    CategoryRule("politics", "elon_musk_tweets", ("elon musk", "@elonmusk")),
    CategoryRule("politics", "white_house_tweets", ("white house tweet",)),
    CategoryRule("politics", "ukraine_president_tweets", ("zelensky tweet", "zelenskyy tweet")),
    CategoryRule("crypto", "btc_updown", ("will bitcoin", "btc up or down")),
    CategoryRule("sports", "football", ("premier league", "champions league")),
]


def test_matches_elon_musk_tweets():
    assert categorize_market("Will Elon Musk tweet 100 times this week?", None, None, RULES) == (
        "politics",
        "elon_musk_tweets",
    )


def test_matches_white_house_tweets():
    assert categorize_market("White House tweet about tariffs?", None, None, RULES) == (
        "politics",
        "white_house_tweets",
    )


def test_matches_ukraine_president_tweets():
    assert categorize_market("Will Zelenskyy tweet before Friday?", None, None, RULES) == (
        "politics",
        "ukraine_president_tweets",
    )


def test_matches_btc_updown():
    assert categorize_market("Will Bitcoin be up today?", None, None, RULES) == (
        "crypto",
        "btc_updown",
    )


def test_matches_football():
    assert categorize_market("Arsenal vs Chelsea", "Premier League matchday", None, RULES) == (
        "sports",
        "football",
    )


def test_matches_via_tags_not_just_question():
    assert categorize_market("Match result?", None, ["Champions League"], RULES) == (
        "sports",
        "football",
    )


def test_case_insensitive():
    assert categorize_market("WILL BITCOIN BE UP TODAY?", None, None, RULES) == (
        "crypto",
        "btc_updown",
    )


def test_no_match_returns_other_uncategorized():
    assert categorize_market("Will it rain in Paris tomorrow?", None, None, RULES) == (
        OTHER_CATEGORY,
        UNCATEGORIZED_SUBCATEGORY,
    )


def test_load_category_rules_from_yaml(tmp_path: Path):
    yaml_path = tmp_path / "markets.yaml"
    yaml_path.write_text(
        """
categories:
  crypto:
    btc_updown:
      keywords:
        - "will bitcoin"
        - "btc up or down"
  politics:
    elon_musk_tweets:
      keywords:
        - "elon musk"
"""
    )
    rules = load_category_rules(yaml_path)
    assert len(rules) == 2
    categories = {(r.category, r.subcategory) for r in rules}
    assert ("crypto", "btc_updown") in categories
    assert ("politics", "elon_musk_tweets") in categories
