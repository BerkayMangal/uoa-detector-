# Phase 5.37 — first daily research roster

Frozen before code, 2026-10-27. Owner authorizes continuing search/tool work and
deployment. A final boundary review found that top-N was applied to each observer
pass, rather than a FIRST family/session roster: if daily bars arrive piecemeal,
a later, higher-ranked name could create another independent PAPER unit after
an earlier name had already been published, exceeding the intended daily cap.
No real orders are involved; prospective signal dates start 2026-10-28 ET.

Fix forward publication only. Append a unique first-observed cohort per frozen
report/config/session, containing at most max_names ranked eligible members with
their original as-of inputs/API context. Database first-writer wins. Later data
cannot add/replace members or repair membership. Publish the original recorded
members/arms idempotently; same-day observations still price existing outcomes.
Future-dated cohort observations are unavailable to an earlier observer.
Preserve pre-existing decisions/observations; if legacy day observations exist,
freeze them within the cap or block new publication when already over cap.
No old evidence deletion, backfilled instruction or extra API call.

The tournament artifact hashes its entire ORIGINAL package, including the old
forward implementation. Keep that original source/report/profile byte-for-byte.
Use a separately versioned forward runtime outside the archived package, still
calling its validated price feature/strategy/context/report functions. Retain
the same PAPER schema/identities and recorded positions; add ONE append-only cohort
table. Include runtime version/hash in newly frozen cohort/decision payloads.
No historical optimization rerun or parameter/result change is needed or allowed.

Tests must demonstrate partial arrivals previously produce >cap, then fixed
first roster; simultaneous writers select one roster, late/future observations
cannot publish, original as-of arms remain immutable, and original pricing/
outcomes/dedup continue. Existing archived tests/report checksum stay valid.
Private route/noindex behaviour unchanged; safe health reads the actual new
runtime state. Full hermetic gate, strict mypy/ruff/lock, green authorized merge
and matching live SHA/job status. Record the boundary and correction candidly.
