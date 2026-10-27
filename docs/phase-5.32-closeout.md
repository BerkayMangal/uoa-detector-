# Phase 5.32 — desk data reliability

Implemented the first ordered package of the owner's 2026-10-27 desk upgrade.
PR/release verification follows the repository gate. No trading thresholds,
decision paths, old profiles, PAPER semantics or lab eligibility rules changed.

- Failed news checks now honor the existing 900-second refresh interval. The
  same failure and checked_at remain visible until retry is due. A recovered
  call after the interval can produce checked news; a failure never becomes
  "no headlines". This fixes repeated quota spending every five-minute cycle.
- News checks retain predefined sanitized HTTP 401/403/404/422 categories,
  daily quota/rate, payload and transport failures. Vendor bodies and arbitrary
  exception text never enter the new diagnostic summaries. Legacy ambiguous
  404/422 records remain ambiguous. Home warning text is concise and Turkish.
- Each lab cycle records counts by exclusion reason and ambiguity type. The
  private page explains why candidates were excluded. Reasons overlap; counts
  are not summed as a count of distinct candidates.
- Public health exposes only predefined category/count summaries in cached job
  metadata. No ticker, price, headline, plan, raw response or database query is
  added to the public endpoint. The private indexing/auth boundary is preserved.

Validation: 14 new diagnostic cases cover failure cooldown/recovery, sanitized
HTTP categories, bounded legacy classification, summary allowlisting and audit
counts/private rendering. Full gate target: 4816 passing tests, 52 key-gated
skips, strict mypy (230 files), ruff, lock and whitespace checks. Release checks
must confirm green CI, Railway SHA and actual safe production categories; local
synthetic failures do not establish the live vendor's cause.

Next: use the live categories to distinguish external entitlement/quota problems
from implementation problems, then continue the separately contracted position,
risk, benchmark, scenario and exploratory calibration packages. No live alpha
verdict or permission to place orders is created by this release.
