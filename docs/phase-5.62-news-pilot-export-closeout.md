# Phase5.62 — prospective news pilot access

Implementation verified. Exact feature/main CI, release SHA, native deployment
and actual pilot source/result evidence are recorded in the release PR after
publication; no unexecuted pilot is reported as measured here.

The private same-origin POST exports one complete ET calendar day through the
unchanged physical-news adapter. Inclusive adapter bounds end one microsecond
before the next midnight, so adjacent exports do not duplicate midnight scans.
Current/future/unverified days and pre-context periods refuse. Partial first
context sessions retain unknown legacy gates rather than gaining full coverage.

No vendor request, DB write, table creation, live threshold change or research
loop occurs in the handler. Metadata preflight caps256 scans/16MiB characters;
the adapter's existing10000 bounds and an8MiB response cap remain. Conflicts,
caps and missing storage refuse the entire response. A nonblocking process lock
also rejects a concurrent export while the first adapter is still executing.
Only typed prices/gates/provenance leave the database; no raw snapshot/headline,
credential or SQL detail is exported. GET is405; auth/origin/private cache guards
are tested.

Full unchanged gate: **6188 passed,52 key-gated skips,two existing warnings**,
151.83s. Strict mypy clean on336 files; Ruff,lock and whitespace checks pass.
New focused cases exercise real stored scans/physical receipts through the
adapter and frozen evaluator, a fully priced five-arm example with modeled
costs, missing marks/receipts, partial first-session identity, deterministic
re-export, DST/nonoverlap, rejection/cap/conflict paths and actual concurrent
lock release. Existing assertions and frozen research implementations remain.

The actual November4/5 pilot uses only already registered A/B/C/D/E and cost
stresses. Disjoint parent identities remain explicit; its combined report is
not an additional independent experiment. Report completeness before returns,
keep missing unknown, and preserve80 complete common sessions as the inference
gate. No fitted trial, selected winner, calibrated probability, historical
option NBBO P&L or promoted live rule is introduced. Mahalle/Stajyer context,
legacy underlying marks, SOURCE PAPER and targeted references remain separate.

Next admissible calibration evidence requires genuinely new complete sessions
and sound quote/benchmark coverage; repeated searches on the frozen negative
historical archive do not supply independent validation.
