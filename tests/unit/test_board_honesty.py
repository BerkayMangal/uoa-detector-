"""Phase 5.2.A7: honesty render tests for the rules of contract section 2 on /, /alfa and /gamma.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 (R-EV1, R-EV2, R-UN1,
R-UN2, R-CA1, R-CA2, R-CO1, R-CO2, R-IV1, R-EM1, R-WD1), §5 A7; decision P12.
R-DL1 (delayed families) belongs to FAZ D and is not rendered yet.

A tmp sqlite database is seeded with a live run whose five rows exercise the
states the rules speak about:

- AAA: a bought call, four lehte families, İŞLENİR: the only clean candidate;
- BBB: an aleyhte family, İŞLENMEZ (17% spread);
- CCC: a legacy row without telemetry and without a quote (six unknowns);
- DDD: a sold put (so ``yukarı``), DAR (6% spread), a flipped sector read;
- SPY: an ETF whose ``no_sector`` reads ``kapsam-dışı``.

Telemetry, the net-premium tape, ticker info, quotes and depth go straight
into their ``alfa_*`` tables, and a gamma row with rich IV feeds the vol board
and /gamma.
"""

from __future__ import annotations

import html
import inspect
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import webapp.board.evidence as evidence_module
from sqlalchemy.orm import Session
from webapp.board.alfa_page import build_alfa_page, is_clean_candidate, load_spread_cutoff_pct
from webapp.board.copy_tr import (
    COUNTER_LEAD,
    EVIDENCE_HOVER,
    IV_NOT_SELL_VOL,
    NO_CLEAN_CANDIDATE,
    NO_COUNTER_FOUND,
    STRONG_LABEL,
    UNKNOWN_NOT_CLEAN,
)
from webapp.board.db import make_engine
from webapp.board.evidence import STAGE_BY_FAMILY, EvidenceCounts, evidence_sort_key
from webapp.board.honesty import forbidden_words
from webapp.board.netprem import TapeMinute, ensure_netprem_tables, upsert_tape
from webapp.board.quotes import (
    DepthSnapshot,
    QuoteSnapshot,
    ensure_quotes_tables,
    upsert_depths,
    upsert_quotes,
)
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.telemetry import AlfaPrintMeta, AlfaStageTelemetry, ensure_telemetry_tables
from webapp.board.ticker_info import (
    TickerInfoSnapshot,
    ensure_ticker_info_tables,
    upsert_ticker_infos,
)
from webapp.board.tradability import QuoteView, assess_tradability

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
    from collections.abc import Callable, Iterator, Sequence

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_REPO / "profiles" / "v5_default.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_BOARD_PAGES = ("/", "/alfa")
_ALL_PAGES = ("/", "/alfa", "/gamma")
_CONFIRMING = {
    "dealer_gamma": "full_short_and_proximate",
    "dark_pool": "confirmed_match",
    "sector": "strong",
    "price_confirmation": "neutral",
}


@dataclass(frozen=True)
class _Row:
    event_id: str
    ticker: str
    option_type: str
    fill_side: str
    premium: str
    score: float
    branches: dict[str, str] = field(default_factory=dict)  # empty: a legacy row without telemetry
    tape: float | None = None  # bullish net premium of the day
    quote: tuple[float, float] | None = None  # (bid, ask)
    issue_type: str | None = None

    @property
    def symbol(self) -> str:
        kind = "C" if self.option_type == "call" else "P"
        return f"{self.ticker}260918{kind}00100000"


_ROWS = (
    _Row("e1", "AAA", "call", "at_ask", "300000", 0.4321, _CONFIRMING, tape=400_000.0, quote=(0.39, 0.41)),
    _Row("e2", "BBB", "call", "at_ask", "200000", 0.6137,
         {**_CONFIRMING, "price_confirmation": "call_contrarian"}, quote=(1.83, 2.17)),
    _Row("e3", "CCC", "call", "at_ask", "100000", 0.8888),
    _Row("e4", "DDD", "put", "at_bid", "50000", 0.2345, {"sector": "contrarian"}, quote=(0.97, 1.03)),
    _Row("e5", "SPY", "call", "at_ask", "40000", 0.5219, {"sector": "no_sector"}, issue_type="ETF"),
)
_GATE_ON_ORDER = ["AAA", "DDD", "SPY", "CCC", "BBB"]  # main (İŞLENİR, DAR), kotasyon yok, İŞLENMEZ

_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)
_ARTICLE = re.compile(
    r'<article[^>]*data-row data-ticker="([^"]+)" data-direction="([^"]+)"[^>]*>(.*?)</article>', re.S,
)
_CHIP = re.compile(
    r'<span class="([^"]*)"\s+data-family="(\w+)" data-family-state="(\w+)"(.*?)'
    r'(?=<span class="px-2 py-0.5 rounded border|</div>)',
    re.S,
)


def _seed(url: str, rows: Sequence[_Row], *, gamma_row: bool) -> None:
    store = SqliteBacktestStore(url, flush_threshold=len(rows) + 1)
    try:
        store.start_run(profile=load_default_profile(), universe_id="live", run_id=_RUN)
        for i, row in enumerate(rows):
            pr = build_print(
                event_id=row.event_id, ts=_TS + timedelta(seconds=i), ticker=row.ticker,
                option_type=row.option_type,  # type: ignore[arg-type]
                strike="100", dte=3, premium=row.premium,
            )
            store.add(
                EnrichedEvent(print=pr, combined_score_post_penalty=row.score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    now = datetime.now(UTC)
    engine = make_engine(url)
    try:
        for ensure in (ensure_telemetry_tables, ensure_quotes_tables, ensure_netprem_tables, ensure_ticker_info_tables):
            ensure(engine)
        with Session(engine) as session:
            for row in rows:
                session.add(
                    AlfaPrintMeta(
                        run_id=_RUN, event_id=row.event_id, ticker=row.ticker, option_chain=row.symbol,
                        fill_side=row.fill_side, option_type=row.option_type, strike="100",
                        expiry=(_TS + timedelta(days=3)).date(), print_ts=_TS, written_at=_TS,
                    ),
                )
                session.add_all(
                    AlfaStageTelemetry(
                        run_id=_RUN, event_id=row.event_id, stage_name=STAGE_BY_FAMILY[family], branch=branch,
                        metadata_json=None, degraded=False, profile_content_hash="h", written_at=_TS,
                    )
                    for family, branch in row.branches.items()
                )
            session.commit()
        priced = [r for r in rows if r.quote is not None]
        upsert_quotes(
            engine,
            [
                QuoteSnapshot(
                    option_symbol=r.symbol, ticker=r.ticker, nbbo_bid=r.quote[0], nbbo_ask=r.quote[1],  # type: ignore[index]
                    last_price=r.quote[1], volume=500, open_interest=1000,  # type: ignore[index]
                    last_tape_time=now - timedelta(minutes=4), returned=True,
                )
                for r in priced
            ],
            fetched_at=now - timedelta(seconds=41),
        )
        upsert_depths(
            engine,
            [
                DepthSnapshot(
                    option_symbol=r.symbol, nbbo_bid_size=137, nbbo_ask_size=108,
                    nbbo_bid_time=now - timedelta(minutes=4), nbbo_ask_time=now - timedelta(minutes=4),
                )
                for r in priced
            ],
            fetched_at=now - timedelta(seconds=60),
        )
        for row in rows:
            if row.tape is not None:
                minute = TapeMinute(
                    trade_date=_TS.date(), tape_time=_TS, net_call_premium=row.tape, net_put_premium=0.0,
                    net_call_volume=None, net_put_volume=None, call_volume=None, put_volume=None, net_delta=None,
                )
                upsert_tape(engine, row.ticker, [minute], fetched_at=now)
        upsert_ticker_infos(
            engine,
            [TickerInfoSnapshot(r.ticker, r.issue_type, None) for r in rows if r.issue_type is not None],
            fetched_at=now,
        )
    finally:
        engine.dispose()
    if gamma_row:
        from webapp.gamma import GammaRepo

        GammaRepo().upsert("TSLA", "2026-09-15", {
            "spot": 250.0, "net_gex": 1.0, "flip": 240.0, "call_wall": 260.0, "put_wall": 240.0,
            "atm_iv": 0.4, "iv_pct": 0.9,
        })


def _reset(m: Any) -> None:
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., TestClient]]:
    import webapp.main as m

    def _make(rows: Sequence[_Row] = _ROWS, *, gamma_row: bool = True, name: str = "board") -> TestClient:
        url = f"sqlite:///{tmp_path / f'{name}.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.delenv("LIVE_TICKERS", raising=False)
        _reset(m)
        _seed(url, rows, gamma_row=gamma_row)
        return authed_client(m.app, monkeypatch)

    yield _make
    _reset(m)


@pytest.fixture
def seeded(board: Callable[..., TestClient]) -> TestClient:
    return board()


def _rows(body: str) -> dict[str, str]:
    return {ticker: inner for ticker, _direction, inner in _ARTICLE.findall(body)}


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


# ---------------------------------------------------------------------------
# A7: the switch
# ---------------------------------------------------------------------------


def test_root_serves_the_board_and_alfa_is_an_alias(seeded: TestClient) -> None:
    root = seeded.get("/")
    alias = seeded.get("/alfa")
    assert root.status_code == alias.status_code == 200
    assert 'id="alfa-board"' in root.text and 'id="alfa-board"' in alias.text
    assert list(_rows(root.text)) == list(_rows(alias.text)) == _GATE_ON_ORDER
    assert 'action="/" data-form="gate"' in root.text
    assert 'action="/alfa" data-form="gate"' in alias.text
    assert '<a href="/" ' in root.text
    assert ">Screener<" not in root.text
    assert 'href="/alfa"' not in root.text


def test_per_print_cards_score_controls_and_conviction_are_gone(seeded: TestClient) -> None:
    body = seeded.get("/", params={"sort": "score", "min_score": "0.5", "ticker": "AAA", "label": "SWEEP_UOA"}).text
    assert list(_rows(body)) == _GATE_ON_ORDER  # the old score sort and filters are ignored
    for removed in (
        "Notable flow", 'name="sort"', 'name="min_score"', 'name="ticker"', 'name="label"',
        "Conviction", "Combined score (high", "BULLISH", "BEARISH", "How to read a card",
    ):
        assert removed not in body, removed
    assert "Vol-premium board" in body  # the vol board stays as a section
    assert "setTimeout" in body  # a live run reloads once per refresh cadence
    assert f"{_SETTINGS.refresh.cadence_seconds * 1000}" in body


# ---------------------------------------------------------------------------
# R-EV1, R-EV2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _BOARD_PAGES)
def test_r_ev1_every_evidence_word_says_it_is_not_a_probability(seeded: TestClient, path: str) -> None:
    body = seeded.get(path).text
    words = re.findall(r'title="([^"]*)" data-evidence-word>([^<]*)<', body)
    assert len(words) == len(_rows(body)) == len(_ROWS)
    assert {title for title, _word in words} == {EVIDENCE_HOVER}
    assert all(re.fullmatch(r"\d lehte · \d aleyhte · \d bilinmiyor", html.unescape(w)) for _t, w in words)
    text = html.unescape(body)
    assert text.count("olasılı") == len(_ROWS)  # only in the hover
    assert "beklenen değer" not in text.lower()


@pytest.mark.parametrize("path", _BOARD_PAGES)
def test_r_ev2_the_score_appears_only_in_the_audit_block(seeded: TestClient, path: str) -> None:
    body = seeded.get(path, params={"gate": "off"}).text
    outside = _AUDIT.sub("", body)
    rows = _rows(body)
    for spec in _ROWS:
        score = f"{spec.score:.2f}"
        assert score not in outside, spec.ticker
        (audit,) = _AUDIT.findall(rows[spec.ticker])
        assert f"Birleşik skor (denetim, sınırsız ölçek): {score}" in _text(audit)


def test_r_ev2_the_score_is_never_a_sort_key() -> None:
    assert list(inspect.signature(evidence_sort_key).parameters) == ["counts", "total_premium"]

    def page(scores: tuple[float, float]) -> list[str]:
        prints = []
        for (ticker, premium), score in zip((("AAA", "300000"), ("BBB", "100000")), scores, strict=True):
            pr = build_print(event_id=ticker, ts=_TS, ticker=ticker, strike="100", dte=3, premium=premium)
            sig = BacktestStore().add(
                EnrichedEvent(print=pr, combined_score_post_penalty=score),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
            prints.append(BoardPrint(run_id=_RUN, event_id=ticker, signal=sig, meta=PrintMetaView("at_ask", None)))
        return [r.ticker for r in build_alfa_page(prints, _SETTINGS, gate_on=False).rows]

    assert page((0.1, 0.9)) == page((0.9, 0.1)) == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# R-UN1, R-UN2
# ---------------------------------------------------------------------------


def test_r_un1_unknown_and_out_of_scope_are_dashed_uncounted_and_not_clean(seeded: TestClient) -> None:
    rows = _rows(seeded.get("/", params={"gate": "off"}).text)
    for ticker, row in rows.items():
        chips = _CHIP.findall(row)
        assert len(chips) == len(_SETTINGS.evidence.counted_families), ticker
        counts = {"supporting": 0, "against": 0, "unknown": 0}
        for classes, _family, state, content in chips:
            dimmed = state in ("unknown", "out_of_scope")
            assert ("border-dashed" in classes) is dimmed, (ticker, state)
            assert (UNKNOWN_NOT_CLEAN in content) is dimmed, (ticker, state)
            if state in counts:
                counts[state] += 1
        word = re.search(r"data-evidence-word>([^<]*)<", row)
        assert word is not None
        assert html.unescape(word.group(1)) == (
            f"{counts['supporting']} lehte · {counts['against']} aleyhte · {counts['unknown']} bilinmiyor"
        )
    assert 'data-family="sector" data-family-state="out_of_scope"' in rows["SPY"]


def test_r_un2_strong_is_refused_at_render_with_three_or_more_unknowns(
    board: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = board()
    plain = _rows(client.get("/").text)
    assert f'data-strength="strong">{STRONG_LABEL}<' in plain["AAA"]  # one unknown family
    monkeypatch.setattr(evidence_module, "classify_strength", lambda counts, settings: "strong")
    forced = _rows(client.get("/").text)
    for ticker in ("CCC", "DDD", "SPY"):  # five or six unknown families
        assert STRONG_LABEL not in forced[ticker], ticker
        assert 'data-strength="strong"' not in forced[ticker], ticker


# ---------------------------------------------------------------------------
# R-CA1, R-CA2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _BOARD_PAGES)
def test_r_ca1_every_row_has_ama_or_the_explicit_fallback(seeded: TestClient, path: str) -> None:
    counters: dict[str, str] = {}
    for ticker, row in _rows(seeded.get(path).text).items():
        found = re.search(r"<p data-counter>([^<]*)</p>", row)
        assert found is not None, ticker
        counter = html.unescape(found.group(1))
        counters[ticker] = counter
        if counter == NO_COUNTER_FOUND:
            checked = re.search(r"<p data-checked>([^<]*)</p>", row)
            assert checked is not None, ticker
            assert html.unescape(checked.group(1)).startswith("Bakılanlar: ")
        else:
            assert counter.startswith(f"{COUNTER_LEAD} "), ticker
    assert counters["AAA"] == NO_COUNTER_FOUND
    assert counters["BBB"] == "AMA maliyet: BBB %17 makas (İŞLENMEZ)."
    assert counters["DDD"] == "AMA maliyet: DDD %6 makas (DAR)."
    assert counters["CCC"].startswith("AMA 6 aile bilinmiyor (")


def test_r_ca2_the_bear_column_matches_the_bull_column(seeded: TestClient) -> None:
    for ticker, row in _rows(seeded.get("/").text).items():
        bull = re.search(r'<div class="([^"]*)" data-case="bull">', row)
        bear = re.search(r'<div class="([^"]*)" data-case="bear">', row)
        grid = re.search(r'<div class="([^"]*)" data-cases>', row)
        assert bull is not None and bear is not None and grid is not None, ticker
        assert bull.group(1) == bear.group(1), ticker
        assert "md:grid-cols-2" in grid.group(1)


# ---------------------------------------------------------------------------
# R-CO1, R-CO2
# ---------------------------------------------------------------------------


def test_r_co1_cost_uses_ask_bid_and_commission_never_mid(seeded: TestClient) -> None:
    rows = _rows(seeded.get("/").text)
    commission = _SETTINGS.cost.commission_per_contract_usd
    bid, ask = _ROWS[0].quote  # type: ignore[misc]
    assert f"data-round-trip>${(ask - bid) * 100 + 2 * commission:.2f}<" in rows["AAA"]
    assert "bid $0.39 / ask $0.41" in _text(rows["AAA"])
    for ticker, row in rows.items():
        chip = row[row.index("data-chip="): row.index("data-cases")]
        assert "mid" not in chip.lower(), ticker
        assert "orta fiyat" not in _text(chip).lower(), ticker


@pytest.mark.parametrize("path", _ALL_PAGES)
def test_r_co2_no_live_price_wording(seeded: TestClient, path: str) -> None:
    assert "canlı alınabilir fiyat" not in html.unescape(seeded.get(path).text)


def test_r_co2_every_quote_shows_its_age(seeded: TestClient) -> None:
    rows = _rows(seeded.get("/", params={"gate": "off"}).text)
    priced = sorted(t for t, row in rows.items() if "data-bid-ask" in row)
    assert priced == ["AAA", "BBB", "DDD"]
    for ticker in priced:
        age = re.search(r"data-quote-age>([^<]*)<", rows[ticker])
        assert age is not None, ticker
        assert re.search(r"kotasyon \d+ sn önce alındı", html.unescape(age.group(1))), ticker


# ---------------------------------------------------------------------------
# R-IV1
# ---------------------------------------------------------------------------


def test_r_iv1_on_every_iv_surface_with_and_without_rows(board: Callable[..., TestClient]) -> None:
    with_rows = board()
    root = html.unescape(with_rows.get("/").text)
    assert "TSLA" in root and "IV-rank" in root  # IV richness is on the page
    for path in _ALL_PAGES:
        assert IV_NOT_SELL_VOL in html.unescape(with_rows.get(path).text), path
    without_rows = board(gamma_row=False, name="no-gamma")
    for path in _ALL_PAGES:
        assert IV_NOT_SELL_VOL in html.unescape(without_rows.get(path).text), path


# ---------------------------------------------------------------------------
# R-EM1
# ---------------------------------------------------------------------------


def test_r_em1_a_clean_candidate_suppresses_the_banner(seeded: TestClient) -> None:
    for path in _BOARD_PAGES:
        body = seeded.get(path).text
        assert NO_CLEAN_CANDIDATE not in body
        assert 'data-state="no-clean-candidate"' not in body
    body = seeded.get("/").text
    assert re.search(r'data-ticker="AAA" data-direction="up" data-clean-candidate="true"', body)
    assert len(re.findall(r'data-clean-candidate="true"', body)) == 1


def test_r_em1_no_clean_candidate_is_a_first_class_state(
    board: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webapp.main as m

    # Phase 5.2.A-fix4 (D10, review FA-04): "Bugün" now requires the run's session to be
    # today's ET date, so the page clock is pinned to the seeded session day.
    monkeypatch.setattr(m, "_now", lambda: _TS + timedelta(hours=1))
    client = board([r for r in _ROWS if r.ticker != "AAA"])
    for path in _BOARD_PAGES:
        body = client.get(path).text
        assert 'data-state="no-clean-candidate"' in body, path
        assert NO_CLEAN_CANDIDATE in body
        assert len(_rows(body)) == len(_ROWS) - 1  # the rows are still shown


def _chip(state_quote: tuple[float, float] | None) -> Any:
    quote = (
        None
        if state_quote is None
        else QuoteView(
            option_symbol="AAA260918C00100000", nbbo_bid=state_quote[0], nbbo_ask=state_quote[1], volume=500,
            last_tape_time=None, fetched_at=datetime.now(UTC), returned=True,
        )
    )
    return assess_tradability(
        "AAA", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=_CUTOFF,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("quote", "counts", "clean"),
    [
        ((0.39, 0.41), EvidenceCounts(1, 0, 2), True),
        ((0.39, 0.41), EvidenceCounts(0, 0, 0), False),
        ((0.39, 0.41), EvidenceCounts(4, 1, 0), False),
        ((0.39, 0.41), EvidenceCounts(4, 0, 3), False),
        ((0.97, 1.03), EvidenceCounts(4, 0, 0), False),  # DAR is not clean
        ((1.83, 2.17), EvidenceCounts(4, 0, 0), False),  # İŞLENMEZ
        (None, EvidenceCounts(4, 0, 0), False),  # kotasyon yok
    ],
)
def test_r_em1_clean_candidate_definition(
    quote: tuple[float, float] | None, counts: EvidenceCounts, clean: bool,
) -> None:
    cutoffs = _SETTINGS.clean_candidate
    assert (cutoffs.min_supporting, cutoffs.max_against, cutoffs.max_unknown) == (1, 0, 2)
    assert is_clean_candidate(_chip(quote), counts, cutoffs) is clean


def test_r_em1_banner_is_never_claimed_for_a_failed_read() -> None:
    assert build_alfa_page(None, _SETTINGS).no_clean_candidate is False
    assert build_alfa_page([], _SETTINGS).no_clean_candidate is True


# ---------------------------------------------------------------------------
# R-WD1 and render hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _ALL_PAGES)
@pytest.mark.parametrize("gate", ["", "off"])
def test_r_wd1_rendered_html_has_no_forbidden_words(seeded: TestClient, path: str, gate: str) -> None:
    body = seeded.get(path, params={"gate": gate} if gate else {}).text
    assert forbidden_words(html.unescape(body)) == []


def test_root_and_gamma_make_zero_unusual_whales_calls(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _no_uw(self: UnusualWhalesClient, path: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        msg = f"a page render must not call Unusual Whales ({path})"
        raise AssertionError(msg)

    monkeypatch.setattr(UnusualWhalesClient, "request_json", _no_uw)
    for path in _ALL_PAGES:
        assert seeded.get(path).status_code == 200
    assert calls == []
