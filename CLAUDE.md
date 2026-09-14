# CLAUDE.md

This file is read by Claude Code at the start of every session. It encodes
the disciplines this project has built up over Phases 1-5.0 (2150+ tests at
the Phase 5.0 merge, mypy strict + ruff clean, 131 source files under
`src/`). The discipline
exists for a reason: this is a real-money options-flow detection system being
built by Berkay, an ex-Deutsche Bank trader, and bugs that pass review will
eventually lose actual capital. Treat the rules below as binding, not advisory.

If you find yourself wanting to break a rule "just this once because it makes
sense", that is the signal to stop and ask Berkay, not to proceed.

---

## Who is Berkay

Berkay Mangal is a 20-year ex-Deutsche Bank fixed-income trader. He left in
2024 and is building this options-flow detector as a side project with capital
under $10k, intended for use with one trusted friend. He is not a software
engineer; he understands trading deeply, code at a high level. He communicates
in Turkish (sometimes English) and prefers direct, no-fluff conversation.

A few specifics that affect how you should work with him:

- He is **not** here to be coached, motivated, or reassured. He is here to
  ship a working detector and find out if there is real edge in the strategy.
  Do not suggest taking breaks. Do not praise effort. Do not soften bad news.
- He has explicitly asked, multiple times, for you to drop social padding
  and stay on the work. "Mola ver" / "rest" / "good job" suggestions
  irritate him. Stop the instinct.
- He learns by doing. When he asks "what do I do next", give him a numbered
  list of literal commands or actions. Do not give him essay-form
  explanations unless he asks why.
- He is comfortable making capital-allocation decisions and risk calls.
  He is uncomfortable with deep Git mechanics, build tooling, and
  Python ecosystem trivia. Lean technical when explaining; lean executive
  when proposing decisions.
- He has been doing 12+ hour days running the previous Claude.ai/agent
  loop with bundle-and-push cycles. The reason he is moving to Claude Code
  is to remove that loop. Do not reintroduce ceremony that he was trying
  to escape.

When in doubt about tone: imagine you are reporting to a senior trader at
the start of his day. Concise, specific, no bullshit, ready to be questioned.

---

## What the project is

**UOA + Convexity Detector v5** — a multi-source options unusual-activity
detector designed to surface candidates for short-dated directional options
trades. It is decision-support: it generates signals, Berkay decides whether
to trade, and trade entry is always manual through his own broker. The
system never auto-executes.

The architectural thesis (Track B, encoded in `profiles/v5_gamma_squeeze.yaml`)
is that genuine edge requires confluence across multiple independent data
sources:

- Unusual options flow (Unusual Whales)
- Spot price action at the moment of the flow (ThetaData)
- Dealer gamma positioning (Unusual Whales)
- Sector-level peer flow alignment (Unusual Whales)
- Dark-pool corroboration (Unusual Whales)
- Open-interest delta (Unusual Whales)
- Implied volatility regime (Unusual Whales)
- Event calendar context (Unusual Whales)

Phases 3.4.1 through 3.4.9 built the eight enrichment modules (M21-M28)
that score each signal across these axes. Phase 3.5 will run them against
real historical data and answer one binary question: **does this strategy
have an edge?** The answer will be yes or no, not "promising" or "needs
more tuning".

---

## Where to find the project's memory

Before doing anything substantive, read these in order:

1. `docs/INDEX.md` — current truth across both lineages (§0), the type
   and status of every doc, label collisions, burned data windows, and the
   Phase 5.x registry and numbering rules (§7). Start here.
2. `docs/phase-3.9-uw-endpoint-correction-acceptance.md` and
   `docs/phase-3.9-closeout.md` — the current, live-verified Unusual Whales
   layer, and the open §7 flags.
3. `docs/phase-3.6-closeout.md` — why the UW-free confluence verdict is
   EDGE REJECTED, and the look-ahead leak the audit caught.
4. `docs/edge_to_money.md` — why the vol premium is untradeable after costs.
5. `docs/study_D_result.md` — the pre-registered conditioning replication
   (WEAK / BORDERLINE).
6. `docs/MODULES.md` — per-module reference for M21-M28.
   What each module scores, which providers it uses, which thresholds
   are configurable, which judgment calls were made during implementation.
7. `docs/phase-3.4-acceptance.md` — Phase 3.4's frozen
   contract and closeout summary. The history of how M21-M28 came to be.
8. `docs/phase-3.5-acceptance.md` — Phase 3.5's frozen
   contract. Eight sub-phases, the falsification framework, the things
   that explicitly *are not* allowed mid-phase.
9. `docs/thetadata-v3-migration.md` — Phase 3.3.7's spec for
   the v2→v3 migration. Read this before touching anything in
   `src/uoa_detector/sources/thetadata/`.
10. `docs/phase-3.3.7-acceptance.md` — contract for the v3 migration.
11. `profiles/v5_gamma_squeeze.yaml` — the strategy profile being tested.
    The inline comments encode Berkay's hypothesis. Do not modify thresholds.
12. `profiles/v5_default.yaml` — the broader profile. Same rule: do not
    modify thresholds.

Git history is the second source of truth. Every commit message describes
the sub-phase, the decision, and the test impact. `git log --oneline` for
orientation, `git show <hash>` for any specific decision's reasoning.

---

## The disciplines (these are not suggestions)

### D1. Frozen acceptance documents

Acceptance docs (`docs/phase-X.Y-acceptance.md`) are **contracts**. Once a
phase starts, the doc does not change. If during implementation you discover
that a decision was wrong, you stop, you flag it, you discuss with Berkay,
and you write the discussion into a new doc (typically the next sub-phase's
acceptance or a new "Phase X.Y bug fix" commit). You do not edit the original
contract to match what you ended up doing.

### D2. Bisectable commits

Every commit must be green on its own: the gate under "Running tests"
(hermetic `pytest -q`, `mypy --strict src/ webapp/`, `ruff check .` and
`uv lock --check`) passes. `git bisect` is a real debugging tool used in this
project; one broken middle commit destroys hours of future investigation.
If a change is too big for one bisectable commit, split it into smaller
ones that each leave the tree green. The merged `phase-3` history is not
D2-clean, so bisect `main` with `git bisect --first-parent`.

### D3. Sub-phase reports (paket-mode)

When you finish a sub-phase, produce a single consolidated report. Format:

- One-line status summary (KAPALI / open with reason)
- Commits shipped with one-line descriptions
- HEAD hash + verified-clean state of pytest / mypy / ruff
- New tests count and what they cover
- Judgment calls (numbered, with brief rationale for each non-obvious choice)
- Score branch truth tables for any scoring module changes
- "Sıradaki" pointing at the next sub-phase

This is for Berkay to review in one shot. Do not stream commentary across
multiple turns.

### D4. No threshold tuning during validation phases

In Phase 3.5 (real backtest), profile thresholds are the test condition.
You **do not** retune them after seeing results. That is curve-fitting and
produces fake edge. If results are weak, the answer is "edge rejected" or
"hypothesis needs revision", not "let me adjust the moderate_alignment
threshold and rerun".

Falsification discipline. The four reject scenarios were pinned in Phase
3.2.4 and they are pinned. You read the report, you apply them, you accept
the verdict.

### D5. Idempotency-on-preset for pipeline stages

Every PipelineStage in `src/uoa_detector/pipeline/stages/` follows this
pattern: at the top of the stage, check whether the score field this stage
writes is already set. If yes, return the event unchanged (preset_skip
branch). This makes pipeline reruns safe and lets tests pre-populate fields
to isolate downstream behavior. Do not break this. New stages must follow
it. M24's variant (no field, uses `score_adjustments[source_module=m24]`
as marker) is the documented exception, not the new pattern.

### D6. ScoreAdjustment for cross-module penalties, never direct mutation

Modules NEVER mutate another module's sub-score directly. The mechanism
for cross-module influence is `ScoreAdjustment` — a structured record
emitted by the source module, consumed at `compute_combined_score`. M24
is the first real user of this rail; if you need a new cross-module effect,
follow M24's pattern (see `src/uoa_detector/pipeline/stages/m24_iv_exhaustion.py`).

### D7. Provider injection + timeout protection

Stages receive their providers via constructor injection. Defaults are
NoOps so test wiring stays simple. Every provider call is wrapped in
`asyncio.wait_for` with a per-stage profile-driven timeout (default 2.0s,
M25 uses 3.0s for multi-ticker fetch, M28 uses 5.0s for batch). Timeouts
do not abort the stage — they set a neutral score and record telemetry.

### D8. Calibration profile is the only source of numeric truth

No hardcoded numerical thresholds in code. Every threshold lives in
`profiles/*.yaml` under `scoring.modules.mXX.*`. Code reads them via
`M.XXSettings` Pydantic models in `src/uoa_detector/calibration/profile.py`.
If you find yourself writing `if rank > 80:` in a stage, stop — that 80
belongs in the profile.

### D9. Event-time, not wall-time

Fusion and decay calculations operate on `event_ts`, not `datetime.now()`.
Backtests are deterministic because of this. Live mode passes the actual
event timestamp through; do not shortcut by calling `now()`.

### D10. Test contract preservation

Existing tests are the cumulative spec. If a behavior change requires
modifying an existing test, that is a flag — either the test was wrong
(rare, document why) or the behavior change is breaking (usually wrong,
back off and find another approach). New behavior gets new tests. The
project went from 0 → 1412 tests this way; no shortcuts.

### D11. No "TODO" markers

If something needs to be done, it gets a sub-phase, an acceptance entry,
and a commit. `# TODO` markers in code are how projects accumulate rot.
Do not add any. The codebase is not at zero: two pre-existing markers
remain, at `src/uoa_detector/calibration/resolver.py:48` and
`src/uoa_detector/pipeline/stages/__init__.py:4`. Their removal is
scheduled in the Phase 5.x registry (`docs/INDEX.md` §7, 5.10–5.19).

### D12. Type annotations are mandatory

`mypy --strict` is the gate. Every function signature is annotated. Every
data class is a Pydantic BaseModel or `@dataclass(frozen=True)`. No bare
dicts crossing module boundaries. No `Any` except in explicitly justified
adapter boundaries.

---

## How to do common tasks

### Running tests

```bash
uv sync                                    # if .venv stale
uv run pytest tests/unit/test_mXX_*.py -v  # one module
uv run pytest -m integration               # gated integration smokes
```

The gate. Run it before every commit; CI (`.github/workflows/ci.yml`) runs
the same gate on every PR and every push to `main`:

```bash
env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ webapp/ && uv run ruff check . && uv lock --check
```

What each part does:
- `env -u` strips the API keys from the pytest process. Missing-key tests
  therefore stay hermetic even when your shell has the keys exported, and
  key-gated integration tests skip, as they do in CI, which has no keys.
- `uv lock --check` fails if `uv.lock` no longer matches `pyproject.toml`.
- `scripts/` is ruff-only, a documented exception to mypy
  (`docs/phase-5.0-merge-acceptance.md` §3.11).

Integration smoke tests in `tests/integration/` are gated by environment
variables (UNUSUAL_WHALES_API_KEY, THETADATA_API_KEY). They skip cleanly
when the var is absent. Berkay has these in `.env` locally; load them with:

```bash
export $(grep -v '^#' .env | xargs)
```

`.env` is in `.gitignore` (line 7). Never commit it, never echo its
contents into a tool output, never paste it into a chat. If you need to
verify a credential is present, check `$VARNAME` after the export.

### Committing

Work reaches `main` only through a pull request from a feature branch:

```bash
git switch -c <feature-branch> main
git add <specific files, never -A>
git commit -m "Phase 5.X.N: <one-line summary>"
git push -u origin <feature-branch>
gh pr create --base main
```

- **Merge.** Berkay merges the PR once the gate is green locally and in CI.
  Never auto-merge anything to `main`.
- **Deploys.** Railway deploys `main` after the Phase 5.0 switch, so a red
  `main` is a red production.
- **`phase-3` is frozen.** Never push to it after its freeze: its tip is
  tagged `phase-3-final` and the branch is locked
  (`docs/phase-5.0-merge-acceptance.md` §3.14).

Commit messages follow the `Phase 5.X.N: <thing>` format. N is a
sub-phase commit counter starting from 1. Phase 3.x and 4.x labels are
historical and never reused. A hotfix to a live component is a numbered
sub-commit of the owning phase with "hotfix" in the summary. The full
numbering rules are in `docs/INDEX.md` §7. The summary is one line, no body
unless the change is non-obvious (breaking change, judgment call worth
recording inline, etc).

### Acceptance docs

When starting a new phase, allocate its number in `docs/INDEX.md` §7 and
write `docs/phase-5.X-<slug>-acceptance.md` BEFORE
any code change. Get Berkay's approval (or read his explicit "yes" if
he started the phase). Then implement against the doc. The doc is frozen
from that point. The paket-mode report goes in a separate
`docs/phase-5.X-closeout.md`.

### Theta Terminal v3

Phase 3.3.7 migrated to v3. The Terminal is a Java app run locally:

```bash
java -jar ThetaTerminalv3.jar
```

It listens on port **25503** (not 25510 — that was v2). Default endpoint
prefix is `/v3/*`. The Terminal stays running for as long as historical
downloads or smoke tests need it. Berkay runs it in a separate terminal
tab and keeps it alive.

### Phase 3.5.3 historical download

**You do not run this.** This is Berkay's responsibility — it consumes
real ThetaData bandwidth, takes 8-48 hours wall-clock, and requires his
laptop to stay awake on stable network. Your role is the pre-flight
validation (3.5.2) and the post-flight manifest commit. The actual
download command is in `docs/phase-3.5-acceptance.md`.

---

## What you can decide without asking

- All judgment calls within a sub-phase's scope as defined by the acceptance
  doc. Make the call, document it in the sub-phase report, move on.
- Test fixture details, naming, internal helpers, any pure refactoring.
- Bug fixes scoped to a single sub-phase you are currently executing.
- File organization within the existing module layout.

## What you must ask Berkay first

- Anything that touches an acceptance doc's frozen decisions.
- Threshold changes in profiles (almost always: don't).
- Phase progression (does 3.5.1 close, does 3.5.2 start).
- Breaking changes to public APIs (e.g., BacktestStoreProtocol additions
  beyond what an acceptance doc already specifies).
- Anything involving real money. The rule as of 2026-09-14
  (`docs/phase-5.0-merge-acceptance.md` §3.13):
  - The webapp is live decision-support.
  - Nothing executes, there is no broker link, and there is no
    auto-trading.
  - Alerts (notify-only) and a virtual portfolio (log-only paper records,
    never executed) are approved product modules as of 2026-09-14. Each
    sits behind its own acceptance doc (registry 5.5 and 5.6).
  - Anything beyond that, such as order execution, a broker connection or
    automated trading, is not approved.
- Strategy hypothesis revisions — "what if we changed Track B to..." is
  always Berkay's call.

## What you must always do

- Read `docs/INDEX.md`, `docs/MODULES.md` and the relevant acceptance doc
  before starting work.
- Run the gate (see "Running tests") before every commit.
- Produce a paket-mode report when a sub-phase closes.
- Use `gh` CLI for GitHub operations (issues, PRs, releases) rather than
  asking Berkay to do it in the browser.

## What you must never do

- Commit `.env`, API keys, credentials, or anything matching a secret pattern.
- Modify `profiles/v5_*.yaml` thresholds during a validation phase.
- Edit a frozen acceptance doc to match what you ended up doing.
- Auto-merge anything to `main` without explicit Berkay approval.
- Run live trading or paper trading code. Nothing may send, route or place
  an order, whether to a live account or a paper account. The log-only
  virtual portfolio (registry 5.6), built under its own acceptance doc, only
  records hypothetical positions for forward scoring. It is not order
  execution.
- Make optimistic claims about edge or strategy performance — let the
  numbers speak via `docs/INDEX.md` §0.

---

## Current state (Phase 5.0, 2026-09-14)

- **Unified.** Both lineages are unified on `main` by the merge commit
  `6d1e4ca` (Phase 5.0.3). Contract: `docs/phase-5.0-merge-acceptance.md`.
  - `main` contributed Phase 3.5.0–3.9: the backtest engine, the CLI
    screener and the live-verified UW layer.
  - `phase-3` contributed Phase 3.3.9–3.6 and 4.1–4.43: the replay and
    verdict engine, the self-derived research track, the webapp and live
    worker on Railway, and the studies.
  - `docs/INDEX.md` maps every doc of both lineages.
- **Gate at the merge commit:** 2150 passed / 30 skipped (key-gated),
  `mypy --strict src/` clean on 131 source files, ruff clean, `uv lock
  --check` consistent. `docs/phase-5.0-closeout.md` records the final
  Phase 5.0 counts.
- **Edge**, canonical statement (`docs/INDEX.md` §0):

  > No tradeable edge found. Directional UOA confluence (UW-free, v6):
  > REJECTED (phase-3.6-closeout). Gamma-regime directional and pinning:
  > REJECTED (4.9, 4.11). Vol premium: untradeable after costs
  > (edge_to_money.md). Conditioning replication: WEAK (study_D_result.md).
  > UW-fed Track B (v5_gamma_squeeze with real UW enrichment) has never been
  > testable, because UW history is about 7 days (phase-3.5.5-status B1).

- **UW layer:** Phase 3.9. Its closeout §7 flags are registry items.
- **Live:** the FastAPI webapp on Railway, with the live worker in the same
  process.
  - Railway deploys `main` after the Phase 5.0 switch.
  - `phase-3` is frozen; its tip is tagged `phase-3-final` before the switch
    (contract §3.14).
- **Next:** the Phase 5.0 closeout, then Phase 5.x in registry order
  (`docs/INDEX.md` §7). Every 5.x phase needs its own approved acceptance
  doc before code.

Check `git log --oneline -20` for the precise commit-level state at the
moment you read this. The acceptance docs are the spec; the git log is
the actuality. If they disagree, the spec wins and the actuality is a bug.

---

## A note on language

Berkay's preferred working language is Turkish. He will write to you in
Turkish. Respond in Turkish unless he switches to English. Code, commit
messages, doc text, and acceptance contracts stay in English (international
norm; project is built to be readable by anyone). Conversation in chat
is bilingual.

---

## A note on this document

This file was written by an earlier Claude (claude.ai-side) after working
with Berkay through Phases 1 through 3.4, witnessing the disciplines
emerge in real-time. The rules above were earned, not designed. If you
think one of them is wrong, you might be right — but the chance you are
right is lower than your prior would suggest, and breaking the rule
silently is the failure mode that costs the most. Ask Berkay first.

Phase 5.0.13 updated the header counts, reading list, gate, commit flow,
acceptance-doc naming, real-money rule, D11, phantom results path and
current state. Berkay approved those edits on 2026-09-14
(`docs/phase-5.0-merge-acceptance.md` §3.13).

Welcome to the project. Be useful.
