"""Phase 5.2.D2a: insider (İçeriden) delayed family (contract §7 D2, decision P14).

Hermetic. Fixture rows are trimmed from the live ``/api/insider/transactions``
responses captured 2026-09-15 (NVDA, ``form_types[]=4&form_types[]=4/A
&start_date=2026-06-17``, plus the unfiltered NVDA sample for the A/F/G/144
rows). Edge cases alter one or two fields of a real row. No network, no env.
"""

from __future__ import annotations

import string
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from webapp.board import delayed as dl
from webapp.board.daily_close import ClosePoint
from webapp.board.db import make_engine
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    INSIDER_TRANSACTIONS_PATH,
    build_delayed_evidence,
    load_delayed_records,
    parse_congress_trades,
    parse_insider_transactions,
    refresh_insider,
    run_delayed_job,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import DelayedSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS: DelayedSettings = load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed
_TODAY = date(2026, 9, 15)  # insider window starts 2026-06-17 (90 days)
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)


def _insider(**fields: object) -> dict[str, Any]:
    return {"ticker": "NVDA", "transactions": 1, "security_title": "Common Stock",
            "stock_price": "211.96", "is_officer": False, "is_director": True, **fields}


_STEVENS_0904 = _insider(
    id="d30ff7d8-cded-4f1d-86f4-5428a1138c93", amount=-622239, transactions=4, price="231.6191",
    transaction_date="2026-09-04", filing_date="2026-09-08", formtype="4", transaction_code="S",
    is_10b5_1=False, owner_name="STEVENS MARK", officer_title="",
    ids=["d765fd55-0895-4893-b553-ebd5737fd470", "50c19559-5cc9-490a-af8a-902194ebc8ea",
         "e0501ea1-e576-427d-931f-16e7e08c5bc6", "a6476764-4eb6-40f8-aef9-6b29fe584a37"],
)
_STEVENS_0903 = _insider(
    id="f3e8a36f-3fb5-40f1-b5b7-3ca5e0cddf7e", amount=-400000, transactions=3, price="228.7868",
    transaction_date="2026-09-03", filing_date="2026-09-08", formtype="4", transaction_code="S",
    is_10b5_1=False, owner_name="STEVENS MARK", officer_title="",
    ids=["aa10c7ae-34ad-42f3-866e-e7d8477acbd0", "1319061b-9395-44c4-a7d5-0938699ef1a3",
         "db20f383-532c-4932-8c87-9d8cfe593b2b"],
)
_TETER_10B5 = _insider(
    id="5193595c-3f98-4be8-9a59-0c73afd8e8d9", amount=-30000, transactions=3, price="217.8840",
    transaction_date="2026-08-31", filing_date="2026-09-02", formtype="4", transaction_code="S",
    is_10b5_1=True, owner_name="TETER TIMOTHY", officer_title="EVP, General Counsel and Sec",
    is_officer=True, is_director=False,
    ids=["6d1453b2-c28d-4ebe-b382-10600f28affd", "284191ce-5d03-4064-be80-1be176d44560",
         "05deb67e-c333-4c95-8f1e-8b896f9c864e"],
)
_STEVENS_0618 = _insider(
    id="e227b6b1-dd3f-49ee-8707-7f58e92180da", amount=-885000, transactions=2, price="210.1729",
    transaction_date="2026-06-18", filing_date="2026-06-23", formtype="4", transaction_code="S",
    is_10b5_1=False, owner_name="STEVENS MARK", officer_title="",
    ids=["fb4874de-1f51-428e-bc2f-00c934b66e63", "20daa283-9534-412a-abb0-e0730bde1dbd"],
)
# Not trades: a grant (A), a Form 144 notice, a gift (G), tax withholding (F).
_PARKER_GRANT = _insider(
    id="3ab32345-c9bc-481f-91a1-d5657ff600a1", amount=172507, price="0.0000",
    transaction_date="2026-09-09", filing_date="2026-09-11", formtype="4", transaction_code="A",
    is_10b5_1=False, owner_name="PARKER NICHOLAS", officer_title="EVP, Worldwide Field Ops",
    ids=["82682939-3152-46c3-8f8b-e91a4dad6ceb"],
)
_STEVENS_144 = _insider(
    id="7088ddde-b55c-47a4-8860-01bf0e61a799", amount=-5000000, price="217.4400",
    transaction_date="2026-09-02", filing_date="2026-09-02", formtype="144", transaction_code="S",
    is_10b5_1=False, owner_name="STEVENS MARK", officer_title="", security_title=None,
    ids=["43920723-df93-4287-8fae-f9ec55032431"],
)
_COXE_GIFT = _insider(
    id="216f3943-b4b5-4f36-ac61-eccf81075f7a", amount=-500000, price="0.0000",
    transaction_date="2026-08-05", filing_date="2026-08-07", formtype="4", transaction_code="G",
    is_10b5_1=False, owner_name="COXE TENCH", officer_title=None,
    ids=["9350b657-dd3c-42b8-ae51-adc4acdce4e6"],
)
_KRESS_TAX = _insider(
    id="c0e5d621-f100-404a-a296-e925cd6e71a2", amount=-40746, price="207.4100",
    transaction_date="2026-06-17", filing_date="2026-06-23", formtype="4", transaction_code="F",
    is_10b5_1=False, owner_name="KRESS COLETTE", officer_title="EVP & Chief Financial Officer",
    ids=["1facb8ea-53db-42c3-856e-418b0dd860bd"],
)

_LIVE = [_STEVENS_0904, _STEVENS_0903, _PARKER_GRANT, _STEVENS_144, _TETER_10B5, _COXE_GIFT,
         _KRESS_TAX, _STEVENS_0618]


def _alter(row: dict[str, Any], **over: object) -> dict[str, Any]:
    return {**row, **over}


def _parse(rows: list[object], today: date = _TODAY) -> dl.FamilyParse:
    return parse_insider_transactions(rows, ticker="nvda", today=today, settings=_SETTINGS)


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


def _insider_results(job: dl.DelayedJobResult) -> list[dl.DelayedFamilyResult]:
    return [r for r in job.results if r.family == "insider"]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_only_form4_purchases_and_sales_are_kept() -> None:
    parsed = _parse(list(_LIVE))
    assert {r.dedupe_key for r in parsed.records} == {
        _parse([row]).records[0].dedupe_key
        for row in (_STEVENS_0904, _STEVENS_0903, _TETER_10B5, _STEVENS_0618)
    }
    assert parsed.rows_skipped == 4  # A grant, Form 144, G gift, F tax withholding
    assert [r.filed_or_asof_date.isoformat() for r in parsed.records] == [
        "2026-09-08", "2026-09-08", "2026-09-02", "2026-06-23",
    ]


def test_side_size_delay_form_and_flags() -> None:
    [sale] = _parse([_STEVENS_0904]).records
    assert (sale.family, sale.ticker, sale.side, sale.form) == ("insider", "NVDA", "sell", "4")
    assert (sale.transaction_date, sale.filed_or_asof_date, sale.delay_days) == (
        date(2026, 9, 4), date(2026, 9, 8), 4,
    )
    assert sale.size_low == pytest.approx(622239 * 231.6191)
    assert sale.size_high == sale.size_low
    assert sale.size_text is None
    assert (sale.flag_10b5_1, sale.flag_late, sale.flag_executive) == (False, None, None)
    [planned] = _parse([_TETER_10B5]).records
    assert planned.flag_10b5_1 is True
    [amended] = _parse([_alter(_STEVENS_0903, formtype="4/A")]).records
    assert amended.form == "4/A"


def test_side_needs_the_amount_sign_and_the_code_to_agree() -> None:
    purchase = _alter(_STEVENS_0618, transaction_code="P", amount=10000, price="205.00",
                      ids=["00000000-0000-4000-8000-000000000001"])
    assert _parse([purchase]).records[0].side == "buy"
    sale_with_positive_amount = _alter(_STEVENS_0618, amount=885000)
    assert _parse([sale_with_positive_amount]).records[0].side is None
    purchase_with_negative_amount = _alter(purchase, amount=-10000)
    assert _parse([purchase_with_negative_amount]).records[0].side is None
    assert _parse([_alter(_STEVENS_0618, amount=0)]).records == ()


@pytest.mark.parametrize("price", ["0.0000", "n/a", None, "-1"])
def test_unusable_price_leaves_the_size_unknown(price: object) -> None:
    [record] = _parse([_alter(_STEVENS_0904, price=price)]).records
    assert (record.size_low, record.size_high) == (None, None)


def test_numbers_and_flags_arrive_as_strings_too() -> None:
    [record] = _parse([_alter(_STEVENS_0904, amount="-622239", is_10b5_1="true")]).records
    assert record.size_low == pytest.approx(622239 * 231.6191)
    assert record.flag_10b5_1 is True
    [unknown_flag] = _parse([_alter(_STEVENS_0904, is_10b5_1=None)]).records
    assert unknown_flag.flag_10b5_1 is None


def test_dedupe_by_ids() -> None:
    reordered = _alter(_STEVENS_0904, ids=list(reversed(_STEVENS_0904["ids"])))
    assert _parse([reordered]).records[0].dedupe_key == _parse([_STEVENS_0904]).records[0].dedupe_key
    parsed = _parse([_STEVENS_0904, dict(_STEVENS_0904)])
    assert (len(parsed.records), parsed.rows_skipped) == (1, 1)
    by_id = _parse([_alter(_STEVENS_0904, ids=[])]).records[0]
    assert by_id.dedupe_key != _parse([_STEVENS_0904]).records[0].dedupe_key
    assert _parse([_alter(_STEVENS_0904, ids=None, id=None)]).records == ()


def test_window_dates_and_ticker_are_enforced() -> None:
    kept = _alter(_STEVENS_0618, transaction_date="2026-06-17")      # first day of the window
    too_old = _alter(_STEVENS_0618, transaction_date="2026-06-16",
                     ids=["00000000-0000-4000-8000-000000000002"])
    future = _alter(_STEVENS_0904, filing_date="2026-09-16")
    backwards = _alter(_STEVENS_0904, filing_date="2026-09-01")
    other = _alter(_STEVENS_0904, ticker="AAPL")
    undated = _alter(_STEVENS_0904, transaction_date=None)
    parsed = _parse([kept, too_old, future, backwards, other, undated, "not-a-row"])
    assert [r.transaction_date for r in parsed.records] == [date(2026, 6, 17)]
    assert parsed.rows_skipped == 6


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def test_job_requests_the_form4_window_and_appends_idempotently(engine: Engine) -> None:
    client = _FakeClient({INSIDER_TRANSACTIONS_PATH: {"data": list(_LIVE), "has_more": False}})
    first = await run_delayed_job(client, engine, ["nvda"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [c for c in client.calls if c[0] == INSIDER_TRANSACTIONS_PATH] == [(
        INSIDER_TRANSACTIONS_PATH,
        {"ticker_symbol": "NVDA", "form_types[]": ["4", "4/A"], "start_date": "2026-06-17"},
    )]
    [result] = _insider_results(first)
    assert (result.status, result.inserted, result.already_stored, result.rows_skipped) == ("ok", 4, 0, 4)
    assert result.truncated is False

    again = await run_delayed_job(client, engine, ["NVDA"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [(r.inserted, r.already_stored) for r in _insider_results(again)] == [(0, 4)]
    assert len(load_delayed_records(engine, "NVDA")) == 4


async def test_has_more_is_reported_as_truncated(engine: Engine) -> None:
    client = _FakeClient({INSIDER_TRANSACTIONS_PATH: {"data": [_STEVENS_0904], "has_more": True}})
    result = await refresh_insider(client, engine, "NVDA", now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert len(client.calls) == 1
    assert (result.family, result.truncated, result.inserted) == ("insider", True, 1)


async def test_insider_error_policy(engine: Engine) -> None:
    not_found = _FakeClient({INSIDER_TRANSACTIONS_PATH: UnusualWhalesNotFoundError("422", status_code=422)})
    [missing] = _insider_results(await run_delayed_job(not_found, engine, ["NVDA"], now=_NOW, settings=_SETTINGS))  # type: ignore[arg-type]
    assert missing.status == "no_data"

    for error in (UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("503"), CircuitBreakerOpenError("open")):
        job = await run_delayed_job(_FakeClient({INSIDER_TRANSACTIONS_PATH: error}), engine, ["NVDA"],  # type: ignore[arg-type]
                                    now=_NOW, settings=_SETTINGS)
        assert job.degraded == (("NVDA", "insider"),)

    limit = _FakeClient({INSIDER_TRANSACTIONS_PATH: UnusualWhalesDailyLimitError("daily_request_limit_hit")})
    with pytest.raises(UnusualWhalesDailyLimitError):
        await run_delayed_job(limit, engine, ["NVDA", "SMCI"], now=_NOW, settings=_SETTINGS)  # type: ignore[arg-type]
    assert [path for path, _ in limit.calls] == [CONGRESS_RECENT_TRADES_PATH, INSIDER_TRANSACTIONS_PATH]


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_CLOSES = (ClosePoint(date(2026, 9, 2), 190.0), ClosePoint(date(2026, 9, 8), 200.0),
           ClosePoint(date(2026, 9, 14), 210.0))


def _items(rows: list[object], today: date = _TODAY) -> tuple[dl.DelayedItem, ...]:
    records = _parse(rows, today=_TODAY).records
    return build_delayed_evidence("NVDA", records, _CLOSES, today=today, settings=_SETTINGS).items


def test_insider_items_carry_filing_date_size_flags_and_outcome() -> None:
    [item] = _items([_STEVENS_0904])
    assert (item.family_label, item.date_label) == ("İçeriden", "bildirim tarihi")
    assert item.delay_text == "işlemden 4 gün sonra bildirildi"
    assert (item.side_text, item.flags, item.who) == ("satış", (), "STEVENS MARK")
    assert item.size_text == "622,239 hisse x $231.62 ≈ $144,122,437"
    assert item.outcome.text == "bildirim tarihinden beri dayanak %+5.0 (2026-09-08 → 2026-09-14 kapanış)"

    [planned] = _items([_TETER_10B5])
    assert planned.who == "TETER TIMOTHY (EVP, General Counsel and Sec)"
    assert planned.flags == ("10b5-1 planlı işlem",)
    [amended] = _items([_alter(_STEVENS_0903, formtype="4/A")])
    assert amended.flags == ("düzeltilmiş beyan (Form 4/A)",)
    [free_price] = _items([_alter(_STEVENS_0904, price="0.0000")])
    assert free_price.size_text is None


def test_a_regrouped_insider_group_supersedes_its_subset() -> None:
    earlier_group = _alter(_STEVENS_0904, amount=-450000, transactions=3, ids=_STEVENS_0904["ids"][:3])
    items = _items([earlier_group, _STEVENS_0904, _STEVENS_0903])
    assert {i.key for i in items} == {
        _parse([_STEVENS_0904]).records[0].dedupe_key, _parse([_STEVENS_0903]).records[0].dedupe_key,
    }


def test_insider_window_follows_the_transaction_date_as_of_today() -> None:
    assert len(_items([_STEVENS_0618])) == 1
    assert _items([_STEVENS_0618], today=date(2026, 9, 17)) == ()  # 2026-06-18 is now outside
    assert _items([_STEVENS_0904], today=date(2026, 9, 7)) == ()   # not filed yet


def test_families_share_one_list_newest_first_congress_before_insider_on_a_tie() -> None:
    congress_row = {
        "name": "Gilbert Cisneros", "ticker": "NVDA", "issuer": "undisclosed", "txn_type": "Sell",
        "politician_id": "739eca36-a8f3-4894-96b1-420354fe17b6", "amounts": "$1,001 - $15,000",
        "transaction_date": "2026-08-18", "filed_at_date": "2026-09-08", "member_type": "house",
    }
    records = (*parse_congress_trades([congress_row], ticker="NVDA", today=_TODAY, settings=_SETTINGS).records,
               *_parse([_TETER_10B5, _STEVENS_0904]).records)
    items = build_delayed_evidence("NVDA", records, _CLOSES, today=_TODAY, settings=_SETTINGS).items
    assert [(i.family, i.filed_or_asof_date.isoformat()) for i in items] == [
        ("congress", "2026-09-08"), ("insider", "2026-09-08"), ("insider", "2026-09-02"),
    ]


def test_every_generated_insider_string_is_clean() -> None:
    for template in dl._TEXT.values():
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
        ensure_clean(template.format(**dict.fromkeys(names, "1")))
    items = _items([_STEVENS_0904, _TETER_10B5, _alter(_STEVENS_0903, formtype="4/A")])
    generated: list[str] = []
    for item in items:  # who is vendor data (names), not generated copy
        generated += [item.family_label, item.date_label, item.delay_text, item.outcome.text, *item.flags]
        generated += [text for text in (item.side_text, item.size_text) if text is not None]
    assert len(generated) >= 15
    for text in generated:
        assert ensure_clean(text) == text
