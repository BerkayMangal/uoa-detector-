# Phase 5.31 — live signal notifications

Engineering and local validation complete. Release evidence is tracked in the
feature PR. Notify-only scope: no decision, threshold, PAPER, lab, source or
broker changes, no new server tables, dependencies or vendor requests.

## Behavior

- `/api/canli/uyarilar` reads the newest stored scan. It projects upward
  BUY/READY plans, validates current-session scan/decision/flow availability,
  advances the stored spot age, and checks the selected option legs' quote age.
  Scan expiry, remaining spot/quote freshness and regular/early session close
  bound every signal. Missing/future/stale data does not create an alert.
- Actual instrument preference determines CALL, CALL SPREAD or HİSSE ALIM.
  A CALL print alone is insufficient. A degraded scan can still carry a valid
  market-relative BUY despite a failed news check; this projection does not
  invent an additional trading policy or reinterpret the headline classifier.
- The desk provides opt-in sound/native notification, a test-sound button,
  disable and scope controls. CALL/call-spread is the default scope; all upward
  BUY plans is optional. First enable and scope changes baseline existing
  plans instead of replaying them as new. Reference/stop/target in an alert
  are explicitly UNDERLYING prices, not option premiums or actual fills.
- Deduplication uses policy + opportunity + preferred instrument, independent
  of quote changes and rec-id churn. Bounded seen IDs live in a local IndexedDB
  read/write transaction, committed before presentation; preferences use
  localStorage. Web Locks also coordinate tabs. Browser verification caught a
  duplicate with localStorage plus a Web Lock alone: cache visibility across
  renderer processes is not an atomic claim. Transactional claims fixed it.
  No strategy payload or notification history is written to browser storage.
- The 30-second poll reads only private stored snapshots. Expired/withdrawn
  plans retire the banner and native notification; failed checks are visible.
  Presentation rechecks expiry after asynchronous storage coordination and
  after tab resumption. Audio/native limitations fall back to in-page alerts;
  unavailable durable deduplication prevents arming rather than risking repeats.
- Live-section replacement preserves the activated audio controller during
  normal five-minute refresh. Existing search/edit pause, age/session expiry
  and form values survive. An edit begun during a refresh also prevents the
  replacement. Timers and event handlers are cleaned before remounting.
- New feed/scripts remain authenticated, noindex, private/no-store. The public
  introduction and exact SEO allowlist are unchanged. Expanded stock, cost and
  PAPER tables have labelled keyboard-accessible scroll regions.

## Validation

- Full final gate: **4802 passed, 52 credential-gated skips, two existing
  warnings**. Strict mypy (229 files), ruff, lock/whitespace and both JavaScript
  syntax checks pass. Thirty new backend cases plus 45 automatically added
  auth cases increase the suite by 75. A real fake-provider engine cycle
  produces a preferred CALL and reaches the private feed with no extra calls.
- Chromium exercises first-enable baseline, fresh CALL, repeated polls, full
  reload, two in-place refreshes, scope changes, stock labelling, native/audio
  invocation, denied/unsupported native notifications, unsupported sound,
  blocked localStorage/IndexedDB, network recovery, delayed/expired responses,
  withdrawn signals and form preservation during an in-flight refresh.
- Eleven successive new-signal races across two simultaneously polling tabs
  each produce exactly one notification. These are local synthetic tests, not
  a production delivery claim. The harness replaces the native Notification
  constructor and instruments actual Web Audio calls; audible hardware/OS
  delivery is not verified in this cloud machine.
- Desktop 1440 px and mobile 390 px, with details expanded and alert visible:
  no document overflow, no uncaught errors, zero reported Axe WCAG A/AA and
  2.1 AA violations. Labelled local fixture images are outside the checkout at
  `/workspace/uoa-design-review/alerts-*.png`.

## Operating limits

This is a page-open browser feature, not background web push. The home page
must remain open; browser tab suspension, device/OS settings, cleared storage
and navigation can interrupt delivery. On a full navigation/reload, sound may
need another gesture; the toolbar reports it. Native permission is requested
only on enable, and unsupported platforms retain available channels. A sound
test invokes audio; it cannot establish that the user's speaker is audible.

Seen IDs are bounded to 1000 and seven days in `live_alerts_v1.yaml`. Claims are
at most once within retained browser state, not a globally exactly-once service;
there is no server inbox, retry queue or cross-device delivery. No real BUY was
manufactured for QA. Statistical edge and broker execution remain separate.

Release: green PR gate, merge through PR, green main CI, successful Railway
status and matching health SHA, new private feed/scripts returning 401 without
credentials, public SEO still 200. Authenticated production-device audio/native
delivery needs the owner's browser activation; this cloud has no production
Basic credentials. Next: observe genuine ready plans and their forward results.
