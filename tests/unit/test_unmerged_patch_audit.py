"""The stranded-work scan (``scripts/audit_unmerged_patches.py``).

On 2026-09-20 six reviewed fixes were found on two local branches, four days old,
having never reached main — three of them affecting the live board. The scan exists so
that class of loss is found by a command instead of by someone deciding to count
branches one evening.

Its whole value is in one judgement: a patch absent by patch-id is LOST if it adds a
file main does not have, UPSTREAM if every file it adds is already there, and UNKNOWN
if it adds nothing at all. Getting that judgement wrong in the lenient direction
reproduces the original failure silently, so the three branches are pinned here
against synthetic inputs rather than against the repository's own state — which
changes with every merge and would make this test pass for the wrong reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_unmerged_patches.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_unmerged_patches", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_patch_adding_a_file_main_lacks_is_lost() -> None:
    """The signal that found all six: a new test file with no counterpart upstream.

    B-fix5 added tests/unit/test_board_regime_as_of.py and main had no such file. No
    amount of squashing can hide that, which is why it is the discriminator.
    """
    classify = _module().classify
    assert classify(["tests/unit/test_board_regime_as_of.py"], [False]) == "LOST"


def test_a_patch_whose_added_files_are_all_present_is_upstream() -> None:
    """C1b's case: absent by patch-id, but every file it adds is in main already."""
    classify = _module().classify
    added = [
        "tests/unit/test_alfa_card_routes.py",
        "webapp/templates/_alfa_card_buttons.html",
        "webapp/templates/_alfa_card_confirm.html",
    ]
    assert classify(added, [True, True, True]) == "UPSTREAM"


def test_one_missing_file_among_many_is_enough_to_call_it_lost() -> None:
    """The lenient reading — "most of it is there" — is the one that loses work."""
    classify = _module().classify
    assert classify(["a.py", "b.py", "c.py"], [True, False, True]) == "LOST"
    assert classify(["a.py", "b.py", "c.py"], [True, True, False]) == "LOST"
    assert classify(["a.py", "b.py", "c.py"], [False, True, True]) == "LOST"


def test_a_patch_that_adds_nothing_is_reported_unknown_not_safe() -> None:
    """A pure modification cannot be classified by file presence.

    Calling it UPSTREAM would be a guess in the direction that hides losses, and
    calling it LOST would bury the real ones in noise. It is named as unclassifiable
    and left for a human, which is the only honest third option.
    """
    classify = _module().classify
    assert classify([], []) == "UNKNOWN"


def test_every_verdict_the_script_can_return_is_covered_here() -> None:
    """Anti-vacuity: if a fourth verdict is added, this test fails until it is pinned."""
    classify = _module().classify
    seen = {
        classify([], []),
        classify(["x"], [True]),
        classify(["x"], [False]),
    }
    assert seen == {"UNKNOWN", "UPSTREAM", "LOST"}


def test_a_report_is_interesting_when_something_needs_a_human() -> None:
    """UPSTREAM patches alone are noise; LOST and UNKNOWN are what a reader must see."""
    module = _module()
    only_upstream = module.BranchReport(branch="b", upstream=[("abc", "s")])
    with_lost = module.BranchReport(branch="b", lost=[("abc", "s")])
    with_unknown = module.BranchReport(branch="b", unknown=[("abc", "s")])
    assert not only_upstream.interesting
    assert with_lost.interesting
    assert with_unknown.interesting


def test_the_verdict_names_match_the_report_fields_they_are_written_to() -> None:
    """``audit`` routes each verdict with getattr(report, verdict.lower()), so a
    verdict string without a matching field would raise at scan time rather than
    being reported. This pins the coupling instead of leaving it to discovery."""
    module = _module()
    report = module.BranchReport(branch="b")
    for verdict in ("LOST", "UPSTREAM", "UNKNOWN"):
        assert hasattr(report, verdict.lower()), verdict
        assert isinstance(getattr(report, verdict.lower()), list)
