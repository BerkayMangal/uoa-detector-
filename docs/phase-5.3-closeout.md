# Phase 5.3 — spot decision frame (closeout)

**Status: CODE COMPLETE — OPEN until the owner merges #42, #45, #47, #48 and #49
and the live board is verified.** The contract is
`docs/phase-5.3-spot-frame-acceptance.md` and is not edited (D1). Every deviation
found while implementing it is recorded in §5 below.

The phase answers one question the board could not answer before: the owner buys
**shares**, and every action cell priced an **option**. Each row now states entry, an
ATR stop, a share count floored to his R, the dollars actually risked, the straddle's
target and R to reach it — arithmetic on his own risk rules, stating no probability.

## 1. Commits

§7 specified six, each its own PR, each green alone.

| # | Commit | PR | Scope |
|---|---|---|---|
| 5.3.1 | `50c5557` | #42 | `regular_session_bars` + `alfa_daily_bar` + the daily job writing it; no UI |
| 5.3.2 | `0def633` | #45 | `webapp/board/spot.py` + the profile block + strict settings |
| — | `88b9198` | `p53-3-base` | integration of #42 and #45, which 5.3.3 needs both of |
| 5.3.3 | `6ba27df` | #47 | row view: spot cells, the `Opsiyon detayı` fold, R-SP1–R-SP8 |
| 5.3.4 | `1fca5c6` | #48 | card freezing + parity test |
| 5.3.5 | `990a48e` | #49 | `values_confirmed_by_owner: true` and the copy that drops `(varsayılan değer)` |
| 5.3.6 | this commit | — | this closeout |

**Adjacent, not one of the six.** `0c6ef1a` (#44), "the board says the market is closed
instead of looking broken", branched off `main` separately and is not in this chain. It
belongs to the same working session and the same owner instruction, but no §7 commit
depends on it.

## 2. Gate

| # | pytest (passed / skipped) | mypy --strict `src/ webapp/` | ruff | `uv lock --check` | `tests/unit` alone |
|---|---|---|---|---|---|
| 5.3.1 | CI green on #42 | CI | CI | CI | — |
| 5.3.2 | 4041 / 52 | 183 files | clean | consistent | — |
| 5.3.3 | 4062 / 52 | 183 files | clean | consistent | 3980 |
| 5.3.4 | 4067 / 52 | 183 files | clean | consistent | 3985 |
| 5.3.5 | 4068 / 52 | 183 files | clean | consistent | 3986 |

**No local figure is recorded for 5.3.1.** It was gated before this closeout began and
the numbers were not kept; CI on #42 is the verification that survives. Saying so is
cheaper than inventing a count, and a closeout that guessed one would be the sort of
claim this phase spent its effort removing.

Every skip is a key-gated integration test. The two pytest warnings predate 5.3.

`tests/unit` was run alone as well as inside the full suite, because P40 recorded a test
that passed only because another directory ran first.

## 3. New tests

36 across four files, every one naming the rule it pins in its docstring.

| File | Tests | Pins |
|---|---|---|
| `test_alfa_daily_bars.py` | 3 | bar parsing; both readers share one filter and cannot drift; the duplicate-day drop |
| `test_alfa_spot_frame.py` | 14 | True Range and Wilder ATR against hand-computed numbers; the arithmetic of R-SP1, R-SP2, R-SP3, R-SP6, R-SP8; the profile block; the `atr_min_sessions < atr_period` refusal |
| `test_alfa_spot_render.py` | 14 | the rendered frame; R-SP1, R-SP4, R-SP5, R-SP6, R-SP7, R-SP8; the fold; the bull/bear grid staying outside it; zero UW calls |
| `test_alfa_card_spot_parity.py` | 5 | the frozen card holds a populated frame; its strings equal the rendered HTML; a refused frame freezes its reason; survival across a restart; the freeze mechanism is generic |

### Rule coverage, checked mechanically rather than claimed

| Rule | Pinned in |
|---|---|
| R-SP1 no bars, no stop | `test_alfa_spot_frame.py`, `test_alfa_spot_render.py` |
| R-SP2 no stop, no size | `test_alfa_spot_frame.py` |
| R-SP3 zero shares is rendered | `test_alfa_spot_frame.py` |
| R-SP4 the target is not a forecast | `test_alfa_spot_render.py` |
| R-SP5 never "al" or "sat" | `test_alfa_spot_render.py` |
| R-SP6 the cap is disclosed | `test_alfa_spot_frame.py`, `test_alfa_spot_render.py` |
| R-SP7 the option gate hides no spot row | `test_alfa_spot_render.py` |
| R-SP8 stale spot, no frame | `test_alfa_spot_frame.py`, `test_alfa_spot_render.py`, `test_alfa_card_spot_parity.py` |

### Mutation proof (§6's closing requirement, P39/P40)

Every test claiming a guard was verified by breaking the guard. Each mutation was
applied, the suite run, and the file restored to a verified-identical md5.

| Commit | Guard broken | Result |
|---|---|---|
| 5.3.2 | the `atr_min_sessions` check removed | RED — and this is how the original fixture was caught: at 3 bars the ATR is `None` anyway, so the first version of that test passed with the guard deleted |
| 5.3.3 | R-SP1/R-SP8 reason mapping returns `None` | RED (3 failed) |
| 5.3.3 | R-SP6 cap disclosure suppressed | RED |
| 5.3.3 | the ATM freshness filter removed | RED |
| 5.3.3 | the P15 default marker suppressed | RED |
| 5.3.3 | the bull/bear grid moved inside the fold | RED |
| 5.3.4 | `snapshot` drops every `spot*` field | RED (5 failed) |
| 5.3.4 | the frame forced empty at build time | RED (4 failed) |
| 5.3.4 | only `row_view.row` frozen instead of the whole view | RED (5 failed) |

## 4. D10 record (existing tests changed)

Twelve, none weakened. Three were strengthened.

**5.3.3 — two.**
- `test_alfa_route_tradability`: the exhaustive `(varsayılan değer)` count went 3 → 4.
  The spot cell is a legitimate fourth site; the comment names it. The count stays
  exhaustive because that is what would catch the marker appearing where the owner's
  values are not in play.
- `test_alfa_cost_cells_unexecutable_quotes`: it sliced the chip strip from `data-chip=`
  to `data-cases`. Moving the grid above the chip made that slice **empty**, so its cell
  assertions would have passed while checking nothing. It now matches the chip div
  itself. This is the more instructive of the two: the restructure did not break the
  test's claim, it removed the text the claim was about.

**5.3.5 — ten**, all inverted rather than halved: absence asserted against the profile,
presence against an explicitly unconfirmed copy. `test_board_settings`,
`test_board_sizing`, `test_board_portfolio`, `test_board_capital_expired`,
`test_alfa_tradability`, `test_alfa_size_render`, `test_alfa_portfolio_render`,
`test_alfa_spot_render`, `test_alfa_route_tradability`, `test_alfa_gate`.

Three of those ten would have gone **vacuous rather than red** — see §5.

## 5. Deviations and judgment calls

Recorded as P43, P44 and P45 in `docs/alfa-board-decisions.md`.

**5.3.1 shipped a write-only table.** `alfa_daily_bar` had `ensure_daily_bar_tables`,
`_store_bars` and `_insert_missing_bars` and no reader at all, so it had been filling up
unread since it landed. 5.3.3 added `load_bars_by_ticker` beside `load_closes_by_ticker`:
one query for the whole board, since a query per ticker would add a round trip per row to
every render. Stored nulls survive the read, which is what lets the ATR window break on a
gap instead of spanning it.

**A board whose bar table was never written logged a caught stack trace per request.**
The read was wrapped, so the page degraded correctly — but a trace on every render hides
the next real one. `db_bars_source` now ensures the table at read time, the pattern
`delayed_panel.py` already uses for `alfa_daily_close`.

**The bull/bear grid moved above the fold.** Folding the option strip in place would have
put the bear case inside a collapsed `<details>`. The honesty auditor would not have
caught it — `visible_text` strips tags and still reads folded text — which is exactly why
it needed deciding rather than noticing later. A counter-argument the reader has to open
is not a counter-argument (R-CA1).

**The spot cell carries `(varsayılan değer)` too** (until 5.3.5 removed it everywhere).
The share count rests on the same unconfirmed `capital_usd` and `r_usd` the option size
cell already disclosed, and shares are what actually get bought.

**Two copy corrections older guards forced.** "beklenen hareket" is banned page-wide so
the vol board's `IV-implied 1σ move` stays the only reading of an implied move; the cell
reads `hedef $X (ATM straddle)`. And R-EV1 is pinned by a **count** —
`text.count("olasılı") == len(rows)`, once per row in the evidence hover — which a
per-row probability denial doubled; the denial is now "tahmin değildir". Neither was
caught by review. Both were caught by tests written for other phases.

**5.3.4 needed no production code, and that was the risk.** `cards.snapshot` is recursive
over dataclasses and `build_card_view` names no board field, so the frame had been frozen
since 5.3.3. The existing parity test asserts `frozen["row_view"] == cards.snapshot(view)`
— snapshot against snapshot — which holds just as well when every cell is `None` on both
sides. The new test compares the frozen JSON against the strings the **HTML** rendered,
after asserting the frame is populated.

**5.3.5's flip turned three tests vacuous rather than red.** All three were named
`test_confirmed_owner_values_drop_the_default_marker` (in `test_alfa_route_tradability`,
`test_alfa_portfolio_render`, `test_alfa_gate`) and each monkeypatched confirmation **on**
before asserting the marker was gone. With the profile shipping confirmed, that is the
default state: they would have kept passing while proving nothing, including with the flag
ignored entirely. `test_alfa_gate`'s was rewritten as a **contrast** assertion — one page,
one set of quotes, one flag apart, must differ — the only form that cannot go vacuous.

**Nothing was tuned.** No profile threshold moved. 5.3.5 changed one disclosure flag whose
value §2's O3 had already fixed, and two comments that still called the numbers defaults
awaiting replacement. `alfa_daily_close` was not touched (P38, D10).

## 6. Open items

1. **The frozen frame is invisible on the card page.** `GET /kart/{id}` renders the
   card's meta line, trade line and fills, not the frozen row view. §5's stated purpose
   is that a card opened in three months explains the decision in the terms it was made
   in, and a frame nobody can see does not do that. §7 scoped 5.3.4 to freezing plus the
   parity test, so widening it would have edited a frozen contract by implementation.
   **The owner's call**: a follow-up phase, or a numbered sub-commit here.
2. **The six PRs await the owner's merge** (#42, #45, #47, #48, #49, and the adjacent
   #44). Nothing reaches `main` without him.
3. **The live board has not been verified against this phase**, because nothing is merged
   yet. After the merges: `/alfa` renders, a row shows a populated frame, and the
   `daily_close` job's UW request count is unchanged.
4. **FAZ C1's card verification is still owed** and needs `WEB_AUTH_USER` /
   `WEB_AUTH_PASSWORD`, which are not in `.env` and cannot be read here — the Railway CLI
   is not linked in this directory.

## Sıradaki

5.3 is closed on the code. What follows is not this phase: the owner's merges, then the
live verification in §6.3, then the open item in §6.1 if he wants it. The `DATABASE_URL`
cutover to the dedicated Postgres (P36/P37) and its post-migration measurements are
independent of 5.3 and still waiting on him.
