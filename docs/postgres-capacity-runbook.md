# PostgreSQL 53100 — preserve evidence, verify actual recovery

Observed 2026-10-28 Istanbul: production `9ffe5b1`, startup/OperationalError,
SQLSTATE **53100**, `webapp/gamma.py:238`, connection_invalidated=false. Earlier
scan snapshot writes and session-source reads failed too. HTTP health is 200;
this is not a healthy signal pipeline. No user orders are routed by this system.

53100 can indicate persistent storage, temporary storage OR a Docker dynamic
shared-memory segment limit. The sanitized `failure.resource` distinguishes
explicit known messages without publishing file/segment names or SQL. A missing
resource reason does not prove which physical resource is exhausted.

## Already prepared software mitigation

Phase 5.43 makes app PostgreSQL queries serial using the immutable operational
`profiles/database_v1.yaml` (`max_parallel_workers_per_gather=0`). Gamma and board
engines share this setting and connection pre-ping. Existing PostgreSQL URL and
PGOPTIONS settings are preserved. No server-wide setting, data deletion, table
reset, transaction replay or resource purchase. This reduces app shared-memory
demand; it does not free a full persistent volume.

## Operator actions if a disk/temporary-storage fault persists

1. Open the Railway project and select the **PostgreSQL service**, then its
   **Volumes / Metrics** view. Check USED and provisioned capacity. The app
   service's deployment volume is a different resource.
2. In the PostgreSQL service logs, inspect the 53100 error. Distinguish an
   explicit shared-memory segment from a persistent/temporary file path. Keep
   credentials, SQL parameters and customer data out of chat/public reports.
3. If the PostgreSQL volume is full, expand that existing volume through its
   Railway size control. Inspect the displayed cost first. Keep the same database
   and volume; no delete/recreate/reset. Extra paid capacity needs the owner's
   budget decision/access, which is not available to this cloud workspace.
4. If only shared memory is affected, verify the serial-query app release before
   changing paid storage. Other services sharing this PostgreSQL may also consume
   that resource; this patch changes only this app's sessions.
5. Verify the recovery conditions below. The existing failed-loop cadence is 5min;
   a pre-loop failure restarts after 30s. There is no need to synthesize past
   instructions or modify paper results to recover.

Do not delete `signal`, telemetry, recommendations, alerts, daily bars, PAPER,
manual trades or frozen research/cohorts. Do not delete pg_wal or run VACUUM FULL
to make room on an already full volume. Even rebuildable-cache cleanup should
first have a specific measured inventory and a separately reviewed scope; none
is part of this incident patch.

## Completion evidence

- `/health` reports the intended new commit SHA.
- `live_alpha.status` is ok or explicitly degraded for a known source issue,
  not starting/failed; mode and cards are present and the failure hint is cleared.
- Actual desk, calibration and tournament jobs report ok.
- `research_data` has an actual check timestamp, expected/latest session and
  coverage. Waiting-start/partial coverage is valid; it is not validated alpha.
- Private routes remain authenticated and old recorded evidence is preserved.

This cloud workspace has repository push/merge access but no Railway token,
database URL or production Basic credentials. It can ship and verify the app;
it cannot directly inspect/resize the Railway PostgreSQL volume or claim an
authenticated production data review. The owner can supply a secure connected
Railway context or perform the concrete capacity operation above; no password
or API token should be pasted into chat.
