"""Live observer — graceful-shutdown wrapper around Pipeline.

Phase 3.3.5.2. The Pipeline's ``run()`` method drains the fused
source stream until it ends; for batch/synthetic runs the stream
ends naturally. Live sources never end on their own — they
reconnect indefinitely until ``source.close()`` is called from
outside.

LiveObserver adds:

  - SIGINT / SIGTERM handlers that close all sources, allowing the
    Pipeline's stream loop to drain naturally and the writer to
    flush before process exit.
  - Optional external shutdown signalling via ``asyncio.Event`` —
    tests fire this directly without touching real signals.
  - Belt-and-braces source.close() in the finally block so even
    an unexpected exception in the pipeline cleanly closes
    sockets / file handles.

Lifecycle:
  observer = LiveObserver(
      pipeline=Pipeline([uw_source, td_source], stages, ...),
      sources=[uw_source, td_source],
  )
  await observer.run()       # blocks until SIGINT / shutdown event
  # Pipeline returned cleanly; SourceFusion drained; writer flushed.

decision (LiveObserver does NOT own pipeline construction):
  Caller builds the Pipeline (with the stages, profile, store,
  writer it wants) and hands it to the observer. Observer's job
  is shutdown plumbing + signal wiring, NOT pipeline assembly.
  This keeps the test surface tiny: tests pass any Pipeline-like
  object that has ``run()``.

decision (close all sources on shutdown, even unexpected exit):
  ``finally`` block iterates self._sources and calls close() on
  each. If a source is already closed, close() is idempotent
  (UnusualWhalesLiveSource and ThetaDataLiveSource both honour
  the contract). Test-only sources without close() are skipped
  via hasattr-check.

decision (signal handlers installed only if asked):
  ``install_signal_handlers=True`` (default) wires SIGINT /
  SIGTERM. Tests pass False to avoid pytest interference.
  Windows lacks SIGTERM; we suppress NotImplementedError.

decision (no built-in heartbeat / status loop in 3.3.5):
  Berkay's directive: dashboard_refresh_seconds is Phase 4
  reserve. 3.3.5 ships only the plumbing — observer runs the
  pipeline and exits cleanly on shutdown. Periodic logging is
  Phase 4 work via the dashboard refresh tick.

decision (return PipelineResult list verbatim):
  Same return type as Pipeline.run(). Lets callers (CLI / tests)
  inspect what was emitted before shutdown without re-reading
  the writer.

decision (idempotent close() — single-flight via _shutting_down flag):
  Multiple SIGINTs (operator hits Ctrl-C twice) trigger one
  close cycle. Subsequent triggers are no-ops. Pinned by
  test_shutdown_idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal Pipeline protocol (allows tests to pass fake pipelines)
# ---------------------------------------------------------------------------


class _PipelineLike(Protocol):
    """Minimal contract LiveObserver needs from a Pipeline.

    The real ``uoa_detector.pipeline.Pipeline`` satisfies this
    structurally; tests pass simpler stand-ins.
    """

    async def run(self) -> Any: ...


class _ClosableSource(Protocol):
    """A source with an async close() method.

    Both ``UnusualWhalesLiveSource`` and ``ThetaDataLiveSource``
    satisfy this; synthetic/batch sources may not.
    """

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# LiveObserver
# ---------------------------------------------------------------------------


class LiveObserver:
    """Graceful-shutdown wrapper around a Pipeline for live runs."""

    def __init__(
        self,
        *,
        pipeline: _PipelineLike,
        sources: Iterable[Any],
        install_signal_handlers: bool = True,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._sources = list(sources)
        self._install_signal_handlers = install_signal_handlers
        self._shutdown_event = shutdown_event
        self._shutting_down = False
        self._close_task: asyncio.Task[None] | None = None

    async def run(self) -> Any:
        """Run the pipeline; close sources on SIGINT / shutdown event.

        Returns whatever ``pipeline.run()`` returns. Always closes
        all sources (in the ``finally`` block) before returning.
        """
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []

        if self._install_signal_handlers:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.add_signal_handler(
                        sig, self._on_shutdown_signal,
                    )
                    installed_signals.append(sig)

        # If an external shutdown_event was provided, watch it in
        # a side task. When set, trigger the same close path.
        external_watcher: asyncio.Task[None] | None = None
        if self._shutdown_event is not None:
            external_watcher = asyncio.create_task(
                self._watch_external_shutdown(),
            )

        try:
            return await self._pipeline.run()
        finally:
            # Close all sources idempotently. Source close is the
            # signal that lets the fusion loop end naturally if
            # we got here via an exception rather than a clean
            # shutdown signal.
            await self._close_all_sources()
            # Cancel the external watcher (if any)
            if external_watcher is not None and not external_watcher.done():
                external_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await external_watcher
            # Remove signal handlers
            for sig in installed_signals:
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.remove_signal_handler(sig)

    async def shutdown(self) -> None:
        """Programmatically request shutdown.

        Same effect as a SIGINT — schedules close() on every
        source. Idempotent.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        await self._close_all_sources()

    # -- internals --------------------------------------------------------

    def _on_shutdown_signal(self) -> None:
        """Sync signal-handler callback. Schedules async close."""
        if self._shutting_down:
            return
        self._shutting_down = True
        _logger.info("LiveObserver: shutdown signal received")
        loop = asyncio.get_running_loop()
        # Store the task reference to keep a strong ref until completion;
        # otherwise the GC may finalise it before the close coroutines run.
        self._close_task = loop.create_task(self._close_all_sources())

    async def _watch_external_shutdown(self) -> None:
        assert self._shutdown_event is not None
        await self._shutdown_event.wait()
        if self._shutting_down:
            return
        self._shutting_down = True
        _logger.info("LiveObserver: external shutdown event set")
        await self._close_all_sources()

    async def _close_all_sources(self) -> None:
        """Best-effort close of every source with a close() method.

        Exceptions are logged and swallowed; we want shutdown to
        always succeed.
        """
        coros: list[Awaitable[None]] = []
        for src in self._sources:
            close = getattr(src, "close", None)
            if close is None:
                continue
            try:
                result = close()
            except Exception:
                _logger.exception(
                    "LiveObserver: error invoking close() on %r", src,
                )
                continue
            if asyncio.iscoroutine(result):
                coros.append(result)
        if not coros:
            return
        for completed in asyncio.as_completed(coros):
            try:
                await completed
            except Exception:
                _logger.exception("LiveObserver: source close raised")


# Suppress unused import warnings for runtime-only references
_ = _ClosableSource
