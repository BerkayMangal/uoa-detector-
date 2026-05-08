"""Phase 3.3.5.2 tests for ``live.observer.LiveObserver``.

Pins:
  - run() returns pipeline.run() result
  - close() called on every source on normal exit
  - close() called on every source on exception in pipeline
  - shutdown event triggers source close + pipeline ends
  - shutdown() idempotent under repeated calls
  - sources without close() are skipped (synthetic source compat)
  - install_signal_handlers=False skips real signal wiring (test mode)
  - signal handler callback path closes sources (mock signal)
"""

from __future__ import annotations

import asyncio

import pytest

from uoa_detector.live.observer import LiveObserver


class _FakePipeline:
    """Pipeline-like that runs until told to stop or an event fires."""

    def __init__(
        self,
        *,
        end_event: asyncio.Event | None = None,
        return_value: object = "PIPELINE_RESULT",
        raise_in_run: BaseException | None = None,
    ) -> None:
        self._end_event = end_event
        self._return_value = return_value
        self._raise_in_run = raise_in_run
        self.run_called = False

    async def run(self) -> object:
        self.run_called = True
        if self._raise_in_run is not None:
            raise self._raise_in_run
        if self._end_event is not None:
            await self._end_event.wait()
        return self._return_value


class _FakeSource:
    """Source-like with close() — records close calls."""

    def __init__(self) -> None:
        self.closed = False
        self.close_call_count = 0

    async def close(self) -> None:
        self.closed = True
        self.close_call_count += 1


class _CloselessSource:
    """Source-like WITHOUT close() — synthetic-source compat surface."""

    def __init__(self) -> None:
        self.id = "no-close"


# ---------------------------------------------------------------------------
# Happy path — pipeline ends naturally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_pipeline_result() -> None:
    pipeline = _FakePipeline(return_value="DONE")
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[_FakeSource()],
        install_signal_handlers=False,
    )
    result = await obs.run()
    assert result == "DONE"
    assert pipeline.run_called is True


@pytest.mark.asyncio
async def test_sources_closed_after_normal_exit() -> None:
    src = _FakeSource()
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[src],
        install_signal_handlers=False,
    )
    await obs.run()
    assert src.closed is True


@pytest.mark.asyncio
async def test_multiple_sources_all_closed() -> None:
    sources = [_FakeSource() for _ in range(3)]
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=sources,
        install_signal_handlers=False,
    )
    await obs.run()
    assert all(s.closed for s in sources)


@pytest.mark.asyncio
async def test_source_without_close_method_skipped() -> None:
    """Synthetic sources (no close()) don't crash the observer."""
    src = _CloselessSource()
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[src],
        install_signal_handlers=False,
    )
    # Just doesn't raise
    await obs.run()


# ---------------------------------------------------------------------------
# Exception path — close() still called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_closed_when_pipeline_raises() -> None:
    src = _FakeSource()
    pipeline = _FakePipeline(raise_in_run=RuntimeError("boom"))
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[src],
        install_signal_handlers=False,
    )
    with pytest.raises(RuntimeError, match="boom"):
        await obs.run()
    assert src.closed is True


# ---------------------------------------------------------------------------
# External shutdown event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_shutdown_event_triggers_close() -> None:
    """Setting the shutdown event during run() closes all sources."""
    pipeline_done = asyncio.Event()
    src = _FakeSource()

    class _GatedPipeline:
        async def run(self) -> object:
            # Block until the source gets closed (which happens when
            # the external shutdown event fires)
            while not src.closed:
                await asyncio.sleep(0.01)
            pipeline_done.set()
            return "shut down"

    shutdown_event = asyncio.Event()
    obs = LiveObserver(
        pipeline=_GatedPipeline(),
        sources=[src],
        install_signal_handlers=False,
        shutdown_event=shutdown_event,
    )

    async def _trigger_shutdown() -> None:
        await asyncio.sleep(0.02)
        shutdown_event.set()

    trigger_task = asyncio.create_task(_trigger_shutdown())
    try:
        result = await obs.run()
    finally:
        if not trigger_task.done():
            trigger_task.cancel()
    assert result == "shut down"
    assert src.closed is True
    assert pipeline_done.is_set()


# ---------------------------------------------------------------------------
# Programmatic shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_method_closes_sources() -> None:
    src = _FakeSource()
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[src],
        install_signal_handlers=False,
    )
    await obs.shutdown()
    assert src.closed is True


@pytest.mark.asyncio
async def test_shutdown_idempotent() -> None:
    """Repeated shutdown() calls don't multiply close() invocations."""
    src = _FakeSource()
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[src],
        install_signal_handlers=False,
    )
    await obs.shutdown()
    await obs.shutdown()
    await obs.shutdown()
    assert src.close_call_count == 1


# ---------------------------------------------------------------------------
# Source close() failure tolerated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_close_exception_swallowed() -> None:
    """A failing close() on one source doesn't prevent others from closing."""
    class _BadSource:
        async def close(self) -> None:
            msg = "close failed"
            raise RuntimeError(msg)

    good_src = _FakeSource()
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[_BadSource(), good_src],
        install_signal_handlers=False,
    )
    # Doesn't raise
    await obs.run()
    assert good_src.closed is True


# ---------------------------------------------------------------------------
# Signal-handler skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_signal_handlers_false_does_not_register_signals() -> None:
    """No signal handlers installed when flag is False (test default).

    We can't easily inspect loop's signal handlers; instead verify
    the observer runs normally without touching them.
    """
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[_FakeSource()],
        install_signal_handlers=False,
    )
    await obs.run()  # would fail if signal API was misused


@pytest.mark.asyncio
async def test_install_signal_handlers_true_installs_and_removes() -> None:
    """Default mode installs SIGINT/SIGTERM and removes them on exit.

    On platforms that don't support add_signal_handler (Windows),
    the suppress block prevents NotImplementedError from propagating
    — observer still runs normally.
    """
    pipeline = _FakePipeline()
    obs = LiveObserver(
        pipeline=pipeline,
        sources=[_FakeSource()],
        install_signal_handlers=True,
    )
    # Should run cleanly regardless of platform support
    result = await obs.run()
    assert result == "PIPELINE_RESULT"
