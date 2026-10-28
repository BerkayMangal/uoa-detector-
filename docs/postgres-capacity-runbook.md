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

## Native connection evidence after phase 5.46

The owner's actual application log reported `FATAL: the database system is in
recovery mode`. This is different evidence from the earlier 53100 capacity
fault. A volume reported below 100MB used does not by itself prove either the
cause of recovery or that every temporary/shared-memory resource is healthy.
The Railway account compute budget is also a separate measure.

`live_alpha.database_hint`, when present, is a fixed allowlisted cause read only
from the native driver exception. `recovery`, `starting` and `shutdown` reflect
explicit server messages. `not_accepting` (57P03 without native detail) is
ambiguous. `refused`, `dns`, `connection_timeout`, `authentication`, `tls`,
`connection_capacity` and `connection_lost` require their corresponding service,
configuration or native code check; they do not establish a repair by themselves.
A missing hint is unknown, not proof the database is fine. No host/user/URL/raw
SQL or driver message is published by this adapter.

Inspect **unique-balance → PostgreSQL service → Deploy Logs**, rather than only
the application logs. Read whether recovery is progressing, repeating a crash,
or has reached `database system is ready to accept connections`. If PostgreSQL
is ready but the app still reports connection refusal/DNS/timeout, compare the
app's existing DATABASE_URL service reference/network endpoint in Railway.
Do not paste its secret value into chat. This workspace has no control-plane
access and does not change the target or invent an internal hostname/port.

During recovery, do not delete WAL, run pg_resetwal, force promotion, replace the
volume or repeatedly restart a progressing recovery. Retain the database's own
native log evidence; a platform recovery banner is not a diagnosis of this
particular database. A provider recovery/escalation or correct service reference
may be needed, but it must follow the observed service state.

Private `/acilis` now returns a clock-only 503 during storage failure: current
Istanbul preparation/open/+15 times, BEKLE and a safe known diagnostic. It is
read-only, never invents journal data or zero P&L and does not automatically
refresh/replay anything. The owner may retry GET manually. Completion still
requires an actual successful scan plus `opening_status: ok`, and real recorded
coverage; a deployed build or a liveness 200 alone is insufficient.
