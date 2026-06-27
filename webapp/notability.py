"""Descriptive 'notability' score for ranking flow (Phase 4.34).

Ranks how LOUD / unusual a flow print is — premium size, aggressiveness (sweep +
at-ask), clustering (the signal's own cluster_density_score, 0..1), freshness,
short DTE. This is NOT a profit prediction (the flow has no proven edge); it is a
triage ordering for the secondary feed. Pure: primitives in, float out.
"""

from __future__ import annotations

import math


def notability_score(
    *,
    premium: float,
    aggressive: bool,
    cluster_density: float,
    age_minutes: float,
    dte: int,
) -> float:
    size = math.log10(max(premium, 1.0))                 # 4 @ $10k, 6 @ $1M
    agg = 1.5 if aggressive else 1.0
    cluster = 1.0 + 0.5 * min(max(cluster_density, 0.0), 1.0)  # up to +50% at density 1.0
    freshness = 1.0 / (1.0 + max(age_minutes, 0.0) / 60.0)  # 1.0 now, 0.5 @ 1h
    urgency = 1.0 + 1.0 / (1.0 + max(dte, 0))            # short DTE louder
    return size * agg * cluster * freshness * urgency
