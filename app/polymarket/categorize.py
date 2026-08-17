"""Keyword/entity-based market categorization.

Deliberately simple substring matching (not NER) for Stage 1. Pure and
dependency-free so it's trivial to unit test; all matching rules live in
config/markets.yaml, not hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

OTHER_CATEGORY = "other"
UNCATEGORIZED_SUBCATEGORY = "uncategorized"


@dataclass(frozen=True)
class CategoryRule:
    category: str
    subcategory: str
    keywords: tuple[str, ...]


def load_category_rules(path: Path) -> list[CategoryRule]:
    """Flatten config/markets.yaml's categories.<cat>.<subcat>.keywords into rules."""
    raw = yaml.safe_load(path.read_text()) or {}
    categories = raw.get("categories") or {}

    rules: list[CategoryRule] = []
    for category, subcats in categories.items():
        for subcategory, body in (subcats or {}).items():
            keywords = tuple((body or {}).get("keywords") or [])
            if not keywords:
                continue
            rules.append(CategoryRule(category=category, subcategory=subcategory, keywords=keywords))
    return rules


def categorize_market(
    question: str,
    event_title: str | None,
    tags: Iterable[str] | None,
    rules: list[CategoryRule],
) -> tuple[str, str]:
    """Return (category, subcategory) for the first matching rule.

    Always returns a value - markets matching no rule get
    (OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY) rather than being dropped, so
    nothing discovered is discarded before it reaches storage.
    """
    blob_parts = [question or "", event_title or "", *(tags or [])]
    blob = " ".join(blob_parts).lower()

    for rule in rules:
        for keyword in rule.keywords:
            if keyword.lower() in blob:
                return rule.category, rule.subcategory

    return OTHER_CATEGORY, UNCATEGORIZED_SUBCATEGORY
