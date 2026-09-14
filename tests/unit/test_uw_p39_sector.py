"""Phase 3.9.10 tests: M25 sector peers (screener by market cap) + peer flow.

Contract: docs/phase-3.9-uw-endpoint-correction-acceptance.md §3.7.
Fixtures are trimmed real rows captured 2026-09-14 (session 2026-09-11):
``/api/stock/AAPL/info``, ``/api/stock/SPY/info``,
``/api/screener/stocks?sectors[]=Technology&order=marketcap...`` and
``/api/option-trades/flow-alerts`` (AAPL,MSFT,NVDA,TSLA,SPY, limit 200).
No network: a fake client serves canned responses and applies the
flow-alerts ticker and epoch-second window filters the way the server does.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import (
    UnusualWhalesProviderCacheTTL,
    UnusualWhalesSettings,
)
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.providers.sector_map import PeerFlowProvider, SectorMapProvider
from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesNotFoundError,
    UnusualWhalesTransientError,
)
from uoa_detector.sources.unusual_whales.flow_alerts import (
    FLOW_ALERTS_PAGE_LIMIT,
    FLOW_ALERTS_PATH,
)
from uoa_detector.sources.unusual_whales.live import _parse_iso_utc
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    SCREENER_STOCKS_PATH,
    UW_SECTORS,
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)

# ---------------------------------------------------------------------------
# Live fixtures
# ---------------------------------------------------------------------------

_INFO_AAPL: dict[str, Any] = {
    "data": {
        "symbol": "AAPL",
        "full_name": "APPLE",
        "sector": "Technology",
        "issue_type": "Common Stock",
        "marketcap": "4849208188600",
        "next_earnings_date": "2026-10-29",
    },
}

# SPY is an ETF: live ``sector`` is null.
_INFO_SPY: dict[str, Any] = {
    "data": {
        "symbol": "SPY",
        "full_name": "SPDR S&P 500 ETF",
        "sector": None,
        "issue_type": "ETF",
        "marketcap": "799371671955",
        "next_earnings_date": None,
    },
}

# First eight rows of the live Technology screener, market cap descending.
_SCREENER_TECH: dict[str, Any] = {
    "data": [
        {"ticker": "NVDA", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "5260789000000"},
        {"ticker": "AAPL", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "4849208188600"},
        {"ticker": "MSFT", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "3680323111704"},
        {"ticker": "AVGO", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "1728006274831"},
        {"ticker": "MU", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "1101451964444"},
        {"ticker": "AMD", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "842569343427"},
        {"ticker": "INTC", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "519229360000"},
        {"ticker": "ORCL", "sector": "Technology",
         "issue_type": "Common Stock", "marketcap": "454407046080"},
    ],
}

_EXPECTED_SCREENER_PARAMS: dict[str, Any] = {
    "sectors[]": "Technology",
    "order": "marketcap",
    "order_direction": "desc",
    "issue_types[]": "Common Stock",
}


def _alert(
    alert_id: str,
    ticker: str,
    option_type: str,
    created_at: str,
    ask_prem: str,
    bid_prem: str,
    alert_rule: str,
    option_chain: str,
) -> dict[str, Any]:
    return {
        "id": alert_id,
        "ticker": ticker,
        "type": option_type,
        "created_at": created_at,
        "total_premium": str(Decimal(ask_prem) + Decimal(bid_prem)),
        "total_ask_side_prem": ask_prem,
        "total_bid_side_prem": bid_prem,
        "has_sweep": False,
        "has_multileg": False,
        "has_singleleg": True,
        "alert_rule": alert_rule,
        "option_chain": option_chain,
        "issue_type": "Common Stock",
    }


# Real flow-alert rows (trimmed), newest first. Expected direction in comment.
_NVDA_CALL_BID = _alert(  # bearish; 19:59:00, after a 19:58 event
    "8d679585-11dd-4921-a21b-5130f734000b", "NVDA", "call",
    "2026-09-11T19:59:00.910533Z", "0", "250297",
    "RepeatedHits", "NVDA260916C00215000",
)
_AAPL_CALL_ASK = _alert(  # bullish
    "434193f0-0ebc-4dde-b34e-6665d5d503f7", "AAPL", "call",
    "2026-09-11T19:57:36.779550Z", "226500", "0",
    "RepeatedHits", "AAPL260911C00325000",
)
_AAPL_PUT_BID = _alert(  # bullish
    "b107f8d2-afe4-4b58-b0a1-b5217bd2caa5", "AAPL", "put",
    "2026-09-11T19:54:36.996963Z", "0", "169455",
    "RepeatedHits", "AAPL260925P00335000",
)
_AAPL_PUT_ASK = _alert(  # bearish
    "cc81a257-acb3-4685-bd72-d1922953310e", "AAPL", "put",
    "2026-09-11T19:43:12.075551Z", "163296", "0",
    "RepeatedHits", "AAPL260914P00330000",
)
_NVDA_PUT_BID = _alert(  # bullish
    "c7c5e31c-05b6-451e-acae-bc4dcfc6e6ba", "NVDA", "put",
    "2026-09-11T19:40:26.090077Z", "0", "264670",
    "RepeatedHits", "NVDA261016P00215000",
)
_AAPL_CALL_BID = _alert(  # bearish
    "4d3445ca-ca46-421d-ace4-717541815351", "AAPL", "call",
    "2026-09-11T19:33:41.180626Z", "0", "169810",
    "RepeatedHits", "AAPL260918C00332500",
)
_AAPL_CALL_MIXED = _alert(  # bullish: both sides traded, ask side larger
    "61c8ca00-09e6-4507-b64b-96ab7bd43586", "AAPL", "call",
    "2026-09-11T19:30:33.359721Z", "47610", "31740",
    "RepeatedHits", "AAPL271217C00330000",
)
_MSFT_PUT_BID = _alert(  # bullish
    "35c4e9d1-a174-4a83-a697-c5f7754a86d9", "MSFT", "put",
    "2026-09-11T19:14:07.534737Z", "0", "104610",
    "RepeatedHits", "MSFT260918P00490000",
)
_MSFT_PUT_BID_2 = _alert(  # bullish
    "8f3399be-e93f-4346-b89c-d237891f8281", "MSFT", "put",
    "2026-09-11T18:56:18.517911Z", "0", "816251",
    "RepeatedHitsAscendingFill", "MSFT261016P00495000",
)
_SPY_PUT_TIE = _alert(  # neutral: equal side premiums (live row)
    "1d5e44ac-b511-4a36-be8e-143b810939b0", "SPY", "put",
    "2026-09-11T18:45:16.388000Z", "26236", "26236",
    "RepeatedHits", "SPY261218P00760000",
)

_ALL_ALERTS: list[dict[str, Any]] = [
    _NVDA_CALL_BID,
    _AAPL_CALL_ASK,
    _AAPL_PUT_BID,
    _AAPL_PUT_ASK,
    _NVDA_PUT_BID,
    _AAPL_CALL_BID,
    _AAPL_CALL_MIXED,
    _MSFT_PUT_BID,
    _MSFT_PUT_BID_2,
    _SPY_PUT_TIE,
]


def _ts(raw: str) -> datetime:
    return _parse_iso_utc(raw)


def _epoch(moment: datetime) -> int:
    return math.floor(moment.timestamp())


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records ``(path, params)``; serves stubs, errors and a flow feed.

    The flow feed mimics the server: rows are filtered by ``ticker_symbol``
    and by the epoch-second ``newer_than``/``older_than`` window, newest
    first, at most ``limit`` rows.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, Exception] = {}
        self.flow_rows: list[dict[str, Any]] = []

    def stub(self, path: str, response: dict[str, Any]) -> None:
        self.responses[path] = response

    def stub_error(self, path: str, exc: Exception) -> None:
        self.errors[path] = exc

    def calls_to(self, path: str) -> list[dict[str, Any] | None]:
        return [params for p, params in self.calls if p == path]

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, dict(params) if params is not None else None))
        if path in self.errors:
            raise self.errors[path]
        if path == FLOW_ALERTS_PATH and path not in self.responses:
            return {"data": self._serve_flow(params or {})}
        return self.responses.get(path, {"data": []})

    def _serve_flow(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        symbols = set(str(params["ticker_symbol"]).split(","))
        older = datetime.fromtimestamp(int(params["older_than"]), tz=UTC)
        newer_raw = params.get("newer_than")
        newer = (
            datetime.fromtimestamp(int(newer_raw), tz=UTC)
            if newer_raw is not None
            else None
        )
        out = [
            row
            for row in self.flow_rows
            if row.get("ticker") in symbols
            and _ts(str(row["created_at"])) <= older
            and (newer is None or _ts(str(row["created_at"])) >= newer)
        ]
        return out[: int(params["limit"])]


def _settings() -> UnusualWhalesSettings:
    return UnusualWhalesSettings(cache_ttl=UnusualWhalesProviderCacheTTL())


def _sector_map(client: _FakeClient) -> UnusualWhalesSectorMapProvider:
    return UnusualWhalesSectorMapProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )


def _peer_flow(client: _FakeClient) -> UnusualWhalesPeerFlowProvider:
    return UnusualWhalesPeerFlowProvider(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
    )


def _info(symbol: str, sector: str | None) -> dict[str, Any]:
    return {"data": {**_INFO_AAPL["data"], "symbol": symbol, "sector": sector}}


# ===========================================================================
# SectorMapProvider — sector_of
# ===========================================================================


def test_providers_implement_protocols() -> None:
    client = _FakeClient()
    assert isinstance(_sector_map(client), SectorMapProvider)
    assert isinstance(_peer_flow(client), PeerFlowProvider)


def test_documented_sector_enum_has_eleven_names() -> None:
    assert len(UW_SECTORS) == 11
    assert "Technology" in UW_SECTORS
    assert "Healthcare" in UW_SECTORS
    assert "Health Care" not in UW_SECTORS


@pytest.mark.asyncio
async def test_sector_of_reads_live_info_sector() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    provider = _sector_map(client)
    assert await provider.sector_of("aapl") == "Technology"
    assert client.calls == [("/api/stock/AAPL/info", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("sector", [None, ""])
async def test_sector_of_etf_or_index_is_none(sector: str | None) -> None:
    client = _FakeClient()
    client.stub("/api/stock/SPY/info", {"data": {**_INFO_SPY["data"], "sector": sector}})
    provider = _sector_map(client)
    assert await provider.sector_of("SPY") is None
    assert await provider.peers_of("SPY") == ()
    assert client.calls_to(SCREENER_STOCKS_PATH) == []


@pytest.mark.asyncio
async def test_sector_of_live_spy_fixture_is_none() -> None:
    client = _FakeClient()
    client.stub("/api/stock/SPY/info", _INFO_SPY)
    assert await _sector_map(client).sector_of("SPY") is None


# ===========================================================================
# SectorMapProvider — peers_of
# ===========================================================================


@pytest.mark.asyncio
async def test_peers_by_marketcap_order_excluding_self() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    client.stub(SCREENER_STOCKS_PATH, _SCREENER_TECH)
    provider = _sector_map(client)

    peers = await provider.peers_of("AAPL")

    assert peers == ("NVDA", "MSFT", "AVGO", "MU", "AMD", "INTC", "ORCL")
    assert list(peers[:5]) == ["NVDA", "MSFT", "AVGO", "MU", "AMD"]
    assert client.calls == [
        ("/api/stock/AAPL/info", None),
        (SCREENER_STOCKS_PATH, _EXPECTED_SCREENER_PARAMS),
    ]


@pytest.mark.asyncio
async def test_peers_uppercased_deduplicated_and_malformed_rows_skipped() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AMD/info", _info("AMD", "Technology"))
    client.stub(
        SCREENER_STOCKS_PATH,
        {
            "data": [
                {"ticker": "nvda"},
                {"ticker": "AMD"},
                {"ticker": " msft "},
                {"ticker": "NVDA"},
                {"ticker": ""},
                {"ticker": None},
                {"marketcap": "1"},
                "AVGO",
                {"ticker": "amd"},
                {"ticker": "MU"},
            ],
        },
    )
    peers = await _sector_map(client).peers_of("amd")
    assert peers == ("NVDA", "MSFT", "MU")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sector", ["Tech", "technology", "Health Care", "Financial"],
)
async def test_undocumented_sector_makes_no_screener_call(sector: str) -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _info("AAPL", sector))
    client.stub(SCREENER_STOCKS_PATH, _SCREENER_TECH)
    provider = _sector_map(client)

    assert await provider.sector_of("AAPL") == sector
    assert await provider.peers_of("AAPL") == ()
    assert await provider.peers_of("AAPL") == ()
    assert client.calls_to(SCREENER_STOCKS_PATH) == []


@pytest.mark.asyncio
async def test_one_screener_call_serves_two_tickers_in_same_sector() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    client.stub("/api/stock/MSFT/info", _info("MSFT", "Technology"))
    client.stub(SCREENER_STOCKS_PATH, _SCREENER_TECH)
    provider = _sector_map(client)

    aapl_peers = await provider.peers_of("AAPL")
    msft_peers = await provider.peers_of("MSFT")
    await provider.sector_of("AAPL")
    await provider.sector_of("MSFT")

    assert aapl_peers[:3] == ("NVDA", "MSFT", "AVGO")
    assert msft_peers[:3] == ("NVDA", "AAPL", "AVGO")
    assert "AAPL" not in aapl_peers
    assert "MSFT" not in msft_peers
    assert len(client.calls_to(SCREENER_STOCKS_PATH)) == 1
    assert len(client.calls_to("/api/stock/AAPL/info")) == 1
    assert len(client.calls_to("/api/stock/MSFT/info")) == 1


@pytest.mark.asyncio
async def test_info_not_found_is_no_sector_and_cached() -> None:
    client = _FakeClient()
    client.stub_error(
        "/api/stock/ZZZZQ/info",
        UnusualWhalesNotFoundError("HTTP 422", status_code=422),
    )
    provider = _sector_map(client)

    assert await provider.sector_of("ZZZZQ") is None
    assert await provider.peers_of("ZZZZQ") == ()
    assert len(client.calls_to("/api/stock/ZZZZQ/info")) == 1
    assert client.calls_to(SCREENER_STOCKS_PATH) == []


@pytest.mark.asyncio
async def test_screener_not_found_is_no_peers() -> None:
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    client.stub_error(
        SCREENER_STOCKS_PATH,
        UnusualWhalesNotFoundError("HTTP 404", status_code=404),
    )
    provider = _sector_map(client)
    assert await provider.peers_of("AAPL") == ()
    assert await provider.sector_of("AAPL") == "Technology"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        UnusualWhalesTransientError("HTTP 503 after retries"),
        CircuitBreakerOpenError("breaker open"),
    ],
)
async def test_sector_map_service_errors_propagate_uncached(exc: Exception) -> None:
    """Contract §4: only 404/422 means no data; service errors still raise."""
    client = _FakeClient()
    client.stub_error("/api/stock/AAPL/info", exc)
    client.stub(SCREENER_STOCKS_PATH, _SCREENER_TECH)
    provider = _sector_map(client)

    with pytest.raises(type(exc)):
        await provider.peers_of("AAPL")

    del client.errors["/api/stock/AAPL/info"]
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    assert (await provider.peers_of("AAPL"))[0] == "NVDA"
    assert len(client.calls_to("/api/stock/AAPL/info")) == 2


# ===========================================================================
# PeerFlowProvider — request shape and bucket caching
# ===========================================================================


@pytest.mark.asyncio
async def test_recent_flow_requests_hour_bucket_with_epoch_cursors() -> None:
    client = _FakeClient()
    client.flow_rows = list(_ALL_ALERTS)
    provider = _peer_flow(client)

    await provider.recent_flow(
        tickers=["nvda", "MSFT", "AAPL", "aapl "],
        before=datetime(2026, 9, 11, 19, 47, 12, 500_000, tzinfo=UTC),
        window=timedelta(minutes=30),
    )

    assert client.calls == [
        (
            FLOW_ALERTS_PATH,
            {
                "ticker_symbol": "AAPL,MSFT,NVDA",
                "limit": FLOW_ALERTS_PAGE_LIMIT,
                "older_than": _epoch(datetime(2026, 9, 11, 20, 0, tzinfo=UTC)),
                "newer_than": _epoch(datetime(2026, 9, 11, 18, 30, tzinfo=UTC)),
            },
        ),
    ]


@pytest.mark.asyncio
async def test_non_utc_before_floors_to_the_utc_hour() -> None:
    client = _FakeClient()
    provider = _peer_flow(client)
    # 15:47 EDT == 19:47 UTC: the bucket is the 19:00 UTC hour.
    before = datetime(2026, 9, 11, 15, 47, tzinfo=timezone(timedelta(hours=-4)))
    await provider.recent_flow(
        tickers=["MSFT"], before=before, window=timedelta(minutes=30),
    )
    params = client.calls_to(FLOW_ALERTS_PATH)[0]
    assert params is not None
    assert params["older_than"] == _epoch(datetime(2026, 9, 11, 20, 0, tzinfo=UTC))
    assert params["newer_than"] == _epoch(datetime(2026, 9, 11, 18, 30, tzinfo=UTC))


@pytest.mark.asyncio
async def test_same_hour_events_share_one_fetch_without_look_ahead() -> None:
    client = _FakeClient()
    client.flow_rows = list(_ALL_ALERTS)
    provider = _peer_flow(client)
    peers = ["AAPL", "NVDA", "MSFT"]
    window = timedelta(minutes=30)

    early = await provider.recent_flow(
        tickers=peers,
        before=datetime(2026, 9, 11, 19, 45, tzinfo=UTC),
        window=window,
    )
    late = await provider.recent_flow(
        tickers=list(reversed(peers)),
        before=datetime(2026, 9, 11, 19, 58, tzinfo=UTC),
        window=window,
    )

    assert len(client.calls_to(FLOW_ALERTS_PATH)) == 1
    # 19:45 event: [19:15, 19:45]. The 19:54/19:57/19:59 alerts are later
    # than the event (look-ahead) and the 19:14 alert is before the window.
    assert [e.when for e in early] == [
        _ts(_AAPL_PUT_ASK["created_at"]),
        _ts(_NVDA_PUT_BID["created_at"]),
        _ts(_AAPL_CALL_BID["created_at"]),
        _ts(_AAPL_CALL_MIXED["created_at"]),
    ]
    # 19:58 event: [19:28, 19:58]. The 19:59:00 NVDA alert is still excluded.
    assert [e.when for e in late] == [
        _ts(_AAPL_CALL_ASK["created_at"]),
        _ts(_AAPL_PUT_BID["created_at"]),
        _ts(_AAPL_PUT_ASK["created_at"]),
        _ts(_NVDA_PUT_BID["created_at"]),
        _ts(_AAPL_CALL_BID["created_at"]),
        _ts(_AAPL_CALL_MIXED["created_at"]),
    ]
    assert all(e.when <= datetime(2026, 9, 11, 19, 58, tzinfo=UTC) for e in late)


@pytest.mark.asyncio
async def test_window_boundaries_are_inclusive() -> None:
    client = _FakeClient()
    client.flow_rows = list(_ALL_ALERTS)
    provider = _peer_flow(client)
    before = _ts(_AAPL_PUT_BID["created_at"])  # 19:54:36.996963
    window = before - _ts(_AAPL_CALL_MIXED["created_at"])  # back to 19:30:33

    events = await provider.recent_flow(
        tickers=["AAPL"], before=before, window=window,
    )
    whens = [e.when for e in events]
    assert whens[0] == before
    assert whens[-1] == before - window
    assert _ts(_AAPL_CALL_ASK["created_at"]) not in whens


@pytest.mark.asyncio
async def test_new_hour_peer_set_or_window_is_a_new_fetch() -> None:
    client = _FakeClient()
    client.flow_rows = list(_ALL_ALERTS)
    provider = _peer_flow(client)
    window = timedelta(minutes=30)
    in_hour = datetime(2026, 9, 11, 19, 58, tzinfo=UTC)

    await provider.recent_flow(tickers=["AAPL"], before=in_hour, window=window)
    await provider.recent_flow(
        tickers=["AAPL"], before=in_hour.replace(minute=5), window=window,
    )
    assert len(client.calls_to(FLOW_ALERTS_PATH)) == 1

    await provider.recent_flow(
        tickers=["AAPL"], before=datetime(2026, 9, 11, 20, 5, tzinfo=UTC),
        window=window,
    )
    await provider.recent_flow(
        tickers=["AAPL", "NVDA"], before=in_hour, window=window,
    )
    await provider.recent_flow(
        tickers=["AAPL"], before=in_hour, window=timedelta(minutes=60),
    )
    flow_calls = client.calls_to(FLOW_ALERTS_PATH)
    assert len(flow_calls) == 4
    assert flow_calls[1] == {
        "ticker_symbol": "AAPL",
        "limit": FLOW_ALERTS_PAGE_LIMIT,
        "older_than": _epoch(datetime(2026, 9, 11, 21, 0, tzinfo=UTC)),
        "newer_than": _epoch(datetime(2026, 9, 11, 19, 30, tzinfo=UTC)),
    }
    assert flow_calls[3] is not None
    assert flow_calls[3]["newer_than"] == _epoch(
        datetime(2026, 9, 11, 18, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_recent_flow_rejects_naive_before_and_single_string() -> None:
    provider = _peer_flow(_FakeClient())
    with pytest.raises(ValueError, match="timezone-aware"):
        await provider.recent_flow(
            tickers=["AAPL"],
            before=datetime(2026, 9, 11, 19, 0),
            window=timedelta(minutes=30),
        )
    with pytest.raises(TypeError):
        await provider.recent_flow(
            tickers="AAPL",
            before=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
            window=timedelta(minutes=30),
        )


@pytest.mark.asyncio
async def test_recent_flow_blank_tickers_make_no_call() -> None:
    client = _FakeClient()
    events = await _peer_flow(client).recent_flow(
        tickers=["", "  "],
        before=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert events == ()
    assert client.calls == []


@pytest.mark.asyncio
async def test_recent_flow_not_found_returns_empty() -> None:
    client = _FakeClient()
    client.stub_error(
        FLOW_ALERTS_PATH,
        UnusualWhalesNotFoundError("HTTP 422", status_code=422),
    )
    events = await _peer_flow(client).recent_flow(
        tickers=["ZZZZQ"],
        before=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert events == ()


@pytest.mark.asyncio
async def test_recent_flow_service_error_propagates() -> None:
    client = _FakeClient()
    client.stub_error(FLOW_ALERTS_PATH, CircuitBreakerOpenError("breaker open"))
    with pytest.raises(CircuitBreakerOpenError):
        await _peer_flow(client).recent_flow(
            tickers=["AAPL"],
            before=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
            window=timedelta(minutes=30),
        )


# ===========================================================================
# PeerFlowProvider — row mapping
# ===========================================================================


@pytest.mark.asyncio
async def test_row_maps_ticker_created_at_and_alert_rule() -> None:
    client = _FakeClient()
    client.flow_rows = [_MSFT_PUT_BID_2]
    events = await _peer_flow(client).recent_flow(
        tickers=["MSFT"],
        before=datetime(2026, 9, 11, 19, 0, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert len(events) == 1
    event = events[0]
    assert event.ticker == "MSFT"
    assert event.when == datetime(2026, 9, 11, 18, 56, 18, 517_911, tzinfo=UTC)
    assert event.label == "RepeatedHitsAscendingFill"
    assert event.direction == "bullish"  # put sold at the bid


@pytest.mark.parametrize(
    ("option_type", "ask_prem", "bid_prem", "multileg", "expected"),
    [
        ("call", "226500", "0", False, "bullish"),
        ("call", "0", "250297", False, "bearish"),
        ("put", "163296", "0", False, "bearish"),
        ("put", "0", "169455", False, "bullish"),
        ("call", "47610", "31740", False, "bullish"),
        ("put", "316149", "160", False, "bearish"),
        ("put", "26236", "26236", False, "neutral"),
        ("call", "0", "0", False, "neutral"),
        ("call", "226500", "0", True, "neutral"),
        ("put", "0", "169455", True, "neutral"),
        ("CALL", "226500", "0", False, "bullish"),
        ("wibble", "226500", "0", False, "neutral"),
        (None, "226500", "0", False, "neutral"),
        ("call", None, "0", False, "neutral"),
        ("call", "226500", "n/a", False, "neutral"),
        ("put", "NaN", "0", False, "neutral"),
        ("call", True, "0", False, "neutral"),
        ("call", 226500, 0, False, "bullish"),
    ],
)
@pytest.mark.asyncio
async def test_direction_truth_table(
    option_type: str | None,
    ask_prem: object,
    bid_prem: object,
    multileg: bool,
    expected: str,
) -> None:
    row: dict[str, Any] = dict(_AAPL_CALL_ASK)
    row["type"] = option_type
    row["total_ask_side_prem"] = ask_prem
    row["total_bid_side_prem"] = bid_prem
    row["has_multileg"] = multileg
    client = _FakeClient()
    client.flow_rows = [row]
    events = await _peer_flow(client).recent_flow(
        tickers=["AAPL"],
        before=datetime(2026, 9, 11, 19, 59, tzinfo=UTC),
        window=timedelta(minutes=30),
    )
    assert len(events) == 1
    assert events[0].direction == expected


@pytest.mark.asyncio
async def test_rows_without_ticker_or_created_at_are_dropped_with_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad_time = dict(_AAPL_CALL_ASK, id="x1", created_at="not-a-time")
    no_ticker = {k: v for k, v in _AAPL_PUT_BID.items() if k != "ticker"}
    missing_label = {k: v for k, v in _AAPL_PUT_ASK.items() if k != "alert_rule"}
    client = _FakeClient()
    client.stub(FLOW_ALERTS_PATH, {"data": [bad_time, no_ticker, missing_label]})

    with caplog.at_level("ERROR"):
        events = await _peer_flow(client).recent_flow(
            tickers=["AAPL"],
            before=datetime(2026, 9, 11, 19, 59, tzinfo=UTC),
            window=timedelta(minutes=30),
        )

    assert len(events) == 1
    assert events[0].when == _ts(_AAPL_PUT_ASK["created_at"])
    assert events[0].label is None
    dropped = [r for r in caplog.records if "Dropping UW peer flow alert" in r.message]
    assert len(dropped) == 2


# ===========================================================================
# M25 stage wiring over both providers (stage code unchanged)
# ===========================================================================


def _call_event(ticker: str, timestamp: datetime) -> EnrichedEvent:
    op = OptionsPrint(
        event_id="p39-sector-1",
        timestamp=timestamp,
        ticker=ticker,
        option_type="call",
        strike=Decimal("325.00"),
        expiry=date(2026, 9, 18),
        dte=7,
        spot_price=Decimal("323.10"),
        premium_paid=Decimal("226500"),
        option_price=Decimal("1.51"),
        implied_volatility=0.25,
        bid=Decimal("1.49"),
        ask=Decimal("1.51"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("unusual_whales",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    return EnrichedEvent(print=op)


@pytest.mark.asyncio
async def test_m25_stage_uses_marketcap_peers_and_event_time_window() -> None:
    profile = load_default_profile()
    m25 = profile.scoring.modules.m25
    client = _FakeClient()
    client.stub("/api/stock/AAPL/info", _INFO_AAPL)
    client.stub(SCREENER_STOCKS_PATH, _SCREENER_TECH)
    client.flow_rows = list(_ALL_ALERTS)
    stage = SectorPeerStage(
        sector_provider=_sector_map(client),
        peer_flow_provider=_peer_flow(client),
    )
    event_ts = datetime(2026, 9, 11, 19, 58, tzinfo=UTC)

    result = await stage.enrich(
        _call_event("AAPL", event_ts), PipelineContext(profile=profile),
    )

    ranked = [
        str(row["ticker"]) for row in _SCREENER_TECH["data"] if row["ticker"] != "AAPL"
    ]
    expected_peers = sorted(ranked[: m25.peer_count])
    flow_calls = client.calls_to(FLOW_ALERTS_PATH)
    assert len(flow_calls) == 1
    assert flow_calls[0] is not None
    assert flow_calls[0]["ticker_symbol"] == ",".join(expected_peers)
    window_start = event_ts.replace(minute=0) - timedelta(
        minutes=m25.peer_window_minutes,
    )
    assert flow_calls[0]["newer_than"] == _epoch(window_start)
    # Peers in the fixture feed with an alert inside [19:58 - window, 19:58]:
    # NVDA put sold at the bid (19:40, bullish). NVDA's 19:59 call is later
    # than the event and never counts; AAPL is the event's own ticker.
    assert stage.last_execution_metadata["peers_queried"] == str(len(expected_peers))
    assert result.sector_confirmation_score == m25.strong_alignment_score
    assert stage.last_execution_metadata["branch"] == "strong"


@pytest.mark.asyncio
async def test_m25_stage_etf_takes_no_sector_branch() -> None:
    profile = load_default_profile()
    client = _FakeClient()
    client.stub("/api/stock/SPY/info", _INFO_SPY)
    stage = SectorPeerStage(
        sector_provider=_sector_map(client),
        peer_flow_provider=_peer_flow(client),
    )
    event = _call_event("SPY", datetime(2026, 9, 11, 19, 58, tzinfo=UTC))

    result = await stage.enrich(event, PipelineContext(profile=profile))

    assert result.sector_confirmation_score == profile.scoring.modules.m25.no_sector_score
    assert stage.last_execution_metadata["branch"] == "no_sector"
    assert client.calls_to(SCREENER_STOCKS_PATH) == []
    assert client.calls_to(FLOW_ALERTS_PATH) == []
