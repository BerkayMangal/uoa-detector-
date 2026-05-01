"""Tests for ``CalibrationResolver`` — ticker/regime resolution and hot-swap."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from uoa_detector.calibration import CalibrationResolver
from uoa_detector.errors import ConfigurationError


@pytest.fixture
def profiles_root(tmp_path: Path) -> Path:
    """Create a working copy of the profiles dir under ``tmp_path``."""
    src = Path("profiles").resolve()
    dst = tmp_path / "profiles"
    shutil.copytree(src, dst)
    return dst


def test_default_loads(profiles_root: Path) -> None:
    r = CalibrationResolver(profiles_root)
    p = r.resolve()
    assert p.profile_id == "v5_default"


def test_default_returned_when_no_match(profiles_root: Path) -> None:
    r = CalibrationResolver(profiles_root)
    assert r.resolve(ticker="NONEXISTENT").profile_id == "v5_default"
    assert r.resolve(regime="nonexistent").profile_id == "v5_default"


def test_ticker_profile_overrides_default(profiles_root: Path) -> None:
    """Add a ticker profile and assert the resolver picks it up after reload."""
    (profiles_root / "tickers" / "NVDA.yaml").write_text(
        "profile_id: nvda_override\n"
        "description: test\n"
        "inherits_from: v5_default\n"
        "cluster: { window_minutes: 45 }\n"
    )
    r = CalibrationResolver(profiles_root)
    p = r.resolve(ticker="NVDA")
    assert p.profile_id == "nvda_override"
    assert p.cluster.window_minutes == 45
    # Inherited fields preserved
    assert p.cluster.decay_minutes == 90


def test_ticker_match_is_case_insensitive(profiles_root: Path) -> None:
    (profiles_root / "tickers" / "AAPL.yaml").write_text(
        "profile_id: aapl_override\ndescription: test\ninherits_from: v5_default\n"
    )
    r = CalibrationResolver(profiles_root)
    assert r.resolve(ticker="aapl").profile_id == "aapl_override"
    assert r.resolve(ticker="AAPL").profile_id == "aapl_override"
    assert r.resolve(ticker="  aapl  ").profile_id == "aapl_override"


def test_regime_falls_back_to_default_when_no_ticker(profiles_root: Path) -> None:
    (profiles_root / "regimes" / "high_vix.yaml").write_text(
        "profile_id: high_vix_override\n"
        "description: test\n"
        "inherits_from: v5_default\n"
        "cluster: { window_minutes: 30 }\n"
    )
    r = CalibrationResolver(profiles_root)
    p = r.resolve(regime="high_vix")
    assert p.profile_id == "high_vix_override"
    assert p.cluster.window_minutes == 30


def test_ticker_overrides_regime(profiles_root: Path) -> None:
    (profiles_root / "regimes" / "high_vix.yaml").write_text(
        "profile_id: high_vix_override\ndescription: test\ninherits_from: v5_default\n"
    )
    (profiles_root / "tickers" / "TSLA.yaml").write_text(
        "profile_id: tsla_override\ndescription: test\ninherits_from: v5_default\n"
    )
    r = CalibrationResolver(profiles_root)
    p = r.resolve(ticker="TSLA", regime="high_vix")
    assert p.profile_id == "tsla_override"


def test_hot_swap_via_reload(profiles_root: Path) -> None:
    """Modify a YAML on disk; ``reload()`` picks up the change."""
    nvda_path = profiles_root / "tickers" / "NVDA.yaml"
    nvda_path.write_text(
        "profile_id: nvda_v1\ndescription: test\ninherits_from: v5_default\n"
        "cluster: { window_minutes: 30 }\n"
    )
    r = CalibrationResolver(profiles_root)
    assert r.resolve(ticker="NVDA").cluster.window_minutes == 30

    # Edit on disk to change a value, then reload.
    nvda_path.write_text(
        "profile_id: nvda_v2\ndescription: test\ninherits_from: v5_default\n"
        "cluster: { window_minutes: 75 }\n"
    )
    r.reload()
    assert r.resolve(ticker="NVDA").profile_id == "nvda_v2"
    assert r.resolve(ticker="NVDA").cluster.window_minutes == 75


def test_missing_default_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigurationError, match="failed to load default profile"):
        CalibrationResolver(empty)


def test_missing_default_raises_ambiguously(tmp_path: Path) -> None:
    """Bootstrap failure currently doesn't distinguish missing-file from
    malformed-YAML from validator-rejection in the exception. Phase 3 will
    wrap underlying causes via ``__cause__`` (see TODO in
    ``CalibrationResolver.__init__``). This test pins the current
    (limited) contract so the Phase 3 widening doesn't go unnoticed —
    when somebody adds ``raise ... from underlying_exc`` in ``reload()``
    and propagates the cause through ``__init__``, this assertion will
    fail and prompt them to update the test (and any callers relying on
    the cause-collapsed behaviour).

    Three failure modes that all currently produce the same exception with
    no ``__cause__``:
      - default file missing (this test exercises that one)
      - default file contains malformed YAML
      - default file parses as YAML but fails Pydantic validation

    Operationally these need different user actions ('create the file' vs
    'fix the syntax' vs 'fix the values'). Right now the only signal is
    the ``calibration_resolver_reload_aborted`` log line's ``reason``
    field, which is fine for human operators but not for programmatic
    callers.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigurationError) as exc_info:
        CalibrationResolver(empty)

    # Document the contract gap: __cause__ is None because reload() catches
    # the underlying exception broadly and __init__ raises a fresh one
    # without `from`. Phase 3 fix will replace this assertion with one
    # that asserts __cause__ IS the underlying FileNotFoundError /
    # YAMLError / ValidationError, depending on the failure mode.
    assert exc_info.value.__cause__ is None, (
        "If __cause__ is now populated, the Phase 3 fix has landed — "
        "rewrite this test to assert the specific underlying type, and "
        "remove the corresponding TODO in CalibrationResolver.__init__."
    )


def test_broken_child_profile_does_not_kill_reload(profiles_root: Path) -> None:
    """A bad ticker profile is dropped with a warning; default still loads."""
    (profiles_root / "tickers" / "BROKEN.yaml").write_text(
        "profile_id: broken\ndescription: x\ninherits_from: v5_default\n"
        "scoring: { uoa: 5.0 }\n"  # invalid weight
    )
    r = CalibrationResolver(profiles_root)
    # Default still works; broken ticker falls back to default
    assert r.resolve().profile_id == "v5_default"
    assert r.resolve(ticker="BROKEN").profile_id == "v5_default"
