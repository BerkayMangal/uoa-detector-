# Phase 5.49 — conservative source-availability receipt

Recorded before implementation of this receipt, 2026-10-31. Review of the legacy
daily job shows its captured observed_at is the job-context now, passed across
successive vendor requests. It is not proof of the actual response/commit time.
Do not edit that archived capture implementation or reinterpret old timestamps
as exact receipt evidence for the new call-context/prospective experiment.

Add an append-only receipt for each immutable daily-bar key, with payload hash
and a physical timestamp sampled after reading the already committed bar. The
new background observer adopts previously unseen rows; no vendor request or
HTTP write is added. First receipt wins. Strict new context/features require a
matching hash and receipt availability at/before the input/call as-of time.
Old records without such evidence stay unknown for historical attribution.
Current daily-coverage diagnostics can still describe stored data without
claiming its exact earlier availability. Publication continues to use a separate
post-cohort-commit receipt and actual next-opening deadline.

This is a conservative implementation of the frozen as-of evidence contract,
not a hypothesis, threshold, historical replay or live-strategy change. All 144
calculated replay artifacts and their research implementation hashes stay fixed.
Synthetic fixtures may explicitly declare receipt timestamps; actual records
must be stamped from the supplied physical publication clock after the read.
