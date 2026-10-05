"""Phase 5.25.12: the page says when the flow on screen is not today's.

The per-card version of this warning is attached inside ``decide`` only after a
card's flow has picked a direction. In the case that produced it — several days
of flow summed into one run, so every ticker read "kararsız" — no card ever
picked a direction and the warning never appeared (live 2026-10-05). The page
now says it on its own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from webapp.live_alpha.page import _flow_warning

from uoa_detector.live_alpha.calendar import MarketMode, Session

_LIVE = Session(
    mode=MarketMode.LIVE, reason="test", et_now=datetime(2026, 10, 5, 10, 13),
    session_date=date(2026, 10, 5), close_et=None, next_session=None,
)
_CLOSED = Session(
    mode=MarketMode.CLOSED, reason="hafta sonu", et_now=datetime(2026, 10, 4, 10, 0),
    session_date=None, close_et=None, next_session=date(2026, 10, 5),
)


def test_no_warning_when_the_flow_is_this_session() -> None:
    assert _flow_warning({"flow_current": True}, _LIVE) == ""


def test_warning_names_the_last_print_when_the_flow_is_not_today() -> None:
    snap = {"flow_current": False, "flow_last_print": datetime(2026, 10, 1, 19, 58, tzinfo=UTC).isoformat()}
    text = _flow_warning(snap, _LIVE)
    assert "bugünkü seansa ait değil" in text
    assert "2026-10-01 19:58 UTC" in text


def test_closed_market_says_last_session_rather_than_warning() -> None:
    snap = {"flow_current": False, "flow_last_print": datetime(2026, 10, 2, 19, 58, tzinfo=UTC).isoformat()}
    assert _flow_warning(snap, _CLOSED).startswith("Piyasa kapalı")
