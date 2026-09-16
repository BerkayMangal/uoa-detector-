"""Phase 5.2.A6: the penalty ledger (``webapp/board/penalty_ledger.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A6 (and §2 R-EV2,
R-WD1; §5 A5 priority 6).

Pins:
  - the ledger names exactly the eight penalties the scoring engine can apply,
    plus the M24 adjustment, with frozen Turkish names;
  - statuses per the contract: ``uygulandı`` with the recorded value and
    reason; ``uygulanmadı`` for thin_oi; ``canlı yolda ölçülmüyor`` for the
    other seven, each with the contract's evidence; ``kaydedilmedi`` for M24,
    with the stage telemetry note when it exists and is not degraded;
  - an applied name the ledger does not know is still listed;
  - numbers come from the profile that wrote the row, found by content hash
    among ``profiles/*.yaml`` (non-profile files are skipped); an unknown or
    unread hash shows no numbers;
  - the DB reader returns ``SignalRow.profile_content_hash`` per event;
  - page: applied penalties become the counter-argument's last priority and
    the fallback's checked list names the ledger;
  - render: the ledger sits inside the Denetim block with nine entries and the
    writing profile; its values never appear outside it;
  - every frozen and generated string passes ``ensure_clean``.
"""

from __future__ import annotations

import html
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board.alfa_page import build_alfa_page, load_spread_cutoff_pct
from webapp.board.copy_tr import NO_COUNTER_FOUND
from webapp.board.db import make_engine
from webapp.board.evidence import STAGE_BY_FAMILY, EvidenceInputs, StageTelemetryView
from webapp.board.honesty import ensure_clean
from webapp.board.netprem import TapeSummary
from webapp.board.penalty_ledger import (
    CONFIG_TEMPLATES,
    EVIDENCE_NOTES,
    LEDGER_COPY,
    LIVE_STATUS,
    M24_KEY,
    PENALTY_NAMES,
    STATUS_LABELS,
    build_penalty_ledger,
    profiles_by_hash,
    read_profile_hashes,
)
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView
from webapp.board.tradability import QuoteView

from tests.conftest import build_enriched, build_print
from tests.unit._webapp_auth import authed_client
from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.backtest.store import BacktestStore
from uoa_detector.calibration import load_default_profile
from uoa_detector.domain.events import AppliedPenalty, EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket
from uoa_detector.scoring.penalties import PenaltyEngine

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

    from uoa_detector.backtest.store import StoredSignal

_REPO = Path(__file__).resolve().parents[2]
_PROFILES = _REPO / "profiles"
_SETTINGS = load_board_settings(_PROFILES / "board_v1.yaml")
_CUTOFF = load_spread_cutoff_pct(_PROFILES / "v5_default.yaml")
_DEFAULT = load_default_profile(profiles_dir=_PROFILES)
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)
_THIN_OI = AppliedPenalty(name="thin_oi", value=-0.2, reason="OI 50 below 100")
_WIDE = AppliedPenalty(name="wide_spread", value=-0.15, reason="Spread 20.0% > 15.0% of mid")
_SEVEN_UNMEASURED = (
    "post_gap_move", "iv_rank_high", "wide_spread", "flow_contradicts_price",
    "post_event", "isolated_print", "next_day_oi_failed",
)


def _signal(event_id: str = "p1", *, ticker: str = "AAA", penalties: list[AppliedPenalty] | None = None) -> StoredSignal:
    pr = build_print(event_id=event_id, ts=_TS, ticker=ticker, option_type="call", strike="100", dte=3, premium="100000")
    return BacktestStore().add(
        EnrichedEvent(print=pr, applied_penalties=list(penalties or [])),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )


def _entries(ledger: Any) -> dict[str, Any]:
    return {e.key: e for e in ledger.entries}


# ---------------------------------------------------------------------------
# Names and statuses
# ---------------------------------------------------------------------------


def test_ledger_names_the_eight_engine_penalties_and_m24() -> None:
    pr = build_print(bid="0.10", ask="0.50", option_price="0.30", open_interest=10, option_type="call")
    event = build_enriched(print_=pr)
    event.is_post_gap = True
    event.is_post_event = True
    event.is_isolated_print = True
    event.next_day_oi_confirmed = False
    event.price_direction = "down"
    PenaltyEngine(_DEFAULT).apply(event, iv_rank=90)
    engine_names = {p.name for p in event.applied_penalties}
    assert set(PENALTY_NAMES) == engine_names | {M24_KEY}
    assert set(LIVE_STATUS) == set(EVIDENCE_NOTES) == set(CONFIG_TEMPLATES) == set(PENALTY_NAMES)
    assert len(PENALTY_NAMES) == 9


def test_statuses_follow_the_contract_with_its_evidence() -> None:
    ledger = build_penalty_ledger(_signal(), profile_hash=None, profile=None)
    entries = _entries(ledger)
    assert [e.key for e in ledger.entries] == list(PENALTY_NAMES)
    assert entries["thin_oi"].status == "not_applied"
    assert entries["thin_oi"].status_label == "uygulanmadı"
    assert entries[M24_KEY].status_label == "kaydedilmedi"
    for key in _SEVEN_UNMEASURED:
        assert entries[key].status_label == "canlı yolda ölçülmüyor", key
    assert "orchestrator.py:213" in entries["iv_rank_high"].detail_text
    assert "flow_poll.py:190-192" in entries["wide_spread"].detail_text
    for key in ("post_gap_move", "post_event", "isolated_print", "flow_contradicts_price"):
        assert "sources/scenarios.py" in entries[key].detail_text, key
    assert "güncellenemiyor" in entries["next_day_oi_failed"].detail_text
    assert ledger.applied_names == ()


def test_applied_penalties_show_value_and_recorded_reason() -> None:
    ledger = build_penalty_ledger(_signal(penalties=[_WIDE, _THIN_OI]), profile_hash=None, profile=None)
    entries = _entries(ledger)
    assert entries["wide_spread"].status_label == "uygulandı"
    assert entries["wide_spread"].details == ("değer -0.15 · kayıtlı gerekçe: Spread 20.0% > 15.0% of mid",)
    assert entries["thin_oi"].details == ("değer -0.20 · kayıtlı gerekçe: OI 50 below 100",)
    assert ledger.applied_names == ("Geniş alış-satış makası", "Düşük açık pozisyon")  # ledger order


def test_an_unknown_applied_name_is_still_listed() -> None:
    novel = AppliedPenalty(name="new_rule", value=-0.05, reason="added later")
    ledger = build_penalty_ledger(_signal(penalties=[novel]), profile_hash=None, profile=None)
    last = ledger.entries[-1]
    assert (last.key, last.name, last.status) == ("new_rule", "tanınmayan ceza (new_rule)", "applied")
    assert ledger.applied_names == ("tanınmayan ceza (new_rule)",)


@pytest.mark.parametrize(
    ("telemetry", "note"),
    [
        (StageTelemetryView("m24_iv_exhaustion", "expensive", False, {"post_earnings_penalty_emitted": "yes"}),
         "aşama telemetrisi: bu baskıda ayar üretildi"),
        (StageTelemetryView("m24_iv_exhaustion", "cheap", False, {"post_earnings_penalty_emitted": "no"}),
         "aşama telemetrisi: bu baskıda ayar üretilmedi"),
        (StageTelemetryView("m24_iv_exhaustion", "expensive", True, {"post_earnings_penalty_emitted": "no"}), None),
        (StageTelemetryView("m24_iv_exhaustion", "no_iv_history", False, {"branch": "no_iv_history"}), None),
        (None, None),
    ],
)
def test_m24_entry_adds_the_stage_telemetry_note(telemetry: StageTelemetryView | None, note: str | None) -> None:
    entry = _entries(build_penalty_ledger(_signal(), profile_hash=None, profile=None, m24_telemetry=telemetry))[M24_KEY]
    assert entry.status == "not_recorded"
    assert entry.details[0] == EVIDENCE_NOTES[M24_KEY]
    assert (entry.details[1] if len(entry.details) > 1 else None) == note


# ---------------------------------------------------------------------------
# The profile that wrote the row
# ---------------------------------------------------------------------------


def test_numbers_come_from_the_profile_that_wrote_the_row() -> None:
    content_hash = _DEFAULT.content_hash()
    ledger = build_penalty_ledger(_signal(), profile_hash=content_hash, profile=_DEFAULT)
    entries = _entries(ledger)
    p, t, m24 = _DEFAULT.penalties, _DEFAULT.penalty_triggers, _DEFAULT.scoring.modules.m24
    assert entries["thin_oi"].details[-1] == f"ceza {p.thin_oi:.2f} · eşik OI < {t.thin_oi_threshold:g}"
    assert entries["thin_oi"].details[-1] == "ceza -0.20 · eşik OI < 100"
    assert entries["wide_spread"].details[-1] == "ceza -0.15 · eşik makas > %15"
    assert entries["flow_contradicts_price"].details[-1] == f"ceza {p.flow_contradicts_price:.2f}"
    assert entries[M24_KEY].details[-1] == (
        f"ayar {m24.post_earnings_iv_penalty:.2f} · IV sıralaması > "
        f"{m24.post_earnings_iv_penalty_threshold:g} ve {m24.post_earnings_session_days:g} seans içinde katalizör"
    )
    assert ledger.profile_text == f"Sayılar satırı yazan profilden: v5_default (hash {content_hash[:12]})"


def test_unknown_or_unread_profile_shows_no_numbers() -> None:
    missing = build_penalty_ledger(_signal(), profile_hash="f" * 64, profile=None)
    assert missing.profile_text == "Satırı yazan profil bulunamadı (hash ffffffffffff); sayılar gösterilmiyor."
    profile_numbers = re.compile(r"^(ceza|ayar) -?\d")
    assert all(not any(profile_numbers.match(d) for d in e.details) for e in missing.entries)
    unread = build_penalty_ledger(_signal(), profile_hash=None, profile=None)
    assert unread.profile_text == "Satırı yazan profil okunamadı; sayılar gösterilmiyor."


def test_profiles_are_found_by_content_hash_and_other_yaml_is_skipped(tmp_path: Path) -> None:
    for name in ("v5_default.yaml", "v5_gamma_squeeze.yaml", "board_v1.yaml"):
        shutil.copy(_PROFILES / name, tmp_path / name)
    (tmp_path / "broken.yaml").write_text("not: [a, profile", encoding="utf-8")
    found = profiles_by_hash(tmp_path)
    assert {p.profile_id for p in found.values()} == {"v5_default", "v5_gamma_squeeze"}
    assert found[_DEFAULT.content_hash()].profile_id == "v5_default"
    assert profiles_by_hash(tmp_path) is found  # loaded once per directory
    assert profiles_by_hash(_PROFILES)[_DEFAULT.content_hash()].profile_id == "v5_default"


def test_profile_hashes_are_read_from_the_signal_table(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'hashes.db'}"
    store = SqliteBacktestStore(url, flush_threshold=3)
    try:
        store.start_run(profile=_DEFAULT, universe_id="live", run_id=_RUN)
        for eid in ("h1", "h2"):
            pr = build_print(event_id=eid, ts=_TS, ticker="AAA", strike="100", dte=3)
            store.add(
                EnrichedEvent(print=pr),
                LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
                PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
            )
    finally:
        store.close()
    engine = make_engine(url)
    try:
        hashes = read_profile_hashes(engine, _RUN, ["h1", "h2", "nope", "h1"])
        other_run = read_profile_hashes(engine, "live-2026-09-14", ["h1"])
        empty = read_profile_hashes(engine, _RUN, [])
    finally:
        engine.dispose()
    assert hashes == {"h1": _DEFAULT.content_hash(), "h2": _DEFAULT.content_hash()}
    assert (other_run, empty) == ({}, {})


# ---------------------------------------------------------------------------
# Page: the ledger feeds the counter-argument (A5 priority 6)
# ---------------------------------------------------------------------------

_MEASURED = {
    "dealer_gamma": "full_short_and_proximate",
    "dark_pool": "confirmed_match",
    "sector": "strong",
    "price_confirmation": "neutral",
}


def _page() -> Any:
    prints = [
        BoardPrint(run_id=_RUN, event_id="a1", signal=_signal("a1", ticker="AAA", penalties=[_THIN_OI]),
                   meta=PrintMetaView(fill_side="at_ask", option_chain=None)),
        BoardPrint(run_id=_RUN, event_id="b1", signal=_signal("b1", ticker="BBB"),
                   meta=PrintMetaView(fill_side="at_ask", option_chain=None)),
    ]
    telemetry = {
        eid: {
            STAGE_BY_FAMILY[f]: StageTelemetryView(stage_name=STAGE_BY_FAMILY[f], branch=b, degraded=False)
            for f, b in _MEASURED.items()
        }
        for eid in ("a1", "b1")
    }
    tapes = {
        (t, _TS.date()): TapeSummary(
            ticker=t, trade_date=_TS.date(), net_call_premium=10 * _SETTINGS.evidence.flow_net_premium_deadband_usd,
            net_put_premium=0.0, minutes=90, first_tape_time=_TS, last_tape_time=_NOW, fetched_at=_NOW,
        )
        for t in ("AAA", "BBB")
    }

    def quotes(symbols: Any) -> Any:
        return {
            s: QuoteView(
                option_symbol=s, nbbo_bid=0.39, nbbo_ask=0.41, volume=500,
                last_tape_time=_NOW - timedelta(minutes=4), fetched_at=_NOW - timedelta(seconds=41), returned=True,
            )
            for s in symbols
        }, {}

    return build_alfa_page(
        prints, _SETTINGS, now=_NOW, spread_cutoff_pct=_CUTOFF, quote_source=quotes,
        evidence_source=lambda run_id, requests: EvidenceInputs(telemetry=telemetry, tapes=tapes, infos={}),
        profile_hash_source=lambda run_id, event_ids: dict.fromkeys(event_ids, _DEFAULT.content_hash()),
        profile_resolver=profiles_by_hash(_PROFILES).get,
    )


def test_applied_penalties_become_the_last_counter_argument() -> None:
    views = {v.row.ticker: v for v in _page().views}
    assert views["AAA"].narrative.counter == "AMA ceza defterinde uygulanan: Düşük açık pozisyon."
    assert views["AAA"].ledger.applied_names == ("Düşük açık pozisyon",)
    assert views["BBB"].narrative.counter == NO_COUNTER_FOUND
    # Phase 5.2.B3 (D10): build_alfa_page now always passes a ChaseCheck, so the fallback's
    # checked list names the chase check too (contract §5 A5: only checks that actually ran).
    assert views["BBB"].narrative.checked == (
        "Bakılanlar: maliyet (İŞLENİR), aleyhte aile (0), kovalama (hâlâ makul), "
        "bilinmeyen aile (1), ceza defteri (0 uygulanan)."
    )
    assert views["BBB"].ledger.profile_text.startswith("Sayılar satırı yazan profilden: v5_default")


def test_a_failed_profile_hash_read_shows_no_numbers() -> None:
    def broken(run_id: str, event_ids: Any) -> Any:
        msg = "signal table unavailable"
        raise RuntimeError(msg)

    page = build_alfa_page(
        [BoardPrint(run_id=_RUN, event_id="a1", signal=_signal("a1"), meta=None)], _SETTINGS,
        now=_NOW, profile_hash_source=broken, profile_resolver=profiles_by_hash(_PROFILES).get,
    )
    (view,) = page.views
    assert view.ledger.profile_text == LEDGER_COPY["profile_unread"]


# ---------------------------------------------------------------------------
# Render on a seeded sqlite run
# ---------------------------------------------------------------------------

_AUDIT = re.compile(r"<details[^>]*data-audit[^>]*>.*?</details>", re.S)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    store = SqliteBacktestStore(url, flush_threshold=2)
    try:
        store.start_run(profile=_DEFAULT, universe_id="live", run_id=_RUN)
        pr = build_print(event_id="r1", ts=_TS, ticker="AAA", option_type="call", strike="100", dte=3)
        store.add(
            EnrichedEvent(print=pr, applied_penalties=[_THIN_OI], combined_score_post_penalty=0.1111),
            LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
            PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
        )
    finally:
        store.close()
    import webapp.main as m

    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None
    yield authed_client(m.app, monkeypatch)
    m._REPO = m._JOURNAL = m._GAMMA = None
    m._BOARD_READER = None


def test_render_puts_the_ledger_inside_the_audit_block(client: TestClient) -> None:
    body = client.get("/alfa").text
    (audit,) = _AUDIT.findall(body)
    statuses = re.findall(r'data-penalty="(\w+)" data-penalty-status="(\w+)"', audit)
    assert len(statuses) == len(PENALTY_NAMES)
    assert dict(statuses)["thin_oi"] == "applied"
    assert dict(statuses)[M24_KEY] == "not_recorded"
    text = html.unescape(re.sub(r"<[^>]+>", "", audit))
    assert LEDGER_COPY["title"] in text
    assert f"Sayılar satırı yazan profilden: v5_default (hash {_DEFAULT.content_hash()[:12]})" in text
    assert "Düşük açık pozisyon: uygulandı · değer -0.20 · kayıtlı gerekçe: OI 50 below 100" in text
    outside = _AUDIT.sub("", body)
    assert "data-ledger" not in outside
    assert "-0.20" not in outside
    # Six unknown families (no telemetry, tape or confirmation) outrank the ledger (A5 priority 5 before 6).
    assert "AMA 6 aile bilinmiyor (" in html.unescape(outside)
    assert "AMA ceza defterinde" not in html.unescape(outside)


# ---------------------------------------------------------------------------
# Copy (R-WD1)
# ---------------------------------------------------------------------------


def test_every_ledger_string_is_clean() -> None:
    texts: list[str] = [
        *PENALTY_NAMES.values(), *STATUS_LABELS.values(), *EVIDENCE_NOTES.values(), *LEDGER_COPY.values(),
    ]
    for ledger in (
        build_penalty_ledger(_signal(penalties=[_WIDE, _THIN_OI]), profile_hash=_DEFAULT.content_hash(), profile=_DEFAULT),
        build_penalty_ledger(_signal(), profile_hash="0" * 64, profile=None),
        build_penalty_ledger(_signal(), profile_hash=None, profile=None),
    ):
        texts.append(ledger.profile_text)
        for entry in ledger.entries:
            texts.extend([entry.name, entry.status_label, entry.detail_text])
    for text in texts:
        assert ensure_clean(text) == text


def test_dictionaries_are_read_only() -> None:
    for mapping in (PENALTY_NAMES, STATUS_LABELS, LIVE_STATUS, EVIDENCE_NOTES, CONFIG_TEMPLATES, LEDGER_COPY):
        with pytest.raises(TypeError):
            mapping["x"] = "y"  # type: ignore[index]
