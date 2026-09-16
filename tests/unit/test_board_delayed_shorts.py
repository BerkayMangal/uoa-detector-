"""Phase 5.2.D3a: Short interest + FTD delayed families (contract §7 D3, decision P14).

Hermetic. Fixture rows are trimmed from the live responses captured 2026-09-15:
``/api/shorts/TSLA/interest-float/v2`` (si_float a fraction, market_date the
as-of date, short_shares_available stuck at 10,000,000) and
``/api/shorts/NVDA/ftds`` (fail date, quantity in shares, price a string).
No network, no env.
"""

from __future__ import annotations

import json
import string
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML
from sqlalchemy.engine import Engine
from webapp.board import delayed as dl
from webapp.board.daily_close import ClosePoint
from webapp.board.db import make_engine
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    FTDS_PATH,
    INSIDER_TRANSACTIONS_PATH,
    SHORT_INTEREST_PATH,
    build_delayed_evidence,
    load_delayed_records,
    parse_ftds,
    parse_short_interest,
    refresh_ftds,
    refresh_short_interest,
    run_delayed_job,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, DelayedSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_BOARD = Path(__file__).resolve().parents[2] / "profiles" / "board_v1.yaml"
_SETTINGS: DelayedSettings = load_board_settings(_BOARD).delayed
_TODAY = date(2026, 9, 15)  # SI window starts 2026-06-17 (90 d); FTD window 2026-07-17 (60 d)
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)


def _si(market_date: str, short_interest: int, total_float: int, si_float: str, synth: str,
        days_to_cover: str, fee_rate: str, rebate_rate: str) -> dict[str, Any]:
    return {
        "symbol": "TSLA", "short_interest": short_interest, "market_date": market_date,
        "short_shares_available": 10000000, "total_float": total_float, "si_float": si_float,
        "si_float_with_synth_long_pct_of_total_shares": synth, "days_to_cover": days_to_cover,
        "fee_rate": fee_rate, "rebate_rate": rebate_rate,
    }


_SI_ROWS = [  # newest first, as live
    _si("2026-08-31", 74230933, 3949547394, "0.01879479484478873935497835426",
        "0.01844806720636228527506555159", "2.04", "0.4081", "3.2219"),
    _si("2026-08-14", 69196896, 3949547394, "0.01752020905107285313411787862",
        "0.01721853668873268868768955688", "2.15", "0.2500", "3.3800"),
    _si("2026-07-31", 68501639, 3949547394, "0.01734417444998002725575091554",
        "0.01704848259376629662946176396", "1.55", "0.2782", "3.3518"),
    _si("2026-07-15", 70646375, 3949547394, "0.01788720781204531103292287774",
        "0.01757287808979736767509028991", "1.78", "0.2500", "3.3700"),
    _si("2026-06-30", 79109257, 3755723871, "0.02106365103431748004564630572",
        "0.02062912631644502680952118864", "1.72", "0.4027", "3.2173"),
    _si("2026-06-15", 78183135, 3755723871, "0.02081706155335188112395710254",
        "0.02039254861363217947597761843", "1.60", "0.2500", "3.3700"),  # outside the window
]

_FTD_ROWS = [  # newest first, only days with fails
    {"date": "2026-08-14", "quantity": 200, "price": "225.30"},
    {"date": "2026-08-13", "quantity": 9320, "price": "224.09"},
    {"date": "2026-08-10", "quantity": 74789, "price": "223.96"},
    {"date": "2026-07-27", "quantity": 548756, "price": "206.84"},
    {"date": "2026-07-17", "quantity": 9409, "price": "207.40"},   # first day inside
    {"date": "2026-07-16", "quantity": 117200, "price": "212.50"},  # outside
]


class _FakeClient:
    def __init__(self, responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        result = self.responses.get(path, {"data": []})
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'board.db'}")


_SI_PATH = SHORT_INTEREST_PATH.format(ticker="TSLA")
_FTD_PATH = FTDS_PATH.format(ticker="TSLA")


def _family(job: dl.DelayedJobResult, family: str) -> list[dl.DelayedFamilyResult]:
    return [r for r in job.results if r.family == family]


# ---------------------------------------------------------------------------
# Profile keys added in 5.2.D3a
# ---------------------------------------------------------------------------


def test_board_profile_pins_the_shorts_windows() -> None:
    assert (_SETTINGS.short_interest_lookback_days, _SETTINGS.ftd_lookback_days) == (90, 60)
    raw = YAML(typ="safe").load(_BOARD.read_text(encoding="utf-8"))
    for key in ("short_interest_lookback_days", "ftd_lookback_days"):
        broken = {**raw, "delayed": {**raw["delayed"], key: 0}}
        with pytest.raises(ValidationError):
            BoardSettings.model_validate(broken)
        missing = {**raw, "delayed": {k: v for k, v in raw["delayed"].items() if k != key}}
        with pytest.raises(ValidationError):
            BoardSettings.model_validate(missing)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_short_interest_records_per_as_of_date_inside_the_window() -> None:
    parsed = parse_short_interest(list(_SI_ROWS), ticker="tsla", today=_TODAY, settings=_SETTINGS)
    assert [r.dedupe_key for r in parsed.records] == [
        "2026-08-31", "2026-08-14", "2026-07-31", "2026-07-15", "2026-06-30",
    ]
    assert parsed.rows_skipped == 1
    latest = parsed.records[0]
    assert (latest.family, latest.ticker, latest.filed_or_asof_date) == ("short_interest", "TSLA", date(2026, 8, 31))
    assert (latest.transaction_date, latest.delay_days, latest.side) == (None, None, None)
    assert (latest.size_text, latest.size_low, latest.size_high) == (None, None, None)
    assert (latest.flag_late, latest.flag_executive, latest.flag_10b5_1, latest.form) == (None, None, None, None)
    payload = json.loads(latest.payload_json)
    assert "short_shares_available" not in payload  # ignored per the probe
    assert payload["si_float"] == "0.01879479484478873935497835426"  # stored as received


@pytest.mark.parametrize(
    "over",
    [{"si_float": None}, {"si_float": "n/a"}, {"si_float": "-0.01"}, {"symbol": "AAPL"},
     {"market_date": "2026-09-16"}, {"market_date": None}],
)
def test_unusable_short_interest_rows_are_skipped(over: dict[str, object]) -> None:
    row = {**_SI_ROWS[0], **over}
    assert parse_short_interest([row], ticker="TSLA", today=_TODAY, settings=_SETTINGS).records == ()


def test_short_interest_row_without_symbol_is_kept_for_the_requested_ticker() -> None:
    row = {k: v for k, v in _SI_ROWS[0].items() if k != "symbol"}
    [record] = parse_short_interest([row], ticker="TSLA", today=_TODAY, settings=_SETTINGS).records
    assert record.ticker == "TSLA"


def test_ftd_records_per_fail_date_inside_the_trailing_window() -> None:
    parsed = parse_ftds(list(_FTD_ROWS), ticker="nvda", today=_TODAY, settings=_SETTINGS)
    assert [r.dedupe_key for r in parsed.records] == [
        "2026-08-14", "2026-08-13", "2026-08-10", "2026-07-27", "2026-07-17",
    ]
    assert parsed.rows_skipped == 1
    first = parsed.records[0]
    assert (first.family, first.ticker, first.delay_days, first.side) == ("ftd", "NVDA", None, None)
    assert first.size_low == pytest.approx(200 * 225.30)
    assert first.size_high == first.size_low


@pytest.mark.parametrize(
    ("over", "kept", "usd"),
    [({"quantity": 0}, False, None), ({"quantity": "9320"}, True, 9320 * 224.09),
     ({"quantity": None}, False, None), ({"price": "n/a"}, True, None), ({"price": "0"}, True, None),
     ({"date": "bad"}, False, None)],
)
def test_ftd_numbers_are_parsed_defensively(over: dict[str, object], kept: bool, usd: float | None) -> None:
    records = parse_ftds([{**_FTD_ROWS[1], **over}], ticker="NVDA", today=_TODAY, settings=_SETTINGS).records
    assert bool(records) == kept
    if kept:
        assert records[0].size_low == (pytest.approx(usd) if usd is not None else None)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def test_job_fetches_v2_and_ftds_and_appends_idempotently(engine: Engine) -> None:
    client = _FakeClient({_SI_PATH: {"data": list(_SI_ROWS)}, _FTD_PATH: {"data": list(_FTD_ROWS)}})
    first = await run_delayed_job(client, engine, ["tsla"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [path for path, _ in client.calls] == [
        CONGRESS_RECENT_TRADES_PATH, INSIDER_TRANSACTIONS_PATH, _SI_PATH, _FTD_PATH,
    ]
    assert dict(client.calls)[_SI_PATH] is None
    assert dict(client.calls)[_FTD_PATH] is None
    assert not [p for p, _ in client.calls if p.endswith("/interest-float")]  # v1 is never called
    [si] = _family(first, "short_interest")
    [ftd] = _family(first, "ftd")
    assert (si.status, si.inserted, si.rows_skipped, si.truncated) == ("ok", 5, 1, False)
    assert (ftd.status, ftd.inserted, ftd.rows_skipped, ftd.truncated) == ("ok", 5, 1, False)

    revised = [{**_SI_ROWS[0], "si_float": "0.99"}, *_SI_ROWS[1:]]
    again = await run_delayed_job(  # type: ignore[arg-type]
        _FakeClient({_SI_PATH: {"data": revised}, _FTD_PATH: {"data": list(_FTD_ROWS)}}),
        engine, ["TSLA"], now=_NOW, settings=_SETTINGS,
    )
    assert [(r.inserted, r.already_stored) for r in _family(again, "short_interest")] == [(0, 5)]
    stored = {r.dedupe_key: r for r in load_delayed_records(engine, "TSLA") if r.family == "short_interest"}
    assert json.loads(stored["2026-08-31"].payload_json)["si_float"] == _SI_ROWS[0]["si_float"]  # never rewritten


async def test_refresh_functions_make_one_request_each(engine: Engine) -> None:
    client = _FakeClient({_SI_PATH: {"data": list(_SI_ROWS)}, _FTD_PATH: {"data": list(_FTD_ROWS)}})
    si = await refresh_short_interest(client, engine, "tsla", now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    ftd = await refresh_ftds(client, engine, "tsla", now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [path for path, _ in client.calls] == [_SI_PATH, _FTD_PATH]
    assert (si.family, si.inserted, ftd.family, ftd.inserted) == ("short_interest", 5, "ftd", 5)


async def test_a_single_object_body_counts_as_one_row(engine: Engine) -> None:
    client = _FakeClient({_SI_PATH: {"data": dict(_SI_ROWS[0])}})
    result = await refresh_short_interest(client, engine, "TSLA", now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert (result.status, result.inserted) == ("ok", 1)


async def test_shorts_error_policy(engine: Engine) -> None:
    not_found = _FakeClient({_SI_PATH: UnusualWhalesNotFoundError("404", status_code=404), _FTD_PATH: {"data": []}})
    job = await run_delayed_job(not_found, engine, ["TSLA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [r.status for r in (*_family(job, "short_interest"), *_family(job, "ftd"))] == ["no_data", "no_data"]

    for error in (UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("503"), CircuitBreakerOpenError("open")):
        degraded = await run_delayed_job(  # type: ignore[arg-type]
            _FakeClient({_SI_PATH: error, _FTD_PATH: {"data": list(_FTD_ROWS)}}), engine, ["TSLA"],
            now=_NOW, settings=_SETTINGS,
        )
        assert degraded.degraded == (("TSLA", "short_interest"),)
        assert _family(degraded, "ftd")[0].status == "ok"

    limit = _FakeClient({_FTD_PATH: UnusualWhalesDailyLimitError("daily_request_limit_hit")})
    with pytest.raises(UnusualWhalesDailyLimitError):
        await run_delayed_job(limit, engine, ["TSLA", "NVDA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert len(limit.calls) == 4


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_CLOSES = (ClosePoint(date(2026, 8, 14), 380.0), ClosePoint(date(2026, 8, 31), 400.0),
           ClosePoint(date(2026, 9, 14), 420.0))


def _evidence(today: date = _TODAY) -> dl.DelayedEvidence:
    records = (
        *parse_short_interest(list(_SI_ROWS), ticker="TSLA", today=_TODAY, settings=_SETTINGS).records,
        *parse_ftds(list(_FTD_ROWS), ticker="TSLA", today=_TODAY, settings=_SETTINGS).records,
    )
    return build_delayed_evidence("TSLA", records, _CLOSES, today=today, settings=_SETTINGS)


def test_short_interest_item_is_labelled_as_of_with_today_minus_as_of_delay() -> None:
    si = _evidence().items[0]
    assert (si.family, si.family_label, si.date_label) == ("short_interest", "Short", "itibarıyla tarihi")
    assert (si.filed_or_asof_date, si.transaction_date, si.delay_days) == (date(2026, 8, 31), None, 15)
    assert si.delay_text == "bugün - itibarıyla tarihi = 15 gün (kaynakta bildirim tarihi yok)"
    assert si.size_text == "açığa satış / serbest dolaşım %1.88; short kapatma süresi 2.04 gün"
    assert (si.side, si.side_text, si.flags, si.who) == (None, None, (), None)
    assert si.outcome.text == "itibarıyla tarihinden beri dayanak %+5.0 (2026-08-31 → 2026-09-14 kapanış)"


def test_ftd_item_is_labelled_by_fail_date_with_notional() -> None:
    ftd = next(i for i in _evidence().items if i.family == "ftd")
    assert (ftd.family_label, ftd.date_label, ftd.filed_or_asof_date) == ("FTD", "FTD günü", date(2026, 8, 14))
    assert (ftd.delay_days, ftd.delay_text) == (32, "bugün - FTD günü = 32 gün (kaynakta bildirim tarihi yok)")
    assert ftd.size_text == "200 hisse x $225.30 ≈ $45,060"
    assert ftd.size_low_usd == pytest.approx(45060.0)
    assert ftd.outcome.text == "FTD gününden beri dayanak %+10.5 (2026-08-14 → 2026-09-14 kapanış)"


def test_as_of_delay_grows_with_today_and_windows_follow_today() -> None:
    later = _evidence(today=date(2026, 9, 20))
    assert later.items[0].delay_days == 20
    as_of = {(i.family, i.filed_or_asof_date) for i in _evidence(today=date(2026, 9, 29)).items}
    assert ("short_interest", date(2026, 6, 30)) not in as_of  # window now starts 2026-07-01
    assert ("ftd", date(2026, 7, 17)) not in {(i.family, i.filed_or_asof_date) for i in _evidence(date(2026, 9, 16)).items}


def test_same_day_short_interest_sorts_before_ftd() -> None:
    items = _evidence().items
    same_day = [i.family for i in items if i.filed_or_asof_date == date(2026, 8, 14)]
    assert same_day == ["short_interest", "ftd"]


def test_short_interest_without_days_to_cover_uses_the_short_line() -> None:
    row = {**_SI_ROWS[0], "days_to_cover": None}
    records = parse_short_interest([row], ticker="TSLA", today=_TODAY, settings=_SETTINGS).records
    [item] = build_delayed_evidence("TSLA", records, (), today=_TODAY, settings=_SETTINGS).items
    assert item.size_text == "açığa satış / serbest dolaşım %1.88"
    assert item.outcome.text == "bilinmiyor"


def test_every_generated_shorts_string_is_clean() -> None:
    for template in dl._TEXT.values():
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
        ensure_clean(template.format(**dict.fromkeys(names, "1")))
    ev = _evidence()
    generated = [ev.bucket_label, ev.exclusion_note]
    for item in ev.items:
        generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text, *item.flags]
        generated += [item.size_text] if item.size_text is not None else []
    assert len(generated) >= 40
    for text in generated:
        assert ensure_clean(text) == text
