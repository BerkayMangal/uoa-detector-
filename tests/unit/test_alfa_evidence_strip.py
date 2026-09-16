"""Phase 5.2.A4: evidence strip, evidence word, strength label, row order and the audit block.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-EV1, R-EV2, R-UN1,
R-UN2 and R-WD1; §5 A4.

Pins:
  - page model: the strip follows ``evidence.counted_families``; the evidence
    word; the strength label through the R-UN2 guard; inside every section
    rows sort by L desc, A asc, U asc, then total premium desc, and a higher
    combined score never moves a row; the evidence source gets the run and the
    source prints; a failed evidence read is flagged and every family reads
    bilinmiyor;
  - the audit block: ``Birleşik skor (denetim, sınırsız ölçek): 0.xx``,
    unclamped, with the pre-penalty score and its inputs; M27 is labelled
    ``önceki seans OI değişimi (bu baskı değil)``;
  - GET /alfa on a seeded sqlite run: every evidence word carries the hover
    ``Kâr olasılığı DEĞİL.``; unknown and kapsam-dışı chips are dashed and
    say ``bilgi yok, temiz demek değil``; the combined score appears only
    inside the Denetim block; ``Güçlü`` is refused at render level for a row
    with 3+ unknown families even when the classifier says strong; a failed
    evidence read is flagged; zero Unusual Whales calls;
  - every new frozen and generated string passes ``ensure_clean``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import webapp.board.alfa_page as page_module
import webapp.board.evidence as evidence_module
from sqlalchemy.orm import Session
from webapp.board.alfa_page import (
    ALFA_COPY,
    AUDIT_INPUT_LABELS,
    build_alfa_page,
    build_audit,
    load_live_legacy_scores,
    load_spread_cutoff_pct,
)
from webapp.board.copy_tr import EVIDENCE_HOVER, STRONG_LABEL, UNKNOWN_NOT_CLEAN
from webapp.board.db import make_engine
from webapp.board.evidence import (
    STAGE_BY_FAMILY,
    EvidenceInputs,
    EvidenceRequest,
    StageTelemetryView,
)
from webapp.board.honesty import ensure_clean
from webapp.board.netprem import TapeMinute, TapeSummary, ensure_netprem_tables, upsert_tape
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry, ensure_telemetry_tables
from webapp.board.tradability import QuoteView

from tests.conftest import build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.sources.unusual_whales.client import UnusualWhalesClient

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from fastapi.testclient import TestClient

    from uoa_detector.domain.events import OptionsPrint

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_LEGACY = load_live_legacy_scores(_REPO / "profiles" / "v5_default.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_DEADBAND = _SETTINGS.evidence.flow_net_premium_deadband_usd

_CONFIRMING = {
    "dealer_gamma": "full_short_and_proximate",
    "dark_pool": "confirmed_match",
    "sector": "strong",
    "price_confirmation": "neutral",
}
# ticker, premium, combined score, telemetry branches by family, bullish tape net (None: no tape)
_BOARD: list[tuple[str, str, float, dict[str, str], float | None]] = [
    ("AAA", "300000", 0.6137, {"sector": "strong"}, None),  # 1 lehte · 0 aleyhte · 5 bilinmiyor
    ("BBB", "200000", 0.5219, {**_CONFIRMING, "price_confirmation": "call_contrarian"}, None),  # 3 · 1 · 2
    ("CCC", "100000", 0.2345, _CONFIRMING, None),  # 3 · 0 · 2
    ("DDD", "50000", 0.4321, _CONFIRMING, _DEADBAND + 150_000),  # 4 · 0 · 1: Güçlü
    ("EEE", "10000", 0.9876, _CONFIRMING, None),  # 3 · 0 · 2; highest score, lowest premium
]
_EVIDENCE_ORDER = ["DDD", "CCC", "EEE", "BBB", "AAA"]
_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)
_ROW = re.compile(r'data-row data-ticker="([^"]+)" data-direction="([^"]+)"')
_CHIP = re.compile(
    r'<span class="([^"]*)"\s+data-family="(\w+)" data-family-state="(\w+)"(.*?)'
    r'(?=<span class="px-2 py-0.5 rounded border|</div>)',
    re.S,
)


def _print(i: int) -> OptionsPrint:
    ticker, premium, _score, _branches, _tape = _BOARD[i]
    return build_print(
        event_id=f"e{i}", ts=_TS + timedelta(seconds=i), ticker=ticker, option_type="call",
        strike="100", dte=3, premium=premium,
    )


def _prints() -> list[BoardPrint]:
    out: list[BoardPrint] = []
    for i, (_ticker, _premium, score, _branches, _tape) in enumerate(_BOARD):
        sig = BacktestStore().add(
            EnrichedEvent(print=_print(i), combined_score_post_penalty=score),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
        out.append(
            BoardPrint(
                run_id=_RUN, event_id=f"e{i}", signal=sig,
                meta=PrintMetaView(fill_side="at_ask", option_chain=None),
            ),
        )
    return out


def _inputs() -> EvidenceInputs:
    telemetry: dict[str, Mapping[str, StageTelemetryView]] = {}
    tapes: dict[Any, TapeSummary] = {}
    for i, (ticker, _premium, _score, branches, tape) in enumerate(_BOARD):
        telemetry[f"e{i}"] = {
            STAGE_BY_FAMILY[family]: StageTelemetryView(
                stage_name=STAGE_BY_FAMILY[family], branch=branch, degraded=False,
            )
            for family, branch in branches.items()
        }
        if tape is not None:
            tapes[(ticker, _TS.date())] = TapeSummary(
                ticker=ticker, trade_date=_TS.date(), net_call_premium=tape, net_put_premium=0.0,
                minutes=90, first_tape_time=_TS, last_tape_time=_NOW,
                fetched_at=_NOW - timedelta(seconds=30),
            )
    return EvidenceInputs(telemetry=telemetry, tapes=tapes, infos={})


class _Source:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[EvidenceRequest]]] = []
        self._fail = fail

    def __call__(self, run_id: str, requests: Sequence[EvidenceRequest]) -> EvidenceInputs:
        self.calls.append((run_id, list(requests)))
        if self._fail:
            msg = "evidence tables unavailable"
            raise RuntimeError(msg)
        return _inputs()


def _page(**kwargs: Any) -> Any:
    options: dict[str, Any] = {"now": _NOW, "evidence_source": _Source(), "legacy_scores": _LEGACY}
    options.update(kwargs)
    return build_alfa_page(_prints(), _SETTINGS, **options)


# ---------------------------------------------------------------------------
# Page model
# ---------------------------------------------------------------------------


def test_rows_sort_by_evidence_then_premium_never_by_score() -> None:
    page = _page(gate_on=False)
    (section,) = page.sections
    assert [v.row.ticker for v in section.views] == _EVIDENCE_ORDER
    assert [r.ticker for r in page.rows] == _EVIDENCE_ORDER
    assert {v.row.ticker: v.evidence.word for v in page.views} == {
        "DDD": "4 lehte · 0 aleyhte · 1 bilinmiyor",
        "CCC": "3 lehte · 0 aleyhte · 2 bilinmiyor",
        "EEE": "3 lehte · 0 aleyhte · 2 bilinmiyor",
        "BBB": "3 lehte · 1 aleyhte · 2 bilinmiyor",
        "AAA": "1 lehte · 0 aleyhte · 5 bilinmiyor",
    }


def test_strip_follows_the_profile_order_and_strength_passes_the_guard() -> None:
    views = {v.row.ticker: v for v in _page().views}
    assert [f.family for f in views["DDD"].evidence.families] == list(_SETTINGS.evidence.counted_families)
    assert {t: (v.strength_key, v.strength_text) for t, v in views.items()} == {
        "DDD": ("strong", STRONG_LABEL),
        "CCC": ("moderate", "Orta"),
        "EEE": ("moderate", "Orta"),
        "BBB": ("moderate", "Orta"),
        "AAA": ("weak", "Zayıf"),
    }


def test_each_gate_section_is_sorted_by_evidence() -> None:
    def quotes(symbols: Sequence[str]) -> tuple[dict[str, QuoteView], dict[str, Any]]:
        priced = {
            s: QuoteView(
                option_symbol=s, nbbo_bid=0.39, nbbo_ask=0.41, volume=500,
                last_tape_time=_NOW - timedelta(minutes=4), fetched_at=_NOW - timedelta(seconds=41),
                returned=True,
            )
            for s in symbols
            if s.startswith(("AAA", "CCC", "DDD"))
        }
        return priced, {}

    page = _page(spread_cutoff_pct=_CUTOFF, quote_source=quotes)
    assert [(s.key, [v.row.ticker for v in s.views]) for s in page.sections] == [
        ("main", ["DDD", "CCC", "AAA"]),
        ("no_quote", ["EEE", "BBB"]),
    ]


def test_evidence_source_gets_the_run_and_the_source_prints() -> None:
    source = _Source()
    _page(evidence_source=source)
    ((run_id, requests),) = source.calls
    assert run_id == _RUN
    assert sorted(r.event_id for r in requests) == ["e0", "e1", "e2", "e3", "e4"]
    assert {(r.ticker, r.trade_date) for r in requests} == {(t, _TS.date()) for t, *_ in _BOARD}


def test_failed_evidence_read_is_flagged_and_reads_unknown() -> None:
    page = _page(evidence_source=_Source(fail=True), gate_on=False)
    assert page.evidence_failed is True
    assert {f.state for v in page.views for f in v.evidence.families} == {"unknown"}
    assert {v.strength_key for v in page.views} == {"weak"}
    assert [r.ticker for r in page.rows] == ["AAA", "BBB", "CCC", "DDD", "EEE"]  # equal evidence: premium


def test_without_an_evidence_source_nothing_reads_clean() -> None:
    page = build_alfa_page(_prints(), _SETTINGS, now=_NOW)
    assert page.evidence_failed is False
    assert {f.state for v in page.views for f in v.evidence.families} == {"unknown"}


def test_audit_block_shows_the_unclamped_score_and_its_inputs() -> None:
    sig = _prints()[3].signal.model_copy(
        update={
            "combined_score_post_penalty": 1.0512,
            "combined_score_pre_penalty": -0.1234,
            "opening_closing_score": 0.7,
            "gamma_score": None,
        },
    )
    audit = build_audit("e3", sig)
    assert audit.score_text == "Birleşik skor (denetim, sınırsız ölçek): 1.05"
    assert audit.pre_text == "Ceza öncesi birleşik skor: -0.12"
    assert audit.source_text == "Kaynak baskı: e3 (baskın sözleşmenin en büyük baskısı)"
    inputs = dict(audit.inputs)
    assert list(inputs) == list(AUDIT_INPUT_LABELS.values())
    assert inputs["önceki seans OI değişimi (bu baskı değil)"] == "0.70"
    assert inputs["Dealer gamma"] == "bilinmiyor"
    missing = build_audit(None, None)
    assert missing.inputs == ()
    assert missing.score_text == "Birleşik skor (denetim, sınırsız ölçek): kayıt okunamadı"
    views = {v.row.ticker: v for v in _page().views}
    assert views["DDD"].audit.score_text == "Birleşik skor (denetim, sınırsız ölçek): 0.43"


def test_every_new_string_is_clean() -> None:
    texts: list[str] = [
        ALFA_COPY[k]
        for k in (
            "evidence_failed", "audit_summary", "audit_source", "audit_score", "audit_pre",
            "audit_inputs", "audit_record_missing",
        )
    ]
    texts.extend(AUDIT_INPUT_LABELS.values())
    texts.extend([EVIDENCE_HOVER, UNKNOWN_NOT_CLEAN])
    for page in (_page(), _page(evidence_source=_Source(fail=True)), build_alfa_page(_prints(), _SETTINGS)):
        for view in page.views:
            texts.extend(
                [view.evidence.word, view.strength_text, view.audit.source_text,
                 view.audit.score_text, view.audit.pre_text],
            )
            texts.extend(f.text for f in view.evidence.families)
            texts.extend(f"{label}: {value}" for label, value in view.audit.inputs)
    for text in texts:
        assert ensure_clean(text) == text


# ---------------------------------------------------------------------------
# Render on a seeded sqlite run
# ---------------------------------------------------------------------------


def _seed(url: str) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(_BOARD) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, (_ticker, _premium, score, _branches, _tape) in enumerate(_BOARD):
            store.add(
                EnrichedEvent(print=_print(i), combined_score_post_penalty=score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        ensure_telemetry_tables(engine)
        ensure_netprem_tables(engine)
        with Session(engine) as session:
            for i, (ticker, _premium, _score, branches, _tape) in enumerate(_BOARD):
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=f"e{i}", ticker=ticker, option_chain=None,
                        fill_side="at_ask", option_type="call", strike="100",
                        expiry=(_TS + timedelta(days=3)).date(), print_ts=_TS, written_at=_TS,
                    ),
                )
                session.add_all(
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id=f"e{i}", stage_name=STAGE_BY_FAMILY[family],
                        branch=branch, metadata_json=None, degraded=False,
                        profile_content_hash="h", written_at=_TS,
                    )
                    for family, branch in branches.items()
                )
            session.commit()
        for ticker, _premium, _score, _branches, tape in _BOARD:
            if tape is None:
                continue
            minute = TapeMinute(
                trade_date=_TS.date(), tape_time=_TS, net_call_premium=tape, net_put_premium=0.0,
                net_call_volume=None, net_put_volume=None, call_volume=None, put_volume=None, net_delta=None,
            )
            upsert_tape(engine, ticker, [minute], fetched_at=datetime.now(UTC))
    finally:
        engine.dispose()


def _reset(m: Any) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = f"sqlite:///{tmp_path / 'board.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    import webapp.main as m

    _reset(m)
    _seed(url)
    yield authed_client(m.app, monkeypatch)
    _reset(m)


def _articles(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in body.split("<article")[1:]:
        found = re.search(r'data-ticker="([^"]+)"', chunk)
        assert found is not None
        out[found.group(1)] = chunk.split("</article>")[0]
    return out


def test_render_orders_rows_by_evidence_and_every_word_carries_the_hover(client: TestClient) -> None:
    body = client.get("/alfa", params={"gate": "off"}).text
    assert [ticker for ticker, _direction in _ROW.findall(body)] == _EVIDENCE_ORDER
    words = re.findall(r'title="([^"]*)" data-evidence-word>([^<]*)<', body)
    assert len(words) == len(_BOARD)
    assert {title for title, _word in words} == {EVIDENCE_HOVER}
    assert html.unescape(words[0][1]) == "4 lehte · 0 aleyhte · 1 bilinmiyor"
    articles = _articles(body)
    assert f'data-strength="strong">{STRONG_LABEL}<' in articles["DDD"]
    assert "Akış: lehte · bu yönde gün içi net prim +$400,000" in html.unescape(articles["DDD"])


def test_unknown_and_out_of_scope_chips_are_dashed_and_say_not_clean(client: TestClient) -> None:
    body = client.get("/alfa", params={"gate": "off"}).text
    chips = _CHIP.findall(body)
    assert len(chips) == len(_BOARD) * len(_SETTINGS.evidence.counted_families)
    states = set()
    for classes, _family, state, content in chips:
        states.add(state)
        if state in ("unknown", "out_of_scope"):
            assert "border-dashed" in classes
            assert UNKNOWN_NOT_CLEAN in content
        else:
            assert "border-dashed" not in classes
            assert UNKNOWN_NOT_CLEAN not in content
    assert states == {"supporting", "against", "neutral", "unknown"}


def test_combined_score_appears_only_in_the_audit_block(client: TestClient) -> None:
    body = client.get("/alfa", params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    articles = _articles(body)
    for ticker, _premium, score, _branches, _tape in _BOARD:
        text = f"{score:.2f}"
        assert text not in outside, ticker
        (audit,) = _AUDIT.findall(articles[ticker])
        assert f"Birleşik skor (denetim, sınırsız ölçek): {text}" in audit


def test_render_refuses_strong_with_three_or_more_unknown_families(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence_module, "classify_strength", lambda counts, settings: "strong")
    articles = _articles(client.get("/alfa", params={"gate": "off"}).text)
    assert STRONG_LABEL not in articles["AAA"]  # 5 unknown families
    assert 'data-strength="strong"' not in articles["AAA"]
    for ticker in ("DDD", "CCC", "EEE", "BBB"):  # 2 or fewer unknown families
        assert f'data-strength="strong">{STRONG_LABEL}<' in articles[ticker]


def test_render_flags_a_failed_evidence_read(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(engine: object, run_id: str, requests: object) -> Any:
        msg = "evidence tables unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(page_module, "read_evidence_inputs", _broken)
    body = client.get("/alfa", params={"gate": "off"}).text
    assert 'data-state="evidence-failed"' in body
    assert set(re.findall(r'data-family-state="(\w+)"', body)) == {"unknown"}
    assert STRONG_LABEL not in body


def test_evidence_render_makes_zero_unusual_whales_calls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"GET /alfa must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    for params in ({}, {"gate": "off"}):
        response = client.get("/alfa", params=params)
        assert response.status_code == 200
        assert "data-evidence-strip" in response.text
    assert calls == []
