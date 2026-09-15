"""Phase 5.2 board foundation: profiles/board_v1.yaml, BoardSettings, alfa_ DB base."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML
from webapp.board.db import TABLE_PREFIX, AlfaBase, normalize_database_url
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import CalibrationProfile

_REPO = Path(__file__).resolve().parents[2]
_BOARD = _REPO / "profiles" / "board_v1.yaml"


def _raw() -> dict[str, object]:
    data = YAML(typ="safe").load(_BOARD.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_board_profile_loads_with_owner_decisions() -> None:
    s = load_board_settings(_BOARD)
    assert s.board_profile_id == "board_v1"
    # K1: the six counted families, in the owner's order.
    assert s.evidence.counted_families == (
        "flow", "dealer_gamma", "dark_pool", "sector", "price_confirmation", "open_interest",
    )
    # R-UN2: 3+ unknown families forbid "Güçlü".
    assert s.evidence.max_unknown_for_strong + 1 == 3
    # Disclosed defaults stay flagged until the owner confirms them (decision P15).
    assert s.sizing.values_confirmed_by_owner is False


def test_board_profile_is_not_a_calibration_section() -> None:
    """Decision P6: calibration profile hashes (and the burned v5/v6 verdicts) stay untouched."""
    assert "board" not in CalibrationProfile.model_fields


def test_untradable_cutoff_is_read_not_duplicated() -> None:
    """A2: İŞLENMEZ uses penalty_triggers.spread_pct_threshold; the board file has no copy."""
    assert "untradable_spread_pct" not in _raw()["tradability"]  # type: ignore[operator]
    assert load_default_profile().penalty_triggers.spread_pct_threshold == 15.0
    s = load_board_settings(_BOARD)
    assert s.tradability.max_tradable_spread_pct < 15.0


def test_content_hash_is_stable_and_sensitive() -> None:
    a = load_board_settings(_BOARD)
    b = load_board_settings(_BOARD)
    assert a.content_hash() == b.content_hash()
    raw = _raw()
    raw["chase"] = {"reasonable_max_pct": 9.0, "late_min_pct": 15.0}
    assert BoardSettings.model_validate(raw).content_hash() != a.content_hash()


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("evidence", {"counted_families": ["flow", "flow"], "strong_min_supporting": 4,
                      "moderate_min_supporting": 2, "max_unknown_for_strong": 2,
                      "flow_net_premium_deadband_usd": 1.0}),
        ("evidence", {"counted_families": ["flow"], "strong_min_supporting": 2,
                      "moderate_min_supporting": 3, "max_unknown_for_strong": 2,
                      "flow_net_premium_deadband_usd": 1.0}),
        ("chase", {"reasonable_max_pct": 15.0, "late_min_pct": 10.0}),
        ("aggregation", {"intentional_min_top_strike_share_pct": 30.0,
                         "scattered_max_top_strike_share_pct": 60.0, "min_strikes_for_scattered": 4}),
        ("opening_closing", {"confirm_open_min_ratio": 0.5, "confirm_close_max_ratio": -0.5,
                             "job_time_et": "25:00"}),
    ],
)
def test_inconsistent_values_are_rejected(section: str, value: dict[str, object]) -> None:
    raw = _raw()
    raw[section] = value
    with pytest.raises(ValidationError):
        BoardSettings.model_validate(raw)


def test_unknown_keys_are_rejected() -> None:
    raw = _raw()
    raw["sizing"] = {**raw["sizing"], "leverage": 2}  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        BoardSettings.model_validate(raw)


def test_unknown_family_is_rejected() -> None:
    raw = _raw()
    raw["evidence"] = {**raw["evidence"], "counted_families": ["flow", "congress"]}  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        BoardSettings.model_validate(raw)


def test_every_alfa_table_carries_the_prefix() -> None:
    for name in AlfaBase.metadata.tables:
        assert name.startswith(TABLE_PREFIX), name


def test_database_url_normalisation() -> None:
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
