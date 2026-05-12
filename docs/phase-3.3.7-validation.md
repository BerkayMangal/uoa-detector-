# Phase 3.3.7 — Validation record

This document is the validation record for Phase 3.3.7. After
Phase 3.3.7.1-4 shipped the v2 → v3 code migration, this sub-phase
(3.3.7.5) updates the smoke tests to v3 paths and asks Berkay to
run them against a real Theta Terminal v3 to confirm the migration
works end-to-end.

The agent prepared the smoke-test code + this template; the agent
does NOT execute the commands (no credentials, no Theta Terminal
v3 on the agent's host). Berkay runs the commands locally, fills
in the Results section below, and commits the completed file.

---

## What this validates

Three real-world checks the unit-test layer can't reach:

  1. **Theta Terminal v3 reachable on port 25503** — connection
     succeeds against ``http://127.0.0.1:25503``. v3 default port
     replaces v2's 25510. Set by ``ThetaDataClient.DEFAULT_BASE_URL``
     (Phase 3.3.7.3).
  2. **v3 endpoints respond with parseable JSON** —
     ``/v3/option/list/symbols`` and ``/v3/option/history/trade``
     return 200 with a JSON body (not the 410 GONE we saw on v2
     endpoints).
  3. **End-to-end M23 wiring** — the price-action provider (Phase
     3.4.3 M23) calls the v3-migrated client, fetches OHLC bars,
     parses ISO timestamps + named-dict bar fields, and produces a
     PriceMovement that the stage consumes successfully.

Unit tests (1412 passing) pin the parsers, URL builders, and
boundary conversions at the wire-format level. This document
records the operator-driven confirmation that the same code paths
work against a real v3 Terminal.

---

## Pre-conditions checklist (before running the commands)

Berkay confirms each item before starting:

- [ ] Local ``phase-3`` branch checked out at HEAD ``c650d41`` or
  later (Phase 3.3.7.4 + smoke-test migration both shipped).
- [ ] ``.env`` exists at repo root with two populated keys:
  ```
  THETADATA_API_KEY=<your_thetadata_password>
  THETADATA_USERNAME=<your_thetadata_email>
  UNUSUAL_WHALES_API_KEY=<not_needed_for_3.3.7.5_but_keep_loaded>
  ```
  (``.env`` is gitignored — verified via Phase 3.3.1 pre-commit hook.)
- [ ] Theta Terminal **v3** running locally:
  - Downloaded from thetadata.net dashboard if not installed
  - Launch with ``java -jar ThetaTerminal.jar``
  - Verify it logs in cleanly (no auth errors in its console)
  - Verify reachable:
    ```
    curl http://127.0.0.1:25503/v3/option/list/symbols?format=json
    ```
    Should return JSON (not connection refused, not 410 GONE).
- [ ] ``uv sync`` completed without errors at HEAD.

---

## Command sequence

Run each command in order. If any FAIL, stop and document under
"Issues encountered" below; do not proceed until resolved.

### Step 1 — ThetaData smoke (3 tests)

```bash
uv run pytest tests/integration/test_thetadata_smoke.py -v
```

Expected: ``3 passed``.

If you see ``410 GONE`` errors → the smoke tests are still hitting
v2 paths (means HEAD is behind ``c650d41``; pull + rerun).
If you see ``connection refused`` → Theta Terminal v3 not running on
port 25503.

### Step 2 — M23 smoke (1 test)

```bash
uv run pytest tests/integration/test_m23_smoke.py -v
```

Expected: ``1 passed``.

This exercises the full M23 pipeline against real v3 OHLC data.
Confirms the provider-injection seam holds across the v3 backend
swap (Phase 3.4.3 stage unchanged; price_action.py provider is
v3-migrated per 3.3.7.3).

### Step 3 — Aggregate ThetaData-dependent integration tests

```bash
uv run pytest -m integration tests/integration/test_thetadata_smoke.py tests/integration/test_m23_smoke.py -v 2>&1 | tail -5
```

Expected: ``4 passed`` (3 ThetaData + 1 M23).

### Step 4 — Full repo sanity (no regressions from 3.3.7.x)

```bash
uv run pytest -q
```

Expected: ``1412 passed, <K> skipped`` where ``K`` is 16 if the
UW key is unloaded (8 UW + 8 M-smokes gated on UW) or fewer if
those are also running.

---

## Results — Berkay fills this section

### Date of validation

`<YYYY-MM-DD HH:MM ET>`

### Step-by-step outcomes

| Step | Description | Expected | Actual | Status |
|------|-------------|---------:|-------:|--------|
| 1 | ThetaData smoke (3 tests) | 3 pass | `<n>` | ☐ pass / ☐ fail |
| 2 | M23 smoke (1 test) | 1 pass | `<n>` | ☐ pass / ☐ fail |
| 3 | Aggregate ThetaData-dependent | 4 pass | `<n>` | ☐ pass / ☐ fail |
| 4 | Full repo sanity | 1412 pass | `<n>` | ☐ pass / ☐ fail |

### What the smoke responses looked like (optional — paste a snippet)

For step 1, the `test_thetadata_historical_one_day_smoke` test
fetches one day of SPY options trades. Paste a representative
trimmed sample of the response shape here:

```
<paste shape of `result`: top-level array of dicts? legacy v2
envelope? both will pass the smoke; the answer informs J3 from
the migration spec>
```

### Aggregate verdict

☐ **PASS** — All 4 smokes pass; v2 → v3 migration is end-to-end
  verified; ready to resume Phase 3.5.1.
☐ **FAIL** — One or more smokes failed; documented below; Phase
  3.3.7 stays open until the cause is fixed.

---

## Issues encountered (Berkay fills if any)

### Issue 1

- **Step:** _(which step number)_
- **Test:** _(which test)_
- **Error message:** _(paste relevant error)_
- **Diagnosis:** _(root cause)_
- **Resolution:** _(what fixed it; may need a "Phase 3.5 bug fix"
  sub-commit per acceptance contract risk section)_
- **Re-run result:** _(passed after fix? still failing?)_

_(Add Issue 2, 3, etc. as needed)_

---

## Common failure modes (reference)

If you encounter one of these, the fix is documented:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| All 3 ThetaData smokes fail with "connection refused" to 127.0.0.1:25503 | Theta Terminal v3 not running | Launch `java -jar ThetaTerminal.jar`, wait for "logged in" message, retry |
| All 3 ThetaData smokes fail with HTTP 410 "We have upgraded to API v3" | Local branch is behind `c650d41` (still on v2 paths) | `git pull` + `uv sync`, retry |
| Smoke connects but returns CSV | `format=json` not being injected | Should be impossible (request_json auto-injects per J5); if it does, file a bug |
| `test_thetadata_historical_one_day_smoke` returns empty | Contract not actually liquid on 2024-01-05, or 0DTE expired before trades happened | Swap target_date / expiry / strike in the test to a contract you know was liquid; commit the parameter change as a "Phase 3.3.7.5 fixture update" |
| M23 smoke returns `branch == "data_missing_neutral"` | ThetaData has no recent intraday data for SPY (off-hours run) | Run during US market hours (09:30-16:00 ET); the smoke spec accepts `data_missing_neutral` as a valid branch but `call_confirmed`/`call_contrarian`/`neutral` indicates richer data |
| JSON decode error on smoke response | Theta Terminal v3 returned an unexpected shape (likely an error body) | Paste the response body in the Issues section; the v3 decoders' defensive fallback should handle it, but unknown shapes warrant investigation |

---

## Done-when checklist (Phase 3.3.7.5 acceptance criteria)

Per ``docs/phase-3.3.7-acceptance.md``:

- [ ] 3 ThetaData smoke tests pass with real Terminal v3 + key
- [ ] 1 M23 smoke test passes
- [ ] ``docs/phase-3.3.7-validation.md`` committed with results
  (this file)
- [ ] Phase 3.3.7 closeout summary appended to
  ``docs/phase-3.3-acceptance.md`` (done in the agent's smoke +
  closeout commit; no Berkay action required)

When all three boxes are checked AND this file is committed,
Phase 3.3.7 is complete and Phase 3.5.1 (credential validation —
already templated in ``docs/phase-3.5.1-validation.md``) resumes
where it left off. v3 Terminal is already running from this
validation; no re-setup needed.

---

## Notes for Phase 3.5.1 hand-off

Phase 3.5.1 was PAUSED at the moment 3.3.7 was inserted (the
acceptance contract calls this out explicitly). After 3.3.7.5
closes:

  1. Theta Terminal v3 stays running (no re-setup needed).
  2. ``.env`` already loaded.
  3. ``docs/phase-3.5.1-validation.md`` template waits for Berkay to
     execute the 7-step command sequence and fill in the results.
  4. Phase 3.5.1's done-when is: 20/20 integration smokes pass
     locally (8 UW + 3 ThetaData + 1 live_market + 8 M21-M28).
     After 3.3.7.5: the 3 ThetaData smokes + 1 M23 smoke are
     ALREADY verified by this validation; the remaining 16 are
     UW-side (8 + 8) plus the 1 live_market.

Phase 3.5.1's check is effectively the UW+M-modules side of the
ledger, since 3.3.7.5 covers ThetaData + M23. Documenting this so
Berkay doesn't re-run 3.3.7.5's tests during 3.5.1 unless something
changes between the two runs.

---

_Template prepared by agent in Phase 3.3.7.5 smoke + closeout
commit. Berkay fills the Results / Issues sections after running
the commands and commits the updated file._
