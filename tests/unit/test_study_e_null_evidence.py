"""Study E's published figures are recomputed from the shipped raw data.

``docs/study-E-result.md`` claims that every number in it is derived from
``data/study_e/null_means.jsonl`` rather than typed. That claim is the reason this
file exists: the §1 cells drifted three times while the document was being
written (n=95 -> 98 -> 191), once in the direction that *flattered* the signal.
A prose promise cannot catch that. This test can, and it fails the gate.

What is pinned here, all recomputed from the two committed files:

  * the §1 verdict table — null mean, sd, p99, the real result's percentile and
    the count of shuffled draws beating it, for both horizons
  * the §5 stability table — every (n, p99, percentile) row, from prefixes of the
    raw file, which is what makes "the verdict does not depend on the count"
    checkable rather than asserted
  * §3's concentration figures, from the closes the study was actually run on
  * §4's nine rank correlations, from the committed flow aggregate — including
    that the two bolded ones are the five-session column, the only ones the
    document treats as worth arguing about
  * the shape of the raw files themselves: 200 unique reps in order, a complete
    254-session x 10-ticker grid with no gaps

What is **not** pinned, stated plainly so nobody reads more into a green test:
the real arm's mean OOS rank ICs (+0.0253, +0.0239) are not recomputed, because
reproducing them means refitting 64 gradient-boosted configurations per fold.
They are read out of the document and used as the threshold the null is measured
against; this test therefore verifies the null columns *given* those two numbers,
not the two numbers themselves. Reproducing them is what
``scripts/study_e_signal.py`` is for, and ``scripts/study_e_null.py`` reruns the
null against the same committed input.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics as st
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "docs" / "study-E-result.md"
_NULL = _REPO / "data" / "study_e" / "null_means.jsonl"
_CLOSES = _REPO / "data" / "study_e" / "closes.csv"

# The horizons as the §1 table labels them, and the key each uses in the raw file.
_LABELS = {"1 session": "h1", "5 sessions": "h5"}

# The document is typeset with a true minus sign; formatted floats produce a
# hyphen. Spelled as an escape so the literal cannot be mistaken for a hyphen by
# a reader or by ruff's confusable check.
_MINUS = "\u2212"


def _reps() -> list[dict[str, float]]:
    return [json.loads(line) for line in _NULL.read_text(encoding="utf-8").splitlines() if line.strip()]


def _p99(values: list[float]) -> float:
    """The same order statistic the document reports: nearest-rank, not interpolated."""
    ordered = sorted(values)
    m = len(ordered)
    return ordered[min(m - 1, round(0.99 * (m - 1)))]


def _percentile_of(values: list[float], real: float) -> float:
    return 100.0 * sum(1 for x in values if x < real) / len(values)


def _ordinal(pct: float) -> str:
    whole = int(pct)
    if whole % 100 in (11, 12, 13):
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(whole % 10, "th")
    return f"{pct:.1f}{suffix}"


def _cells(line: str) -> list[str]:
    """The cells of one markdown table row, bold markers stripped."""
    return [c.strip().replace("**", "") for c in line.strip().strip("|").split("|")]


def _row_starting(prefix: str) -> list[str]:
    line = next((ln for ln in _DOC.read_text(encoding="utf-8").splitlines() if ln.startswith(prefix)), None)
    assert line is not None, f"the document no longer has a table row starting {prefix!r}"
    return _cells(line)


# ---------------------------------------------------------------------------
# the raw files themselves
# ---------------------------------------------------------------------------


def test_the_null_file_holds_two_hundred_unique_reps_in_order() -> None:
    """A resumable run can duplicate or skip a rep across a kill; neither happened."""
    reps = _reps()
    numbers = [int(r["rep"]) for r in reps]
    assert len(reps) == 200
    assert numbers == list(range(200)), "reps must be complete and in order, with no gaps"
    assert all("h1" in r and "h5" in r for r in reps), "every rep carries both horizons"


def test_the_closes_input_is_a_complete_grid() -> None:
    """``load_closes`` refuses gaps, so the shipped input must have none (§3)."""
    rows = list(csv.reader(_CLOSES.open(encoding="utf-8")))
    tickers = {r[0] for r in rows}
    days = {r[1] for r in rows}
    assert len(rows) == 2540
    assert len(tickers) == 10
    assert len(days) == 254, "254 closes give the 253 return days §3 counts"
    assert len(rows) == len(tickers) * len(days), "a complete grid, not a ragged one"


# ---------------------------------------------------------------------------
# §1 — the verdict table
# ---------------------------------------------------------------------------


def test_section_1_table_is_derived_from_the_raw_null() -> None:
    reps = _reps()
    for label, key in _LABELS.items():
        cells = _row_starting(f"| {label} |")
        real = float(cells[1])
        values = [float(r[key]) for r in reps]

        assert cells[2] == f"{st.fmean(values):+.4f}", f"{label}: null mean is not the file's mean"
        assert cells[3] == f"{st.pstdev(values):.4f}", f"{label}: null sd is not the file's sd"
        assert cells[4] == f"{_p99(values):+.4f}", f"{label}: null p99 is not the file's p99"
        assert cells[5] == _ordinal(_percentile_of(values, real)), f"{label}: percentile is stale"
        beaten = sum(1 for x in values if x >= real)
        assert cells[6] == f"{beaten} / {len(values)}", f"{label}: beaten count is stale"


def test_the_verdict_is_a_rejection_on_both_horizons() -> None:
    """The document's conclusion must follow from its own numbers, not sit beside them.

    Threshold 1 of the pre-registration is the 99th percentile. If a future rerun
    ever cleared it, this assertion fails and the rejection wording must change
    with it — which is the point.
    """
    reps = _reps()
    for label, key in _LABELS.items():
        real = float(_row_starting(f"| {label} |")[1])
        values = [float(r[key]) for r in reps]
        assert real < _p99(values), f"{label}: the real result cleared its null; §1 says it did not"
        assert _percentile_of(values, real) < 99.0
    assert "**Rejected.**" in _DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §5 — the stability table, the claim that the count did not decide the verdict
# ---------------------------------------------------------------------------


def test_section_5_stability_table_recomputes_from_prefixes_of_the_raw_file() -> None:
    reps = _reps()
    reals = {key: float(_row_starting(f"| {label} |")[1]) for label, key in _LABELS.items()}
    checked = 0
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\| \d+ \| [+-]", line):
            continue
        cells = _cells(line)
        n = int(cells[0])
        prefix = reps[:n]
        assert len(prefix) == n, f"the table states n={n} but the file holds {len(reps)} reps"
        h1 = [float(r["h1"]) for r in prefix]
        h5 = [float(r["h5"]) for r in prefix]
        assert cells[1] == f"{_p99(h1):+.4f}", f"n={n}: h1 p99"
        assert cells[2] == f"{_percentile_of(h1, reals['h1']):.1f}%", f"n={n}: h1 percentile"
        assert cells[3] == f"{_p99(h5):+.4f}", f"n={n}: h5 p99"
        assert cells[4] == f"{_percentile_of(h5, reals['h5']):.1f}%", f"n={n}: h5 percentile"
        checked += 1
    assert checked == 5, f"§5 should carry five sample sizes; found {checked}"


def test_no_sample_size_ever_reached_the_threshold() -> None:
    """§5's sentence 'it never rises above the 78th percentile at any n'."""
    reps = _reps()
    reals = {key: float(_row_starting(f"| {label} |")[1]) for label, key in _LABELS.items()}
    for key, real in reals.items():
        worst = max(
            _percentile_of([float(r[key]) for r in reps[:n]], real)
            for n in (25, 50, 75, 100, 200)
        )
        assert worst <= 78.0, f"{key}: reached the {worst:.1f}th percentile, above the stated 78th"


# ---------------------------------------------------------------------------
# §3 — why the data cannot support more
# ---------------------------------------------------------------------------


def test_section_3_concentration_figures_come_from_the_shipped_closes() -> None:
    """Average pairwise log-return correlation and the first eigenvalue's share.

    Both are stated to three and one decimal places in §3. Simple returns give
    0.358 and 45.2% on the same file, which is why §3 names the method: the
    figures are not method-free and the document must not imply they are.
    """
    rows = list(csv.reader(_CLOSES.open(encoding="utf-8")))
    tickers = sorted({r[0] for r in rows})
    days = sorted({r[1] for r in rows})
    by_key = {(r[0], r[1]): float(r[2]) for r in rows}
    closes = np.array([[by_key[(t, d)] for t in tickers] for d in days])

    log_returns = np.diff(np.log(closes), axis=0)
    assert log_returns.shape == (253, 10)

    corr = np.corrcoef(log_returns, rowvar=False)
    off_diagonal = corr[np.triu_indices_from(corr, k=1)]
    eigenvalues = np.linalg.eigvalsh(corr)[::-1]

    doc = _DOC.read_text(encoding="utf-8")
    assert f"log** returns {off_diagonal.mean():.3f}" in doc, (
        f"§3's average pairwise correlation should be {off_diagonal.mean():.3f}"
    )
    share = 100.0 * eigenvalues[0] / eigenvalues.sum()
    assert f"**{share:.1f}%** of the cross-section" in doc, (
        f"§3's first-eigenvalue share should be {share:.1f}%"
    )
    assert math.isclose(off_diagonal.mean(), 0.361, abs_tol=5e-4), (
        "the shipped closes no longer produce the correlation §3 reports"
    )


# ---------------------------------------------------------------------------
# §4 — the options flow, described rather than modelled
# ---------------------------------------------------------------------------


def _average_ranks(values: list[float]) -> np.ndarray:
    """Ranks with ties averaged, which is what a Spearman correlation requires."""
    array = np.asarray(values, dtype=float)
    ranks = np.empty(len(array))
    ranks[array.argsort()] = np.arange(len(array), dtype=float)
    for value in set(array.tolist()):
        tied = array == value
        if tied.sum() > 1:
            ranks[tied] = ranks[tied].mean()
    return ranks


def _spearman(left: list[float], right: list[float]) -> float:
    return float(np.corrcoef(_average_ranks(left), _average_ranks(right))[0, 1])


def _closes_by_ticker() -> dict[str, list[tuple[str, float]]]:
    series: dict[str, list[tuple[str, float]]] = {}
    for ticker, day, close in csv.reader(_CLOSES.open(encoding="utf-8")):
        series.setdefault(ticker, []).append((day, float(close)))
    return {t: sorted(rows) for t, rows in series.items()}


def test_section_4_rank_correlations_come_from_the_committed_flow_aggregate() -> None:
    """The nine cells of §4, recomputed from ``flow_daily.csv`` and the closes.

    Two details of the method are load-bearing and were recovered by measurement,
    not assumption, so they are asserted here rather than described:

      * the score is ``combined_score_post``; ``combined_score_pre`` reproduces
        none of the same-day or next-session cells
      * each column uses every observation for which that horizon exists, giving
        n = 133, 123 and 83 — the table mixes three sample sizes, and the 83 is
        the one §4's p-value discussion is about
    """
    series = _closes_by_ticker()
    index = {t: {day: i for i, (day, _) in enumerate(rows)} for t, rows in series.items()}

    def horizon_return(ticker: str, day: str, sessions: int) -> float | None:
        rows = series[ticker]
        position = index[ticker].get(day)
        if position is None:
            return None
        start, end = (position - 1, position) if sessions == 0 else (position, position + sessions)
        if start < 0 or end >= len(rows):
            return None
        return rows[end][1] / rows[start][1] - 1.0

    flow = list(csv.DictReader(_CLOSES.parent.joinpath("flow_daily.csv").open(encoding="utf-8")))
    assert len(flow) == 133, "§4 counts 133 (ticker, day) observations"
    assert sum(int(row["print_count"]) for row in flow) == 17_037, "§4 counts 17,037 prints"
    assert len({row["day"] for row in flow}) == 15, "§4 counts 15 calendar days"

    features = {
        "print count": [float(row["print_count"]) for row in flow],
        "mean score": [float(row["mean_score"]) for row in flow],
        "max score": [float(row["max_score"]) for row in flow],
    }
    expected_n = {0: 133, 1: 123, 5: 83}
    for name, column in features.items():
        cells = _row_starting(f"| {name} |")
        for position, sessions in enumerate((0, 1, 5), start=1):
            xs: list[float] = []
            ys: list[float] = []
            for row, value in zip(flow, column, strict=True):
                moved = horizon_return(row["ticker"], row["day"], sessions)
                if moved is None:
                    continue
                xs.append(value)
                ys.append(moved)
            assert len(xs) == expected_n[sessions], (
                f"{name} at horizon {sessions}: expected {expected_n[sessions]} observations"
            )
            stated = cells[position].replace(_MINUS, "-")
            assert stated == f"{_spearman(xs, ys):+.3f}", (
                f"{name} vs horizon {sessions}: §4 states {stated}"
            )


def test_the_two_bold_figures_are_the_five_session_column() -> None:
    """§4 calls exactly two figures out and then argues they are not findings.

    If a future edit bolded a different cell, the paragraph underneath would be
    describing numbers that are no longer the ones marked.
    """
    text = _DOC.read_text(encoding="utf-8")
    bolded = set(re.findall(rf"\*\*([{_MINUS}+-]0\.\d{{3}})\*\*", text))
    assert bolded == {f"{_MINUS}0.306", f"{_MINUS}0.314"}, f"unexpected bold cells: {bolded}"
    assert "are **not\nfindings**" in text
