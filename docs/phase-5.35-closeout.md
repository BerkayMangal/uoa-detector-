# Phase 5.35 closeout — six-family strategy tournament

KAPALI — six new frozen search families completed, report and prospective API-arm
PAPER monitoring delivered. No validated alpha and no live BUY-policy replacement.
Owner explicitly authorizes continued variations, PR merge and deployment in chat.

## Result

7,776 configurations; 46,686 distinct portfolio evaluations (7,776 x six train/
validation windows + six winners x primary test, two sensitivities, two sparse-flow
diagnostics). 226 configurations met the frozen temporal selection gates. Each
family winner and the overall selection were fixed BEFORE final-test evaluation.
There are also 36 ledger replays of the selected train/validation evaluations.

The overall validation-selected winner is **oversold_reversal**, ID
`acc5265a097298d0`. It LOST **$1,289.19** in the internal test and is rejected.
A different winner was NOT substituted after seeing that result.

| Family | Test closes | Net model $ | Matched SPY excess $ | Close-marked max DD | Joint search p | PAPER |
|---|---:|---:|---:|---:|---:|---|
| Relative recovery | 37 | +1002.40 | +816.89 | 10.71% | .6517 | Experimental only |
| Relative momentum | 18 | +1176.36 | +765.46 | 10.17% | .9950 | No: <20 closes |
| Trend pullback | 33 | -249.24 | -420.45 | 9.19% | 1.0000 | No |
| Oversold reversal | 40 | -1289.19 | -1377.91 | 20.09% | .8060 | No |
| Range breakout | 25 | -49.66 | +78.07 | 2.87% | 1.0000 | No: negative net |
| Volatility compression | 39 | -1701.05 | -1583.73 | 19.71% | .9751 | No |

NO family passes the 95th-percentile joint search diagnostic. These are all
reused Study F/Study E prices/observations, including the internal test; even a
smaller p-value would not create untouched evidence or reverse an old study.
Trade dependence, selected universe and corporate-action adjustment uncertainty
remain limits. No probability-of-profit forecast is inferred.

The recovery family's preselected candidate `9c8fa417f2b916e3`: five-session
lookback, >=4pp lag behind SPY, nonnegative candle body, <=1 prior-ATR daily
increase, no extra volume/regime gate, five-session hold, equal cash sizing,
max four names. Validation folds had net +$593.56 / +$2059.07 / +$861.39 and
matched excess +$998.63 / +$1050.25 / +$1013.35. Test slippage sensitivity:
10bps/side +$909.90; 20bps/side +$701.08, both 37 trades. It is only a family-
specific PAPER hypothesis, NOT the overall winning/validated strategy.

The phase 5.34 three-session candidate and its report remain unchanged. The
new test starts July 8, the old test June 30: their dollar figures are not a
like-for-like superiority comparison.

## Data/access audit

Used all eligible on-disk research inputs: 17,640 OHLCV rows / 70 names / 252
sessions plus all 133 sparse daily-flow rows / ten names. For the recovery
candidate, historical flow coverage is 0 known rows / 288 eligible test signals;
that combination cannot be calibrated from this file. Missing rows are unknown.

`artifacts/tournament-v1/data-inventory.json` inventories hashes, sizes and uses
or exclusions for the available price, flow, universe, static baseline/calendar,
prior study and option replay/capability files. Reserved Study G prices, seed/
preview/pytest databases and synthetic historical fixtures are excluded. Sparse
option snapshots do not become a multi-session option-return history.

UW/Theta/production DB credentials are absent locally. Public Yahoo/Stooq history
returned proxy HTTP 403. GitHub's secret-metadata API also denies this integration;
no secret values were accessed. Historical vendor calls were NOT performed or
misrepresented. Existing production observations power the new prospective arms;
no new vendor calls or cap/entitlement changes in this release.

## Artifacts and audit

- New `profiles/tournament_v1.yaml`: all family gates, windows, budgets, resampling,
  selection and prospective controls fixed before results.
- `report.json`: six selected ledgers, equity paths, all validation/internal-test/
  sensitivity/flow results, joint-null draws, configuration and implementation hashes.
- `grid.jsonl.gz`: all 7,776 six-window score records; compressed to avoid loading
  the full grid into a production request. SHA-256 and row count in the report.
- Report SHA-256:
  `82c3f27f34fb63b41c4645b1f467cdc06d13d1c99b561a17a963dfb7a9b42404`.
- One completed frozen search, no parameter revision after inspection. Source,
  profile, implementation, prior calibration and compressed grid integrity pass.
- Independent audit reconciles **66** selected ledgers: whole shares, debit <=
  $2,500/name, nonnegative cash, max slots, unique active names, closed counts and
  final cash/P&L. Expanding training windows overlap: their counts are not
  independent observations. Each separate split starts at $10,000.

## Future records and UI

Private `/stratejiler`, linked from home and Alfa lab, clearly exposes the rejected
overall selection, family statuses, the small positive-momentum sample, costs,
coverage, joint-null limitation and future PAPER scoreboard. Mobile tables are
keyboard-focusable scroll regions; all details collapse by default. Stylesheet
URL advances to 5.35.1 for cached browsers.

The one qualifying family may create independent PAPER units starting with
2026-10-28 ET signal closes. Six fixed arms: price only, observed flow, flow+gamma,
flow+dark pool, flow+OI, full context. First as-of context freezes membership;
late/degraded/absent inputs never repair it. Exact declared next-open/horizon-close
prices, whole quantities, model slippage, source dates and missing results persist
in three new append-only tables. No broker orders, no real-trade edits and no
retroactive missed instructions. Independent units are NOT a recreation of the
historical cash-constrained portfolio and are labelled accordingly.

Enrichment freshness uses the stored print-event clock and telemetry availability;
the exact vendor timestamp for every M21-M27 payload was not retained. This is an
explicit proxy, not a newly claimed timestamp guarantee. Flow is corrected observed
alert-side premium, not exchange-wide tape or the legacy historical mean score.
All API-arm effects require future measurements; none is historically validated.

## Verification and delivery

- Full hermetic gate **4940 passed, 52 key-gated skips**: +17 behavioural cases,
  +15 automatic route-auth cases. Strict mypy **255 source files**, ruff, lock,
  diff clean. Old tests preserved except additive table classification.
- Tests cover causal prefixes/missing sessions, prior high/range/volume/ATR,
  strict market-MA equality, cash-before-exits, missing future close retaining the
  entry, ATR quantity-only sizing, fixed family selection/failed samples, joint
  seeded calendar bootstrap, actual cached context availability/degradation,
  frozen arm membership, exact future PAPER endpoints/dedup/as-of views,
  no late instructions, route privacy and failure isolation.
- Chromium real frozen report plus labelled local market/DB fixture at 1440px and
  390px, every fold open/closed, no viewport overflow, zero axe WCAG A/AA
  violations or JS errors. Screenshots outside git in
  `/workspace/uoa-design-review/tournament-*`. Production Basic credentials are
  absent; authenticated visual tests are local, public SHA/status/auth checks live.
- Owner authorizes merge/deployment. Complete with green PR/main CI, Railway
  success and matching live SHA plus safe `tournament_status=ok` before announcement.

No scoring module/profile or prior study verdict changed; score truth tables do
not apply. Next meaningful evidence is prospective results and broader genuinely
available histories, not declaring a best historical equity curve to be alpha.
