# Options PAPER tracker — what runs, what is only tested, and one real example

**2026-09-23** · Phase 5.24 (`docs/INDEX.md` §7: "exit monitoring and outcome recording")

Log-only paper records. Nothing here sends, routes or places an order.

---

## 1. Tested code and the live job are different claims

| Claim | State on 2026-09-23 | Evidence |
|---|---|---|
| Tracker logic (calendar, marks, exit decision) is correct | **TESTED** | `tests/unit/test_options_alpha_tracking.py`, including reproduction of both real 2026-09-08 cards from recorded UW quotes |
| Daily job registers, marks, decides, writes a heartbeat | **TESTED** | `tests/unit/test_options_paper_job.py`: mid-hold, full horizon, unknown horizon day, our failed fetch, refused quota, registry |
| Atomic quota reservation holds under concurrency | **TESTED** | `tests/unit/test_quota_ledger.py`: 40 threads race for 6 free slots on one database; exactly 6 are granted |
| The same code on real UW data | **RAN LOCALLY** | `scripts/options_alpha_tracker_e2e.py` → `artifacts/options-alpha-v1/tracker_e2e.json` (local SQLite) |
| The job runs in production on a schedule | **NOT YET** | It is in the board refresher's daily-job registry (`options_paper`). It starts on Railway only after this branch is merged to `main` (the owner merges). The first production heartbeat row in `alfa_opt_paper_heartbeat` is the proof. Until that row exists, "live" is not claimed |

## 2. How it works

- **Where it runs:** the existing board refresher loop (`webapp/board/refresher.py`),
  as a daily job at `outcomes.job_time_et` (17:30 ET). `alfa_job_run` markers give it
  the same catch-up after a restart that every board job has.
- **Register:** every committed `paper_card_*.json` under
  `artifacts/options-alpha-v1/replay/` goes into `alfa_opt_paper_position` once,
  verbatim, keeping its `data_origin`.
- **Calendar:** SPY's stored sessions (`alfa_daily_close`), exactly as the outcome
  job uses them. A holiday shifts the hold. It never becomes an unpriced day.
- **Mark:** one `/api/option-contract/{occ}/historic` call per leg. Closable value
  is leg by leg: long leg at the bid, short leg at the ask. A missing leg is a missing
  mark, never zero. Complete marks are appended to `alfa_opt_paper_mark`.
- **Decide:** the fixed exit engine. Mid-hold the position is `still_open`. The
  card's plan (`time_target_stop`) decides the final state, and all three variants
  are stored. An unpriced horizon day waits one further session, then is
  finalised as `no_exit_data`, which the screen shows as **bilinmiyor** (unknown).
  Written once to `alfa_opt_paper_outcome`.
- **Heartbeat:** every run appends a row to `alfa_opt_paper_heartbeat`: when it ran,
  the last session it saw, how many positions and marks, requests granted and
  refused, and the status. `/opsiyon` shows the latest row. A tracker that never
  ran says so in words; it is never shown as "no open positions".
- **Quota:** `webapp/board/quota_ledger.py`. Every request is reserved before it is
  made, using one conditional `UPDATE` that stays atomic in Postgres and SQLite.
  The vendor's own key-wide count is folded in first. The cap is the board
  profile's `refresh.daily_request_soft_cap`. A refusal stops spending for the day.

## 3. Two defects the first real run and the audit caught

1. **The exit engine booked an unknown horizon day as a realised exit.** Fixed in
   PR #77 (`AUDIT_H10_TRADES.md`). The tracker depends on the fix: mid-hold means
   `still_open`, not "time exit on the last mark".
2. **Commission was charged twice on a card of more than one structure.**
   `card.py` writes the card's `commission_usd` as the total
   (`price.commission_usd × structures`). The tracker multiplied it by the quantity
   again. The first real run surfaced it: the 2026-09-08 QQQ card (2 structures)
   came out exactly 5.20 $ worse than `score_card`, with identical exit values.
   Fixed. A regression test pins both real cards to `score_card`'s numbers.

## 4. One real example, end to end

**This is a REPLAY.** The entry is the close of a completed session (2026-09-08),
priced from the end-of-day NBBO snapshot. It is a simulated fill at a snapshot
price, not an order, and not an opportunity that exists today. The outcome is
**real**: it comes from what the two contracts actually quoted in the five
sessions that followed.

| Step | What | Source |
|---|---|---|
| Data | SPY chain 2026-09-08 (1,170 contracts scanned, 31 eligible) | UW `option-chains?date=2026-09-08`; `replay/2026-09-08/chain_trimmed_SPY.json` |
| Hypothesis | `H00_pipeline_smoke`: the pipeline's own smoke test. **It has no validated edge** and is labelled `RESEARCH_ONLY` / `WATCH` on the card | card `counter_argument` |
| Trigger | `SPY261009C00768000`: volume 1,324 > open interest 309, the session's most traded eligible contract | card `trigger` |
| Contract / spread | Bull call debit 768/769, expiry 2026-10-09. The spread cut the debit by 94.2% against the frozen 20% rule | card `eligibility_reason` |
| Cost | Long 768C at ask 10.50, short 769C at bid 9.89 → 0.61, +2% slippage → **0.62**/share. Commission 2.60 $ round trip | card legs; `profiles/options_alpha_v1.yaml` |
| Risk | Max loss **64.60 $**, max profit 35.40 $, stop 0.31/share, target capped at 1.00/share, 5-session hold, forced close at DTE 7 | card |
| PAPER card | `sig_d1ce1ed71d8c9136`, `data_origin: replay` | `replay/2026-09-08/paper_card_SPY.json` |
| Tracking | Closable value: 09-09 0.43 · 09-10 0.39 · 09-11 0.47 · 09-14 0.42 · 09-15 0.34 | UW `/historic` per leg, 2026-09-23 run |
| Exit / outcome | Time exit 2026-09-15 at 0.34 → (0.34 − 0.62) × 100 − 2.60 = **−30.60 $**. The stop (0.31) was not touched inside the hold | `tracker_e2e.json`; matches `paper_card_SPY_outcome.json` |

The same run on the 2026-09-08 QQQ card (bear put 700/699, 2 structures): stop on
2026-09-09 at 0.20 → **−47.20 $**, also matching `score_card`.

**Open, tracked forward:** the two 2026-09-22 cards (SPY 775/776 call, QQQ 760/761
call) are registered and open. Their entry is also a replay of a completed close.
Their first mark is the 2026-09-23 close, which had not happened when the local
run was made. **No outcome is claimed for them.**

## 5. What this does not show

- **Not an edge.** Both closed cards lost, and the hypothesis behind them is a
  pipeline smoke test. Every research family in this scope has either been
  rejected or has insufficient data (`run_manifest.json`).
- **Not a live opportunity.** No card in this scope was generated in real time.
- **Not intraday.** B-quality data: end-of-day NBBO, no quote timestamp, so the
  sequence of events inside a day is unknown.
