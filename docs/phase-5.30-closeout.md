# Phase 5.30 — prospective alpha laboratory

Engineering implementation complete. Release checks are tracked in the feature
PR; empirical validation remains open until prospective outcomes exist. This
phase does not change the canonical statement that no tradeable edge is proven.

## Delivered

1. The existing UW stock flow-alerts response is archived before the legacy
   reducer. Original JSON, provider time, first observed availability and payload
   revisions survive replay/restart. Successful empty polls are coverage evidence;
   an unmade, failed or malformed request cannot stand in for an empty feed.
   Seven new tables are classified append-only in the existing durability guard.
   No historical reconstruction, old-table migration or new API requests.
2. A separate 15-minute flow calculation uses call ask + put bid for upward
   premium and call bid + put ask for downward premium. Thus a 510k/490k call
   contributes 510k up, 490k down, net 20k, not one million up. Multi-leg,
   unknown-flag, invalid-partition, invalid-contract and contradictory alerts
   cannot make a clean treated or control observation. Alert deduplication
   does not establish independent underlying trades.
3. The peer-recovery pilot freezes the first stored common-eligible observation
   per version/ticker/ET session, including its arm and originally available
   priced peers. A later flow reversal cannot move that cohort. Missing capture,
   page saturation gaps, stale prices, missing anchors and unavailable calendar
   sessions are explicit exclusions. Successful continuous empty capture may
   support a control; missing capture never does.
4. News reuse is conservative and auditable: positive/negative/mixed/unclassified
   headline rules, same-text syndication, retrieval failure, first-observed flow
   versus headline order, and a cached-price comparison only when a prior quote
   was actually recorded before the headline. Conflicting alert revisions also
   invalidate their directional timing evidence. No extra NLP service, surprise
   inference, causal attribution, or news filter in the baseline experiment.
5. Private `/laboratuvar` shows coverage and corrected flow, frozen arms, priced
   denominators and results, with news/method/operational details folded away.
   Existing authentication, noindex and private/no-store protections apply.
   Health exposes only research status/counts/timestamps/error classes.

## Decisions and operational limits

- New `alpha_lab_v1.yaml` freezes a pilot; all pre-existing profiles, legacy
  evaluation, BUY ranking, sizing, PAPER tracking and rejected studies remain
  unchanged. Research cannot promote a rule into live recommendations.
- Primary horizon is three sessions; one and five are labelled secondary.
  Entry is the next regular-session open, exit the Nth session close including
  entry as session one. Every stock/peer/SPY comparison uses those exact dates,
  with 5 bps adverse slippage each side. Missing any required price leaves that
  outcome unknown; no peer is dropped to improve price coverage.
- These are descriptive stock counterfactuals, without fees, taxes or borrow,
  not broker fills, an executed hedge, or an options return. Controls are
  observational, the universe is UW-selected and samples overlap. No automated
  significance, pass verdict, or claim of alpha is supplied. Power, stability,
  multiplicity and cost sensitivity require a separately frozen analysis plan.
- Raw capture runs off the event loop in bounded batches with a five-second
  wait bound. The shadow job follows the legacy scan with a 30-second wait
  bound and a single-flight lock. Python cannot terminate a database thread
  already in progress: the lock prevents another research cycle from overlapping
  a timed-out one. Legacy ingestion and evaluation continue on research failure.
- An incomplete-outcome batch is bounded to 500 cohorts; a persisted rotating
  cursor prevents old missing-price cohorts from monopolizing processing.
  The screen discloses cohort backlog and the last processed batch's missing
  and pending horizon counts, rather than implying all outcomes were processed.
- Reporting bounds observations to the latest 50 tickers and uses SQL aggregates
  for result groups. Underlying forward evidence is retained without a deletion
  policy. A cold start needs 15 minutes of observed continuous flow coverage.

## Verification

- Final local gate: **4727 passed, 52 credential-gated skips, two existing
  warnings**; strict mypy (228 files), ruff, locked dependency check and whitespace
  check pass. No vendor-key integration tests are represented as executed.
- 49 new alpha-lab cases cover the source-to-archive path without extra calls,
  exact premium allocation, invalid/multi-leg/unknown flags, original/revised
  identities, as-of availability, polling gaps/saturation, frozen arms and peers,
  matched outcomes, missing prices, exchange holidays/early close, headline
  negation/syndication/order and quote availability, failure/timeout isolation,
  supervisor wiring and private rendering. The existing authentication matrix
  additionally exercises the new route; net suite increase is 64 cases.
- Chromium at 1440 and 390 pixels: no document overflow with all details open,
  no script errors or external asset requests; corrected sides, both arms,
  headline order, price context and retrieval failure render correctly.
  Axe WCAG A/AA and 2.1 AA: zero reported violations. Scroll regions are
  keyboard-focusable. Browser evidence uses explicitly labelled LOCAL fixtures,
  not production results; images live outside the checkout in
  `/workspace/uoa-design-review/lab-fixture-*.png`.
- Full suite includes the append-only protection and pre-existing live/PAPER
  policy tests. No scoring-module branch or public allowlist was changed.

Release verification: green PR gate, merge through PR, Railway success, matching
health SHA, gated `/laboratuvar` and public SEO checks; observe actual capture/job
status after startup. Production Basic credentials are not present in this
workspace, so authenticated production-screen inspection is not claimed.

Next: accumulate forward cohorts and complete future 1/3/5-session outcomes,
inspect missingness and arm balance, then freeze the separate statistical
analysis plan before making any performance or live-promotion decision.
