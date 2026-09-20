"""Study F's published figures are recomputed from the shipped raw data.

``docs/study-F-result.md`` claims every number in it comes from
``data/study_f/null_forward.jsonl``, ``data/study_f/null_idio.jsonl`` and
``data/study_f/bars.csv`` rather than being typed. This file holds that claim to
account, because Study E's cells drifted three times while its document was being
written — once in the direction that flattered the signal — and a prose promise
cannot catch that. This can, and it fails the gate.

What is pinned, all recomputed here:

  * §1's verdict table — the real best-of-ten IC, the winning configuration, the
    null's mean, sd and 99th percentile, the real result's percentile and the
    count of shuffled draws that beat it, for both horizons
  * §4's market-neutral table, including the claim that nothing in 200 shuffled
    draws reaches the horizon-5 result — the one cell in the study that clears a
    99th percentile, and the one the protocol's order does not let be a finding
  * §5's stability table from prefixes of the raw file, and the claim that
    horizon 1 would have PASSED at n=25, which is what pre-registering the pass
    count bought
  * §3's panel counts and §7's liquidity figures, from the committed bars
  * the raw files' own shape: 402 records per label, reps complete and in order

Expectations are computed from the shipped files with arithmetic written out here,
never by importing the runner. A test that recomputes a figure with the code that
produced it proves determinism and nothing else.

Prose assertions read the document through ``_flat``, which collapses whitespace
and normalises the typographic minus. An assertion should fail when a figure goes
stale, not when someone rewraps a paragraph or the editor inserts a line break
between a number and its noun.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import statistics as st
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "docs" / "study-F-result.md"
_FORWARD = _REPO / "data" / "study_f" / "null_forward.jsonl"
_IDIO = _REPO / "data" / "study_f" / "null_idio.jsonl"
_BARS = _REPO / "data" / "study_f" / "bars.csv"

_NULL_PASSES = 200
_HORIZONS = {"1 session": 1, "5 sessions": 5}
# The document is typeset with a true minus; formatted floats produce a hyphen.
# Spelled as an escape so the literal cannot be mistaken for a hyphen by a reader
# or flagged as a confusable by ruff — the same arrangement Study E's test uses.
_MINUS = "\u2212"


def _raw() -> str:
    """The document as written — for anything that reads it line by line."""
    return _DOC.read_text("utf-8")


def _flat() -> str:
    """The document as one line, with the typographic minus normalised."""
    return re.sub(r"\s+", " ", _raw()).replace(_MINUS, "-")


def _records(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _arm(path: Path, horizon: int) -> tuple[dict[str, object], list[float]]:
    """(the real pass, the null bests) for one horizon of one label."""
    rows = [r for r in _records(path) if r["horizon"] == horizon]
    real = next(r for r in rows if int(str(r["rep"])) < 0)
    nulls = [float(str(r["best"])) for r in rows if int(str(r["rep"])) >= 0]
    return real, nulls


def _p99(values: list[float]) -> float:
    """Nearest-rank, not interpolated — the order statistic the document reports."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))]


def _percentile_of(values: list[float], real: float) -> float:
    return 100.0 * sum(1 for v in values if v < real) / len(values)


def _cells(line: str) -> list[str]:
    return [
        c.strip().replace("**", "").replace("`", "")
        for c in line.strip().strip("|").split("|")
    ]


def _row_starting(prefix: str) -> list[str]:
    line = next((ln for ln in _raw().splitlines() if ln.startswith(prefix)), None)
    assert line is not None, f"the document no longer has a table row starting {prefix!r}"
    return _cells(line)


def _null_winners(path: Path, horizon: int) -> dict[str, int]:
    wins: dict[str, int] = {}
    for record in _records(path):
        if record["horizon"] == horizon and int(str(record["rep"])) >= 0:
            name = str(record["best_config"])
            wins[name] = wins.get(name, 0) + 1
    return wins


# ---------------------------------------------------------------------------
# the raw files themselves
# ---------------------------------------------------------------------------


def test_both_labels_hold_402_passes_with_complete_reps() -> None:
    """A resumable runner can duplicate or skip a rep across a kill; neither happened."""
    for path in (_FORWARD, _IDIO):
        records = _records(path)
        assert len(records) == 402, f"{path.name}: {len(records)} records"
        for horizon in (1, 5):
            reps = sorted(int(str(r["rep"])) for r in records if r["horizon"] == horizon)
            assert reps == list(range(-1, _NULL_PASSES)), (
                f"{path.name} h{horizon}: reps must be -1..199 complete and gapless"
            )


def test_every_pass_searched_all_ten_configurations() -> None:
    """§6's null is a distribution of best-of-ten; a pass that scored fewer would
    make the null a distribution of a different statistic (§8 defect 5)."""
    for path in (_FORWARD, _IDIO):
        for record in _records(path):
            mean_ic = record["mean_ic"]
            assert isinstance(mean_ic, dict)
            assert len(mean_ic) == 10, f"{path.name} rep {record['rep']}: {len(mean_ic)} configs"
            assert float(str(record["best"])) == max(float(v) for v in mean_ic.values())


def test_the_committed_panel_is_the_one_the_document_describes() -> None:
    """§3 and §7: 70 tickers x 252 sessions, and the two names the $20M rule drops."""
    rows = list(csv.DictReader(_BARS.open(encoding="utf-8")))
    by_ticker: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(
            (float(row["close"]), float(row["volume"])),
        )
    assert len(rows) == 17_640
    assert len(by_ticker) == 70
    assert {len(v) for v in by_ticker.values()} == {252}

    dollar = {
        ticker: st.median([close * volume for close, volume in bars])
        for ticker, bars in by_ticker.items()
    }
    dropped = sorted(t for t, value in dollar.items() if value < 20_000_000.0)
    assert dropped == ["CHGG", "ROOT"], f"the $20M rule drops {dropped}"
    assert len(by_ticker) - len(dropped) == 68

    doc = _flat()
    assert f"CHGG (median daily dollar volume ${dollar['CHGG'] / 1e6:.1f}M)" in doc
    assert f"ROOT (${dollar['ROOT'] / 1e6:.1f}M)" in doc
    assert "leaving 68 names" in doc
    # 252 sessions - 60 warmup - horizon, the usable days §3 counts.
    assert "191 usable days at horizon 1 and 187 at horizon 5" in doc


# ---------------------------------------------------------------------------
# §1 — the verdict table
# ---------------------------------------------------------------------------


def test_section_1_table_is_derived_from_the_forward_null() -> None:
    for label, horizon in _HORIZONS.items():
        cells = _row_starting(f"| {label} |")
        real, nulls = _arm(_FORWARD, horizon)

        assert cells[1] == f"{float(str(real['best'])):+.4f}", f"{label}: real IC"
        assert cells[2] == str(real["best_config"]), f"{label}: winning configuration"
        assert cells[3] == f"{st.fmean(nulls):+.4f}", f"{label}: null mean"
        assert cells[4] == f"{st.pstdev(nulls):.4f}", f"{label}: null sd"
        assert cells[5] == f"{_p99(nulls):+.4f}", f"{label}: null p99"
        assert cells[6] == f"{_percentile_of(nulls, float(str(real['best']))):.1f}th", (
            f"{label}: percentile of the real result"
        )
        beaten = sum(1 for v in nulls if v >= float(str(real["best"])))
        assert cells[7] == f"{beaten} / {len(nulls)}", f"{label}: draws beating it"


def test_the_verdict_is_a_rejection_on_both_horizons() -> None:
    """The conclusion must follow from the document's own numbers, not sit beside them.

    If a rerun ever cleared the 99th percentile, this fails and the wording has to
    change with it — which is the point.
    """
    for horizon in (1, 5):
        real, nulls = _arm(_FORWARD, horizon)
        best = float(str(real["best"]))
        assert best <= _p99(nulls), f"h{horizon} cleared its null; §1 says it did not"
        assert _percentile_of(nulls, best) < 99.0
    assert "**Rejected on both horizons.**" in _flat()


def test_the_stated_shortfall_at_horizon_1_is_the_ratio_in_the_file() -> None:
    """§1 says the IC would have to be about 1.24x larger. That is p99 / real."""
    real, nulls = _arm(_FORWARD, 1)
    ratio = _p99(nulls) / float(str(real["best"]))
    assert f"**{ratio:.2f} times larger**" in _flat(), (
        f"the shortfall ratio should be {ratio:.2f}"
    )


def test_horizon_5_sits_below_its_own_nulls_mean_and_loses_to_most_draws() -> None:
    real, nulls = _arm(_FORWARD, 5)
    best = float(str(real["best"]))
    assert best < st.fmean(nulls), "§1 claims the real result is below the null mean"
    beaten = sum(1 for v in nulls if v >= best)
    assert beaten > len(nulls) / 2, "§1 claims more than half the draws beat it"


def test_the_stability_threshold_t2_is_what_the_document_reports() -> None:
    """3 of 3 folds at horizon 1; 2 of 3 at horizon 5, which is 67% against 75%."""
    real_1, _ = _arm(_FORWARD, 1)
    real_5, _ = _arm(_FORWARD, 5)
    assert int(str(real_1["folds"])) == 3
    assert float(str(real_1["positive_fold_share"])) == 1.0
    assert int(str(real_5["folds"])) == 3
    assert abs(float(str(real_5["positive_fold_share"])) - 2 / 3) < 1e-9
    doc = _flat()
    assert "positive in **3 of 3** folds" in doc
    assert "positive in 2 of 3, which is 67% against the 75%" in doc


# ---------------------------------------------------------------------------
# §2 — what the null proves
# ---------------------------------------------------------------------------


def test_section_2_quotes_the_nulls_own_maxima() -> None:
    nulls_1 = _arm(_FORWARD, 1)[1]
    nulls_5 = _arm(_FORWARD, 5)[1]
    expected = f"its maxima are {max(nulls_1):+.4f} and {max(nulls_5):+.4f}"
    assert expected in _flat(), f"§2's null maxima should read: {expected}"


def test_section_2_quotes_the_configurations_that_win_on_shuffled_labels() -> None:
    """The claim that a flexible model wins on noise, with its own counts."""
    wins = _null_winners(_FORWARD, 1)
    expected = (
        f"won by `ridge` {wins['ridge']} times and by the analog predictor "
        f"{wins['analog']} times at horizon 1"
    )
    assert expected in _flat(), f"§2's null winners should read: {expected}"


def test_section_2_real_arm_figures_for_the_two_null_favourites() -> None:
    real, _ = _arm(_FORWARD, 1)
    mean_ic = real["mean_ic"]
    assert isinstance(mean_ic, dict)
    ridge, analog = float(mean_ic["ridge"]), float(mean_ic["analog"])
    assert f"(`ridge` {ridge:+.4f}, `analog` {analog:+.4f})" in _flat(), (
        f"§2's real-arm figures should be ridge {ridge:+.4f}, analog {analog:+.4f}"
    )
    # The claim that rests on them: both score worse on real labels than the winner.
    assert ridge < float(str(real["best"]))
    assert analog < float(str(real["best"]))


# ---------------------------------------------------------------------------
# §4 — the market-neutral arm, recorded but not promoted
# ---------------------------------------------------------------------------


def test_section_4_table_is_derived_from_the_market_neutral_null() -> None:
    for label, horizon in (("1 session (idio)", 1), ("5 sessions (idio)", 5)):
        cells = _row_starting(f"| {label} |")
        real, nulls = _arm(_IDIO, horizon)
        best = float(str(real["best"]))
        assert cells[1] == f"{best:+.4f}", f"{label}: real IC"
        assert cells[2] == f"{st.fmean(nulls):+.4f}", f"{label}: null mean"
        assert cells[3] == f"{_p99(nulls):+.4f}", f"{label}: null p99"
        assert cells[4] == f"{max(nulls):+.4f}", f"{label}: null max"
        assert cells[5] == f"{_percentile_of(nulls, best):.1f}th", f"{label}: percentile"
        beaten = sum(1 for v in nulls if v >= best)
        assert cells[6] == f"{beaten} / {len(nulls)}", f"{label}: draws beating it"


def test_the_market_neutral_horizon_5_really_does_clear_its_null() -> None:
    """§4's honesty depends on this being true: the cell it declines to promote is
    a genuine pass on its own terms, not a rounding artifact."""
    real, nulls = _arm(_IDIO, 5)
    best = float(str(real["best"]))
    assert best > _p99(nulls)
    assert sum(1 for v in nulls if v >= best) == 0
    assert _percentile_of(nulls, best) == 100.0
    doc = _flat()
    assert "**not one of 200 shuffled draws reaches it**" in doc
    # And the document must still refuse it, or the protocol's order was abandoned.
    assert "The protocol does not let this be a finding" in doc
    assert "it does not reach the board" in doc


def test_the_forward_arm_at_horizon_5_is_why_t3_is_never_reached() -> None:
    real, nulls = _arm(_FORWARD, 5)
    pct = _percentile_of(nulls, float(str(real["best"])))
    assert f"sits at the {pct:.1f}th percentile of its null" in _flat()


# ---------------------------------------------------------------------------
# §5 — the pass count, and the cell that would have lied at n=25
# ---------------------------------------------------------------------------


def test_section_5_stability_table_recomputes_from_prefixes() -> None:
    real_1, nulls_1 = _arm(_FORWARD, 1)
    real_5, nulls_5 = _arm(_FORWARD, 5)
    best_1, best_5 = float(str(real_1["best"])), float(str(real_5["best"]))
    checked = 0
    for line in _raw().splitlines():
        if not re.match(r"^\| \d+ \| [+-]0\.\d{4} \|", line):
            continue
        cells = _cells(line)
        n = int(cells[0])
        assert len(nulls_1) >= n and len(nulls_5) >= n, f"the table states n={n}"
        assert cells[1] == f"{_p99(nulls_1[:n]):+.4f}", f"n={n}: h1 p99"
        assert cells[2] == f"{_percentile_of(nulls_1[:n], best_1):.1f}%", f"n={n}: h1 percentile"
        assert cells[3] == f"{_p99(nulls_5[:n]):+.4f}", f"n={n}: h5 p99"
        assert cells[4] == f"{_percentile_of(nulls_5[:n], best_5):.1f}%", f"n={n}: h5 percentile"
        checked += 1
    assert checked == 5, f"§5 should carry five sample sizes; found {checked}"


def test_horizon_1_would_have_passed_at_twenty_five_passes() -> None:
    """§5's central claim, and the reason the count was fixed before the run.

    This is not a curiosity: it is the difference between this document and one
    that published an edge.
    """
    real, nulls = _arm(_FORWARD, 1)
    best = float(str(real["best"]))
    assert best > _p99(nulls[:25]), "at n=25 the real result must clear the prefix's p99"
    assert best <= _p99(nulls), "at n=200 it must not"
    assert "**At n=25 horizon 1 would have passed T1**" in _flat()


def test_horizon_5_never_approaches_the_threshold_at_any_n() -> None:
    """Quantified over EVERY n, not only the five the table lists — the quantifier
    Study E's equivalent test failed to honour, found by the 2026-09-19 audit."""
    real, nulls = _arm(_FORWARD, 5)
    best = float(str(real["best"]))
    by_n = {n: _percentile_of(nulls[:n], best) for n in range(1, len(nulls) + 1)}
    peak = max(by_n.values())
    settled = max(pct for n, pct in by_n.items() if n >= 25)
    doc = _flat()
    assert f"peaks at {peak:.1f} over all n" in doc, f"the peak is {peak:.1f}"
    assert f"at {settled:.1f} once n ≥ 25" in doc, f"the settled maximum is {settled:.1f}"
    assert peak < 99.0


# ---------------------------------------------------------------------------
# §6 — nothing ships
# ---------------------------------------------------------------------------


def test_the_document_states_that_nothing_reaches_the_board() -> None:
    doc = _flat()
    assert "**Nothing from this study reaches the board.**" in doc
    assert "no score, no probability" in doc


# ---------------------------------------------------------------------------
# §2a — what a SHAP plot of this model would have been worth
# ---------------------------------------------------------------------------

_SHAP = {
    "1 session, forward": (_REPO / "data" / "study_f" / "shap_forward_h1.json", _FORWARD, 1),
    "5 sessions, forward": (_REPO / "data" / "study_f" / "shap_forward_h5.json", _FORWARD, 5),
    "5 sessions, market-neutral": (
        _REPO / "data" / "study_f" / "shap_idio_h5.json", _IDIO, 5,
    ),
}


def _shap(label: str) -> dict[str, object]:
    return json.loads(_SHAP[label][0].read_text(encoding="utf-8"))


def test_section_2a_table_is_derived_from_the_shipped_shap_files() -> None:
    for label in _SHAP:
        data = _shap(label)
        cells = _row_starting(f"| {label} |")
        real = data["real_vs_shuffled_spearman"]
        between = data["shuffled_vs_shuffled_spearman"]
        assert isinstance(real, dict)
        assert isinstance(between, dict)
        assert cells[1] == f"{real['mean']:+.3f} (sd {real['sd']:.3f})", (
            f"{label}: real-vs-shuffled agreement"
        )
        assert cells[2] == f"{between['mean']:+.3f} (sd {between['sd']:.3f})", (
            f"{label}: shuffled-vs-shuffled agreement"
        )
        overlap = data["real_top5_overlap_with_shuffled"]
        between_overlap = data["shuffled_top5_overlap_between"]
        assert isinstance(overlap, dict)
        assert isinstance(between_overlap, dict)
        assert cells[3] == f"{overlap['mean']:.2f} / 5", f"{label}: real top-5 overlap"
        assert cells[4] == f"{between_overlap['mean']:.2f} / 5", f"{label}: shuffled pairs"


def test_each_shap_cell_ran_on_the_configuration_that_won_it() -> None:
    """A SHAP pass on a configuration the search did not pick describes nothing.

    §6 fixes SHAP to *the winning* configuration, so the cell's config must equal the
    ``best_config`` its own null file recorded for that horizon and label.
    """
    for label, (_path, null_path, horizon) in _SHAP.items():
        data = _shap(label)
        real, _nulls = _arm(null_path, horizon)
        assert data["config"] == str(real["best_config"]), label
        assert data["horizon"] == horizon, label
        assert data["shuffles"] == 20, label
        assert len(data["features"]) == 37, label  # type: ignore[arg-type]
        assert len(data["real_importance"]) == 37, label  # type: ignore[arg-type]


def test_the_real_arm_agrees_with_shuffles_less_than_shuffles_agree_with_each_other() -> None:
    """§2a's first claim, held in every cell.

    This is the direction that made the earlier "SHAP would only describe noise"
    wording too strong. If it ever inverts, the paragraph is wrong and this fails
    before a reader can be misled by it.
    """
    for label in _SHAP:
        data = _shap(label)
        real = data["real_vs_shuffled_spearman"]
        between = data["shuffled_vs_shuffled_spearman"]
        assert isinstance(real, dict)
        assert isinstance(between, dict)
        assert real["mean"] < between["mean"], f"{label}: agreement direction"
        overlap = data["real_top5_overlap_with_shuffled"]
        between_overlap = data["shuffled_top5_overlap_between"]
        assert isinstance(overlap, dict)
        assert isinstance(between_overlap, dict)
        assert overlap["mean"] < between_overlap["mean"], f"{label}: top-5 direction"


def test_section_2a_quotes_the_figure_its_argument_rests_on() -> None:
    """The horizon-5 shuffled overlap: 5 features, 4.27 of them shared, on dead labels."""
    data = _shap("5 sessions, forward")
    between_overlap = data["shuffled_top5_overlap_between"]
    assert isinstance(between_overlap, dict)
    assert f"**{between_overlap['mean']:.2f} of 5**" in _flat(), (
        f"§2a should quote {between_overlap['mean']:.2f} of 5"
    )


def test_section_2a_names_the_horizon_1_top_five_it_shows_the_reader() -> None:
    """Compared as a set: the document lists them by importance, the file sorts them."""
    top5 = _shap("1 session, forward")["real_top5"]
    assert isinstance(top5, list)
    doc = _flat()
    for name in top5:
        assert f"`{name}`" in doc, f"§2a should name {name}"
    assert len(top5) == 5


def test_the_document_withdraws_the_claim_the_measurement_did_not_support() -> None:
    """The earlier draft said SHAP was not computed because it would show only noise.

    Both halves changed, and the document has to say so rather than quietly reading as
    though it always measured this.
    """
    doc = _flat()
    assert "was too strong and is withdrawn" in doc
    assert "**SHAP was computed after all**" in doc
    # And §6's rule must still be stated as having held.
    assert "no feature was added, removed or re-weighted" in doc


def test_the_shap_script_parses_a_configuration_label_back_exactly() -> None:
    """``xgb_d3_n100_lr0.03`` must become the configuration the search used.

    A silent mis-parse would fit a different model and describe it as the winner. The
    expectation is written out here rather than derived from the string.
    """
    path = _REPO / "scripts" / "study_f_shap.py"
    spec = importlib.util.spec_from_file_location("study_f_shap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module._xgb_kwargs("xgb_d3_n100_lr0.03") == {
        "max_depth": 3, "n_estimators": 100, "learning_rate": 0.03,
    }
    assert module._xgb_kwargs("xgb_d2_n300_lr0.1") == {
        "max_depth": 2, "n_estimators": 300, "learning_rate": 0.1,
    }
    # And the non-tree configurations are refused rather than silently mangled.
    for label in ("ridge", "analog"):
        try:
            module._xgb_kwargs(label)
        except SystemExit:
            continue
        msg = f"{label} should be refused: SHAP here is defined on the tree models"
        raise AssertionError(msg)
