# Phase 5.26 — basket-relative lag (`basket_relative_v1`)

**Type: FROZEN.** Registry slot 5.26 (`docs/INDEX.md` §7). Written before any
code of this phase. Frozen from the owner's approval on 2026-10-12 (D1): a
decision found wrong during implementation is flagged and recorded in a new
document, never edited into this one.

**Owner approval, 2026-10-12.** Four questions were put to Berkay in
`docs/` note form and answered in the project thread:

1. Baskets are **hand-written by him** and the file must be easy to edit. His
   stated goal beyond that is a system that *finds* the hot names itself —
   highest volume, most news. The hand-written basket is the starting point;
   discovery is a later phase, not this one.
2. **Product first**, labelled experimental; any study is scored forward in
   parallel.
3. The episode record opens **whether or not he trades**, so selection bias
   stays measurable.
4. Ozan will look at the board through Berkay's own login. No second user
   account, so the 5.1 login work is not pulled forward and the open UW
   licence question about a second *account* is untouched.

---

## 1. What this phase builds, and what it does not

**Builds.** One measured number per ticker per basket — the ticker's return
minus its peers' return over the same two sessions — and one plain column on
the "Hisse panosu" at `/` that states it.

**Does not build.** No prediction, no score, no decision, no alert, no episode
record, no study. The column is display. Nothing in `src/uoa_detector/pipeline/`
changes, no combined score moves, no calibration profile is touched.

The episode record (the design's step 3) and hot-spot discovery (the owner's
real target) are later phases. They are named here only so the boundary of
this one is unambiguous.

## 2. Why a hand-written basket

The vendor's own grouping cannot express the thing being measured, and this is
measured rather than asserted:

- `GET /api/stock/{t}/info` returns one of eleven coarse sectors
  (`sector_peer.py:124-138`).
- `peers_of` ranks that sector by market cap and takes the top five
  (`docs/phase-3.9-uw-endpoint-correction-acceptance.md` §3.7). For COHR that
  answers with the largest technology names, not with AAOI and LITE.
- The membership is current-as-of-fetch. The code says so itself:
  "must not be used in historical replay without a date-aware source"
  (`sources/unusual_whales/providers/sector_peer.py:85-88`). This is why the
  H05 family is marked KISITLI in `docs/options-alpha-v1/HYPOTHESES.md`.

A basket committed to git carries the date it was written. A basket written in
October cannot have known what happened in June, which is what makes it safe
for a later backtest. That property is the reason for the choice, not
convenience.

## 3. Frozen decisions

### 3.1 The basket file

`profiles/baskets_v1.yaml`, a standalone profile with its own loader, **not** a
new section of `BoardSettings`. Adding a section to `BoardSettings` would change
`board_profile_hash`, which is stored on every decision card, and would make
cards written before and after this phase incomparable. `live_alpha_v1.yaml`
set the precedent for a standalone policy profile.

The file is the only place that says which names belong together. It is edited
by hand and the commit is the record of when a membership changed.

### 3.2 The residual

For ticker `i` in basket `B` over `window_sessions` sessions:

- `r_i = close_i(end) / close_i(start) - 1`
- `r_peers = mean(r_j for j in B, j != i)`
- `residual_i = (r_i - r_peers) * 100`, in percent units

**Self is excluded from the peer mean.** Including it dilutes the name's own
deviation by `1/n` and understates the gap the owner is looking for. Ranking is
unaffected either way — `residual_i` is monotone in `r_i` under both — so the
choice only sets the magnitude, and the self-excluded magnitude is the one that
answers "this name against its peers".

### 3.3 The two anchor dates are shared

`end` is the newest stored close date across the basket's members. `start` is
the date `window_sessions` stored sessions before `end`, taken from the union
of the members' date sets.

A member contributes only if it has a close on **both anchor dates exactly**.
"On or before" is rejected: it would silently measure different windows for
different members and present the result as a comparison. A member with a gap
is dropped from the mean and the dropped count is shown, so thin coverage looks
thin instead of looking like a clean reading.

### 3.4 When no number is shown

In this order:

1. Fewer than `min_members_with_data` members priced on both anchors →
   `yetersiz veri`.
2. `abs(r_peers_all) < min_basket_move_pct` → `sepet hareketsiz`. A name cannot
   lag a group that did not move, and saying it did would be the board's own
   kind of lie.
3. Otherwise the residual, with the word set by `lag_flag_pct`.

### 3.5 The cutoffs are display choices, made before any result

| Key | Value | Why this number |
|---|---|---|
| `window_sessions` | 5 | One trading week; the horizon the journal and the outcome job already score |
| `lag_flag_pct` | 3.0 | A five-session residual below this is inside the ordinary dispersion of high-beta names like AAOI. Picks a word only |
| `min_basket_move_pct` | 2.0 | Below this the group is flat and the residual is two noises subtracted |
| `min_members_with_data` | 3 | Two names is a pair spread, not a basket; the mean needs at least two peers besides self |

None is fitted. None may be tuned after seeing how the column reads (D4). A
change is a new `basket_version` with its own record.

### 3.6 Basket members get a daily close

`run_daily_closes` covers the basket members in addition to the live-run and
journal tickers. One `ohlc/1d` request per name per day, against a 30,000/day
limit and a 24,000 soft cap. This is what lets a name sit in a basket without
being on the live board.

Only the close job is widened. The delayed families cost four requests per
ticker and are left alone.

### 3.7 The column says nothing about what to do

The text states the measured gap, the window and the coverage. It contains no
advice word. `/` is outside the honesty guard's route list since the 5.25
override, but the rule the guard encodes still governs what this column may
say.

## 4. Out of scope, stated so it is not drifted into

- Lead/lag prediction ("AAOI moved, so COHR will"). H04 tested the nearest
  relative of that claim and it was REJECTED with the sign reversed
  (`docs/options-alpha-v1/RESULT_H04.md`). It is not in this phase.
- Any change to M25 or to `sector_confirmation`. The pipeline is untouched.
- Automatic basket discovery from volume or news. The owner's target, and the
  next phase.
- Backtesting the residual. The 2026-05-13 → 2026-09-15 window already carries
  nine declared trials from the options-alpha H family; a study on it needs its
  own pre-registration and a forward score.

## 5. Acceptance

- `profiles/baskets_v1.yaml` loads into strict frozen models; a malformed file
  fails at startup, not on the first page request.
- The residual is pure arithmetic with no I/O, unit-tested including
  self-exclusion, the shared anchors, the dropped-member path and each of the
  three no-number branches.
- The column renders on `/` and is absent, not broken, when the basket has no
  data.
- The gate is green: hermetic `pytest -q`, `mypy --strict src/ webapp/`,
  `ruff check .`, `uv lock --check`.

## 6. Verification owed after merge

The column has never been seen against live data. After the first live session
with the optical names in `LIVE_TICKERS`, the closeout records what the column
actually printed, and whether coverage was full or thin.
