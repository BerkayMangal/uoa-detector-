"""Phase 5.0.5: the live worker wires degrading live UW enrichment.

``run_live_worker`` used to build its stages with
``cell_runner.fusion_stages_with_uw``, whose providers propagate every UW
error. It now calls ``build_live_stage_pipeline(client, profile,
degrade_transient_errors=True)``. This test drives one loop iteration with the
network and the pipeline stubbed (as in ``test_gamma_live_loop_wiring.py``)
and inspects what reaches ``Pipeline``:

  - the default stage order;
  - every provider except M23's is its degrading wrapper around the real UW
    provider, all on the one client the worker built;
  - M22 and M24 share one catalyst wrapper;
  - the client key comes from ``Credentials().require_unusual_whales_api_key()``;
  - the store is the replay-safe, flush-every-signal live store.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from webapp import worker

from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
from uoa_detector.pipeline.stages import default_stage_pipeline
from uoa_detector.pipeline.stages.m21_dealer_gamma import DealerGammaStage
from uoa_detector.pipeline.stages.m22_event_calendar import EventCalendarStage
from uoa_detector.pipeline.stages.m23_price_confirmation import (
    PriceConfirmationStage,
)
from uoa_detector.pipeline.stages.m24_iv_exhaustion import IVExhaustionStage
from uoa_detector.pipeline.stages.m25_sector_peer import SectorPeerStage
from uoa_detector.pipeline.stages.m26_dark_pool import DarkPoolStage
from uoa_detector.pipeline.stages.m27_opening_closing import OpeningClosingStage
from uoa_detector.sources.unusual_whales.providers.catalyst_calendar import (
    UnusualWhalesCatalystCalendarProvider,
)
from uoa_detector.sources.unusual_whales.providers.dark_pool import (
    UnusualWhalesDarkPoolProvider,
)
from uoa_detector.sources.unusual_whales.providers.dealer_gamma import (
    UnusualWhalesDealerGammaProvider,
)
from uoa_detector.sources.unusual_whales.providers.degrading import (
    DegradingCatalystCalendarProvider,
    DegradingDarkPoolPrintProvider,
    DegradingDealerPositioningProvider,
    DegradingIVHistoryProvider,
    DegradingOpenInterestProvider,
    DegradingPeerFlowProvider,
    DegradingSectorMapProvider,
)
from uoa_detector.sources.unusual_whales.providers.iv_history import (
    UnusualWhalesIVHistoryProvider,
)
from uoa_detector.sources.unusual_whales.providers.open_interest import (
    UnusualWhalesOpenInterestProvider,
)
from uoa_detector.sources.unusual_whales.providers.price_action import (
    UnusualWhalesPriceActionProvider,
)
from uoa_detector.sources.unusual_whales.providers.sector_peer import (
    UnusualWhalesPeerFlowProvider,
    UnusualWhalesSectorMapProvider,
)


class _Stop(BaseException):
    """Escapes the worker's ``except Exception`` so the test can end the loop."""


class _Restarted(BaseException):
    """Raised if the worker reaches its restart backoff: wiring failed."""


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
        self.tickers = tickers
        self.kwargs = kwargs

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_live_worker_hands_pipeline_degrading_live_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    opened: list[SqliteBacktestStore] = []
    real_open_live_store = worker._open_live_store

    def _recording_open_live_store(database_url: str) -> SqliteBacktestStore:
        store = real_open_live_store(database_url)
        opened.append(store)
        return store

    class _CapturingPipeline:
        def __init__(
            self, sources: list[object], stages: list[object], **kwargs: Any,
        ) -> None:
            captured["sources"] = list(sources)
            captured["stages"] = list(stages)
            captured["kwargs"] = kwargs

        async def run(self) -> list[object]:
            raise _Stop

    async def _restart_sleep(_seconds: float) -> None:
        raise _Restarted

    monkeypatch.setattr(worker, "_open_live_store", _recording_open_live_store)
    monkeypatch.setattr(
        worker,
        "Credentials",
        lambda: SimpleNamespace(
            require_unusual_whales_api_key=lambda: SecretStr("test-key"),
        ),
    )
    monkeypatch.setattr(worker, "UnusualWhalesClient", _StubClient)
    monkeypatch.setattr(worker, "UnusualWhalesFlowPollSource", _StubSource)
    monkeypatch.setattr(worker, "Pipeline", _CapturingPipeline)
    monkeypatch.setattr(worker.asyncio, "sleep", _restart_sleep)

    with pytest.raises(_Stop):
        await worker.run_live_worker(
            tickers=["SPY"], database_url=f"sqlite:///{tmp_path / 'live.db'}",
        )

    # The worker's own client, built from the required credential.
    (source,) = captured["sources"]
    assert isinstance(source, _StubSource)
    client = source.client
    assert isinstance(client, _StubClient)
    assert client.api_key.get_secret_value() == "test-key"

    # The replay-safe live store, exactly as _open_live_store builds it.
    (store,) = opened
    assert captured["kwargs"]["store"] is store
    assert store._replay_safe is True
    assert store._flush_threshold == 1

    stages = captured["stages"]
    assert [type(s).__name__ for s in stages] == [
        type(s).__name__ for s in default_stage_pipeline()
    ]
    by_type = {type(s): s for s in stages}

    m21 = by_type[DealerGammaStage]._provider
    assert isinstance(m21, DegradingDealerPositioningProvider)
    assert isinstance(m21.inner, UnusualWhalesDealerGammaProvider)
    assert m21.inner._client is client

    m22 = by_type[EventCalendarStage]._provider
    assert isinstance(m22, DegradingCatalystCalendarProvider)
    assert isinstance(m22.inner, UnusualWhalesCatalystCalendarProvider)
    assert m22.inner._client is client

    m23 = by_type[PriceConfirmationStage]._provider
    assert isinstance(m23, UnusualWhalesPriceActionProvider)
    assert m23._client is client

    m24 = by_type[IVExhaustionStage]
    assert isinstance(m24._iv_provider, DegradingIVHistoryProvider)
    assert isinstance(m24._iv_provider.inner, UnusualWhalesIVHistoryProvider)
    assert m24._catalyst_provider is m22

    m25 = by_type[SectorPeerStage]
    assert isinstance(m25._sector_provider, DegradingSectorMapProvider)
    assert isinstance(m25._sector_provider.inner, UnusualWhalesSectorMapProvider)
    assert isinstance(m25._peer_flow_provider, DegradingPeerFlowProvider)
    assert isinstance(m25._peer_flow_provider.inner, UnusualWhalesPeerFlowProvider)

    m26 = by_type[DarkPoolStage]._provider
    assert isinstance(m26, DegradingDarkPoolPrintProvider)
    assert isinstance(m26.inner, UnusualWhalesDarkPoolProvider)

    m27 = by_type[OpeningClosingStage]._provider
    assert isinstance(m27, DegradingOpenInterestProvider)
    assert isinstance(m27.inner, UnusualWhalesOpenInterestProvider)
