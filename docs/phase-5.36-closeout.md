# Phase 5.36 closeout — native quota observation freshness

KAPALI — reproduced and fixed prior-day header poisoning of new ET reservations.
This does NOT establish that today's actual quota is available or raise any cap.
Owner authorizes continuing API/research improvements and deployments in chat.

## Change

A native UW client retains its last measured daily request count until another
valid header arrives. Previously Live Alpha and options PAPER passed that integer
into every new ET-day reservation without knowing when it had been measured.
A yesterday count of 24,000 could raise an otherwise empty new-day ledger to its
cap before a new request could measure the new day. A local SQLite reproduction
confirmed the refusal with no real API calls.

Native client now exposes a read-only UTC response-availability timestamp alongside
its unchanged count property. Only a valid nonnegative header updates both;
missing/malformed/negative headers retain the old observation. Valid 429/404
headers still count and typed errors/retries remain unchanged.

Both reservation consumers use `observed_for_day`: explicit native timestamps
must match the ledger's requested New York date and cannot be future/naive.
UTC midnight alone is not an ET-day reset; DST uses ZoneInfo. Timestamp-less
legacy adapters keep their previous conservative semantics. Same-day measured
high counts still raise the monotonic atomic floor and refuse requests. No ledger
reset, old-row repair, profile/cap change or extra vendor call was added.

Refresher SoftCap's existing unchanged-value guard is left intact; this fix targets
the two atomic reservation consumers that lacked observation freshness. Vendor
reset timezone is not newly inferred: requested ET ledger day remains the existing
policy. The first actual response measures current usage; genuine quota refusal
and unavailable credentials/histories remain explicit limits.

## Verification

- 11 new cases: stale/future/invalid timestamps, timestamp-less compatibility,
  ET/UTC/DST boundaries, prior-day reservation recovery, same-day actual cap,
  native valid/zero/bad/missing headers and read-only timestamp, valid daily-limit
  error header/fail-fast behaviour and the options PAPER consumer.
- Focused 24 tests pass, all existing assertions preserved.
- Full hermetic gate: 4951 passed / 52 key-gated skips; strict mypy 255 files;
  ruff, lock and diff clean.
- Both frozen research reports still validate, including tournament's compressed
  grid. No scoring module, configuration or research result changed; score truth
  tables do not apply.
- Green PR/main CI and Railway success + matching live SHA are checked after the
  owner's authorized merge. Native header handling was exercised hermetically;
  no production credentials or actual tomorrow quota availability are claimed.

Next: prospective PAPER coverage/outcomes and genuinely accessible broader data,
not silently promoting a historical strategy or enlarging vendor limits.
