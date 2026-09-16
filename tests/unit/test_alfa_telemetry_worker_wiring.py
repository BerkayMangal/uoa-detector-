"""Phase 5.2.A0c: the live worker attaches the Alfa Board telemetry writer.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.1 ("A new
``DecisionRecordWriter`` attached to its ``Pipeline``"); decision P7.

Same harness as ``test_live_worker_wiring.py``: the network and the pipeline
are stubbed and the test inspects what reaches ``Pipeline``.

Pins:
  - ``Pipeline`` receives an ``AlfaTelemetryWriter`` as
    ``decision_record_writer``, still open when ``run`` starts;
  - the writer watches exactly the degrading wrappers on the stages handed to
    that same ``Pipeline`` (identity), for M21, M22, M24, M25, M26 and M27;
  - its run id is the run the replay-safe live store is writing
    (``live-<UTC date>``), and it writes to the worker's database URL;
  - the store is still the one ``_open_live_store`` built;
  - after a pipeline error the worker closes that writer, and the next
    iteration binds a fresh writer to the fresh stages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from webapp import worker
from webapp.board.telemetry import AlfaTelemetryWriter, degrading_wrappers_by_stage

if TYPE_CHECKING:
    from pathlib import Path

    from uoa_detector.backtest.sqlite_store import SqliteBacktestStore

_WRAPPED_STAGES = {
    "m21_dealer_gamma", "m22_event_calendar", "m24_iv_exhaustion",
    "m25_sector_peer", "m26_dark_pool", "m27_opening_closing",
}


class _Stop(BaseException):
    """Escapes the worker's ``except Exception`` so the test can end the loop."""


class _StubClient:
    def __init__(self, *, api_key: SecretStr, settings: object) -> None:
        self.api_key = api_key
        self.settings = settings

    async def aclose(self) -> None:
        return None


class _StubSource:
    source_id = "unusual_whales"

    def __init__(self, client: object, tickers: list[str], **kwargs: object) -> None:
        self.client = client

    async def close(self) -> None:
        return None


def _patch_network(monkeypatch: pytest.MonkeyPatch) -> list[SqliteBacktestStore]:
    opened: list[SqliteBacktestStore] = []
    real_open_live_store = worker._open_live_store

    def _recording_open_live_store(database_url: str) -> SqliteBacktestStore:
        store = real_open_live_store(database_url)
        opened.append(store)
        return store

    monkeypatch.setattr(worker, "_open_live_store", _recording_open_live_store)
    monkeypatch.setattr(
        worker,
        "Credentials",
        lambda: SimpleNamespace(require_unusual_whales_api_key=lambda: SecretStr("test-key")),
    )
    monkeypatch.setattr(worker, "UnusualWhalesClient", _StubClient)
    monkeypatch.setattr(worker, "UnusualWhalesFlowPollSource", _StubSource)
    return opened


def _same_wrappers(writer: AlfaTelemetryWriter, stages: list[object]) -> bool:
    expected = degrading_wrappers_by_stage(stages)
    watched = writer.degrading_by_stage
    return set(watched) == set(expected) and all(
        [id(w) for w in watched[name]] == [id(w) for w in expected[name]] for name in expected
    )


@pytest.mark.asyncio
async def test_run_live_worker_attaches_the_telemetry_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    opened = _patch_network(monkeypatch)
    captured: dict[str, Any] = {}

    class _CapturingPipeline:
        def __init__(self, sources: list[object], stages: list[object], **kwargs: Any) -> None:
            captured["stages"] = list(stages)
            captured["kwargs"] = kwargs

        async def run(self) -> list[object]:
            writer = captured["kwargs"]["decision_record_writer"]
            captured["closed_at_run"] = writer.closed
            captured["run_id_at_run"] = writer._run_id_source()
            raise _Stop

    async def _restart_sleep(_seconds: float) -> None:
        raise AssertionError("worker reached its restart backoff: wiring failed")

    monkeypatch.setattr(worker, "Pipeline", _CapturingPipeline)
    monkeypatch.setattr(worker.asyncio, "sleep", _restart_sleep)
    database_url = f"sqlite:///{tmp_path / 'live.db'}"

    with pytest.raises(_Stop):
        await worker.run_live_worker(tickers=["SPY"], database_url=database_url)

    kwargs = captured["kwargs"]
    (store,) = opened
    assert kwargs["store"] is store  # _open_live_store unchanged
    assert store._replay_safe is True

    writer = kwargs["decision_record_writer"]
    assert isinstance(writer, AlfaTelemetryWriter)
    assert captured["closed_at_run"] is False
    assert writer._database_url == database_url
    assert set(writer.degrading_by_stage) == _WRAPPED_STAGES
    assert _same_wrappers(writer, captured["stages"])
    assert captured["run_id_at_run"] == f"live-{datetime.now(UTC).date().isoformat()}"


@pytest.mark.asyncio
async def test_worker_closes_the_writer_on_error_and_rebinds_on_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_network(monkeypatch)
    iterations: list[dict[str, Any]] = []

    class _FlakyPipeline:
        def __init__(self, sources: list[object], stages: list[object], **kwargs: Any) -> None:
            iterations.append({"stages": list(stages), "writer": kwargs["decision_record_writer"]})

        async def run(self) -> list[object]:
            if len(iterations) == 1:
                msg = "simulated pipeline failure"
                raise RuntimeError(msg)
            raise _Stop

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(worker, "Pipeline", _FlakyPipeline)
    monkeypatch.setattr(worker.asyncio, "sleep", _no_sleep)

    with pytest.raises(_Stop):
        await worker.run_live_worker(
            tickers=["SPY"], database_url=f"sqlite:///{tmp_path / 'live.db'}",
        )

    first, second = iterations
    assert first["writer"] is not second["writer"]
    assert first["writer"].closed is True
    assert second["writer"].closed is False
    assert _same_wrappers(second["writer"], second["stages"])
    assert not _same_wrappers(second["writer"], first["stages"])
