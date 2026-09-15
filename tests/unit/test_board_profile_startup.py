"""Phase 5.2.A-fix3: the app refuses to start with a broken board profile (review RT-4).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.3 (``BoardSettings`` is
strict; the İŞLENMEZ cutoff is read from the live calibration profile) and §11
(a deploy is verified through ``/health``).

Pins:
  - a missing ``profiles/board_v1.yaml`` fails the app's startup, so ``/health``
    never answers 200 while ``GET /`` would return 500;
  - an invalid board profile (``refresh.cadence_seconds`` out of range) fails
    startup the same way;
  - a missing live calibration profile fails startup too;
  - with valid files, startup loads the board settings, the spread cutoff and
    the legacy scores once.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import webapp.main as m
from fastapi.testclient import TestClient

from uoa_detector.errors import ConfigurationError

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "live_config_from_env", lambda: None)
    monkeypatch.setattr(m, "_BOARD_SETTINGS", None)
    monkeypatch.setattr(m, "_SPREAD_CUTOFF_PCT", None)
    monkeypatch.setattr(m, "_LEGACY_SCORES", None)


def _profiles_copy(root: Path) -> Path:
    shutil.copytree(_REPO / "profiles", root / "profiles")
    return root / "profiles"


def test_missing_board_profile_fails_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (_profiles_copy(tmp_path) / "board_v1.yaml").unlink()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError), TestClient(m.app):
        pass


def test_invalid_board_profile_fails_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = _profiles_copy(tmp_path) / "board_v1.yaml"
    text = board.read_text(encoding="utf-8")
    assert "cadence_seconds: 300" in text
    board.write_text(text.replace("cadence_seconds: 300", "cadence_seconds: 10"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="cadence_seconds"), TestClient(m.app):
        pass


def test_missing_calibration_profile_fails_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (_profiles_copy(tmp_path) / "v5_default.yaml").unlink()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError, match=r"v5_default\.yaml"), TestClient(m.app):
        pass


def test_valid_profiles_are_loaded_at_startup() -> None:
    with TestClient(m.app) as client:
        assert client.get("/health").status_code == 200
    assert m._BOARD_SETTINGS is not None
    assert m._BOARD_SETTINGS.refresh.cadence_seconds == 300
    assert m._SPREAD_CUTOFF_PCT == 15.0
    assert m._LEGACY_SCORES is not None
