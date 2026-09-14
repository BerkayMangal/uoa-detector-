"""Phase 3.9.9 tests: UW open interest on /api/option-contract/{OCC}/historic.

Contract: docs/phase-3.9-uw-endpoint-correction-acceptance.md §3.6.
Fixture rows are trimmed from the live AAPL261016C00340000 response
captured 2026-09-14 (newest first, start-of-day OI: row D = OI after
D-1's trading).

Pins:
  - at(when): latest row with date <= ET date(when); as_of 00:00 ET
  - next_day(trade_date): earliest row with date > trade_date
  - one parameterless fetch per OCC symbol serves both methods
  - 404/422 -> None, cached as empty; other client errors propagate
  - malformed rows skipped; rows under "data" tolerated
  - OCC symbol construction unchanged
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesNotFoundError,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)

_PATH = "/api/option-contract/AAPL261016C00340000/historic"

# Trimmed live rows (capture 2026-09-14 11:4xZ), newest first as served.
_LIVE_ROWS: list[dict[str, Any]] = [
    {"date": "2026-09-14", "open_interest": 104505, "volume": 0,
     "last_tape_time": "2026-09-14T10:30:18Z",
     "implied_volatility": "0.261298044050294",
     "nbbo_bid": None, "nbbo_ask": None},
    {"date": "2026-09-11", "open_interest": 106553, "volume": 9324,
     "last_tape_time": "2026-09-11T21:45:10Z",
     "implied_volatility": "0.25110282209338",
     "nbbo_bid": "6.90", "nbbo_ask": "7.10"},
    {"date": "2026-09-10", "open_interest": 15611, "volume": 111857,
     "last_tape_time": "2026-09-10T21:32:03Z",
     "implied_volatility": "0.261522380571824",
     "nbbo_bid": "5.45", "nbbo_ask": "5.55"},
    {"date": "2026-09-09", "open_interest": 15089, "volume": 3875,
     "last_tape_time": "2026-09-09T21:31:52Z",
     "implied_volatility": "0.263244241418865",
     "nbbo_bid": "2.72", "nbbo_ask": "2.82"},
]

_CONTRACT: dict[str, Any] = {
    "ticker": "AAPL",
    "strike": Decimal("340"),
    "expiry": date(2026, 10, 16),
    "option_type": "call",
}


class _FakeClient:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._response = response if response is not None else {"chains": []}
        self._error = error

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._response


def _live_client() -> _FakeClient:
    return _FakeClient(
        response={
            "chains": [dict(row) for row in _LIVE_ROWS],
            "etf_holdings": [],
        },
    )


def _provider(client: _FakeClient) -> UnusualWhalesOpenInterestProvider:
    return UnusualWhalesOpenInterestProvider(
        client=client,  # type: ignore[arg-type]
        settings=UnusualWhalesSettings(
            cache_ttl=UnusualWhalesProviderCacheTTL(),
        ),
    )


# ---------------------------------------------------------------------------
# at()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_intraday_event_returns_event_day_row() -> None:
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 106553
    assert snap.as_of == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)  # 00:00 EDT
    assert snap.ticker == "AAPL"
    assert snap.strike == Decimal("340")
    assert snap.expiry == date(2026, 10, 16)
    assert snap.option_type == "call"


@pytest.mark.asyncio
async def test_at_prior_session_close_returns_prior_day_row() -> None:
    """M27's prior-close query (D-1 21:00Z) gets row D-1, not row D."""
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 10, 21, 0, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 15611
    assert snap.as_of == datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_at_selects_by_et_date_not_utc_date() -> None:
    """03:30Z on 09-11 is 23:30 ET on 09-10: row 09-10, not row 09-11."""
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 11, 3, 30, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 15611


@pytest.mark.asyncio
async def test_at_weekend_returns_friday_row() -> None:
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),  # Sunday
    )
    assert snap is not None
    assert snap.open_interest == 106553
    assert snap.as_of == datetime(2026, 9, 11, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_at_premarket_returns_published_same_day_row() -> None:
    """Row D is published before D's open (live: 09-14 row at 10:30Z)."""
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 14, 11, 44, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 104505


@pytest.mark.asyncio
async def test_at_before_first_row_returns_none() -> None:
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 8, 15, 0, tzinfo=UTC),
    )
    assert snap is None


@pytest.mark.asyncio
async def test_at_naive_when_is_read_as_utc() -> None:
    provider = _provider(_live_client())
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 11, 3, 30),
    )
    assert snap is not None
    assert snap.open_interest == 15611


# ---------------------------------------------------------------------------
# next_day()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_day_friday_trade_returns_monday_row() -> None:
    provider = _provider(_live_client())
    snap = await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 11))
    assert snap is not None
    assert snap.open_interest == 104505
    assert snap.as_of == datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_next_day_weekday_trade_returns_following_row() -> None:
    provider = _provider(_live_client())
    snap = await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 10))
    assert snap is not None
    assert snap.open_interest == 106553


@pytest.mark.asyncio
async def test_next_day_without_later_row_returns_none() -> None:
    provider = _provider(_live_client())
    snap = await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 14))
    assert snap is None


# ---------------------------------------------------------------------------
# Fetch, cache, errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_and_next_day_share_one_parameterless_fetch() -> None:
    client = _live_client()
    provider = _provider(client)
    await provider.at(**_CONTRACT, when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC))
    await provider.at(**_CONTRACT, when=datetime(2026, 9, 10, 21, 0, tzinfo=UTC))
    await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 11))
    await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 10))
    assert client.calls == [(_PATH, None)]


@pytest.mark.asyncio
async def test_different_contracts_fetch_separately() -> None:
    client = _live_client()
    provider = _provider(client)
    when = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    await provider.at(**_CONTRACT, when=when)
    await provider.at(
        ticker="AAPL", strike=Decimal("345"), expiry=date(2026, 10, 16),
        option_type="call", when=when,
    )
    assert [path for path, _ in client.calls] == [
        _PATH,
        "/api/option-contract/AAPL261016C00345000/historic",
    ]


@pytest.mark.asyncio
async def test_not_found_returns_none_and_is_cached() -> None:
    client = _FakeClient(
        error=UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    )
    provider = _provider(client)
    assert await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
    ) is None
    assert await provider.next_day(
        **_CONTRACT, trade_date=date(2026, 9, 10),
    ) is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_auth_error_propagates() -> None:
    """A bad key must never look like "no data" (contract §3.9)."""
    client = _FakeClient(error=UnusualWhalesAuthError("HTTP 401"))
    provider = _provider(client)
    with pytest.raises(UnusualWhalesAuthError):
        await provider.at(
            **_CONTRACT, when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_rows_are_skipped() -> None:
    client = _FakeClient(
        response={
            "chains": [
                "not-a-row",
                {"open_interest": 500},
                {"date": "2026-09-13x", "open_interest": 501},
                {"date": None, "open_interest": 502},
                {"date": "2026-09-12", "open_interest": None},
                {"date": "2026-09-12", "open_interest": "n/a"},
                {"date": "2026-09-12"},
                {"date": "2026-09-12", "open_interest": True},
                {"date": "2026-09-12", "open_interest": 12.5},
                {"date": "2026-09-11", "open_interest": "106553"},
                {"date": "2026-09-10", "open_interest": 15611},
            ],
        },
    )
    provider = _provider(client)
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 13, 15, 0, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 106553
    after = await provider.next_day(**_CONTRACT, trade_date=date(2026, 9, 11))
    assert after is None


@pytest.mark.asyncio
async def test_duplicate_date_first_row_in_response_wins() -> None:
    client = _FakeClient(
        response={
            "chains": [
                {"date": "2026-09-11", "open_interest": 1},
                {"date": "2026-09-11", "open_interest": 2},
            ],
        },
    )
    provider = _provider(client)
    at_snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
    )
    next_snap = await provider.next_day(
        **_CONTRACT, trade_date=date(2026, 9, 10),
    )
    assert at_snap is not None
    assert next_snap is not None
    assert at_snap.open_interest == next_snap.open_interest == 1


@pytest.mark.asyncio
async def test_rows_under_data_key_and_winter_as_of() -> None:
    """``data`` is tolerated; as_of is 00:00 EST (05:00Z) in winter."""
    client = _FakeClient(
        response={"data": [{"date": "2026-01-15", "open_interest": 42}]},
    )
    provider = _provider(client)
    snap = await provider.at(
        **_CONTRACT, when=datetime(2026, 1, 15, 20, 0, tzinfo=UTC),
    )
    assert snap is not None
    assert snap.open_interest == 42
    assert snap.as_of == datetime(2026, 1, 15, 5, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_occ_symbol_construction_unchanged() -> None:
    client = _FakeClient(
        response={"chains": [{"date": "2026-09-11", "open_interest": 7}]},
    )
    provider = _provider(client)
    snap = await provider.at(
        ticker="spy", strike=Decimal("172.5"), expiry=date(2026, 12, 18),
        option_type="put", when=datetime(2026, 9, 11, 15, 0, tzinfo=UTC),
    )
    assert client.calls == [
        ("/api/option-contract/SPY261218P00172500/historic", None),
    ]
    assert snap is not None
    assert snap.ticker == "SPY"
    assert snap.strike == Decimal("172.5")
    assert snap.option_type == "put"
