"""Shared board harness for the Phase 5.2.C1 review-fix tests.

The C1b route tests seed a run and drive the board through a ``TestClient``.
The review fixes need that same board in several new files, so the seeding
lives here instead of being copied into each one. ``tests/unit/_webapp_auth``
is the existing precedent for a shared, non-collected helper module (the
leading underscore keeps pytest from collecting it).

Nothing here asserts anything: every pin belongs to the file that uses it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from webapp.board.telemetry import AlfaPrintMeta, ensure_telemetry_tables

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import ModuleType

    import httpx
    import pytest
    from fastapi.testclient import TestClient

RUN = "live-2026-09-16"
TS = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 16, 14, 5, tzinfo=UTC)
ORIGIN = {"Origin": "http://testserver"}

# Two rows: SPY yukarı (two bought calls on one strike) and NVDA aşağı (a bought put).
SPECS = (
    ("s1", "SPY", "call", "760", "400000", 0),
    ("s2", "SPY", "call", "760", "100000", 10),
    ("n1", "NVDA", "put", "170", "80000", 20),
)


def seed(url: str) -> None:
    """One stored run with the two rows above, plus the print metadata the board reads."""
    store = SqliteBacktestStore(url, flush_threshold=len(SPECS) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=RUN)
        for event_id, ticker, option_type, strike, premium, second in SPECS:
            store.add(
                EnrichedEvent(
                    print=build_print(
                        event_id=event_id, ts=TS + timedelta(seconds=second), ticker=ticker,
                        option_type=option_type, strike=strike, dte=2, premium=premium,  # type: ignore[arg-type]
                    ),
                    combined_score_post_penalty=0.42,
                ),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = create_engine(url)
    try:
        ensure_telemetry_tables(engine)
        with Session(engine) as session:
            session.add_all(
                AlfaPrintMeta(
                    run_id=RUN, event_id=event_id, ticker=ticker, option_chain=None,
                    fill_side="at_ask", option_type=option_type, strike=strike,
                    expiry=(TS + timedelta(days=2)).date(), print_ts=TS, written_at=TS,
                )
                for event_id, ticker, option_type, strike, _premium, _second in SPECS
            )
            session.commit()
    finally:
        engine.dispose()


def reset_singletons(module: ModuleType) -> None:
    """Drop the webapp's cached repositories so each test gets its own database."""
    module._REPO = module._JOURNAL = module._GAMMA = None
    module._BOARD_READER = None
    module._CARDS = None


def open_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "cards-fix.db",
) -> Iterator[tuple[TestClient, ModuleType]]:
    """An authenticated client over a seeded board, with the page clock pinned.

    Used as ``yield from open_board(...)`` inside each file's own ``board`` fixture.
    """
    url = f"sqlite:///{tmp_path / name}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)  # no live worker in tests
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)  # no price fetch on the journal POST
    import webapp.main as m

    reset_singletons(m)
    monkeypatch.setattr(m, "_now", lambda: NOW)  # quote and tape ages are clock-driven
    seed(url)
    yield authed_client(m.app, monkeypatch), m
    reset_singletons(m)


def press(
    client: TestClient, decision: str = "pas", *, ticker: str = "SPY", direction: str = "up",
    run_id: str = RUN, headers: dict[str, str] | None = None, **extra: str,
) -> httpx.Response:
    """Press ``Logla`` / ``Pas geç`` on one row, the way the rendered form does."""
    data = {
        "decision": decision, "card_run_id": run_id,
        "card_ticker": ticker, "card_direction": direction,
    }
    data.update(extra)
    return client.post(
        "/alfa/card", data=data,
        headers=ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def card_id_from(location: str, key: str) -> str:
    """The card id a redirect carries (``?card_id=`` after Logla, ``?pas=`` after Pas geç)."""
    return dict(parse_qsl(urlsplit(location).query))[key]


def trade_form(**extra: str) -> dict[str, str]:
    """The journal form's minimum fields, plus whatever a test adds."""
    data = {
        "ticker": "SPY", "direction": "bullish", "instrument": "call",
        "contracts": "1", "entry_price": "2.50", "thesis": "board card",
    }
    data.update(extra)
    return data
