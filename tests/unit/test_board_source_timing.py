"""Phase 5.2.PERF9: every board data source is timed, and the line proves it.

The page stage has measured 0.70 s and 23.5 s on identical code within the same
hour, and the existing ``board timing:`` line could not say which of the dozen
reads inside ``build_alfa_page`` cost it. ``_build_board_page`` now wraps each
source and logs the breakdown from ``webapp.main`` — the only logger that
reaches the Railway deployment log (REG-9).

The test that matters here is not "a line is logged" but "no source escapes it":
a thirteenth source added later and passed unwrapped would be invisible, which
is exactly the hole this instrumentation exists to close. So the expected set of
names is pinned, and a source passed without ``_timed`` leaves its name out of
the line and fails the assertion.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import pytest
from webapp.board import alfa_page

# The twelve reads build_alfa_page performs, by the name each is timed under.
_EXPECTED: frozenset[str] = frozenset(
    {
        "quote", "evidence", "profile_hash", "delayed", "atm", "flow_since",
        "oi", "catalyst", "regime", "trades", "holdings", "sector",
    },
)

# How to call each source once, so the wrapper records a mark for it.
_MOMENT = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)
_CALLS: dict[str, tuple[object, ...]] = {
    "quote_source": (("SPY261016C00760000",),),
    "evidence_source": ("live-2026-09-17", ()),
    "profile_hash_source": ("live-2026-09-17", ()),
    "delayed_source": (("SPY",), date(2026, 9, 17)),
    "atm_source": (("SPY",),),
    "flow_since_source": ((),),
    "oi_source": ((),),
    "catalyst_source": ((), _MOMENT),
    "regime_source": (_MOMENT,),
    "trades_source": (),
    "holdings_source": (),
    "sector_source": (("SPY",),),
}

_FACTORIES = (
    "db_quote_source", "db_evidence_source", "db_profile_hash_source",
    "db_delayed_source", "db_atm_source", "db_flow_since_source",
    "db_oi_source", "db_catalyst_source", "db_regime_source",
    "db_holdings_source", "db_sector_source",
)


def _stub_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """No source touches a database in this test; only the wrapping is under test."""
    for name in _FACTORIES:
        monkeypatch.setattr(
            alfa_page, name, lambda *_a, **_k: (lambda *_ar, **_kw: {}), raising=True,
        )


def _drive(monkeypatch: pytest.MonkeyPatch, *, skip: str | None = None) -> set[str]:
    """Build the page with a captured builder, calling every source once.

    ``skip`` omits one source from the call set, which is how the negative
    control below proves the assertion can fail.
    """
    import webapp.main as m

    _stub_sources(monkeypatch)
    monkeypatch.setattr(m, "_board_reader", lambda: type("R", (), {"engine": object()})())
    monkeypatch.setattr(m, "_journal", lambda: type("J", (), {"list": lambda _s, _x: ()})())
    monkeypatch.setattr(m, "_legacy_scores", lambda: None)
    monkeypatch.setattr(m, "_spread_cutoff_pct", lambda: None)

    def capture(*_args: object, **kwargs: Any) -> object:
        for key, call_args in _CALLS.items():
            if key == skip:
                continue
            source = kwargs.get(key)
            assert source is not None, f"{key} is no longer passed to build_alfa_page"
            source(*call_args)
        return object()

    monkeypatch.setattr(alfa_page, "build_alfa_page", capture)
    m._build_board_page(None, gate_on=True, run_latest_ts=None)
    return set()


def _logged_names(caplog: pytest.LogCaptureFixture) -> set[str]:
    lines = [r.getMessage() for r in caplog.records if "board sources:" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one breakdown line, got {lines}"
    body = lines[0].split("board sources:", 1)[1]
    return {part.split("=", 1)[0].strip() for part in body.split() if "=" in part}


def test_every_board_source_is_timed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="webapp.main"):
        _drive(monkeypatch)
    assert _logged_names(caplog) == set(_EXPECTED), (
        "a board data source is missing from the timing line — an unwrapped source "
        "reads from the database on every render and nothing measures it"
    )


def test_the_assertion_can_fail_when_a_source_goes_unmeasured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The negative control: a guard that cannot fail is worth nothing.

    Two vacuous guards were found in this project in two days, so this file
    proves its own assertion has teeth instead of asserting that it does.
    """
    with caplog.at_level(logging.WARNING, logger="webapp.main"):
        _drive(monkeypatch, skip="catalyst_source")
    names = _logged_names(caplog)
    assert "catalyst" not in names
    assert names != set(_EXPECTED)
