"""``docs/MODULES.md`` states the live thresholds, so it must agree with the profile.

D8 makes `profiles/*.yaml` the only source of numeric truth. MODULES.md is what an
operator reads to find out what those numbers are, and on 2026-09-19 five of its
eight module blocks disagreed with the profile — not by a digit, but by scheme:

  * M22 documented 3-day pre-positioning bands; the profile scores survives-vs-
    expires with a 14-day look-ahead
  * M23 documented a 5-minute forward confirmation window; the profile reads a
    30-minute lookback
  * M24 documented two settings and omitted eleven, including the entire IV-rank
    scoring scheme, and stated the penalty as 0.20 where the profile says -0.4
  * M25 documented peer *counts*; the profile thresholds alignment *ratios*
  * M26 documented a 100,000-share floor; the profile uses $5,000,000 of notional

Each block also carried a rationale paragraph defending a threshold that does not
exist. Prose that argues for the wrong number is worse than prose that omits it.

This test makes that class of drift impossible to reintroduce: every numeric
setting in a ```yaml block of MODULES.md must exist in the profile with the same
value, and every numeric setting in the profile must be documented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import ruamel.yaml

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "docs" / "MODULES.md"
_PROFILE = _REPO / "profiles" / "v5_default.yaml"

# "Default thresholds" documents the default profile; the gamma-squeeze profile
# overrides a subset and is not what this page describes.
_BLOCK = re.compile(r"```yaml\n(m\d\d:.*?)```", re.DOTALL)
_SETTING = re.compile(r"^\s{2,}([a-z_0-9]+):\s*(-?[\d_]+(?:\.\d+)?)\s*(?:#.*)?$", re.MULTILINE)


def _profile_modules() -> dict[str, dict[str, float]]:
    loaded: Any = ruamel.yaml.YAML(typ="safe").load(_PROFILE.read_text(encoding="utf-8"))
    modules = loaded["scoring"]["modules"]
    return {
        name: {
            key: float(value)
            for key, value in (settings or {}).items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }
        for name, settings in modules.items()
    }


def _documented_blocks() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for block in _BLOCK.findall(_DOC.read_text(encoding="utf-8")):
        name = block.split(":", 1)[0].strip()
        out[name] = {
            key: float(value.replace("_", ""))
            for key, value in _SETTING.findall(block)
        }
    return out


def test_the_page_documents_every_module_the_profile_scores() -> None:
    documented, live = _documented_blocks(), _profile_modules()
    assert set(documented) == set(live), (
        f"documented but not in the profile: {sorted(set(documented) - set(live))}; "
        f"scored but undocumented: {sorted(set(live) - set(documented))}"
    )
    assert len(documented) == 8, f"M21-M28 is eight blocks; found {len(documented)}"


@pytest.mark.parametrize("module", sorted(_profile_modules()))
def test_each_documented_block_matches_the_profile(module: str) -> None:
    """One case per module, so a failure names the module rather than the page."""
    documented = _documented_blocks().get(module, {})
    live = _profile_modules()[module]

    only_documented = sorted(set(documented) - set(live))
    only_live = sorted(set(live) - set(documented))
    differing = {
        key: (documented[key], live[key])
        for key in sorted(set(documented) & set(live))
        if documented[key] != live[key]
    }

    assert not only_documented, (
        f"{module}: documented settings absent from the profile — an operator would "
        f"tune something that does not exist: {only_documented}"
    )
    assert not only_live, (
        f"{module}: live settings the page never mentions: {only_live}"
    )
    assert not differing, (
        f"{module}: the page states a different value than the profile "
        f"(documented, live): {differing}"
    )


def test_the_comparison_catches_each_kind_of_drift_it_exists_to_catch() -> None:
    """A guard that cannot fail is not a guard.

    The three shapes are checked against a synthetic pair rather than the real
    files, so this stays true when MODULES.md is correct — which is exactly when a
    same-file assertion would become vacuous.
    """
    live = {"a": 1.0, "b": 2.0}

    # a setting the page invents
    documented = {"a": 1.0, "b": 2.0, "c": 3.0}
    assert sorted(set(documented) - set(live)) == ["c"]

    # a setting the page omits
    documented = {"a": 1.0}
    assert sorted(set(live) - set(documented)) == ["b"]

    # the same setting with a different value
    documented = {"a": 1.0, "b": 9.0}
    assert {k: (documented[k], live[k]) for k in live if documented[k] != live[k]} == {
        "b": (9.0, 2.0),
    }


def test_underscored_and_decimal_literals_are_read_as_numbers() -> None:
    """``5_000_000`` in the page and ``5000000.0`` in the profile are the same value."""
    block = "m99:\n  size_usd: 5_000_000\n  ratio: 0.005\n  negative: -0.4\n"
    parsed = {k: float(v.replace("_", "")) for k, v in _SETTING.findall(block)}
    assert parsed == {"size_usd": 5_000_000.0, "ratio": 0.005, "negative": -0.4}


def test_a_commented_setting_is_still_read() -> None:
    """Most of the page's settings carry trailing comments; they are not values."""
    block = "m99:\n  window: 30                   # lookback for peer flow\n"
    assert _SETTING.findall(block) == [("window", "30")]
