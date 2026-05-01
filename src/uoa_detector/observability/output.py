"""Decision-record output writers — NDJSON, Pretty, Parquet.

Three implementations sharing a structural ``DecisionRecordWriter``
Protocol:

  - ``NDJSONWriter``: appends one JSON object per line to a file (or
    stdout). Each line is a complete record — concurrent readers see
    only complete records. Suitable for ``jq`` / ``pandas.read_json
    (lines=True)``.

  - ``PrettyWriter``: human-readable terminal output. Multi-line per
    record with section headers. Used for the default CLI display.

  - ``ParquetWriter``: appends to a parquet file via pyarrow. Records
    accumulate in memory and write on ``close()`` (single-file write)
    OR every ``flush_every`` records (rolling-batch writes). For Phase
    2's synthetic scenarios this is single-write at close.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from uoa_detector.observability.decision_record import SignalDecisionRecord


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DecisionRecordWriter(Protocol):
    """Writes ``SignalDecisionRecord``s to a sink (file, stream, parquet)."""

    def write(self, record: SignalDecisionRecord) -> None:
        """Persist one decision record."""
        ...

    def close(self) -> None:
        """Flush any buffered state and release resources. Idempotent."""
        ...


# ---------------------------------------------------------------------------
# NDJSON
# ---------------------------------------------------------------------------


class NDJSONWriter:
    """Append one JSON object per line.

    Construct with either a file path (opens text-append mode, line-
    buffered) or an open text stream (e.g., ``sys.stdout``). Each
    ``write()`` call serializes the record via Pydantic's ``model_dump_json``
    (handles Decimal/datetime/UUID natively) and appends a newline.

    Records are flushed immediately so a partial run leaves valid NDJSON
    on disk — important for long backtests.
    """

    def __init__(self, sink: Path | TextIO) -> None:
        self._owns_handle = False
        if hasattr(sink, "write") and not isinstance(sink, type(sys.stdout)):
            # Open file or stdout-like stream — caller owns it.
            self._handle: TextIO = sink  # type: ignore[assignment]
        elif hasattr(sink, "write"):
            self._handle = sink  # type: ignore[assignment]
        else:
            # Path-like — open it ourselves.
            self._handle = open(sink, mode="a", encoding="utf-8")  # noqa: SIM115
            self._owns_handle = True
        self._closed = False

    def write(self, record: SignalDecisionRecord) -> None:
        if self._closed:
            msg = "NDJSONWriter is closed"
            raise RuntimeError(msg)
        # by_alias=True emits 'print' instead of 'print_' for EnrichedEvent
        # — matches the field shape callers expect when consuming records.
        line = record.model_dump_json(by_alias=True)
        self._handle.write(line)
        self._handle.write("\n")
        self._handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_handle:
            self._handle.close()


# ---------------------------------------------------------------------------
# Pretty
# ---------------------------------------------------------------------------


class PrettyWriter:
    """Human-readable terminal output.

    Each record renders as a multi-line block with section headers
    (Identity, Sub-scores, Adjustments, Penalties, Decision, Size).
    Useful for sanity-checking; not intended for downstream parsing.
    """

    def __init__(self, sink: TextIO | None = None) -> None:
        self._handle: TextIO = sink or sys.stdout
        self._closed = False

    def write(self, record: SignalDecisionRecord) -> None:
        if self._closed:
            msg = "PrettyWriter is closed"
            raise RuntimeError(msg)

        ev = record.event
        pr = ev.print_
        lines: list[str] = []
        lines.append(
            f"=== {pr.ticker} {pr.option_type.upper()} "
            f"{pr.strike} {pr.expiry} | {pr.timestamp.isoformat()} ===",
        )
        lines.append(
            f"  decision: {record.decision.label.value} "
            f"(R={record.size.max_r}); reason: {record.decision.reason}",
        )
        lines.append(
            f"  combined_score: pre={ev.combined_score_pre_penalty} "
            f"post={ev.combined_score_post_penalty}",
        )

        # Score breakdown
        components = ", ".join(
            f"{k}={v:+.4f}" for k, v in record.score_breakdown.items()
        )
        lines.append(f"  breakdown: {components}")

        # Sub-scores
        sub_parts = []
        for name in (
            "uoa_score", "convexity_score", "event_score", "gamma_score",
            "price_confirmation_score", "sector_confirmation_score",
            "time_of_day_weight", "cluster_density_score", "relative_premium_score",
        ):
            v = getattr(ev, name)
            sub_parts.append(f"{name}={v}")
        lines.append("  sub-scores: " + ", ".join(sub_parts))

        # Adjustments
        if ev.score_adjustments:
            adj_parts = [
                f"{a.target}{a.delta:+.4f} ({a.source_module}: {a.reason})"
                for a in ev.score_adjustments
            ]
            lines.append("  adjustments: " + "; ".join(adj_parts))

        # Penalties
        if ev.applied_penalties:
            pen_parts = [
                f"{p.name}={p.value:+.4f}" for p in ev.applied_penalties
            ]
            lines.append("  penalties: " + ", ".join(pen_parts))

        # Source agreement
        sa = pr.source_agreement
        lines.append(
            f"  source: tier={sa.confidence_tier}, "
            f"sources_seen={list(sa.sources_seen)}, "
            f"exchanges_seen={list(sa.exchanges_seen)}",
        )

        # Profile
        lines.append(
            f"  profile: {record.profile_id} (content_hash="
            f"{record.profile_content_hash[:12]}...)",
        )

        if ev.missing_sub_scores:
            lines.append(f"  missing_sub_scores: {ev.missing_sub_scores}")

        self._handle.write("\n".join(lines) + "\n\n")
        self._handle.flush()

    def close(self) -> None:
        self._closed = True
        # Don't close stdout — the process owns it.


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


class ParquetWriter:
    """Buffer records and write to a parquet file on ``close()``.

    Phase 2: simple single-file write. For long-running backtests, swap
    to ``pyarrow.parquet.ParquetWriter`` row-group streaming in a future
    phase. The buffer pattern is fine for typical scenario runs (8-event
    synthetic = trivial buffer).

    Records flatten to a wide schema: top-level scalar fields stay
    scalars; nested objects (event, source_agreement, applied_penalties,
    score_breakdown, stage_executions) become JSON-encoded strings. This
    keeps the schema stable across pyarrow versions and avoids struct-
    type fragility — analysts can ``json.loads()`` what they need.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._buffer: list[dict[str, object]] = []
        self._closed = False

    def write(self, record: SignalDecisionRecord) -> None:
        if self._closed:
            msg = "ParquetWriter is closed"
            raise RuntimeError(msg)
        # Flat top-level: identity + label + score for grep-ability;
        # everything else as a JSON string for schema stability.
        ev = record.event
        pr = ev.print_
        flat: dict[str, object] = {
            "decision_emitted_at": record.decision_emitted_at,
            "profile_id": record.profile_id,
            "profile_content_hash": record.profile_content_hash,
            "ticker": pr.ticker,
            "option_type": pr.option_type,
            "strike": str(pr.strike),
            "expiry": pr.expiry.isoformat(),
            "dte": pr.dte,
            "premium_paid": str(pr.premium_paid),
            "timestamp": pr.timestamp,
            "label": record.decision.label.value,
            "label_reason": record.decision.reason,
            "max_r": record.size.max_r,
            "combined_score_pre_penalty": ev.combined_score_pre_penalty,
            "combined_score_post_penalty": ev.combined_score_post_penalty,
            "confidence_tier": pr.source_agreement.confidence_tier,
            # Nested objects → JSON strings
            "event_json": ev.model_dump_json(by_alias=True),
            "score_breakdown_json": json.dumps(record.score_breakdown),
            "stage_executions_json": json.dumps(
                [e.model_dump() for e in record.stage_executions],
            ),
        }
        self._buffer.append(flat)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._buffer:
            return
        # Lazy import — pyarrow is heavy; only import on actual write.
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist(self._buffer)
        pq.write_table(table, self._path)  # type: ignore[no-untyped-call]
