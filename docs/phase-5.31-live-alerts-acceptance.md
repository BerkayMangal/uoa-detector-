# Phase 5.31 — opt-in live signal notifications

Frozen before code, 2026-10-27. Owner asks "neye gpore call veriyoruyz? call
verince uyari gelse ya:)". Standing authorization includes PR, merge and deploy.
The channel question is optional; pending an answer, implement browser alerts
with an explicit page-open limitation. No external recipient or service added.

## Contract

1. Explain the existing two-stage live policy: upward BUY eligibility versus
   priced instrument preference. An ALIM can prefer stock; CALL and bullish
   call debit spread are distinct instruments. No legacy thresholds, decisions,
   rankings, PAPER records, frozen profiles or alpha-lab rules change.
2. Private read-only JSON feed reads the newest stored scan only, with no UW
   calls or table writes. It exposes current upward BUY/READY plans in LIVE
   mode only, including the chosen instrument and planned underlying reference,
   stop and target. Conditional, WATCH, bearish, failed, previous-session,
   future-dated and stale inputs cannot alert. Scan, spot and selected option
   quotes must still be fresh at delivery; session close caps every lifetime.
   Missing timestamps/coverage cannot become an executable CALL.
3. Default scope: CALL and bullish call debit spread when that is the preferred
   ready instrument. Optional scope: all upward BUY plans, clearly identifying
   stock versus options. A mere CALL print in the flow never triggers an alert.
   Do not blanket-reject a degraded scan whose unrelated news failure still
   allows a valid market-relative live-policy BUY.
4. User explicitly arms alerts in this browser. Request native notification
   permission only on that click; audio is activated by a user gesture. Supply
   sound test, disable and scope controls. Denied/unsupported notification or
   audio remains visible and degrades to available in-page channels. Never
   promise delivery when the page is closed, the browser suspends the tab or
   the OS silences notifications. No broker or messaging integration.
5. Baseline currently eligible plans on first enable/scope change; do not
   replay them as newly created. Deduplicate per policy/opportunity/preferred
   instrument across polls and reloads; quote/price and rec-id changes alone
   cannot repeat an alert. Web Locks coordinate same-browser tabs; without
   that capability, explicitly limit cross-tab coordination rather than
   claim exactly-once delivery. Preferences and bounded seen IDs are local
   to this browser; no notification history containing strategy payloads.
6. Poll only the private stored-snapshot feed every 30 seconds while armed.
   Recheck expiry before sound/native display and after tab resumption. Reject
   stale responses, network/auth failures and unsuccessful fetches; a failure
   is displayed rather than interpreted as no signals. Each native notification
   carries the instrument and directs to the desk to refresh the plan.
7. Preserve the activated audio context during regular page refresh: replace
   the live section in place, retaining the toolbar/controller. Keep the
   existing search/edit refresh pause, form values and scan/session expiry
   guards; do not refresh over an edit begun during the HTTP request. No
   duplicate timers/handlers across replacement and no executable remote HTML.
8. Use the existing charcoal/copper UI. New feed/assets remain authenticated,
   noindex and private/no-store. Public introduction/SEO exceptions unchanged.
   Ship no new runtime dependencies, API credentials, persistent tables or
   vendor requests. Operational cadence/seen limits in a new versioned profile.

## Verification and release

- Meaningful backend tests: real stored-scan feed, no-scan/fresh/future/stale,
  closed/early-close/previous-session, conditional and bearish rejection,
  stock versus actual CALL/spread, selected-leg quote freshness, missing spot
  age, degraded news with valid BUY, private route/assets/auth/noindex.
- Chromium local fixtures: first-enable baseline, new ready signal, repeat
  polls/reload/two tabs, scope changes, denied/unsupported permission, storage
  failure and network recovery, visible banner + sound/native invocation,
  expiry on resume, no stale sound, form preservation through polling and
  refresh, and continued alerts after multiple in-place refreshes.
- Mobile/desktop overflow and automated accessibility scan; public pages
  retain their privacy boundary. Full repo gate, green PR and main CI,
  Railway SHA and production private route/asset checks.
- Local test notifications are labelled synthetic. Do not manufacture a real
  production BUY to demonstrate the alert or change a trading decision to
  make one appear. Report browser permission/page-open constraints explicitly.
