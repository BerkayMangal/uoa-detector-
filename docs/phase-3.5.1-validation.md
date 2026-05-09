# Phase 3.5.1 — Local credential validation

This document is the validation record for Phase 3.5.1. Berkay runs
the operator commands below locally, then fills in the results
sections at the bottom and commits the completed file.

The agent prepares the template; the agent does not execute the
commands. Phase 3.5.1's done-when criteria require Berkay's local
machine: credentials are not in the agent's environment.

---

## What this validates

The four data dependencies Phase 3.5 needs:

  1. **`UNUSUAL_WHALES_API_KEY`** — UW Pro API key in `.env` resolves
     into `Credentials` and the live HTTP client connects.
  2. **`THETADATA_API_KEY` + `THETADATA_USERNAME`** — ThetaData Pro
     credentials in `.env` resolve and ThetaData smokes pass.
  3. **Theta Terminal running locally** — Java app reachable on
     `127.0.0.1:25510`, logged in cleanly. ThetaData historical
     smokes fail to connect without it.
  4. **All eight per-module integration smokes** — M21 through M28
     smokes hit live UW (and ThetaData for M23) endpoints and
     return well-formed responses.

Phase 3.5.2 (download dry-run) and Phase 3.5.3 (full Tier-2
download) consume hours-to-days of bandwidth. **Failing a smoke
here is a 5-minute fix; failing on the download is a 50-day fix.**

---

## Pre-conditions checklist (before running the commands)

Berkay confirms each item before starting:

- [ ] `.env` exists at repo root with three populated keys:
  ```
  UNUSUAL_WHALES_API_KEY=uw_<your_key>
  THETADATA_API_KEY=<your_thetadata_password>
  THETADATA_USERNAME=<your_thetadata_email>
  ```
  (`.env` is gitignored — verified via Phase 3.3.1 pre-commit hook)
- [ ] Theta Terminal running on this machine:
  - Download from thetadata.net dashboard if not installed
  - Launch with `java -jar ThetaTerminal.jar`
  - Verify it logs in cleanly (no auth errors in its console)
  - Verify reachable: `curl http://127.0.0.1:25510/v2/list/exchanges`
    returns JSON, not connection refused
- [ ] Local `phase-3` branch checked out at HEAD `559e4f5` or later
  (Phase 3.5 acceptance contract committed)
- [ ] `uv sync` completed without errors
- [ ] Time of day is within US market hours (Mon-Fri 09:30-16:00 ET)
  if running the live_market smoke. If outside hours, that test
  alone will skip — that's fine.

---

## Command sequence

Run each command in order. If any FAIL, stop and document under
"Issues encountered" below; do not proceed until resolved.

### Step 1 — Confirm credentials load from .env

```bash
uv run pytest tests/integration/test_unusual_whales_smoke.py::test_unusual_whales_credentials_load_from_env_smoke \
              tests/integration/test_thetadata_smoke.py::test_thetadata_credentials_load_from_env_smoke \
              -v
```

Expected: `2 passed`. If skipped, `.env` is not being read — fix
before continuing.

### Step 2 — Confirm live HTTP connections (UW + ThetaData)

```bash
uv run pytest tests/integration/test_unusual_whales_smoke.py::test_unusual_whales_client_connects_smoke \
              tests/integration/test_thetadata_smoke.py::test_thetadata_terminal_reachable_smoke \
              tests/integration/test_thetadata_smoke.py::test_thetadata_historical_one_day_smoke \
              -v
```

Expected: `3 passed`. If UW returns 403, check key tier; if
ThetaData reachable test fails, Theta Terminal isn't running.

### Step 3 — Run all UW provider smokes

```bash
uv run pytest tests/integration/test_unusual_whales_smoke.py -v
```

Expected: `8 passed`. Includes dealer_gamma, iv_history, dark_pool,
catalyst_calendar, open_interest, sector_peer providers + the two
already-validated client/credentials tests.

### Step 4 — Run all per-module M21-M28 integration smokes

```bash
uv run pytest tests/integration/test_m2[1-8]_smoke.py -v
```

Expected: `8 passed`. Each smoke fetches a live data sample for SPY
(M25 uses AAPL) and exercises the module's full code path.

### Step 5 — Run live_market smoke (only during market hours)

```bash
uv run pytest -m live_market -v
```

Expected (during market hours): `1 passed`.
Expected (outside market hours): `0 deselected, 1 skipped` — that's
acceptable.

### Step 6 — Aggregate verification

```bash
uv run pytest tests/integration/ -v 2>&1 | tail -5
```

Expected (during market hours, with all keys present):
- `82 passed` (62 always-pass + 20 newly-passing smokes)
- `0 skipped`

Expected (outside market hours):
- `81 passed` (one live_market test correctly skipped)
- `1 skipped`

### Optional Step 7 — Full repo sanity (background check)

```bash
uv run pytest -q
uv run mypy --strict src/
uv run ruff check .
```

Expected: `1391 passed, 0 skipped` (or `1 skipped` outside market
hours), mypy clean, ruff clean. If anything else changed, that's
unexpected — investigate before continuing.

---

## Results — Berkay fills this section

### Date of validation

`<YYYY-MM-DD HH:MM ET>`

### Step-by-step outcomes

| Step | Description | Expected | Actual | Status |
|------|-------------|---------:|-------:|--------|
| 1 | Credentials load from .env | 2 pass | `<n>` | ☐ pass / ☐ fail |
| 2 | Live HTTP connections | 3 pass | `<n>` | ☐ pass / ☐ fail |
| 3 | UW provider smokes | 8 pass | `<n>` | ☐ pass / ☐ fail |
| 4 | M21-M28 integration smokes | 8 pass | `<n>` | ☐ pass / ☐ fail |
| 5 | live_market smoke | 1 pass or skip | `<n pass / n skip>` | ☐ pass / ☐ skip-acceptable / ☐ fail |
| 6 | Aggregate `pytest tests/integration/` | 81 or 82 pass | `<n pass / n skip>` | ☐ pass / ☐ fail |
| 7 (opt) | Full repo sanity | 1391 pass | `<n>` | ☐ pass / ☐ fail / ☐ skipped |

### Was the live_market step run during market hours?

☐ Yes — should be `1 passed`
☐ No — `1 skipped` is acceptable, will rerun before Phase 3.5.3

### Theta Terminal startup configuration

For the long-running download in Phase 3.5.3, Theta Terminal must
stay up. Confirm one of:

☐ Theta Terminal is configured to launch on system startup
☐ Berkay will manually start it before kicking off Phase 3.5.3 and
  monitor it during the 8-48h window
☐ Theta Terminal was already running when Phase 3.5.1 started (and
  will remain running through Phase 3.5.3)

### Aggregate verdict

☐ **PASS** — All 20 smokes accounted for (passed or live_market-skipped);
  ready to proceed to Phase 3.5.2.
☐ **PASS WITH CAVEAT** — All passed except live_market (outside market
  hours); will rerun before Phase 3.5.3 sign-off.
☐ **FAIL** — One or more smokes failed; documented below.

---

## Issues encountered (Berkay fills if any)

### Issue 1

- **Step:** _(which step number)_
- **Test:** _(which test or command)_
- **Error message:** _(paste relevant error)_
- **Diagnosis:** _(root cause — one of the common modes from
  Phase 3.5.1's "Common failure modes" list, or new)_
- **Resolution:** _(what fixed it)_
- **Re-run result:** _(passed after fix? still failing?)_

_(Add Issue 2, 3, etc. as needed)_

---

## Common failure modes (reference)

If you encounter one of these, the fix is documented:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| ThetaData smokes fail with `connection refused` to 127.0.0.1:25510 | Theta Terminal not running | Launch `java -jar ThetaTerminal.jar`, wait for "logged in" message, retry |
| UW smokes fail with HTTP 403 | UW API key invalid or wrong tier | Verify key in UW dashboard; ensure Pro tier subscription active |
| UW smokes fail with HTTP 429 | Rate limit hit | Lower `data_sources.unusual_whales.rate_limit_requests_per_second` in profile (default in v5_default.yaml); wait 60s and retry |
| Credentials load tests skip | `.env` not at repo root or wrong variable names | Check `.env` exists; variable names exactly match the three documented above |
| M23 smoke fails but M21/M22/M24-M28 pass | ThetaData issue specifically (M23 is the only ThetaData consumer) | Check Theta Terminal logs; restart if stuck |
| All UW smokes pass, all M-smokes skip | M-smokes use a different env-var loading path | Verify `.env` is being picked up by `pytest` (run from repo root, not subdir) |
| live_market smoke skips outside market hours | Expected | Re-run during market hours before Phase 3.5.3 |

---

## Done-when checklist (Phase 3.5.1 acceptance criteria)

Per `docs/phase-3.5-acceptance.md`:

- [ ] 20/20 integration smoke tests pass locally
  - [ ] 8 UW smokes
  - [ ] 3 ThetaData smokes
  - [ ] 1 live_market smoke (or documented skip with re-run plan)
  - [ ] 8 per-module M21-M28 smokes
- [ ] `docs/phase-3.5.1-validation.md` committed with results filled
  in (this file)
- [ ] Berkay confirms Theta Terminal will be running during
  Phase 3.5.3 download window

When all three boxes are checked and this file is committed, Phase
3.5.1 is complete and Phase 3.5.2 (download dry-run) can begin.

---

## Notes for Phase 3.5.2 hand-off

If validation passed, Phase 3.5.2 (download dry-run) can begin
immediately. If anything in this validation revealed a config nudge
(e.g., rate limit lowered), document the change in this file's
"Issues encountered" section — Phase 3.5.2 inherits whatever config
state lands on disk after Phase 3.5.1.

---

_Template prepared by agent in Phase 3.5.1 setup commit. Berkay
fills the Results / Issues sections after running the commands and
commits the updated file._
