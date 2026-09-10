"""Screener self-diagnostic — Phase 3.8.

One run of the screener should reveal end-to-end data health without a
second tool. This module turns the pipeline's existing telemetry into two
compact, stderr-friendly lines:

  1. Flow ingestion: ``flow rows fetched=X, mapped=Y, dropped=Z`` (plus, when
     ``Z > 0``, a sample of the *keys* of dropped rows — never their values,
     so no data leaks). Supplied by the REST flow source's own counters.
  2. Enrichment health: a per-module OK / no-data / error tally computed from
     ``SignalDecisionRecord.stage_executions[*].metadata`` (the ``branch``
     each M-stage recorded). This exposes, e.g., "M21 is erroring on every
     event" or "M25 never gets sector data" from a single run.

Pure functions of their inputs — no scoring, no thresholds, no I/O.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from uoa_detector.observability.decision_record import SignalDecisionRecord

# The seven enrichment M-stages wired into the live pipeline (Phase 3.8).
# M28 is the overnight batch validator, not a PipelineStage, so it is absent.
ENRICHMENT_STAGE_NAMES: tuple[str, ...] = (
    "m21_dealer_gamma",
    "m22_event_calendar",
    "m23_price_confirmation",
    "m24_iv_exhaustion",
    "m25_sector_peer",
    "m26_dark_pool",
    "m27_opening_closing",
)

# Idempotency marker — the stage did not run because the field was preset.
# Not a health outcome; excluded from the tally.
_PRESET_SKIP = "preset_skip"

# Branches that mean "the provider had nothing usable for this event" (as
# opposed to erroring). Everything not here and not a timeout/error branch is
# counted OK — the provider answered and a real score branch was taken.
_NO_DATA_BRANCHES: frozenset[str] = frozenset(
    {
        "no_data",
        "neutral_fallback",
        "data_missing_neutral",
        "no_iv_history",
        "no_sector",
        "empty_peer_flow",
        "no_qualifying_prints",
        "no_current_data",
        "no_prior_data",
        "unknown_option_type",
    },
)


class FlowStats(NamedTuple):
    """Flow-ingestion counters from a fetch-then-map source (REST)."""

    fetched: int
    mapped: int
    dropped: int
    dropped_sample: tuple[tuple[str, ...], ...] = ()


class ModuleHealth(NamedTuple):
    """OK / no-data / error tally for one enrichment module."""

    ok: int
    no_data: int
    error: int

    @property
    def total(self) -> int:
        return self.ok + self.no_data + self.error


def classify_branch(branch: str | None) -> str | None:
    """Classify one stage's ``branch`` into ``ok`` / ``no_data`` / ``error``.

    Returns ``None`` for ``preset_skip`` (not a health outcome) and for a
    missing branch (stage exposed no telemetry).
    """
    if branch is None or branch == _PRESET_SKIP:
        return None
    if "timeout" in branch or "error" in branch:
        return "error"
    if branch in _NO_DATA_BRANCHES:
        return "no_data"
    return "ok"


def module_health(
    records: Iterable[SignalDecisionRecord],
) -> dict[str, ModuleHealth]:
    """Tally OK / no-data / error per enrichment module across all records."""
    counters: dict[str, Counter[str]] = {
        name: Counter() for name in ENRICHMENT_STAGE_NAMES
    }
    for record in records:
        for entry in record.stage_executions:
            if entry.stage_name not in counters:
                continue
            branch = (entry.metadata or {}).get("branch")
            outcome = classify_branch(branch)
            if outcome is not None:
                counters[entry.stage_name][outcome] += 1
    return {
        name: ModuleHealth(
            ok=c["ok"], no_data=c["no_data"], error=c["error"],
        )
        for name, c in counters.items()
    }


def _short_name(stage_name: str) -> str:
    """``m21_dealer_gamma`` -> ``M21``."""
    head = stage_name.split("_", 1)[0]
    return head.upper()


def format_enrichment_line(
    records: Iterable[SignalDecisionRecord],
) -> str:
    """One-line per-module health tally, e.g.

    ``enrichment: M21 ok=3/nodata=1/err=0 | M22 ...``.
    """
    health = module_health(records)
    parts = [
        f"{_short_name(name)} "
        f"ok={h.ok}/nodata={h.no_data}/err={h.error}"
        for name, h in health.items()
    ]
    return "enrichment: " + " | ".join(parts)


def format_flow_line(
    *,
    fetched: int | None,
    mapped: int | None,
    dropped: int | None,
    dropped_sample: Sequence[Sequence[str]] = (),
) -> str:
    """One-line flow-ingestion tally.

    When the counters are ``None`` (a source that does not fetch-then-map in
    one shot, e.g. live WS / historical replay), reports ``n/a``. When
    ``dropped > 0``, appends a sample of dropped rows' *keys* so a schema
    mismatch is a named one-line fix.
    """
    if fetched is None or mapped is None or dropped is None:
        return "flow rows fetched=n/a, mapped=n/a, dropped=n/a"
    line = f"flow rows fetched={fetched}, mapped={mapped}, dropped={dropped}"
    if dropped > 0 and dropped_sample:
        samples = "; ".join(
            "{" + ",".join(sorted(keys)) + "}" for keys in dropped_sample
        )
        line += f" | dropped row keys sample: {samples}"
    return line
