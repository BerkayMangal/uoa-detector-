"""Phase 5.0.6: UW ``ohlc/1d`` row order and session filter (contract §3.9).

The live payload is NEWEST FIRST and carries pre-market ("pr"), regular ("r")
and post-market ("po") rows per date. The bug took ``data[-1]`` (a close about
a year stale) and ``closes[-21:]`` (the oldest 21 rows, sessions mixed). The
fixture below is a trimmed, hand-built copy of that shape; the anchor rows
(2026-09-14 pr/r and 2025-09-15 po/r) carry the probed SPY values. No live call.
"""

from __future__ import annotations

import math
import random
import statistics
from itertools import pairwise

import pytest
from webapp import pricing
from webapp.gamma_live import _realized_vol

# Regular-session closes, NEWEST FIRST, one per trading date. 2026-09-07 is
# Labor Day (no row). The last two dates fall outside the 21-close window.
_REGULAR_NEWEST_FIRST: list[tuple[str, float]] = [
    ("2026-09-14", 762.04), ("2026-09-11", 757.30), ("2026-09-10", 760.12),
    ("2026-09-09", 751.88), ("2026-09-08", 748.95), ("2026-09-04", 753.41),
    ("2026-09-03", 749.02), ("2026-09-02", 744.67), ("2026-09-01", 746.20),
    ("2026-08-31", 739.85), ("2026-08-28", 741.33), ("2026-08-27", 736.90),
    ("2026-08-26", 738.44), ("2026-08-25", 731.07), ("2026-08-24", 733.62),
    ("2026-08-21", 728.15), ("2026-08-20", 730.81), ("2026-08-19", 725.40),
    ("2026-08-18", 722.96), ("2026-08-17", 727.55), ("2026-08-14", 719.38),
    ("2026-08-13", 716.02), ("2025-09-15", 660.91),
]


def _row(day: str, session: str, close: float) -> dict[str, object]:
    px = f"{close:.2f}"
    return {
        "close": px, "date": day, "high": px, "low": px, "market_time": session,
        "open": px, "total_volume": "1000000", "volume": "250000",
    }


def _newest_first_payload() -> dict[str, object]:
    """Newest first, several sessions per date, in-date order varied as live."""
    rows: list[dict[str, object]] = []
    last = len(_REGULAR_NEWEST_FIRST) - 1
    for i, (day, close) in enumerate(_REGULAR_NEWEST_FIRST):
        if i == 0:  # session in progress: pre-market + regular, no post yet
            rows += [_row(day, "pr", 759.01), _row(day, "r", close)]
        elif i == last:  # oldest date as probed: post-market then regular
            rows += [_row(day, "po", 660.70), _row(day, "r", close)]
        elif i % 2:
            rows += [_row(day, "pr", close + 25.0), _row(day, "r", close),
                     _row(day, "po", close - 40.0)]
        else:
            rows += [_row(day, "po", close - 40.0), _row(day, "pr", close + 25.0),
                     _row(day, "r", close)]
    return {"data": rows}


def _shuffled(payload: dict[str, object], seed: int) -> dict[str, object]:
    data = payload["data"]
    assert isinstance(data, list)
    rows = list(data)
    random.Random(seed).shuffle(rows)
    return {"data": rows}


def _annualised_vol(chronological_closes: list[float]) -> float:
    rets = [math.log(b / a) for a, b in pairwise(chronological_closes)]
    return statistics.stdev(rets) * math.sqrt(252)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _patch_http(monkeypatch: pytest.MonkeyPatch, payload: object) -> list[str]:
    urls: list[str] = []

    def _get(url: str, **_kwargs: object) -> _FakeResponse:
        urls.append(url)
        return _FakeResponse(payload)

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-placeholder")
    monkeypatch.setattr(pricing.httpx, "get", _get)
    return urls


# ---------------------------------------------------------------------------
# latest_close
# ---------------------------------------------------------------------------


def test_latest_close_is_newest_regular_close(monkeypatch: pytest.MonkeyPatch) -> None:
    urls = _patch_http(monkeypatch, _newest_first_payload())
    close = pricing.latest_close("spy")
    assert close == 762.04  # 2026-09-14 "r"; not rows[0] "pr" 759.01
    assert close != 660.91  # the old data[-1] bug: a year-stale close
    assert urls == ["https://api.unusualwhales.com/api/stock/SPY/ohlc/1d"]


@pytest.mark.parametrize("seed", range(5))
def test_latest_close_ignores_row_order(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    _patch_http(monkeypatch, _shuffled(_newest_first_payload(), seed))
    assert pricing.latest_close("SPY") == 762.04


def test_latest_close_none_when_newest_regular_close_is_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _newest_first_payload()
    rows = payload["data"]
    assert isinstance(rows, list)
    rows[1] = {**rows[1], "close": "n/a"}  # the 2026-09-14 "r" row
    _patch_http(monkeypatch, payload)
    # Never fall back silently to an older day's close.
    assert pricing.latest_close("SPY") is None


def test_latest_close_none_without_regular_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, {"data": [_row("2026-09-14", "pr", 759.01),
                                       _row("2026-09-11", "po", 750.00)]})
    assert pricing.latest_close("SPY") is None


# ---------------------------------------------------------------------------
# _realized_vol
# ---------------------------------------------------------------------------


def test_realized_vol_uses_newest_21_regular_closes() -> None:
    newest_21 = [close for _day, close in _REGULAR_NEWEST_FIRST[:21]]
    expected = _annualised_vol(list(reversed(newest_21)))
    rv = _realized_vol(_newest_first_payload())
    assert rv == pytest.approx(expected, rel=1e-12)
    assert rv == pytest.approx(0.0936957, rel=1e-5)  # hand-computed pin
    # The fixture discriminates: the oldest 21 regular closes give another value.
    oldest_21 = [close for _day, close in _REGULAR_NEWEST_FIRST[-21:]]
    assert _annualised_vol(list(reversed(oldest_21))) != pytest.approx(expected, rel=1e-3)


@pytest.mark.parametrize("seed", range(5))
def test_realized_vol_ignores_row_order(seed: int) -> None:
    assert _realized_vol(_shuffled(_newest_first_payload(), seed)) == _realized_vol(
        _newest_first_payload(),
    )


def test_realized_vol_guard_counts_regular_closes_only() -> None:
    rows: list[dict[str, object]] = []
    for day, close in _REGULAR_NEWEST_FIRST[:4]:  # 4 regular closes (< 5)
        rows += [_row(day, "pr", close + 25.0), _row(day, "r", close),
                 _row(day, "po", close - 40.0)]
    assert _realized_vol({"data": rows}) is None
