# Phase5.66.3 — evidence-read performance verification

The initial Phase5.66 release is main`d2647a42161a0f7a11ac57ab20c571f60d2d7eba`,
PR144. Exact feature/main CI passed and Railway deploymentb0c9a200-f2f5-4375-b894-6380e172c85b
wasSUCCESS. New model/fundamental/fusion services reached expected weekend states;
old source/alpha-lab workers completed startup successfully. The complete
November6 report returned200 in108.3348s with all4candidates/12paired references
and their exact source rows unchanged. That response disproved an assumption
that local profile parsing improvement alone would fix native latency.

## Change and limits

`webapp/evidence_reads.py` reuses immutable ORM evidence results only inside an
explicit fixed-time observation. Keys bind engine, statement and exact typed
parameters; mixed/opaque SQL, callable values, alternate schema and special
execution options bypass caching. It retains at most512keys/8192rows/64MiB and
releases them on exit. Over-budget results preserve the complete uncached stream.
Detached snapshots prevent callers mutating cached ORM objects. Core/ORM writes,
flushes and commits disable reuse including ancestor scopes. Threads and async
tasks cannot borrow another observation's results. No cache crosses requests,
provider calls, commits, worker polls or decision times.

Original shadow/timed capture storage and verifiers are unchanged. All original hashes and
physical source checks still execute. Only the presentation/API observation
boundary and new components use this helper. New component identities bind its
exact bytes, so changed new workers activate at the actual next deployment time,
with no backdate into the old November6 cohort. No eligible new fusion candidate
exists before this operational change.

The decision worker batches presence checks for its16preassigned decision keys,
then applies the original full verifier to every present row. Missing/future/
invalid records retain their previous semantics. Claims and final decisions are
written after the read scope ends. The durability scanner exempts only the exact
typed ContextVar token-restoration line; an independent AST assertion pins that
receiver and any other reset still fails. Its database exceptions are unchanged.

## Measurements

- Full nonempty four-candidate report: exact output and original six-field source
  identity parity;627→261SQL,366cache hits,167distinct evidence reads. Remaining94
  table-inspection reads bypass this ORM cache. Local measured CPU/wall improvement
  does not establish native response time.
- Synthetic fixed-time decision selection:4/4eligible,80→49comparable SQL;
  including the roster query50total:36eligibility,1presence batch,12empty feature
  projections and1roster. Claims/writes/model work are excluded. The old proxy's
  implied per-query cost suggests8.5s, but this is an estimate, not native proof.
- Runtime tests cover adverse keys/SQL, exact JSON parameter order, mutation,
  writes/commits, concurrency, budgets, missing/new receipts and unchanged full
  source projection. Existing end-to-end pipeline and authenticated readonly API
  assertions also pass.

Unchanged local gate: **6848passed,52key-gatedskipped,2existingwarnings**;
strictmypy385files, Ruff, lock and whitespace checks clean. This adds34 passing
cases over Phase5.66 (32 cache tests and2 additional guards).

Native timing and final exact CI/deployment SHA are appended to the release PR
following publication. This optimization does not alter signal criteria, old
results, model budgets, statistical gates, subscriptions or database placement.
It cannot create missing historical multimodal data or establish an edge.
