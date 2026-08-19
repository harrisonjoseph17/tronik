from pathlib import Path

from app.config.loader import load_config


def test_load_config_from_real_files():
    config = load_config(
        profile_path=Path("config/profile.yaml"),
        markets_path=Path("config/markets.yaml"),
    )
    assert config.filters.min_liquidity == 1000.0
    assert config.filters.max_spread == 0.10
    assert config.db_path == Path("data/polymarket.db")
    assert len(config.category_rules) == 4
    categories = {(r.category, r.subcategory) for r in config.category_rules}
    assert ("politics", "elon_musk") in categories
    assert ("politics", "white_house") in categories
    assert ("crypto", "btc_up_down") in categories
    assert ("sports", "basketball") in categories
    assert config.target_subcategories == {
        "politics": frozenset({"elon_musk", "white_house"}),
        "crypto": frozenset({"btc_up_down"}),
        "sports": frozenset({"basketball"}),
    }


def test_load_config_missing_files_uses_defaults(tmp_path: Path):
    config = load_config(
        profile_path=tmp_path / "does_not_exist.yaml",
        markets_path=tmp_path / "also_missing.yaml",
    )
    assert config.filters.min_liquidity == 1000.0
    assert config.category_rules == []


def test_load_config_partial_profile_uses_defaults_for_missing_keys(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("filters:\n  min_liquidity: 5000\n")
    config = load_config(profile_path=profile_path, markets_path=tmp_path / "missing.yaml")
    assert config.filters.min_liquidity == 5000.0
    assert config.filters.max_spread == 0.10  # default preserved


def test_load_config_analysis_settings_from_real_files():
    config = load_config(
        profile_path=Path("config/profile.yaml"),
        markets_path=Path("config/markets.yaml"),
    )
    assert config.analysis.min_snapshots_for_history == 3
    assert config.analysis.max_probability_adjustment == 0.15
    assert config.analysis.category_reliability["crypto"] == 0.8
    assert config.analysis.risk_gate_min_edge == 0.05
    assert config.analysis.compound_min_score == 60.0


def test_load_config_analysis_settings_missing_uses_defaults(tmp_path: Path):
    config = load_config(
        profile_path=tmp_path / "does_not_exist.yaml",
        markets_path=tmp_path / "also_missing.yaml",
    )
    assert config.analysis.min_snapshots_for_history == 3
    assert config.analysis.category_reliability == {
        "politics": 0.7,
        "crypto": 0.8,
        "sports": 0.6,
        "other": 0.3,
    }


def test_load_config_partial_analysis_section_uses_defaults_for_missing_keys(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("analysis:\n  risk_gate_min_edge: 0.10\n")
    config = load_config(profile_path=profile_path, markets_path=tmp_path / "missing.yaml")
    assert config.analysis.risk_gate_min_edge == 0.10
    assert config.analysis.compound_min_score == 60.0  # default preserved


def test_load_config_target_subcategories_missing_uses_narrow_default(tmp_path: Path):
    config = load_config(
        profile_path=tmp_path / "does_not_exist.yaml",
        markets_path=tmp_path / "also_missing.yaml",
    )
    assert config.target_subcategories == {
        "politics": frozenset({"elon_musk", "white_house"}),
        "crypto": frozenset({"btc_up_down"}),
        "sports": frozenset({"basketball"}),
    }


def test_load_config_target_subcategories_custom_override(tmp_path: Path):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "target_subcategories:\n  crypto:\n    - btc_up_down\n"
    )
    config = load_config(profile_path=profile_path, markets_path=tmp_path / "missing.yaml")
    assert config.target_subcategories == {"crypto": frozenset({"btc_up_down"})}
