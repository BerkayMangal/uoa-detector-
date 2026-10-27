"""A measured prior-day API count must never poison a new reservation day."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from freezegun import freeze_time
from webapp.board import options_paper as paper
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables, observed_for_day, reserved_on
from webapp.live_alpha.uw import Budget

from tests.unit.test_uw_client_daily_count import _client, _ok, _Script
from uoa_detector.sources.unusual_whales.client import UnusualWhalesDailyLimitError

NOW = datetime(2026, 10, 28, 5, tzinfo=UTC)
DAY = date(2026, 10, 28)


@pytest.mark.parametrize(
    "at",
    [
        NOW - timedelta(days=1),
        NOW + timedelta(seconds=1),
        None,
        "unverified",
        NOW.replace(tzinfo=None),
    ],
)
def test_stale_future_and_invalid_explicit_observations_cannot_set_floor(at: object) -> None:
    client = SimpleNamespace(last_daily_request_count=24000, last_daily_request_at=at)
    assert observed_for_day(client, DAY, now=NOW) is None


def test_existing_timestamp_less_clients_keep_conservative_counts() -> None:
    assert observed_for_day(SimpleNamespace(last_daily_request_count=24000), DAY, now=NOW) == 24000
    assert observed_for_day(SimpleNamespace(last_daily_request_count=True), DAY, now=NOW) is None


def test_et_boundary_not_utc_midnight_decides_observation_day() -> None:
    client = SimpleNamespace(
        last_daily_request_count=24000, last_daily_request_at=datetime(2026, 10, 27, 23, tzinfo=UTC)
    )
    assert (
        observed_for_day(client, date(2026, 10, 27), now=datetime(2026, 10, 28, 2, tzinfo=UTC))
        == 24000
    )
    assert observed_for_day(client, DAY, now=NOW) is None
    # After DST ends, midnight New York is 05:00 UTC rather than 04:00 UTC.
    client.last_daily_request_at = datetime(2026, 11, 2, 4, 59, tzinfo=UTC)
    assert (
        observed_for_day(client, date(2026, 11, 2), now=datetime(2026, 11, 2, 5, tzinfo=UTC))
        is None
    )


def test_new_day_reserves_but_actual_fresh_high_count_still_refuses(tmp_path: Path) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    ensure_quota_tables(e)
    client = SimpleNamespace(
        last_daily_request_count=24000, last_daily_request_at=NOW - timedelta(days=1)
    )
    budget = Budget(e, DAY, 24000, client, counter_clock=lambda: NOW)
    assert budget.take(NOW) and reserved_on(e, DAY) == 1
    client.last_daily_request_at = NOW
    assert not budget.take(NOW) and reserved_on(e, DAY) == 24000
    assert budget.reserved == 1 and budget.refused == 1


async def test_native_valid_headers_stamp_availability_and_bad_headers_do_not_refresh() -> None:
    client = _client(_Script([_ok("0"), _ok(None), _ok("broken"), _ok("-1"), _ok("0")]))
    try:
        assert client.last_daily_request_at is None
        with freeze_time(NOW - timedelta(days=1)):
            await client.request_json("/api/stock/SPY/info")
        first = client.last_daily_request_at
        assert first == NOW - timedelta(days=1) and client.last_daily_request_count == 0
        with freeze_time(NOW):
            for _ in range(3):
                await client.request_json("/api/stock/SPY/info")
                assert client.last_daily_request_at == first
            await client.request_json("/api/stock/SPY/info")
        assert client.last_daily_request_at == NOW
        with pytest.raises(AttributeError):
            client.last_daily_request_at = NOW  # type: ignore[misc]
    finally:
        await client.aclose()


async def test_error_header_records_time_and_keeps_daily_quota_fail_fast() -> None:
    script = _Script(
        [
            lambda: httpx.Response(
                429,
                text='{"code":"daily_request_limit_hit"}',
                headers={"x-uw-daily-req-count": "30000"},
            )
        ]
    )
    client = _client(script, max_attempts=3)
    try:
        with freeze_time(NOW), pytest.raises(UnusualWhalesDailyLimitError):
            await client.request_json("/api/stock/SPY/info")
        assert client.last_daily_request_at == NOW and client.last_daily_request_count == 30000
        assert script.calls == 1
    finally:
        await client.aclose()


async def test_option_paper_consumer_ignores_old_header_without_changing_fresh_cap(
    tmp_path: Path,
) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    ensure_quota_tables(e)

    class Client:
        last_daily_request_count = 24000
        last_daily_request_at = NOW - timedelta(days=1)
        calls = 0

        async def request_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            return {"chains": []}

    client = Client()
    stats = paper.TrackerReport()
    with freeze_time(NOW):
        assert (
            await paper._historic(
                client, e, "NVDA261120C00100000", day=DAY, cap=24000, report=stats
            )
            == {}
        )
        client.last_daily_request_at = NOW
        with pytest.raises(paper.QuotaRefusedError):
            await paper._historic(
                client, e, "NVDA261120C00100000", day=DAY, cap=24000, report=stats
            )
    assert client.calls == 1 and stats.requests_granted == stats.requests_refused == 1


def test_header_observed_during_scan_is_current_at_the_later_reservation(tmp_path: Path) -> None:
    e = make_engine(f"sqlite:///{tmp_path / 'scan-clock.db'}")
    ensure_quota_tables(e)
    client = SimpleNamespace(
        last_daily_request_count=24000, last_daily_request_at=NOW + timedelta(seconds=5)
    )
    budget = Budget(e, DAY, 24000, client, counter_clock=lambda: NOW + timedelta(seconds=10))
    assert not budget.take(NOW)
    assert reserved_on(e, DAY) == 24000
