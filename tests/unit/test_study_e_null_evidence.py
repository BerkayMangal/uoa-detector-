"""Study E's published figures are recomputed from the shipped raw data.

``docs/study-E-result.md`` claims that every number in it is derived from the files
that ship with it rather than typed. That claim is the reason this file exists: the
§1 cells drifted three times while the document was being written (n=95 -> 98 ->
191), once in the direction that *flattered* the signal. A prose promise cannot
catch that. This can, and it fails the gate.

Two null files ship, and the distinction is load-bearing:

  * ``null_means_purged.jsonl`` is the **primary** run — the harness with a purge at
    the fold boundary, which is what the protocol always specified. §1, §2 and §5
    report it.
  * ``null_means.jsonl`` is the original, unpurged run. It is kept because deleting
    it would erase both the evidence that the leak existed and the measurement of
    how large it was. §1a compares the two.

What is pinned here, all recomputed:

  * §1's verdict table from the purged null, and §1a's two-run comparison from both
  * that the purge is a **no-op at horizon 1** (identical on all 200 reps) and
    **active at horizon 5** (identical on none) — the arithmetic that shows the code
    change touched only what it was supposed to
  * the shortfall ratios §1 states, as p99 / real
  * §2's spread figures, §5's stability table from prefixes, and §5's claim about
    every n rather than only the five tabulated
  * §3's concentration figures and §4's nine rank correlations, unchanged by the
    purge because neither depends on the walk-forward
  * the shape of the raw files: 200 unique reps in order in each, and a complete
    254-session x 10-ticker grid with no gaps

What is **not** pinned, stated plainly so nobody reads more into a green test: the
real arm's mean OOS rank ICs (+0.0253, +0.0075) are not recomputed, because
reproducing them means refitting 64 gradient-boosted configurations per fold. They
are read out of the document and used as the threshold the null is measured against.
Reproducing them is what ``scripts/study_e_signal.py`` is for, and
``scripts/study_e_null.py`` reruns the null against the same committed input.
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
_NULL = _REPO / "data" / "study_e" / "null_means_purged.jsonl"
_NULL_UNPURGED = _REPO / "data" / "study_e" / "null_means.jsonl"
_CLOSES = _REPO / "data" / "study_e" / "closes.csv"

# The horizons as the §1 table labels them, and the key each uses in the raw files.
_LABELS = {"1 session": "h1", "5 sessions": "h5"}

# The document is typeset with a true minus sign; formatted floats produce a
# hyphen. Spelled as an escape so the literal cannot be mistaken for a hyphen by
# a reader or by ruff's confusable check.
_MINUS = "\u2212"


def _reps(path: Path = _NULL) -> list[dict[str, float]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
    """The cells of one markdown table row, bold markers and typographic minus normalised."""
    return [
        c.strip().replace("**", "").replace(_MINUS, "-")
        for c in line.strip().strip("|").split("|")
    ]


def _row_starting(prefix: str) -> list[str]:
    line = next(
        (ln for ln in _DOC.read_text(encoding="utf-8").splitlines() if ln.startswith(prefix)),
        None,
    )
    assert line is not None, f"the document no longer has a table row starting {prefix!r}"
    return _cells(line)


def _flat() -> str:
    """The document as one line, so an assertion tests the sentence and not its wrapping."""
    return re.sub(r"\s+", " ", _DOC.read_text(encoding="utf-8")).replace(_MINUS, "-")


# ---------------------------------------------------------------------------
# the raw files themselves
# ---------------------------------------------------------------------------


def test_both_null_files_hold_two_hundred_unique_reps_in_order() -> None:
    """A resumable run can duplicate or skip a rep across a kill; neither did.

    The purged run was killed once by the OS under memory pressure and resumed in
    batches, which is exactly the case this checks.
    """
    for path in (_NULL, _NULL_UNPURGED):
        reps = _reps(path)
        numbers = [int(r["rep"]) for r in reps]
        assert len(reps) == 200, f"{path.name}: {len(reps)} reps"
        assert numbers == list(range(200)), f"{path.name}: reps must be complete and gapless"
        assert all("h1" in r and "h5" in r for r in reps), f"{path.name}: both horizons"


def test_the_purge_is_a_no_op_at_horizon_1_and_active_at_horizon_5() -> None:
    """The arithmetic that shows the code change touched only what it should.

    The purge is ``horizon - 1`` sessions, so at horizon 1 it removes nothing: with
    the same seeds and the same panel, every rep must come out bit-identical. At
    horizon 5 it removes four training days from every fold, so no rep can survive
    unchanged. If either direction ever breaks — the purge leaking into horizon 1, or
    quietly vanishing from horizon 5 — this fails.
    """
    purged = {int(r["rep"]): r for r in _reps(_NULL)}
    unpurged = {int(r["rep"]): r for r in _reps(_NULL_UNPURGED)}
    assert set(purged) == set(unpurged)

    identical_h1 = sum(
        1 for rep in purged if math.isclose(purged[rep]["h1"], unpurged[rep]["h1"], abs_tol=1e-12)
    )
    identical_h5 = sum(
        1 for rep in purged if math.isclose(purged[rep]["h5"], unpurged[rep]["h5"], abs_tol=1e-12)
    )
    assert identical_h1 == 200, (
        f"horizon 1 is a no-op for the purge; {200 - identical_h1} reps differ"
    )
    assert identical_h5 == 0, (
        f"horizon 5 must change under the purge; {identical_h5} reps are unchanged"
    )
    doc = _flat()
    assert "**200 of 200** null reps are identical" in doc
    assert "(0 of 200 identical)" in doc


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
# §1 — the verdict table, from the purged run
# ---------------------------------------------------------------------------


def test_section_1_table_is_derived_from_the_purged_null() -> None:
    reps = _reps()
    for label, key in _LABELS.items():
        cells = _row_starting(f"| {label} |")
        real = float(cells[1])
        values = [float(r[key]) for r in reps]

        # ``_cells`` already folds the document's typographic minus to a hyphen, so
        # the formatted float compares directly.
        assert cells[2] == f"{st.fmean(values):+.4f}", (
            f"{label}: null mean is not the purged file's mean"
        )
        assert cells[3] == f"{st.pstdev(values):.4f}", f"{label}: null sd"
        assert cells[4] == f"{_p99(values):+.4f}", f"{label}: null p99"
        assert cells[5] == _ordinal(_percentile_of(values, real)), f"{label}: percentile is stale"
        beaten = sum(1 for x in values if x >= real)
        assert cells[6] == f"{beaten} / {len(values)}", f"{label}: beaten count is stale"


def test_the_verdict_is_a_rejection_on_both_horizons() -> None:
    """The conclusion must follow from the document's own numbers, not sit beside them.

    Threshold 1 is the 99th percentile. If a future rerun ever cleared it, this
    assertion fails and the rejection wording must change with it.
    """
    reps = _reps()
    for label, key in _LABELS.items():
        real = float(_row_starting(f"| {label} |")[1])
        values = [float(r[key]) for r in reps]
        assert real < _p99(values), f"{label}: the real result cleared its null; §1 says it did not"
        assert _percentile_of(values, real) < 99.0
    assert "**Rejected.**" in _flat()


def test_the_stated_shortfall_ratios_are_the_ratios_in_the_file() -> None:
    """§1 says 2.9x at one session and 13.1x at five. Those are p99 / real."""
    reps = _reps()
    doc = _flat()
    for label, key in _LABELS.items():
        real = float(_row_starting(f"| {label} |")[1])
        ratio = _p99([float(r[key]) for r in reps]) / real
        assert f"**{ratio:.1f} times larger**" in doc, (
            f"{label}: the shortfall ratio should be {ratio:.1f}"
        )


def test_section_1a_compares_the_two_runs_from_both_files() -> None:
    """The four percentile cells of §1a, each recomputed from the file it belongs to."""
    purged, unpurged = _reps(_NULL), _reps(_NULL_UNPURGED)
    rows = {
        "1 session (purge is a no-op)": "h1",
        "5 sessions (purge is active)": "h5",
    }
    for label, key in rows.items():
        cells = _row_starting(f"| {label} |")
        real_unpurged, real_purged = float(cells[1]), float(cells[2])
        assert cells[3] == _ordinal(
            _percentile_of([float(r[key]) for r in unpurged], real_unpurged),
        ), f"{label}: unpurged percentile"
        assert cells[4] == _ordinal(
            _percentile_of([float(r[key]) for r in purged], real_purged),
        ), f"{label}: purged percentile"
    # The claim the section rests on: the purge cost horizon 5 a factor of about 3.
    h5 = _row_starting("| 5 sessions (purge is active) |")
    factor = float(h5[1]) / float(h5[2])
    assert f"factor of {factor:.1f}" in _flat(), f"the stated factor should be {factor:.1f}"


def test_the_per_fold_figures_in_section_1_are_the_purged_ones() -> None:
    """Threshold 4's fold list must be the purged run's, not the original's."""
    doc = _flat()
    assert "(+0.021, -0.176, +0.178)" in doc, "horizon 5's purged folds"
    assert "(-0.017, -0.077, +0.097, +0.099)" in doc, "horizon 1's folds, unchanged"
    # The unpurged horizon-5 folds may appear only in §1a's narrative, never as the
    # threshold-4 evidence.
    assert doc.count("(+0.020, -0.159, +0.211)") <= 1


# ---------------------------------------------------------------------------
# §2 — what the null proves
# ---------------------------------------------------------------------------


def test_section_2_quotes_the_purged_nulls_spread() -> None:
    reps = _reps()
    h1 = [float(r["h1"]) for r in reps]
    h5 = [float(r["h5"]) for r in reps]
    doc = _flat()
    assert f"sd {st.pstdev(h1):.4f} and {st.pstdev(h5):.4f}" in doc, "§2's sd figures"
    assert f"percentiles at {_p99(h1):+.4f} and {_p99(h5):+.4f}" in doc, "§2's p99 figures"
    assert f"maxima at {max(h1):+.4f} and {max(h5):+.4f}" in doc, "§2's maxima"


# ---------------------------------------------------------------------------
# §5 — the stability table, the claim that the count did not decide the verdict
# ---------------------------------------------------------------------------


def test_section_5_stability_table_recomputes_from_prefixes_of_the_purged_file() -> None:
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
    """§5's claim, quantified over EVERY n instead of the five the table lists.

    An earlier version took its maximum over n in (25, 50, 75, 100, 200) — exactly
    the sizes §5 tabulates — so it could not contradict the "at any n" quantifier it
    named in its own docstring. Found by the 2026-09-19 audit; this is the second
    time a guard in this file was written so it could not fail.
    """
    reps = _reps()
    reals = {key: float(_row_starting(f"| {label} |")[1]) for label, key in _LABELS.items()}
    doc = _flat()
    peaks = {}
    for key, real in reals.items():
        by_n = {
            n: _percentile_of([float(r[key]) for r in reps[:n]], real)
            for n in range(1, len(reps) + 1)
        }
        peaks[key] = max(by_n.values())
        settled = max(pct for n, pct in by_n.items() if n >= 25)
        assert peaks[key] < 99.0, f"{key}: cleared its own null at some sample size"
        assert settled <= peaks[key] + 1e-9, f"{key}: settled maximum exceeds the peak"
    assert f"peaks at the **{peaks['h1']:.1f}th** percentile at horizon 1" in doc, (
        f"h1's peak is {peaks['h1']:.1f}"
    )
    assert f"the **{peaks['h5']:.1f}th** at horizon 5" in doc, f"h5's peak is {peaks['h5']:.1f}"


# ---------------------------------------------------------------------------
# §3 — why the data cannot support more
# ---------------------------------------------------------------------------


def test_section_3_concentration_figures_come_from_the_shipped_closes() -> None:
    """Average pairwise log-return correlation and the first eigenvalue's share.

    Both are stated to three and one decimal places in §3. Simple returns give
    0.358 and 45.2% on the same file, which is why §3 names the method: the
    figures are not method-free and the document must not imply they are. Neither
    depends on the walk-forward, so the purge does not touch them.
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
            assert cells[position] == f"{_spearman(xs, ys):+.3f}", (
                f"{name} vs horizon {sessions}: §4 states {cells[position]}"
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
