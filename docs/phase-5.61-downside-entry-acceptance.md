# Phase5.61 — downside entry delivery

Owner requests downside opportunities alongside upward UOA signals. Existing P4
bearish path already requires directional flow, negative price confirmation, live/fresh data and a priced PUT/debit-put instrument; opening policy already
supports PUT purchases. Browser entry allowlist currently suppresses both PUT
instruments. Complete delivery/visibility without changing the frozen strategy,
opening Python, source/news/research hashes or thresholds.

Canonical opening feed remains the sole entry source. Distinguish buying a PUT
for a downside thesis from closing an existing position, short stock or writing
naked options. Surface direction/instrument clearly on current entry cards.
Allow both PUT instruments through entry notification validation and add explicit
PUT-only scope. Existing CALL-only preference remains CALL-only; existing/all
scope becomes all supported directions under the owner's explicit request.
Do not enable alerts, request permission automatically, replay old entries or
weaken baseline/deduplication/expiry/storage failure checks. Unknown scopes fail
closed and existing exit selection remains independent.

Verify actual bearish source→opening publication→canonical feed, valid options
with source-session/quote/cost gates, no unpriced short-stock substitution, browser
PUT/debit-PUT delivery, direction filters and stale/replay/permission safeguards.
Keep original test assertions; extend only where new scope explicitly supersedes
old upward-only behavior. New downside references are already direction-adjusted
in the separate timed collector and have bearish return/excess tests; no option
P&L/optimal holding period or calibrated profitability is inferred.

Run full unchanged hermetic pytest/mypy/Ruff/lock before commit, exact green PR
and main CI, normal manual merge, native fullSHA/SUCCESS/healthy scan plus actual
desktop/mobile controls. Never manufacture a production bearish call for proof.
