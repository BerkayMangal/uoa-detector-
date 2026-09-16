"""Phase 5.2.A-fix4: clean-candidate honesty (review FA-03, FA-04).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-EV1 ("families
pointing the same way") and R-EM1; §5 A5 (reason sentence); decision P9
(dealer gamma is non-directional).

Pins:
  - FA-03: a lehte dealer gamma family is never listed under
    "{direction} gösteriyor"; it gets its own "yön göstermez, hareketi
    büyütebilir" clause. A row whose only lehte family is dealer gamma is not a
    clean candidate, so it cannot suppress the R-EM1 banner. The evidence word
    still counts it as lehte (§9).
  - FA-04: ``Bugün temiz aday yok`` only for today's ET session; a past run
    gets the dated variant; an unknown session date the undated one; a failed
    evidence or quote read never claims "no clean candidate" and shows the
    unread variant instead; a failed print read and no runs show no banner.
  - Every new string passes ``ensure_clean``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.aggregate import build_board_rows
from webapp.board.alfa_page import (
    ALFA_COPY,
    build_alfa_page,
    is_clean_candidate,
    load_spread_cutoff_pct,
)
from webapp.board.copy_tr import NO_CLEAN_CANDIDATE
from webapp.board.evidence import (
    STAGE_BY_FAMILY,
    EvidenceCounts,
    EvidenceInputs,
    StageTelemetryView,
    build_row_evidence,
)
from webapp.board.honesty import ensure_clean
from webapp.board.narrative import REASON_TEMPLATES, build_narrative
from webapp.board.netprem import TapeSummary
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.tradability import QuoteView, assess_tradability

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from tests.unit.test_board_honesty import _reset, _Row, _rows, _seed
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # the seeded session (test_board_honesty._TS)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_GAMMA_ONLY = {
    "dealer_gamma": "full_short_and_proximate",
    "dark_pool": "direction_unclear",
    "sector": "weak",
    "price_confirmation": "neutral",
}


def _prints(ts: datetime = _TS) -> list[BoardPrint]:
    pr = build_print(event_id="g1", ts=ts, ticker="GMO", option_type="put", strike="100", dte=3, premium="100000")
    sig = BacktestStore().add(
        EnrichedEvent(print=pr),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )
    return [BoardPrint(run_id=_RUN, event_id="g1", signal=sig, meta=PrintMetaView(fill_side="at_ask", option_chain=None))]


def _gamma_telemetry() -> dict[str, StageTelemetryView]:
    return {
        STAGE_BY_FAMILY[f]: StageTelemetryView(stage_name=STAGE_BY_FAMILY[f], branch=b, degraded=False)
        for f, b in _GAMMA_ONLY.items()
    }


def _gamma_only_evidence() -> tuple[Any, Any]:
    (row,) = build_board_rows(_prints(), _SETTINGS.aggregation)
    telemetry = _gamma_telemetry()
    tape = TapeSummary(
        ticker="GMO", trade_date=_TS.date(), net_call_premium=10_000.0, net_put_premium=0.0,
        minutes=90, first_tape_time=_TS, last_tape_time=_NOW, fetched_at=_NOW,
    )
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=None, telemetry=telemetry, tape=tape, ticker_info=None,
    )
    return row, evidence


def _tradable_chip() -> Any:
    quote = QuoteView(
        option_symbol="GMO260918P00100000", nbbo_bid=0.99, nbbo_ask=1.01, volume=500,
        last_tape_time=None, fetched_at=_NOW - timedelta(seconds=41), returned=True,
    )
    return assess_tradability(
        "GMO", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=_NOW,
    )


# ---------------------------------------------------------------------------
# FA-03
# ---------------------------------------------------------------------------


def test_gamma_only_row_says_gamma_points_no_way_and_is_not_clean() -> None:
    row, evidence = _gamma_only_evidence()
    assert evidence.counts == EvidenceCounts(supporting=1, against=0, unknown=1)  # §9: still lehte
    chip = _tradable_chip()
    assert chip.state == "tradable"
    narrative = build_narrative(row, evidence, chip, settings=_SETTINGS.narrative)
    assert narrative.reason.startswith(
        "Neden: yönü gösteren bağımsız kaynak yok; Dealer gamma yön göstermez, hareketi büyütebilir; ",
    )
    assert "gösteriyor" not in narrative.reason
    assert not is_clean_candidate(
        chip, evidence.counts, _SETTINGS.clean_candidate,
        non_directional_supporting=len(evidence.non_directional_supporting_labels()),
    )
    page = build_alfa_page(
        _prints(), _SETTINGS, spread_cutoff_pct=_CUTOFF, now=_NOW,
        quote_source=lambda symbols: ({s: QuoteView(s, 0.99, 1.01, 500, None, _NOW, True) for s in symbols}, {}),
        evidence_source=lambda run_id, requests: EvidenceInputs(
            telemetry={"g1": _gamma_telemetry()}, tapes={}, infos={},
        ),
    )
    (view,) = page.views
    assert view.clean_candidate is False
    assert page.no_clean_candidate is True


def test_directional_support_with_gamma_keeps_the_count_and_adds_the_clause() -> None:
    (row,) = build_board_rows(_prints(), _SETTINGS.aggregation)
    telemetry = {
        **_gamma_telemetry(),
        STAGE_BY_FAMILY["sector"]: StageTelemetryView(STAGE_BY_FAMILY["sector"], "strong", False),
    }
    evidence = build_row_evidence(
        row, settings=_SETTINGS, now=_NOW, signal=None, telemetry=telemetry, tape=None, ticker_info=None,
    )
    narrative = build_narrative(row, evidence, _tradable_chip(), settings=_SETTINGS.narrative)
    assert narrative.reason.startswith(
        "Neden: 1 bağımsız kaynak aşağıyı gösteriyor (Sektör); Dealer gamma yön göstermez, hareketi büyütebilir; ",
    )
    assert is_clean_candidate(
        _tradable_chip(), EvidenceCounts(2, 0, 2), _SETTINGS.clean_candidate, non_directional_supporting=1,
    )
    assert not is_clean_candidate(
        _tradable_chip(), EvidenceCounts(1, 0, 2), _SETTINGS.clean_candidate, non_directional_supporting=1,
    )


def test_new_reason_templates_are_clean() -> None:
    for key in ("supporting_amplified", "amplified_only"):
        assert ensure_clean(REASON_TEMPLATES[key]) == REASON_TEMPLATES[key]
    for key in ("no_clean_candidate_dated", "no_clean_candidate_undated", "clean_candidate_unread"):
        assert ensure_clean(ALFA_COPY[key]) == ALFA_COPY[key]


# ---------------------------------------------------------------------------
# FA-04: page model
# ---------------------------------------------------------------------------


def test_banner_wording_follows_the_session_date() -> None:
    today = build_alfa_page(_prints(), _SETTINGS, now=_NOW)
    assert (today.no_clean_candidate, today.no_clean_candidate_text) == (True, NO_CLEAN_CANDIDATE)

    past = build_alfa_page(_prints(), _SETTINGS, now=_NOW + timedelta(days=14))
    assert past.no_clean_candidate is True
    assert past.no_clean_candidate_text == "2026-09-15 seansında temiz aday yok"
    assert "Bugün" not in past.no_clean_candidate_text

    undated = build_alfa_page([], _SETTINGS, now=_NOW)
    assert undated.no_clean_candidate_text == "Bu çalışmada temiz aday yok"
    from_run = build_alfa_page([], _SETTINGS, now=_NOW, run_latest_ts=_TS)
    assert from_run.no_clean_candidate_text == NO_CLEAN_CANDIDATE


def test_a_late_evening_print_uses_its_et_date() -> None:
    # 2026-09-16 01:30 UTC is 2026-09-15 21:30 ET: still the 15th's session.
    late = build_alfa_page(_prints(datetime(2026, 9, 16, 1, 30, tzinfo=UTC)), _SETTINGS, now=_NOW)
    assert late.session_date == _TS.date()
    assert late.no_clean_candidate_text == NO_CLEAN_CANDIDATE


def _failing_source(*args: object) -> Any:
    msg = "table unavailable"
    raise RuntimeError(msg)


def test_failed_evidence_or_quote_read_never_claims_no_clean_candidate() -> None:
    evidence_failed = build_alfa_page(_prints(), _SETTINGS, now=_NOW, evidence_source=_failing_source)
    quotes_failed = build_alfa_page(
        _prints(), _SETTINGS, now=_NOW, spread_cutoff_pct=_CUTOFF, quote_source=_failing_source,
    )
    for page in (evidence_failed, quotes_failed):
        assert page.no_clean_candidate is False
        assert page.clean_candidate_unread is True
    load_failed = build_alfa_page(None, _SETTINGS, now=_NOW)
    assert (load_failed.no_clean_candidate, load_failed.clean_candidate_unread) == (False, False)


# ---------------------------------------------------------------------------
# FA-03 / FA-04: render
# ---------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    import webapp.main as m

    def _make(rows: Sequence[_Row], *, now: datetime) -> TestClient:
        url = f"sqlite:///{tmp_path / 'clean.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("LIVE_TICKERS", raising=False)
        monkeypatch.setattr(m, "_now", lambda: now)
        _reset(m)
        _seed(url, rows, gamma_row=False)
        return authed_client(m.app, monkeypatch)

    yield _make
    _reset(m)


_GMO = _Row("g1", "GMO", "put", "at_ask", "100000", 0.44, _GAMMA_ONLY, tape=-10_000.0, quote=(0.99, 1.01))


def _states(body: str) -> list[str]:
    return re.findall(r'data-state="([^"]+)"', body)


def test_gamma_only_row_does_not_suppress_the_banner(board: Callable[..., TestClient]) -> None:
    body = board([_GMO], now=_TS + timedelta(hours=1)).get("/").text
    row = _rows(body)["GMO"]
    assert 'data-clean-candidate="false"' in body
    assert "no-clean-candidate" in _states(body)
    assert NO_CLEAN_CANDIDATE in html.unescape(body)
    reason = re.search(r"<p data-reason>([^<]*)</p>", row)
    assert reason is not None
    assert "aşağıyı gösteriyor" not in html.unescape(reason.group(1))
    assert "Dealer gamma yön göstermez, hareketi büyütebilir" in html.unescape(reason.group(1))


def test_a_past_run_is_dated_not_today(board: Callable[..., TestClient]) -> None:
    body = html.unescape(board([_GMO], now=_TS + timedelta(days=16)).get("/").text)
    assert "no-clean-candidate" in _states(body)
    assert "2026-09-15 seansında temiz aday yok" in body
    assert NO_CLEAN_CANDIDATE not in body


@pytest.mark.parametrize("failing", ["read_evidence_inputs", "read_board_quotes"])
def test_a_failed_evidence_or_quote_read_shows_the_unread_state(
    board: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, failing: str,
) -> None:
    import webapp.board.alfa_page as page_module

    client = board([_GMO], now=_TS + timedelta(hours=1))
    monkeypatch.setattr(page_module, failing, _failing_source)
    body = client.get("/").text
    states = _states(body)
    assert "no-clean-candidate" not in states
    assert "clean-candidate-unread" in states
    assert NO_CLEAN_CANDIDATE not in html.unescape(body)
