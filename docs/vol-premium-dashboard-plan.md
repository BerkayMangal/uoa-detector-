# Vol-Premium Edge Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an edge-first landing dashboard — a vol-premium board (ranked by IV-rank, earnings names demoted) above the existing flow cards (re-sorted by descriptive notability) — built only on the one tested edge, with honesty guardrails (descriptive, never prescriptive).

**Architecture:** Two new PURE, unit-testable modules (`webapp/vol_board.py`, `webapp/notability.py`) do all logic over plain inputs; the dashboard route adapts DB rows into those inputs and the template renders them. The vol board reads the existing `gamma_regime` data (zero new UW calls); one nullable `next_earnings` column + one existing-provider call per name in the market-hours-gated gamma refresh adds the earnings flag.

**Tech Stack:** FastAPI + Jinja2 + SQLAlchemy (SQLite/Postgres) + pytest. Python 3.14, `mypy --strict`, `ruff`.

**Spec:** `docs/vol-premium-dashboard-design.md` (Phase 4.33).

**Conventions (CLAUDE.md):** every commit green via
`PYTHONPATH=src:. .venv/bin/python -m pytest -q` **and**
`PYTHONPATH=src .venv/bin/python -m mypy --strict src/` **and**
`.venv/bin/python -m ruff check .`. (`uv run` is flaky in this env — use the
direct `.venv` invocations above.) `webapp/` is type-checked the same as `src/`
if it’s under the mypy path; run mypy on `webapp/` too:
`PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/`. Type-annotate
everything. No threshold tuning. `git add <specific files>`, never `-A`.

---

## File Structure

| File | Responsibility | New/Mod |
|---|---|---|
| `webapp/vol_board.py` | Pure: `GammaContext` rows + earnings → ordered `VolBoardRow` list | new |
| `webapp/notability.py` | Pure: notability score from primitives | new |
| `webapp/explanations.py` | Vol-board copy: structure template, read text, caveat | modify |
| `webapp/gamma.py` | `next_earnings` column + `GammaContext` field + repo persist/read | modify |
| `webapp/gamma_live.py` | Fetch + persist `next_earnings` per name in the refresh loop | modify |
| `webapp/main.py` | Dashboard route: build vol-board, order signals by notability | modify |
| `webapp/templates/dashboard.html` | Vol-board hero partial + "Notable flow" header | modify |
| `tests/unit/test_vol_board.py` | vol_board ordering/demotion/threshold/math | new |
| `tests/unit/test_notability.py` | notability monotonicity/ordering | new |
| `tests/unit/test_dashboard_route.py` | route renders with vol-board present | new/extend |

Honesty constraint is binding throughout: **descriptive, not prescriptive; no
buy/sell language; regime is context only.** Do NOT rank by
`GammaContext.vol_signal == "sell"` (the long-gamma+high-IV cell Study D found
weak OOS). Rank by `iv_pct`.

---

## Task 1: `webapp/vol_board.py` — pure vol-board assembly

**Files:**
- Create: `webapp/vol_board.py`
- Test: `tests/unit/test_vol_board.py`

The board ranks by `iv_pct` descending; any name whose `next_earnings` falls
within `window_days` is demoted below all clean names (still shown, flagged);
a `below_threshold` flag marks `iv_pct < rich_threshold`. Expected move (±, as a
fraction) over `window_days` = `atm_iv * sqrt(window_days/365)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_vol_board.py
"""Unit tests for the pure vol-premium board assembly (Phase 4.34)."""

from __future__ import annotations

import math
from datetime import date

from webapp.gamma import GammaContext
from webapp.vol_board import VolBoardRow, build_vol_board


def _ctx(ticker: str, iv_pct: float | None, *, net_gex: float = 1.0,
         atm_iv: float | None = 0.50) -> GammaContext:
    return GammaContext(
        ticker=ticker, as_of="2026-06-26", spot=100.0, net_gex=net_gex,
        flip=98.0, call_wall=110.0, put_wall=90.0, atm_iv=atm_iv, iv_pct=iv_pct,
    )


def test_sorted_by_iv_rank_desc() -> None:
    rows = build_vol_board(
        {"A": _ctx("A", 0.40), "B": _ctx("B", 0.90), "C": _ctx("C", 0.70)},
        earnings={}, now=date(2026, 6, 26),
    )
    assert [r.ticker for r in rows] == ["B", "C", "A"]


def test_earnings_in_window_demoted_and_flagged() -> None:
    rows = build_vol_board(
        {"HI": _ctx("HI", 0.95), "LO": _ctx("LO", 0.30)},
        earnings={"HI": date(2026, 7, 5)},  # 9 days out, inside 30d window
        now=date(2026, 6, 26),
    )
    # LO (clean) ranks ABOVE HI despite lower IV-rank, because HI has earnings.
    assert [r.ticker for r in rows] == ["LO", "HI"]
    hi = next(r for r in rows if r.ticker == "HI")
    assert hi.earnings_in_window is True
    assert next(r for r in rows if r.ticker == "LO").earnings_in_window is False


def test_earnings_outside_window_not_demoted() -> None:
    rows = build_vol_board(
        {"HI": _ctx("HI", 0.95)},
        earnings={"HI": date(2026, 9, 1)},  # >30d out
        now=date(2026, 6, 26),
    )
    assert rows[0].earnings_in_window is False


def test_below_threshold_flag() -> None:
    rows = build_vol_board(
        {"RICH": _ctx("RICH", 0.80), "THIN": _ctx("THIN", 0.50)},
        earnings={}, now=date(2026, 6, 26), rich_threshold=0.75,
    )
    by = {r.ticker: r for r in rows}
    assert by["RICH"].below_threshold is False
    assert by["THIN"].below_threshold is True


def test_expected_move_fraction() -> None:
    rows = build_vol_board(
        {"X": _ctx("X", 0.60, atm_iv=0.50)}, earnings={},
        now=date(2026, 6, 26), window_days=30,
    )
    assert rows[0].expected_move_pct == \
        round(0.50 * math.sqrt(30 / 365) * 100, 1)


def test_none_iv_pct_sorts_last_and_no_crash() -> None:
    rows = build_vol_board(
        {"N": _ctx("N", None), "G": _ctx("G", 0.50)},
        earnings={}, now=date(2026, 6, 26),
    )
    assert rows[-1].ticker == "N"
    assert rows[-1].iv_rank is None


def test_none_atm_iv_gives_none_expected_move() -> None:
    rows = build_vol_board(
        {"X": _ctx("X", 0.60, atm_iv=None)}, earnings={},
        now=date(2026, 6, 26),
    )
    assert rows[0].expected_move_pct is None


def test_row_carries_regime_and_walls() -> None:
    rows = build_vol_board({"X": _ctx("X", 0.60, net_gex=-2.0)}, earnings={},
                           now=date(2026, 6, 26))
    r = rows[0]
    assert r.regime == "short"
    assert r.call_wall == 110.0 and r.put_wall == 90.0
    assert isinstance(r, VolBoardRow)
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_vol_board.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.vol_board'`.

- [ ] **Step 3: Write the implementation**

```python
# webapp/vol_board.py
"""Pure assembly of the vol-premium board (Phase 4.34).

Ranks the live gamma_regime names by IV-rank (richness of vol to sell) — NOT by
the long-gamma+high-IV "sell cell" (Study D showed that conditioning is weak
OOS, docs/study_D_result.md). Names with earnings inside the structure window
are demoted and flagged: high IV before earnings is a justified charge, not free
premium (the classic vol-selling trap). No DB, no HTTP — fully unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from webapp.gamma import GammaContext


@dataclass(frozen=True)
class VolBoardRow:
    ticker: str
    iv_rank: int | None              # iv_pct * 100, rounded; None if unknown
    atm_iv: float | None
    expected_move_pct: float | None  # ± over window_days, percent
    regime: str                      # "long" | "short" (context only)
    regime_label: str
    call_wall: float | None
    put_wall: float | None
    earnings_in_window: bool
    earnings_date: date | None
    below_threshold: bool            # iv_pct < rich_threshold


def _expected_move_pct(atm_iv: float | None, window_days: int) -> float | None:
    if atm_iv is None:
        return None
    return round(atm_iv * math.sqrt(window_days / 365) * 100, 1)


def build_vol_board(
    contexts: dict[str, GammaContext],
    *,
    earnings: dict[str, date | None],
    now: date,
    window_days: int = 30,
    rich_threshold: float = 0.75,
) -> list[VolBoardRow]:
    """Order names for the board: clean (no earnings in window) first, then
    earnings names; within each group, IV-rank descending (None last)."""
    rows: list[VolBoardRow] = []
    for ticker, ctx in contexts.items():
        edate = earnings.get(ticker)
        in_window = edate is not None and now <= edate <= _add_days(now, window_days)
        iv_rank = None if ctx.iv_pct is None else round(ctx.iv_pct * 100)
        rows.append(VolBoardRow(
            ticker=ticker,
            iv_rank=iv_rank,
            atm_iv=ctx.atm_iv,
            expected_move_pct=_expected_move_pct(ctx.atm_iv, window_days),
            regime=ctx.regime,
            regime_label=ctx.regime_label,
            call_wall=ctx.call_wall,
            put_wall=ctx.put_wall,
            earnings_in_window=in_window,
            earnings_date=edate,
            below_threshold=(ctx.iv_pct is None or ctx.iv_pct < rich_threshold),
        ))
    # Sort key: clean before earnings; then IV-rank desc (None -> -1, sorts last).
    rows.sort(key=lambda r: (r.earnings_in_window, -(r.iv_rank if r.iv_rank is not None else -1)))
    return rows


def _add_days(d: date, n: int) -> date:
    return date.fromordinal(d.toordinal() + n)
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_vol_board.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check webapp/vol_board.py tests/unit/test_vol_board.py`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/vol_board.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add webapp/vol_board.py tests/unit/test_vol_board.py
git commit -m "Phase 4.34.1: pure vol-premium board assembly (IV-rank sort, earnings demotion)"
```

---

## Task 2: `webapp/explanations.py` — vol-board copy

**Files:**
- Modify: `webapp/explanations.py` (append; keep all copy in this one place)
- Test: `tests/unit/test_vol_board.py` (add cases for the copy helpers)

Static, honesty-governed copy: a structure template (per regime), a templated
plain-English read, and a fixed caveat. No "buy/sell"; "rich vol to sell, you
build it".

- [ ] **Step 1: Add failing tests**

```python
# append to tests/unit/test_vol_board.py
from webapp.explanations import vol_caveat, vol_read, vol_structure


def test_vol_structure_is_defined_risk_template_no_buy_word() -> None:
    txt = vol_structure(regime="long")
    assert "iron fly" in txt or "credit spread" in txt
    assert "DTE" in txt
    assert "buy" not in txt.lower()


def test_vol_read_mentions_iv_rank_and_earnings_flag() -> None:
    clean = vol_read(iv_rank=88, earnings_in_window=False)
    assert "88" in clean and "earnings" in clean.lower()
    flagged = vol_read(iv_rank=88, earnings_in_window=True)
    assert "earnings" in flagged.lower()
    assert clean != flagged


def test_vol_caveat_is_honest_and_static() -> None:
    c = vol_caveat()
    assert "context" in c.lower()
    assert "tail" in c.lower() or "spike" in c.lower()
```

- [ ] **Step 2: Run, verify fail**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_vol_board.py -q`
Expected: FAIL — `ImportError: cannot import name 'vol_structure'`.

- [ ] **Step 3: Implement (append to `webapp/explanations.py`)**

```python
def vol_structure(*, regime: str) -> str:
    """Defined-risk structure TEMPLATE (type, not strikes) for harvesting vol
    premium. Honesty: a template the user sizes/strikes — never a recommendation."""
    base = "~30 DTE, ~30Δ iron fly or credit spread — collect vol premium, defined risk"
    return base if regime == "long" else base + " (short-gamma: moves amplify, keep size small)"


def vol_read(*, iv_rank: int, earnings_in_window: bool) -> str:
    """Plain-English description of WHY this name is on the board. Descriptive."""
    rich = f"IV-rank {iv_rank} — implied vol is high in its own 1y range, so the vol here is rich to sell."
    earn = (" ⚠ Earnings inside the window — the high IV is a justified charge, not free premium."
            if earnings_in_window else " No earnings in the window.")
    return rich + earn


def vol_caveat() -> str:
    """Fixed honesty line shown on every vol-board row."""
    return ("The base vol premium is the tested edge; the gamma regime is context, "
            "not extra return. Size for a vol spike (fat left tail).")
```

- [ ] **Step 4: Run, verify pass**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_vol_board.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check webapp/explanations.py`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/explanations.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add webapp/explanations.py tests/unit/test_vol_board.py
git commit -m "Phase 4.34.2: vol-board copy — structure template, read, caveat (honesty-governed)"
```

---

## Task 3: `webapp/notability.py` — pure notability score

**Files:**
- Create: `webapp/notability.py`
- Test: `tests/unit/test_notability.py`

Pure score over primitives so it’s testable without the `SignalRow` schema; the
dashboard route (Task 6) maps a row → these args. Notability is DESCRIPTIVE
(“how loud / unusual is this flow”), not predictive.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_notability.py
"""Unit tests for the descriptive notability score (Phase 4.34)."""

from __future__ import annotations

from webapp.notability import notability_score


def _base() -> dict[str, float | bool | int]:
    return dict(premium=50_000.0, aggressive=False, cluster_count=1,
                age_minutes=30.0, dte=30)


def test_higher_premium_scores_higher() -> None:
    lo = notability_score(**{**_base(), "premium": 10_000.0})
    hi = notability_score(**{**_base(), "premium": 500_000.0})
    assert hi > lo


def test_aggressive_scores_higher() -> None:
    assert notability_score(**{**_base(), "aggressive": True}) > \
           notability_score(**{**_base(), "aggressive": False})


def test_more_clustering_scores_higher() -> None:
    assert notability_score(**{**_base(), "cluster_count": 5}) > \
           notability_score(**{**_base(), "cluster_count": 1})


def test_fresher_scores_higher() -> None:
    assert notability_score(**{**_base(), "age_minutes": 1.0}) > \
           notability_score(**{**_base(), "age_minutes": 300.0})


def test_shorter_dte_scores_higher() -> None:
    assert notability_score(**{**_base(), "dte": 2}) > \
           notability_score(**{**_base(), "dte": 90})


def test_score_is_finite_and_nonnegative() -> None:
    s = notability_score(**_base())
    assert s >= 0.0 and s == s  # not NaN
```

- [ ] **Step 2: Run, verify fail**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_notability.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# webapp/notability.py
"""Descriptive 'notability' score for ranking flow (Phase 4.34).

Ranks how LOUD / unusual a flow print is — premium size, aggressiveness (sweep +
at-ask), clustering (repeat prints in the name), freshness, short DTE. This is
NOT a profit prediction (the flow has no proven edge); it is a triage ordering
for the secondary feed. Pure: primitives in, float out.
"""

from __future__ import annotations

import math


def notability_score(
    *,
    premium: float,
    aggressive: bool,
    cluster_count: int,
    age_minutes: float,
    dte: int,
) -> float:
    size = math.log10(max(premium, 1.0))                 # 4 @ $10k, 6 @ $1M
    agg = 1.5 if aggressive else 1.0
    cluster = 1.0 + 0.2 * max(cluster_count - 1, 0)      # +20% per extra print
    freshness = 1.0 / (1.0 + max(age_minutes, 0.0) / 60.0)  # 1.0 now, 0.5 @ 1h
    urgency = 1.0 + 1.0 / (1.0 + max(dte, 0))            # short DTE louder
    return size * agg * cluster * freshness * urgency
```

- [ ] **Step 4: Run, verify pass**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_notability.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check webapp/notability.py tests/unit/test_notability.py`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/notability.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add webapp/notability.py tests/unit/test_notability.py
git commit -m "Phase 4.34.3: pure descriptive notability score for the flow feed"
```

---

## Task 4: `webapp/gamma.py` — `next_earnings` column + view field + persist

**Files:**
- Modify: `webapp/gamma.py` — `GammaRow` (after line 51), `GammaContext`
  (after line 64), `GammaRepo.upsert` (line 203), `GammaRepo.latest` (line 217)
- Test: `tests/unit/test_gamma_earnings.py` (new)

`next_earnings` is a nullable ISO date string column; `GammaContext.next_earnings`
exposes it parsed to `date | None`. SQLite auto-creates the column for a fresh
table (the repo recreates schema); document that an existing Postgres table needs
the column added (the gamma table is refreshed, not migration-critical).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gamma_earnings.py
from __future__ import annotations

from datetime import date

from webapp.gamma import GammaRepo


def test_upsert_and_read_next_earnings(tmp_path) -> None:
    repo = GammaRepo(f"sqlite:///{tmp_path/'g.db'}")
    repo.reset()
    repo.upsert("NVDA", "2026-06-26", {
        "spot": 1000.0, "net_gex": 1.0, "flip": None, "call_wall": None,
        "put_wall": None, "atm_iv": 0.5, "iv_pct": 0.9,
        "next_earnings": "2026-07-30",
    })
    ctx = repo.latest()["NVDA"]
    assert ctx.next_earnings == date(2026, 7, 30)


def test_missing_next_earnings_is_none(tmp_path) -> None:
    repo = GammaRepo(f"sqlite:///{tmp_path/'g.db'}")
    repo.reset()
    repo.upsert("XOM", "2026-06-26", {
        "spot": 100.0, "net_gex": 1.0, "flip": None, "call_wall": None,
        "put_wall": None, "atm_iv": 0.2, "iv_pct": 0.3,
    })
    assert repo.latest()["XOM"].next_earnings is None
```

- [ ] **Step 2: Run, verify fail**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_gamma_earnings.py -q`
Expected: FAIL — `TypeError`/`AttributeError` (`next_earnings` unknown).

- [ ] **Step 3: Implement the changes**

In `GammaRow` (after `iv_pct`, line 51):
```python
    next_earnings: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO date
```

In `GammaContext` (after `iv_pct`, line 64):
```python
    next_earnings: date | None = None
```
Add at the top of `gamma.py` imports: `from datetime import date`.

In `GammaRepo.upsert` — when building the row, read the optional key and store
the string (it arrives as an ISO string or absent):
```python
        # inside upsert, alongside the other m[...] assignments:
        row.next_earnings = m.get("next_earnings")  # type: ignore[assignment]
```
(If `upsert` constructs a fresh `GammaRow(...)`, add `next_earnings=m.get("next_earnings")` to the constructor kwargs instead.)

In `GammaRepo.latest` — when building each `GammaContext`, parse the string:
```python
        # inside latest(), per row -> GammaContext(...):
            next_earnings=date.fromisoformat(row.next_earnings) if row.next_earnings else None,
```

- [ ] **Step 4: Run, verify pass**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_gamma_earnings.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate (model change touches the suite)**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest -q`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/gamma.py`
Run: `.venv/bin/python -m ruff check webapp/gamma.py tests/unit/test_gamma_earnings.py`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add webapp/gamma.py tests/unit/test_gamma_earnings.py
git commit -m "Phase 4.34.4: gamma_regime next_earnings column + parsed GammaContext field"
```

---

## Task 5: `webapp/gamma_live.py` — fetch + persist `next_earnings`

**Files:**
- Modify: `webapp/gamma_live.py` — `fetch_one` (line ~102) and the dict it
  passes to `repo.upsert`
- Test: `tests/unit/test_gamma_live_earnings.py` (new)

Add the soonest future earnings date per name from the existing catalyst
provider (`/api/earnings/{t}`); degrade to `None` on any error (D7). The fetch
already runs inside the market-hours-gated `gamma_refresh_loop` (Phase 4.28).

- [ ] **Step 1: Write the failing test** (uses a fake client; no network)

```python
# tests/unit/test_gamma_live_earnings.py
from __future__ import annotations

import asyncio


class _FakeClient:
    def __init__(self, earnings_payload: object) -> None:
        self._p = earnings_payload

    async def request_json(self, path: str) -> object:
        if "earnings" in path:
            return self._p
        if "greek-exposure" in path:
            return {"data": [{"strike": 100.0, "call_gex": 1.0, "put_gex": -0.5}]}
        return {"data": [{"date": "2026-06-26", "close": 100.0,
                          "volatility": 0.5, "iv_rank_1y": 90.0}]}


def test_next_earnings_picked_from_payload() -> None:
    from webapp.gamma_live import _next_earnings_date
    payload = {"data": [{"report_date": "2026-05-01"},
                        {"report_date": "2026-07-30"}]}
    # soonest FUTURE relative to a reference is chosen by the loop; the helper
    # returns sorted future dates' first element given 'today'.
    assert _next_earnings_date(payload, today="2026-06-26") == "2026-07-30"


def test_next_earnings_none_on_garbage() -> None:
    from webapp.gamma_live import _next_earnings_date
    assert _next_earnings_date({"nope": 1}, today="2026-06-26") is None
    assert _next_earnings_date({"data": []}, today="2026-06-26") is None
```

- [ ] **Step 2: Run, verify fail**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_gamma_live_earnings.py -q`
Expected: FAIL — `ImportError: cannot import name '_next_earnings_date'`.

- [ ] **Step 3: Implement**

Add the pure helper to `webapp/gamma_live.py`:
```python
def _next_earnings_date(resp: object, *, today: str) -> str | None:
    """Soonest report_date >= today from a UW /api/earnings payload; None if
    absent/garbage."""
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        return None
    future = sorted(
        str(r["report_date"]) for r in data
        if isinstance(r, dict) and isinstance(r.get("report_date"), str)
        and str(r["report_date"]) >= today
    )
    return future[0] if future else None
```

In `fetch_one`, after the existing greek/iv fetches succeed, fetch earnings
defensively and thread it into the returned context dict. Locate where
`fetch_one` returns `(as_of, ctx)` and add before it:
```python
    today = datetime.now(UTC).date().isoformat()
    try:
        e_resp = await client.request_json(f"/api/earnings/{ticker.upper()}")
        ctx["next_earnings"] = _next_earnings_date(e_resp, today=today)
    except UnusualWhalesError as exc:
        _logger.warning("earnings fetch failed for %s: %s", ticker, exc)
        ctx["next_earnings"] = None
```
(`ctx` is the dict from `context_from_uw`; it already flows into `repo.upsert`.
`datetime`/`UTC` are already imported from Task-independent Phase 4.28 edit; if
not, add `from datetime import UTC, datetime`.)

- [ ] **Step 4: Run, verify pass**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_gamma_live_earnings.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + type-check**

Run: `.venv/bin/python -m ruff check webapp/gamma_live.py tests/unit/test_gamma_live_earnings.py`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/gamma_live.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add webapp/gamma_live.py tests/unit/test_gamma_live_earnings.py
git commit -m "Phase 4.34.5: fetch+persist soonest future earnings into gamma refresh (degrades to None)"
```

---

## Task 6: `webapp/main.py` — dashboard route wiring

**Files:**
- Modify: `webapp/main.py` — `dashboard()` (line 159) + the template context dict (line ~186-200)
- Test: covered by Task 7's route smoke test

Build the vol board from `_gamma().latest()` + earnings, and order signals by
notability via a thin adapter. Keep it inside the existing `_safe(...)` guards.

- [ ] **Step 1: Add the adapter + wiring (no separate test; Task 7 covers render)**

At module level in `main.py`:
```python
from webapp.notability import notability_score
from webapp.vol_board import build_vol_board


def _signal_notability(sig: object) -> float:
    """Map a SignalRow to notability primitives. getattr with safe defaults so a
    missing field never breaks the ordering."""
    return notability_score(
        premium=float(getattr(sig, "premium_paid", 0.0) or 0.0),
        aggressive=bool(getattr(sig, "has_sweep", False))
        or getattr(sig, "fill_side", "") == "at_ask",
        cluster_count=int(getattr(sig, "cluster_count", 1) or 1),
        age_minutes=float(getattr(sig, "age_minutes", 0.0) or 0.0),
        dte=int(getattr(sig, "dte", 30) or 30),
    )
```
> NOTE for implementer: confirm the real attribute names on `SignalRow`
> (`webapp/repo.py`) — `premium_paid`, `has_sweep`, `fill_side`, `dte` — and the
> freshness source (compute `age_minutes` from `event_ts` vs now if no field
> exists). Adjust the `getattr` keys to the actual names; the `getattr`
> defaults keep it safe if a name is wrong, but use the real ones.

Inside `dashboard()`, after `matched = _safe(lambda: repo.signals(flt), [])`:
```python
    matched = sorted(matched, key=_signal_notability, reverse=True)
    gamma_ctx = _safe(lambda: _gamma().latest(), {})
    vol_rows = _safe(lambda: build_vol_board(
        gamma_ctx,
        earnings={t: c.next_earnings for t, c in gamma_ctx.items()},
        now=datetime.now(UTC).date(),
    ), [])
```
Add `"vol_board": vol_rows,` to the `TemplateResponse` context dict (alongside
`"signals": matched` and `"gamma": gamma_ctx` — reuse `gamma_ctx`, don't call
`latest()` twice). Ensure `from datetime import UTC, datetime` is imported.

- [ ] **Step 2: Type-check + lint**

Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/main.py`
Run: `.venv/bin/python -m ruff check webapp/main.py`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add webapp/main.py
git commit -m "Phase 4.34.6: dashboard route — build vol-board, order flow by notability"
```

---

## Task 7: `webapp/templates/dashboard.html` — hero partial + smoke test

**Files:**
- Modify: `webapp/templates/dashboard.html` — add vol-board section above the
  signal-card loop; wrap existing cards under a "Notable flow" header
- Test: `tests/unit/test_dashboard_route.py` (new or extend existing route smoke test)

Render `vol_board` rows at the top. Each row: ticker, IV-rank bar, ATM IV,
expected move, regime label (context), walls, `vol_structure`, `vol_read`,
`vol_caveat`, earnings flag, a "Log to journal" link to `/journal/new`. Rows with
`below_threshold` get a dimmed class. NO buy/sell words in the markup.

- [ ] **Step 1: Write the failing render test**

```python
# tests/unit/test_dashboard_route.py
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient


def test_dashboard_renders_vol_board(monkeypatch) -> None:
    import webapp.main as m
    from webapp.gamma import GammaContext

    ctx = {"NVDA": GammaContext(
        ticker="NVDA", as_of="2026-06-26", spot=1000.0, net_gex=1.0, flip=None,
        call_wall=1100.0, put_wall=950.0, atm_iv=0.5, iv_pct=0.9,
        next_earnings=None)}

    class _G:
        def latest(self) -> dict[str, GammaContext]:
            return ctx

    class _R:
        def signals(self, _f: object) -> list[object]:
            return []

        def ticker_label_options(self, *a: object, **k: object) -> tuple[list[str], list[str]]:
            return ([], [])

    monkeypatch.setattr(m, "_gamma", lambda: _G())
    monkeypatch.setattr(m, "_repo", lambda: _R())

    body = TestClient(m.app).get("/").text
    assert "NVDA" in body
    assert "IV-rank" in body or "IV rank" in body
    assert "Notable flow" in body
    assert "buy" not in body.lower().split("notable flow")[0]  # no buy-word in vol board
```

- [ ] **Step 2: Run, verify fail**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_dashboard_route.py -q`
Expected: FAIL — "Notable flow"/"IV-rank" not in body.

- [ ] **Step 3: Edit `dashboard.html`**

Above the existing `{% for sig in signals %}` card loop, insert a vol-board
section (Tailwind classes consistent with the existing markup), iterating
`vol_board`. For each `row`:
- header: `{{ row.ticker }}` · `IV-rank {{ row.iv_rank }}` (bar width
  `{{ row.iv_rank }}%`) · `ATM IV {{ '%.0f'|format(row.atm_iv*100) }}%` ·
  `±{{ row.expected_move_pct }}% /30d`
- `{{ row.regime_label }}` · walls `{{ row.call_wall }}` / `{{ row.put_wall }}`
- `{{ vol_structure(regime=row.regime) }}`
- `{{ vol_read(iv_rank=row.iv_rank, earnings_in_window=row.earnings_in_window) }}`
- small: `{{ vol_caveat() }}`
- if `row.earnings_in_window`: an amber `⚠ earnings` badge
- add `class="opacity-50"` when `row.below_threshold`
- a `<a href="/journal/new?ticker={{ row.ticker }}">Log to journal</a>`

Expose the three copy helpers to Jinja by adding them to the template context
(in `main.py` `_EXPLAIN` dict or the per-render context):
`"vol_structure": explanations.vol_structure, "vol_read": explanations.vol_read,
"vol_caveat": explanations.vol_caveat`. Guard `atm_iv`/`expected_move_pct` for
`None` with `{% if ... %}`.

Then wrap the existing card list start with a header:
`<h2 class="...">Notable flow</h2>` immediately before the `{% for sig in signals %}`.

- [ ] **Step 4: Run, verify pass**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest tests/unit/test_dashboard_route.py -q`
Expected: PASS.

- [ ] **Step 5: Full gate**

Run: `PYTHONPATH=src:. .venv/bin/python -m pytest -q`
Run: `PYTHONPATH=src .venv/bin/python -m mypy --strict webapp/`
Run: `.venv/bin/python -m ruff check .`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add webapp/templates/dashboard.html webapp/main.py tests/unit/test_dashboard_route.py
git commit -m "Phase 4.34.7: vol-board hero partial + Notable-flow header; route smoke test"
```

---

## Task 8: Manual verification + close

- [ ] Run the app locally against the sample data and eyeball the board:
  `LIVE_TICKERS= PYTHONPATH=src .venv/bin/python -m uvicorn webapp.main:app --port 8099`
  then open `http://127.0.0.1:8099/` — confirm: vol board on top (IV-rank order),
  earnings names demoted/flagged, "Notable flow" below, NO buy/sell language,
  caveat present on each row.
- [ ] Paket-mode report (CLAUDE.md D3): commits shipped, HEAD hash, pytest/mypy/ruff
  clean state, new test count, judgment calls. Then ask Berkay before pushing
  (phase-3 is the Railway deploy branch → push triggers a redeploy).

---

## Self-Review (completed by plan author)

**Spec coverage:** layout (T7) · vol board hero + IV-rank sort + earnings demotion + threshold (T1) · structure template/read/caveat (T2) · notable-flow notability sort (T3, T6) · gamma_regime data reuse + earnings column/fetch (T4, T5) · honesty guardrails (T1 no vol_signal ranking, T2 copy, T7 no-buy assertion) · testing (every unit + route smoke) · non-goals respected (no strikes, no universe change, no alerts). All spec sections map to a task.

**Placeholder scan:** the only deferred item is the exact `SignalRow` attribute names in T6, explicitly flagged with a safe `getattr` fallback and an instruction to confirm against `webapp/repo.py` — not a silent placeholder.

**Type/name consistency:** `VolBoardRow`, `build_vol_board`, `notability_score`, `vol_structure/vol_read/vol_caveat`, `next_earnings`, `_next_earnings_date` are used identically across tasks.
