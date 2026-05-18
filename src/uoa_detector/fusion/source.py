"""``SourceFusion`` — event-time watermark processing for cross-source reconciliation.

Why watermarks (and not merge-sort)
-----------------------------------

A naive multi-source consumer might pick the smallest-timestamp pending event
across all sources at each step (merge-sort). That works for synthetic tests
where every source has data ready, but it fails in production for two reasons:

  1. **Variable latency.** Polygon ~50ms, Unusual Whales ~800ms, IBKR variable.
     If UW hasn't emitted yet, Polygon's events sit unprocessed waiting for
     UW to "catch up". End-to-end latency balloons to the slowest source's
     latency on every event.

  2. **Source death.** If UW dies entirely, merge-sort waits forever for the
     next UW event. The whole pipeline deadlocks.

The correct primitive is **event-time processing with watermarks** — the
pattern used by Flink, Kafka Streams, and Beam. Each source advertises its
own "watermark" (the highest event-time it has emitted so far). The global
watermark is the minimum across active sources: any event with
``event_time <= global_watermark`` is guaranteed in its final position
because no earlier event can arrive from any source. Buckets close when
their deadline (``first_seen + window_ms``) is below the global watermark.

A stalled source (no events for ``stalled_source_timeout_ms`` walltime) gets
its watermark force-advanced to ``walltime - allowed_lateness_ms``, so one
slow or dead source can't hold up the global watermark indefinitely.


Algorithm
---------

State per source ``s``:
  - ``watermark[s]``: max event-time seen from ``s``, or ``None`` if no events yet.
  - ``last_event_walltime[s]``: walltime of last event (or source start), used
    for stall detection.
  - ``done[s]``: True after the source's stream ends.

State for fusion:
  - ``open_buckets[key]: list[Bucket]`` — multiple temporally-disjoint
    buckets per fusion key may be open simultaneously.

Per-event processing:
  1. **Late check.** If ``ev.timestamp < global_watermark``, log
     ``late_event`` warning and drop.
  2. **Watermark advance.** ``watermark[ev.source_id] = max(prev, ev.timestamp)``.
  3. **Bucket assignment.** Find an open bucket for ``ev.bucket_key`` whose
     temporal span (max - min, including ``ev.timestamp``) stays below
     ``window_ms`` if extended. Add to it; else open a new bucket.
  4. **Closure check.** Any open bucket whose ``first_seen + window_ms <=
     global_watermark`` is emitted as a canonical OptionsPrint and removed.

Periodic stall check (every ~quarter of ``stalled_source_timeout_ms``):
  - For each non-done source: if ``walltime_now - last_event_walltime[s] >=
    stalled_source_timeout_ms``, force ``watermark[s] = walltime_now -
    allowed_lateness_ms`` (advancing if higher than current).
  - Then re-run the closure check.

End-of-stream: drain all remaining open buckets unconditionally.


Worked example: single-source fast path
---------------------------------------
``SourceFusion([polygon], params)``: ``is_single_source`` is True → bypass
windowing entirely. Each ``RawPrint`` becomes a canonical ``OptionsPrint``
immediately with ``confidence_tier="single"``, the source's
``source_event_id`` preserved verbatim as the canonical ``event_id``.


Worked example: multi-source happy path
---------------------------------------
``SourceFusion([polygon, unusual_whales], params)``, ``window_ms=500``.

  - Polygon emits ``P_t0`` at t=0. ``watermark[polygon] = t0``;
    ``watermark[unusual_whales] = None`` → ``global_wm = None`` → no closure.
    Bucket ``B1 = [P_t0]``, ``first_seen=t0``, ``last_seen=t0``.
  - UW emits ``U_t100`` at t=100. ``span(B1 ∪ {U_t100}) = 100 < 500`` → joins
    ``B1``. ``B1 = [P_t0, U_t100]``. ``global_wm = min(t100, t0) = t0``.
    ``B1.deadline = t0+500 = t500``. ``t500 > t0`` → no close.
  - Polygon emits ``P_t800`` at t=800. ``span(B1 ∪ {P_t800}) = 800 >= 500``
    → doesn't fit ``B1``. Opens ``B2 = [P_t800]``. ``global_wm = min(t800,
    t100) = t100``. Still ``t500 > t100`` → ``B1`` doesn't close yet.
  - UW emits ``U_t900`` at t=900. ``span(B2 ∪ {U_t900}) = 100 < 500`` → joins
    ``B2``. ``global_wm = min(t800, t900) = t800``. Now ``B1.deadline = t500
    <= t800`` → close, emit canonical for ``B1`` (tier=unanimous if prints
    agree). ``B2.deadline = t1300 > t800`` → still open.
  - End of stream: drain ``B2`` unconditionally.


Worked example: stalled-source recovery
---------------------------------------
``SourceFusion([polygon, unusual_whales, ibkr], params)``, ``window_ms=500``,
``stalled_source_timeout_ms=2000``, ``allowed_lateness_ms=200``.

  - Polygon emits ``P_t0`` at walltime W0. ``watermark[polygon]=t0``.
  - UW emits ``U_t100`` at walltime W0+10ms. ``watermark[unusual_whales]=t100``.
  - IBKR emits one event ``I_t0`` at walltime W0+5ms. ``watermark[ibkr]=t0``.
  - global_wm = min(t0, t100, t0) = t0. No bucket closures yet.
  - **IBKR dies** (no more events ever).
  - Polygon and UW continue emitting. ``global_wm`` stays stuck at IBKR's
    last watermark (t0), even as polygon's and UW's watermarks advance to
    t10000+. Buckets accumulate, never close.
  - Periodic stall check at walltime W0+2000ms detects IBKR last-emitted at
    W0+5ms, age=1995ms... not yet at 2000. Wait.
  - Stall check at walltime W0+2050ms: IBKR age=2045ms >= 2000ms.
    ``watermark[ibkr] = (W0+2050ms) - 200ms``. Assuming W0 ≈ event-time-zero
    in production, this is ``t1850``. Now ``global_wm = min(t0+2050ms,
    polygon.wm, ibkr.wm) = ibkr.wm = t1850``.
  - Buckets with ``first_seen + 500 <= t1850`` (i.e., ``first_seen <= t1350``)
    close. Pipeline unblocks.
  - If IBKR comes back later with an event at ``t<t1850``, that event is
    treated as late and dropped.


Worked example: late-arrival drop
---------------------------------
A late arrival is one with ``event_time < global_watermark``. By the time
``global_watermark`` advanced past ``E.timestamp``, the bucket(s) that could
have contained ``E`` have already been emitted (or were never opened) — so
including ``E`` retroactively would falsify a previously-emitted canonical.

Drop with a structured ``late_event`` warning carrying ``source_id``,
``event_time``, ``global_watermark``, and ``lateness_ms``. The currently-open
canonical-print stream is unaffected.

The drop site is a single ``continue`` — Phase 3 may swap in a "late bucket"
emission path here, e.g., emitting a separate canonical with a flag, but
that's out of scope for Phase 2.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import median

import structlog

from uoa_detector.calibration.profile import FusionParams
from uoa_detector.domain.events import OptionsPrint
from uoa_detector.domain.raw_print import RawPrint
from uoa_detector.errors import DataSourceError
from uoa_detector.fusion.agreement import classify_agreement
from uoa_detector.sources.base import RawFlowSource

_logger = structlog.get_logger(__name__)

# Bucket key — the four fields fusion treats as identity-defining for a print.
# Two prints with the same key from different sources are candidates for
# fusion; different keys never fuse.
_BucketKey = tuple[str, Decimal, date, str]  # (ticker, strike, expiry, option_type)


def _bucket_key(p: RawPrint) -> _BucketKey:
    """Compute the fusion bucket key from a ``RawPrint``."""
    return (p.ticker, p.strike, p.expiry, p.option_type)


@dataclass
class _Bucket:
    """An open fusion bucket. ``first_seen`` and ``last_seen`` are the min/max
    event-times across ``prints``; the temporal span is their difference.
    """

    prints: list[RawPrint]
    first_seen: datetime
    last_seen: datetime


@dataclass
class _SourceState:
    """Per-source fusion state.

    ``watermark`` is the max event-time observed so far (or forced via stall
    advance). ``last_event_walltime`` is the walltime of the last real event
    or stall-advance, used to detect stalls. ``done`` flips True after the
    source's stream ends.
    """

    watermark: datetime | None
    last_event_walltime: datetime
    done: bool = False
    # The walltime of the most recent stall-driven watermark advance for this
    # source — set so we don't re-fire stall logic every tick once a source
    # has gone silent. (See _advance_stalled_watermarks for the read site.)
    last_stall_advance_walltime: datetime | None = field(default=None)


class SourceFusion:
    """Reconcile ``RawFlowSource`` streams into canonical ``OptionsPrint``s
    using event-time watermark processing.

    See module docstring for the full algorithm and worked examples.

    :param sources: One or more ``RawFlowSource`` instances. With one source
        and ``force_multi_source=False`` (default), the constructor takes
        the fast path: each ``RawPrint`` is wrapped immediately into an
        ``OptionsPrint`` with ``confidence_tier="single"`` and zero skew.
    :param params: ``profile.fusion`` settings — windows, watermark tunables,
        and tier thresholds.
    :param force_multi_source: If True, even single-source input goes through
        windowed fusion. Useful for testing the watermark path with one source.
    :param clock: Walltime source for stall detection and forced-watermark
        computation. Defaults to ``lambda: datetime.now(UTC)``. Both purposes
        share the same clock so stall timing and forced-wm advancement are
        coherent.
    """

    def __init__(
        self,
        sources: Sequence[RawFlowSource],
        params: FusionParams,
        *,
        force_multi_source: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not sources:
            msg = "SourceFusion requires at least one source"
            raise DataSourceError(msg)
        self._sources = list(sources)
        self._params = params
        self._force_multi_source = force_multi_source
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def is_single_source(self) -> bool:
        """True iff the fast path (no windowing) is in effect."""
        return len(self._sources) == 1 and not self._force_multi_source

    async def stream(self) -> AsyncIterator[OptionsPrint]:
        """Yield canonical ``OptionsPrint`` events with attached ``SourceAgreement``."""
        if self.is_single_source:
            async for raw in self._sources[0].stream():
                yield self._build_canonical([raw])
            return

        async for canonical in self._stream_multi():
            yield canonical

    async def close(self) -> None:
        """Close every underlying ``RawFlowSource`` (idempotent)."""
        for s in self._sources:
            await s.close()

    # ------------------------------------------------------------------
    # Multi-source watermark path
    # ------------------------------------------------------------------

    async def _stream_multi(self) -> AsyncIterator[OptionsPrint]:
        """Multi-source path with watermark-based bucket closure."""
        now = self._clock()
        state: dict[str, _SourceState] = {
            s.source_id: _SourceState(watermark=None, last_event_walltime=now)
            for s in self._sources
        }

        open_buckets: dict[_BucketKey, list[_Bucket]] = {}

        # Async queue receiving (kind, source_id, raw) triples from feeder tasks.
        # kind ∈ {"event", "done"}.
        queue: asyncio.Queue[tuple[str, str, RawPrint | None]] = asyncio.Queue()

        async def feed(source: RawFlowSource) -> None:
            try:
                async for raw in source.stream():
                    await queue.put(("event", source.source_id, raw))
            finally:
                await queue.put(("done", source.source_id, None))

        feeder_tasks = [asyncio.create_task(feed(s)) for s in self._sources]
        sources_done_count = 0

        # Stall-check cadence: 4 ticks per stall-timeout window. Bounded
        # below by 10ms so we don't spin in tests with very small timeouts.
        check_interval_s = max(
            self._params.stalled_source_timeout_ms / 1000.0 / 4.0,
            0.010,
        )

        try:
            while sources_done_count < len(self._sources):
                try:
                    kind, sid, raw_or_none = await asyncio.wait_for(
                        queue.get(),
                        timeout=check_interval_s,
                    )
                except TimeoutError:
                    # Timer-driven stall check.
                    self._advance_stalled_watermarks(state)
                    for canonical in self._flush_closed_buckets(state, open_buckets):
                        yield canonical
                    continue

                if kind == "done":
                    state[sid].done = True
                    sources_done_count += 1
                    for canonical in self._flush_closed_buckets(state, open_buckets):
                        yield canonical
                    continue

                # kind == "event"
                assert raw_or_none is not None
                raw = raw_or_none

                # Late-arrival check (BEFORE updating this source's watermark).
                global_wm = self._global_watermark(state)
                if global_wm is not None and raw.timestamp < global_wm:
                    lateness_ms = int(
                        (global_wm - raw.timestamp).total_seconds() * 1000.0
                    )
                    _logger.warning(
                        "late_event",
                        source_id=sid,
                        event_time=raw.timestamp.isoformat(),
                        global_watermark=global_wm.isoformat(),
                        lateness_ms=lateness_ms,
                        ticker=raw.ticker,
                        late_event=True,
                    )
                    continue  # drop

                # Update source state
                s_state = state[sid]
                if s_state.watermark is None or raw.timestamp > s_state.watermark:
                    s_state.watermark = raw.timestamp
                s_state.last_event_walltime = self._clock()

                # Add to bucket
                self._add_to_bucket(open_buckets, raw)

                # Closure check
                for canonical in self._flush_closed_buckets(state, open_buckets):
                    yield canonical

            # All sources done. Drain remaining buckets unconditionally.
            for k in list(open_buckets):
                for bucket in open_buckets[k]:
                    yield self._build_canonical(bucket.prints)
                del open_buckets[k]

        finally:
            for t in feeder_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*feeder_tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Bucket assignment + watermark math
    # ------------------------------------------------------------------

    def _add_to_bucket(
        self,
        open_buckets: dict[_BucketKey, list[_Bucket]],
        ev: RawPrint,
    ) -> None:
        """Add ``ev`` to an existing bucket if its span stays under window_ms;
        else open a new bucket for the same key.

        With watermarks, multiple buckets per key can be open simultaneously
        (they are temporally disjoint). We try each in turn; if none fits,
        we open a new one.
        """
        key = _bucket_key(ev)
        existing = open_buckets.get(key, [])
        for bucket in existing:
            new_first = min(bucket.first_seen, ev.timestamp)
            new_last = max(bucket.last_seen, ev.timestamp)
            span_ms = (new_last - new_first).total_seconds() * 1000.0
            if span_ms < self._params.window_ms:
                bucket.prints.append(ev)
                bucket.first_seen = new_first
                bucket.last_seen = new_last
                return

        # No existing bucket fits — open a new one.
        new_bucket = _Bucket(
            prints=[ev],
            first_seen=ev.timestamp,
            last_seen=ev.timestamp,
        )
        if key in open_buckets:
            open_buckets[key].append(new_bucket)
        else:
            open_buckets[key] = [new_bucket]

    def _global_watermark(self, state: dict[str, _SourceState]) -> datetime | None:
        """Compute the global watermark = min watermark over non-done sources.

        Returns ``None`` (no closure) if any non-done source has not yet
        emitted (and has not been stall-advanced) — we can't safely close
        anything because that source may still emit older events.
        """
        active_watermarks: list[datetime] = []
        for s in state.values():
            if s.done:
                continue  # done sources don't constrain the watermark
            if s.watermark is None:
                return None  # at least one active source is unbounded — block
            active_watermarks.append(s.watermark)

        if not active_watermarks:
            return None  # all sources done; closure happens via the EOS drain
        return min(active_watermarks)

    def _advance_stalled_watermarks(self, state: dict[str, _SourceState]) -> None:
        """For each non-done source whose last event is older than
        ``stalled_source_timeout_ms``, force its watermark to
        ``walltime_now - allowed_lateness_ms``.

        Idempotent within a single tick: ``last_event_walltime`` is bumped
        to ``now`` after a forced advance so we don't re-fire repeatedly
        before the next real event or the next timeout window.
        """
        now = self._clock()
        stall_threshold = timedelta(milliseconds=self._params.stalled_source_timeout_ms)
        lateness = timedelta(milliseconds=self._params.allowed_lateness_ms)
        forced_wm = now - lateness

        for sid, s in state.items():
            if s.done:
                continue
            if (now - s.last_event_walltime) < stall_threshold:
                continue
            # Stall fired. Advance the watermark monotonically.
            if s.watermark is None or forced_wm > s.watermark:
                s.watermark = forced_wm
                _logger.info(
                    "source_stall_watermark_advanced",
                    source_id=sid,
                    forced_watermark=forced_wm.isoformat(),
                )
            # Reset the stall clock so we don't re-fire on every check.
            s.last_event_walltime = now
            s.last_stall_advance_walltime = now

    def _flush_closed_buckets(
        self,
        state: dict[str, _SourceState],
        open_buckets: dict[_BucketKey, list[_Bucket]],
    ) -> Iterator[OptionsPrint]:
        """Yield (and remove) every bucket whose ``first_seen + window_ms <=
        global_watermark``. No-op if global_watermark is None.
        """
        global_wm = self._global_watermark(state)
        if global_wm is None:
            return

        window = timedelta(milliseconds=self._params.window_ms)

        for k in list(open_buckets):
            remaining: list[_Bucket] = []
            for bucket in open_buckets[k]:
                deadline = bucket.first_seen + window
                if deadline <= global_wm:
                    yield self._build_canonical(bucket.prints)
                else:
                    remaining.append(bucket)
            if remaining:
                open_buckets[k] = remaining
            else:
                del open_buckets[k]

    # ------------------------------------------------------------------
    # Canonical OptionsPrint construction
    # ------------------------------------------------------------------

    def _build_canonical(self, prints: list[RawPrint]) -> OptionsPrint:
        """Combine a fusion bucket into one canonical ``OptionsPrint``.

        Field-aggregation rules:
          - Bucket-key fields (ticker, strike, expiry, option_type, dte) —
            shared by construction; take from the first print.
          - ``timestamp``: earliest across the bucket.
          - ``premium_paid``, ``option_price``, ``spot_price``, ``bid``, ``ask``:
            median across reporting sources, Decimal-friendly via
            ``statistics.median``.
          - ``implied_volatility``: median of non-None values; raises if no
            source supplies (architectural pressure to pair sourceless-IV
            adapters with a ``QuoteSnapshotSource``).
          - ``open_interest``: max of non-None values (OI is monotonic per
            session); raises if all None.
          - ``fill_side``, ``is_iso``: modal value with deterministic
            tie-breaking by sort order (``False`` < ``True``).
          - ``exchange``: empty string for multi-print buckets — no single
            venue is canonical. Full set in
            ``source_agreement.exchanges_seen``. Single-print bucket inherits
            the source's exchange verbatim.
          - ``event_id``: for n=1 buckets, the source's ``source_event_id``
            verbatim (preserves upstream IDs for downstream correlation);
            for n≥2 buckets, ``"fused:" + sha256(sorted source_event_ids)[:16]``.
          - ``source_agreement``: ``classify_agreement(prints, params.tier_thresholds)``.
        """
        primary = prints[0]

        # ``implied_volatility`` / ``open_interest`` are None when no
        # source in the bucket supplies them. A pure ThetaData replay
        # (Phase 3.5.5) has neither — ThetaData v3 exposes no historical
        # print-level IV and its trade_quote feed carries no OI. Rather
        # than reject the bucket, the canonical print carries None and
        # downstream consumers degrade (print IV has no scoring use; the
        # thin-OI penalty and M28's at-event OI baseline skip on None).
        iv_values = [
            p.implied_volatility for p in prints if p.implied_volatility is not None
        ]
        iv = float(median(iv_values)) if iv_values else None

        oi_values = [p.open_interest for p in prints if p.open_interest is not None]
        oi = max(oi_values) if oi_values else None

        fill_counter = Counter(p.fill_side for p in prints)
        modal_fill = sorted(fill_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        iso_counter = Counter(p.is_iso for p in prints)
        modal_iso = sorted(iso_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        bid = median(p.bid for p in prints)
        ask = median(p.ask for p in prints)
        option_price = median(p.option_price for p in prints)
        spot_price = median(p.spot_price for p in prints)
        premium_paid = median(p.premium_paid for p in prints)

        if len(prints) == 1:
            event_id = prints[0].source_event_id
            exchange = prints[0].exchange
        else:
            composite = ",".join(sorted(p.global_event_id for p in prints))
            event_id = (
                "fused:"
                + hashlib.sha256(composite.encode("utf-8")).hexdigest()[:16]
            )
            exchange = ""

        return OptionsPrint(
            event_id=event_id,
            timestamp=min(p.timestamp for p in prints),
            ticker=primary.ticker,
            option_type=primary.option_type,
            strike=primary.strike,
            expiry=primary.expiry,
            dte=primary.dte,
            spot_price=spot_price,
            premium_paid=premium_paid,
            option_price=option_price,
            implied_volatility=iv,
            bid=bid,
            ask=ask,
            fill_side=modal_fill,
            exchange=exchange,
            is_iso=modal_iso,
            open_interest=oi,
            source_agreement=classify_agreement(prints, self._params.tier_thresholds),
        )
