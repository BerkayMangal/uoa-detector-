# Phase 3.3.7 — ThetaData API v2 → v3 migration

This is an unplanned sub-phase added during Phase 3.5 setup. Phase
3.3.2 implemented the ThetaData adapter against API v2; ThetaData
released v3 in 2025 and v2 endpoints now return `410 GONE` from
the current Theta Terminal release. The v2 binary that Phase 3.3.2
expected is no longer publicly available.

This sub-phase migrates the ThetaData adapter to API v3. It is
narrow scope: only `src/uoa_detector/sources/thetadata/` and its
tests. No M-stage logic changes; no profile changes outside the
ThetaData section; no domain model changes.

After this phase, Phase 3.5.1 (credential validation) can resume.

---

## Approved decisions (locked in before implementation)

1. **Migration target: API v3 stable.** Per ThetaData docs at
   docs.thetadata.us. Beta features deferred unless required by
   existing functionality.

2. **Reference document: ThetaData's official migration guide.**
   `docs.thetadata.us/Articles/Getting-Started/v2-migration-guide.html`.
   Agent reads this, follows the v2 → v3 endpoint mappings.
   Decisions where the guide is silent are documented as judgment
   calls.

3. **Scope is the existing surface only.** Whatever Phase 3.3.2's
   adapter exposes today (mapping, client, historical, live), the
   v3 adapter exposes the same surface. No new features. No
   v3-only capabilities (Greeks, IV, etc.) added in this phase.

4. **Default port assumption changes from 25510 to 25503.** Theta
   Terminal v3 listens on 25503 by default. Updated in
   `data_sources.thetadata.host` profile section.

5. **All tests stay green.** mapping unit tests pin response shapes
   that may have changed; tests get updated alongside the
   implementation. Smoke tests gated by `THETADATA_API_KEY`
   continue to be gated.

6. **`hist` + `bulk_hist` consolidated into `history`.** Per v3
   migration guide, the historical fetch is one endpoint now.
   Phase 3.3.2 had two code paths; Phase 3.3.7 collapses them.

7. **Idempotent re-running.** If Berkay starts Phase 3.3.7, gets
   half-way, and stops — re-running picks up from the green commit
   point with no manual cleanup.

---

## Phase 3.3.7.1 — Investigation + endpoint mapping doc

Before any code change, agent reads ThetaData v3 docs and writes
a translation table.

**Scope:**
- Agent fetches `docs.thetadata.us/Articles/Getting-Started/v2-migration-guide.html`
- Writes `docs/thetadata-v3-migration.md` with:
  - Per-endpoint v2 → v3 mapping (URL path, parameter names,
    response shape changes)
  - Identified breaking changes that affect our adapter
  - Items deferred (not currently used in our adapter)
  - Items that need new judgment (v2 had X, v3 doesn't, what do
    we do?)
- This doc is the working spec for the rest of Phase 3.3.7

**Done when:**
- `docs/thetadata-v3-migration.md` committed
- All v2 endpoints we use have a v3 mapping listed
- Items not yet decided are flagged with `TBD: judgment needed`

---

## Phase 3.3.7.2 — Mapping module update

Update `src/uoa_detector/sources/thetadata/mapping.py` for v3
response shapes.

**Scope:**
- v3 response field names where they differ
- DTE / strike encoding / condition codes — verify v3 still uses
  the same OPRA Pillar conventions
- New fields in v3 responses we should ignore (forward-compat)
- Unit tests updated with v3-shape fixtures
- Decimal strike round-trip still pinned

**Done when:**
- `tests/unit/test_thetadata_mapping.py` (or wherever current
  mapping tests live) all green
- mypy strict + ruff clean
- No `# TODO v3` markers left in mapping.py

---

## Phase 3.3.7.3 — Client + historical update

Update `client.py` and `historical.py` to v3 endpoints.

**Scope:**
- Default port → 25503
- Endpoint URL paths updated to `/v3/*`
- `hist` and `bulk_hist` consolidated to single `history` endpoint
- Auth / rate limit / retry / circuit breaker logic UNCHANGED
  (these don't depend on v2 vs v3)
- Historical downloader uses new endpoint shape but produces same
  parquet output (Phase 3.3.4 download script doesn't need to
  change)
- Mock HTTP transport tests updated with v3 response shapes

**Done when:**
- All client + historical unit tests green
- mypy strict + ruff clean
- Backwards-compat for the parquet schema preserved (Phase 3.3.4
  download script + Phase 3.5.3 download stay compatible)

---

## Phase 3.3.7.4 — Live source update

Update `live.py` for v3 WebSocket frame format if changed.

**Scope:**
- Verify v3 WS frame structure (per migration guide)
- Subscription payload format if changed
- All WS unit tests updated with v3 frames
- Reconnect logic unchanged

**Done when:**
- All live source unit tests green
- Backoff-resets-on-frame still pinned (Phase 3.3.2 invariant)

---

## Phase 3.3.7.5 — Smoke tests + closeout

Update smoke tests for v3, verify migration works end-to-end with
real Terminal.

**Scope:**
- `tests/integration/test_thetadata_smoke.py` updated:
  - Default port 25503
  - v3 endpoint URLs
  - All 3 smoke tests still gated by `THETADATA_API_KEY`
- Berkay starts Theta Terminal v3 locally, sets `THETADATA_API_KEY`,
  runs smoke tests, captures pass/fail in
  `docs/phase-3.3.7-validation.md`
- M23 smoke test (Phase 3.4.3) still passes since it uses the same
  `ThetaDataPriceActionProvider` which is now v3-backed

**Done when:**
- 3 ThetaData smoke tests pass with real Terminal v3 + key
- 1 M23 smoke test passes
- `docs/phase-3.3.7-validation.md` committed with results
- Phase 3.3.7 acceptance summary appended to
  `docs/phase-3.3-acceptance.md` (or its own closeout doc)

---

## Cross-cutting acceptance

- `pytest -q` green at every commit
- `mypy --strict` clean
- `ruff check .` clean
- No new dependencies added (httpx, websockets sufficient for v3)
- Phase 3.3.4 download script (`scripts/download_tier2.py`) works
  unchanged — produces same parquet output
- Phase 3.4.3 M23 stage works unchanged — provider injection is
  the seam, implementation behind it changed
- 1391 → 1391 (or higher; new tests OK) tests passing

---

## What Phase 3.3.7 explicitly does NOT do

- Add v3-only features (Greeks endpoint, IV endpoint, MCP server)
- Change M21-M28 module logic
- Touch profile threshold values
- Reduce existing test coverage

---

## What Berkay needs to do during Phase 3.3.7

1. Phase 3.3.7.5 smoke tests need real Theta Terminal v3 running
   locally + `THETADATA_API_KEY` in `.env`. Berkay runs the smoke
   step, agent does the code+test work for 3.3.7.1-3.3.7.4.

2. After Phase 3.3.7 closes, Phase 3.5.1 resumes with v3 Terminal
   already running.

---

## Risk: v3 may have semantic differences not in the migration guide

The migration guide covers endpoint mappings but not necessarily
every behavior change. Common gotchas:
- Response timestamp format (epoch ms vs ISO?)
- Field defaults (null vs missing?)
- Pagination conventions
- Error message formats

Phase 3.3.7.1 investigation flags these. If found, Phase 3.3.7.2-4
fixes them. If a behavior change is found AFTER Phase 3.3.7 closes
(e.g., during Phase 3.5.5 backtest), it's a "Phase 3.5 bug fix"
sub-commit, not a re-opening of 3.3.7.

---

This document is the contract for Phase 3.3.7. It will not be
revised mid-implementation; if a decision needs revisiting, that
discussion happens between sub-phases, not within them.
