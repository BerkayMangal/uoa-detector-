"""Phase 5.2.D1a: congress delayed family (contract §7 D1, decision P14).

Hermetic. Fixture rows are trimmed from the live ``/api/congress/recent-trades
?ticker=NVDA&limit=200`` response captured 2026-09-15 (alfa_probe); edge cases
alter one field of a real row. A fake client and a tmp sqlite file stand in for
UW and Postgres. No network, no env.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine
from webapp.board import delayed as dl
from webapp.board.daily_close import AlfaDailyClose, ClosePoint, ensure_daily_close_tables
from webapp.board.db import TABLE_PREFIX, make_engine, session_factory
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    AlfaDelayed,
    DelayedEvidence,
    DelayedItem,
    DelayedOutcome,
    build_delayed_evidence,
    ensure_delayed_tables,
    load_delayed_evidence,
    load_delayed_records,
    parse_congress_trades,
    refresh_congress,
    run_delayed_job,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import DelayedSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS: DelayedSettings = load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed
_TODAY = date(2026, 9, 15)
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)  # 17:05 ET

_CISNEROS = "739eca36-a8f3-4894-96b1-420354fe17b6"
_WHITEHOUSE = "7a981a50-60bb-4cb9-afaa-3667a221a6ef"
_TRUMP = "888dc73f-f1eb-485a-a241-80657aaaaff9"


def _row(**over: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Gilbert Cisneros", "ticker": "NVDA", "issuer": "undisclosed", "is_active": True,
        "notes": "NVIDIA Corporation - Common Stock\n(NVDA) [ST]", "transaction_date": "2026-08-18",
        "txn_type": "Sell", "politician_id": _CISNEROS, "amounts": "$1,001 - $15,000",
        "filed_at_date": "2026-09-11", "reporter": "Hon. Gilbert Cisneros", "member_type": "house",
    }
    base.update(over)
    return base


_WH = {"name": "Sheldon Whitehouse", "reporter": "Sheldon Whitehouse", "politician_id": _WHITEHOUSE,
       "member_type": "senate", "notes": "NVIDIA Corporation - Common Stock",
       "transaction_date": "2026-08-13", "filed_at_date": "2026-09-02"}
_DT = {"name": "Donald J Trump", "reporter": "Donald J Trump", "politician_id": _TRUMP,
       "member_type": "executive", "notes": "NVIDIA CORP"}

# Live order: newest transaction date first; filing dates out of order.
_LIVE_ROWS: list[dict[str, Any]] = [
    _row(),                                                                    # delay 24
    _row(**_WH, issuer="self", amounts="$15,001 - $50,000"),                   # delay 20
    _row(**_WH, issuer="spouse", amounts="$1,001 - $15,000"),                  # same day, other issuer
    _row(transaction_date="2026-07-17", txn_type="Buy", filed_at_date="2026-09-04"),  # delay 49: late
    _row(**_DT, txn_type="Buy", amounts="$15,001 - $50,000",
         transaction_date="2026-05-15", filed_at_date="2026-07-01"),           # delay 47: late, executive
    _row(**_DT, txn_type="Buy", amounts="$1,000,001 - $5,000,000",
         transaction_date="2026-02-10", filed_at_date="2026-05-14"),           # delay 93
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
        key = f"{path}?ticker={(params or {}).get('ticker')}"
        result = self.responses.get(key, self.responses.get(path, {"data": []}))
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(f"sqlite:///{tmp_path / 'board.db'}")


def _parse(rows: list[object], **kw: Any) -> dl.FamilyParse:
    return parse_congress_trades(rows, ticker=kw.get("ticker", "NVDA"), today=kw.get("today", _TODAY),
                                 settings=kw.get("settings", _SETTINGS))


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def test_ensure_creates_the_prefixed_append_only_table(engine: Engine) -> None:
    ensure_delayed_tables(engine)
    ensure_delayed_tables(engine)
    assert AlfaDelayed.__tablename__.startswith(TABLE_PREFIX)
    pk = inspect(engine).get_pk_constraint("alfa_delayed")["constrained_columns"]
    assert pk == ["ticker", "family", "dedupe_key"]
    columns = {c["name"] for c in inspect(engine).get_columns("alfa_delayed")}
    assert {
        "filed_or_asof_date", "transaction_date", "delay_days", "side", "size_text", "size_low",
        "size_high", "flag_late", "flag_executive", "flag_10b5_1", "form", "payload_json",
    } <= columns


def test_module_exposes_no_rewrite_path() -> None:
    names = [n.lower() for n in dir(dl) if not n.startswith("__")]
    for word in ("update", "delete", "reset", "drop", "upsert"):
        assert not [n for n in names if word in n], word


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_orders_by_filing_date_and_normalizes_fields() -> None:
    parsed = _parse(list(_LIVE_ROWS))
    assert parsed.rows_skipped == 0
    assert [r.filed_or_asof_date.isoformat() for r in parsed.records] == [
        "2026-09-11", "2026-09-04", "2026-09-02", "2026-09-02", "2026-07-01", "2026-05-14",
    ]
    sell = parsed.records[0]
    assert (sell.family, sell.ticker, sell.side, sell.delay_days) == ("congress", "NVDA", "sell", 24)
    assert sell.transaction_date == date(2026, 8, 18)
    assert (sell.size_text, sell.size_low, sell.size_high) == ("$1,001 - $15,000", 1001.0, 15000.0)
    assert (sell.flag_late, sell.flag_executive, sell.flag_10b5_1, sell.form) == (False, False, None, None)
    assert json.loads(sell.payload_json) == _LIVE_ROWS[0]

    late_buy = parsed.records[1]
    assert (late_buy.side, late_buy.delay_days, late_buy.flag_late) == ("buy", 49, True)
    executive = parsed.records[4]
    assert (executive.delay_days, executive.flag_late, executive.flag_executive) == (47, True, True)
    big = parsed.records[5]
    assert (big.size_low, big.size_high) == (1_000_001.0, 5_000_000.0)


def test_late_flag_is_strictly_above_the_profile_cutoff() -> None:
    assert _SETTINGS.congress_late_days == 45
    at_cutoff = _row(transaction_date="2026-07-28", filed_at_date="2026-09-11")  # 45 days
    above = _row(transaction_date="2026-07-27", filed_at_date="2026-09-11")      # 46 days
    flags = {r.delay_days: r.flag_late for r in _parse([at_cutoff, above]).records}
    assert flags == {45: False, 46: True}
    relaxed = _SETTINGS.model_copy(update={"congress_late_days": 49})
    assert _parse([_LIVE_ROWS[3]], settings=relaxed).records[0].flag_late is False


def test_same_day_trades_by_self_and_spouse_are_distinct_but_exact_repeats_collapse() -> None:
    parsed = _parse([_LIVE_ROWS[1], _LIVE_ROWS[2], dict(_LIVE_ROWS[2])])
    assert len({r.dedupe_key for r in parsed.records}) == 2
    assert parsed.rows_skipped == 1
    # The key ignores free-text notes, so a vendor edit of the notes is still the same disclosure.
    edited = _parse([_row(notes="edited")]).records[0]
    assert edited.dedupe_key == _parse([_row()]).records[0].dedupe_key


@pytest.mark.parametrize(
    ("txn_type", "side"),
    [("Buy", "buy"), ("Purchase", "buy"), ("Sell", "sell"), ("Sale (Partial)", "sell"),
     ("Sale (Full)", "sell"), ("Sell (PARTIAL)", "sell"), ("Receive", None), ("Exchange", None),
     ("", None), (None, None)],
)
def test_transaction_type_spellings(txn_type: object, side: str | None) -> None:
    records = _parse([_row(txn_type=txn_type)]).records
    assert (records[0].side if records else None) == side


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [("$1,001 - $15,000", ("$1,001 - $15,000", 1001.0, 15000.0)),
     ("", (None, None, None)),
     (None, (None, None, None)),
     ("Over $50,000,000", ("Over $50,000,000", 50_000_000.0, None)),
     ("$15,000", ("$15,000", 15000.0, 15000.0)),
     ("undisclosed", ("undisclosed", None, None)),
     ("$50,000 - $1,000", ("$50,000 - $1,000", None, None))],
)
def test_amount_range_parsing(amounts: object, expected: tuple[object, ...]) -> None:
    record = _parse([_row(amounts=amounts)]).records[0]
    assert (record.size_text, record.size_low, record.size_high) == expected


def test_rows_outside_the_window_or_unusable_are_skipped() -> None:
    assert _SETTINGS.congress_lookback_days == 365
    rows: list[object] = [
        _row(filed_at_date="2025-09-15", transaction_date="2025-09-01"),  # first day inside
        _row(filed_at_date="2025-09-14", transaction_date="2025-09-01"),  # one day too old
        _row(filed_at_date="2026-09-16", transaction_date="2026-09-10"),  # after today
        _row(ticker="AMD"),
        _row(filed_at_date="2026-08-01"),                                  # filed before the trade
        _row(transaction_date=None),
        _row(filed_at_date="not-a-date"),
        "not-a-row",
    ]
    parsed = _parse(rows)
    assert [r.filed_or_asof_date for r in parsed.records] == [date(2025, 9, 15)]
    assert parsed.rows_skipped == 7
    assert _parse({"data": []}).records == ()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def test_job_calls_only_recent_trades_and_appends_idempotently(engine: Engine) -> None:
    client = _FakeClient({CONGRESS_RECENT_TRADES_PATH: {"data": list(_LIVE_ROWS)}})
    first = await run_delayed_job(client, engine, ["nvda", "NVDA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert client.calls == [(CONGRESS_RECENT_TRADES_PATH, {"ticker": "NVDA", "limit": 200})]
    assert all("unusual-trades" not in path for path, _ in client.calls)
    [result] = first.results
    assert (result.status, result.inserted, result.already_stored, result.truncated) == ("ok", 6, 0, False)

    edited = [dict(r, notes="vendor edit") for r in _LIVE_ROWS]
    second = await run_delayed_job(_FakeClient({CONGRESS_RECENT_TRADES_PATH: {"data": edited}}), engine,  # type: ignore[arg-type]
                                   ["NVDA"], now=_NOW, settings=_SETTINGS)
    assert (second.results[0].inserted, second.results[0].already_stored) == (0, 6)
    stored = load_delayed_records(engine, "NVDA")
    assert len(stored) == 6
    assert {json.loads(r.payload_json)["notes"] for r in stored} != {"vendor edit"}
    with session_factory(engine)() as s:
        assert s.execute(select(func.count()).select_from(AlfaDelayed)).scalar_one() == 6
        row = s.execute(select(AlfaDelayed).where(AlfaDelayed.delay_days == 24)).scalar_one()
        assert row.fetched_at.replace(tzinfo=UTC) == _NOW


async def test_refresh_congress_is_one_request_for_one_ticker(engine: Engine) -> None:
    client = _FakeClient({CONGRESS_RECENT_TRADES_PATH: {"data": list(_LIVE_ROWS)}})
    result = await refresh_congress(client, engine, " nvda ", now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert (result.ticker, result.family, result.inserted) == ("NVDA", "congress", 6)


async def test_a_full_page_is_reported_as_truncated(engine: Engine) -> None:
    rows = [_row(transaction_date=date.fromordinal(date(2026, 9, 11).toordinal() - i).isoformat())
            for i in range(200)]
    client = _FakeClient({CONGRESS_RECENT_TRADES_PATH: {"data": rows}})
    [result] = (await run_delayed_job(client, engine, ["NVDA"], now=_NOW, settings=_SETTINGS)).results  # type: ignore[arg-type]
    assert result.truncated is True
    assert result.inserted == 200


async def test_error_policy(engine: Engine) -> None:
    client = _FakeClient({
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=ZZZZ": UnusualWhalesNotFoundError("422", status_code=422),
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=SMCI": {"data": []},
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=NVDA": {"data": list(_LIVE_ROWS)},
    })
    job = await run_delayed_job(client, engine, ["ZZZZ", "SMCI", "NVDA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [(r.ticker, r.status) for r in job.results] == [
        ("ZZZZ", "no_data"), ("SMCI", "no_data"), ("NVDA", "ok"),
    ]


@pytest.mark.parametrize(
    "error", [UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("503"), CircuitBreakerOpenError("open")],
)
async def test_degradable_errors_mark_degraded_and_continue(engine: Engine, error: Exception) -> None:
    client = _FakeClient({
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=SMCI": error,
        f"{CONGRESS_RECENT_TRADES_PATH}?ticker=NVDA": {"data": list(_LIVE_ROWS)},
    })
    job = await run_delayed_job(client, engine, ["SMCI", "NVDA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert job.degraded == (("SMCI", "congress"),)
    assert len(load_delayed_records(engine, "NVDA")) == 6


@pytest.mark.parametrize(
    "error", [UnusualWhalesDailyLimitError("daily_request_limit_hit"), UnusualWhalesAuthError("401")],
)
async def test_daily_limit_and_auth_propagate(engine: Engine, error: Exception) -> None:
    client = _FakeClient({CONGRESS_RECENT_TRADES_PATH: error})
    with pytest.raises(type(error)):
        await run_delayed_job(client, engine, ["NVDA", "SMCI"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert len(client.calls) == 1


async def test_naive_now_is_rejected(engine: Engine) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await run_delayed_job(_FakeClient({}), engine, ["NVDA"], now=datetime(2026, 9, 15, 21, 5),  # type: ignore[arg-type]
                              settings=_SETTINGS)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_CLOSES = (
    ClosePoint(date(2026, 9, 1), 98.0),
    ClosePoint(date(2026, 9, 11), 100.0),
    ClosePoint(date(2026, 9, 14), 103.0),
)


def _evidence(rows: list[object], closes: tuple[ClosePoint, ...] = _CLOSES, today: date = _TODAY) -> DelayedEvidence:
    return build_delayed_evidence("nvda", _parse(rows).records, closes, today=today, settings=_SETTINGS)


def test_items_carry_filing_date_delay_side_flags_and_outcome() -> None:
    ev = _evidence(list(_LIVE_ROWS))
    assert (ev.ticker, ev.bucket_label) == ("NVDA", "ek kanıt (gecikmeli)")
    assert [i.filed_or_asof_date.isoformat() for i in ev.items][:2] == ["2026-09-11", "2026-09-04"]

    sell = ev.items[0]
    assert (sell.family_label, sell.date_label) == ("Kongre", "bildirim tarihi")
    assert sell.delay_text == "işlemden 24 gün sonra bildirildi"
    assert (sell.side_text, sell.flags, sell.who) == ("satış", (), "Gilbert Cisneros")
    assert sell.outcome == DelayedOutcome(
        known=True, pct=pytest.approx(3.0), base_day=date(2026, 9, 11), through_day=date(2026, 9, 14),
        text="bildirim tarihinden beri dayanak %+3.0 (2026-09-11 → 2026-09-14 kapanış)",
    )
    late = ev.items[1]
    assert (late.side_text, late.flags) == ("alış", ("geç bildirim",))
    whitehouse = ev.items[2]
    assert whitehouse.outcome.base_day == date(2026, 9, 1)  # on or before the 09-02 filing
    assert whitehouse.outcome.text.startswith("bildirim tarihinden beri dayanak %+5.1 ")
    executive = next(i for i in ev.items if i.filed_or_asof_date == date(2026, 7, 1))
    assert executive.flags == ("geç bildirim", "yürütme beyanı (Kongre üyesi değil)")
    assert executive.outcome == DelayedOutcome(False, None, None, None, "bilinmiyor")  # no close that old


def test_outcome_is_unknown_without_a_session_after_the_filing() -> None:
    filed_today = _row(transaction_date="2026-09-10", filed_at_date="2026-09-15")
    [item] = _evidence([filed_today]).items
    assert item.outcome.known is False
    assert item.outcome.text == "bilinmiyor"
    [empty] = _evidence([_row()], closes=()).items
    assert empty.outcome.text == "bilinmiyor"


def test_items_follow_the_window_as_of_today_and_the_ticker() -> None:
    records = _parse(list(_LIVE_ROWS)).records
    earlier = build_delayed_evidence("NVDA", records, _CLOSES, today=date(2026, 9, 5), settings=_SETTINGS)
    assert date(2026, 9, 11) not in {i.filed_or_asof_date for i in earlier.items}  # not filed yet
    much_later = build_delayed_evidence("NVDA", records, _CLOSES, today=date(2027, 5, 20), settings=_SETTINGS)
    assert date(2026, 5, 14) not in {i.filed_or_asof_date for i in much_later.items}
    assert build_delayed_evidence("AMD", records, _CLOSES, today=_TODAY, settings=_SETTINGS).items == ()


def test_view_types_have_no_count_fields() -> None:
    """R-DL1: delayed items are never counted, so nothing here can feed a count."""
    for view in (DelayedEvidence, DelayedItem, DelayedOutcome):
        for f in fields(view):
            assert not f.name.startswith("n_"), (view.__name__, f.name)
            for word in ("count", "lehte", "aleyhte", "support", "against", "unknown", "total", "score"):
                assert word not in f.name, (view.__name__, f.name)


def test_every_generated_string_is_clean() -> None:
    for template in dl._TEXT.values():
        ensure_clean(template.format(days=45, pct="+3.0", base="2026-09-11", through="2026-09-14"))
    ev = _evidence(list(_LIVE_ROWS))
    generated = [ev.bucket_label, ev.exclusion_note]
    for item in ev.items:  # who and size_text are vendor data, not generated copy
        generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text, *item.flags]
        generated += [item.side_text] if item.side_text is not None else []
    assert generated
    for text in generated:
        assert ensure_clean(text) == text


async def test_load_delayed_evidence_reads_the_tables(engine: Engine) -> None:
    await run_delayed_job(_FakeClient({CONGRESS_RECENT_TRADES_PATH: {"data": list(_LIVE_ROWS)}}), engine,  # type: ignore[arg-type]
                          ["NVDA"], now=_NOW, settings=_SETTINGS)
    ensure_daily_close_tables(engine)
    with session_factory(engine)() as s:
        s.add_all(AlfaDailyClose(ticker="NVDA", day=p.day, close=p.close, fetched_at=_NOW) for p in _CLOSES)
        s.commit()
    loaded = load_delayed_evidence(engine, "nvda", today=_TODAY, settings=_SETTINGS)
    assert loaded == _evidence(list(_LIVE_ROWS))
