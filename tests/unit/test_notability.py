"""Unit tests for the descriptive notability score (Phase 4.34)."""

from __future__ import annotations

from webapp.notability import notability_score


def _base() -> dict[str, float | bool | int]:
    return dict(premium=50_000.0, aggressive=False, cluster_density=0.0,
                age_minutes=30.0, dte=30)


def test_higher_premium_scores_higher() -> None:
    lo = notability_score(**{**_base(), "premium": 10_000.0})
    hi = notability_score(**{**_base(), "premium": 500_000.0})
    assert hi > lo


def test_aggressive_scores_higher() -> None:
    assert notability_score(**{**_base(), "aggressive": True}) > \
           notability_score(**{**_base(), "aggressive": False})


def test_more_clustering_scores_higher() -> None:
    assert notability_score(**{**_base(), "cluster_density": 0.8}) > \
           notability_score(**{**_base(), "cluster_density": 0.0})


def test_fresher_scores_higher() -> None:
    assert notability_score(**{**_base(), "age_minutes": 1.0}) > \
           notability_score(**{**_base(), "age_minutes": 300.0})


def test_shorter_dte_scores_higher() -> None:
    assert notability_score(**{**_base(), "dte": 2}) > \
           notability_score(**{**_base(), "dte": 90})


def test_score_is_finite_and_nonnegative() -> None:
    s = notability_score(**_base())
    assert s >= 0.0 and s == s  # not NaN
