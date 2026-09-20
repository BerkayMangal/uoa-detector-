"""Find work that exists on a local branch and nowhere in main.

    uv run python scripts/audit_unmerged_patches.py            # report
    uv run python scripts/audit_unmerged_patches.py --strict   # exit 1 if anything is lost

On 2026-09-20 six reviewed fixes were found sitting on `p52-faz-b` and `p52-faz-c`,
four days after they were written, having never reached main. Three of them affected
the live board: a regime reading that could render a week-old value as "2 dk önce", a
T+1 confirmation that could never resolve and spent a UW request every pre-market
until its contract expired, and an unmapped label that took the render path to a 500.

Nothing noticed, and the reason is that the obvious check lies. `git rev-list
main..branch` counts commits by SHA, so a branch whose work was rebased or squashed
into main still shows commits "ahead" — and a branch that is *genuinely* behind looks
exactly the same. Of 70 local branches, 18 had commits ahead by SHA; only 4 carried
patches that were really absent, and only 6 patches out of 9 were really lost.

So this script asks the question in two steps, the way the manual pass did:

  1. ``git cherry`` compares by patch-id, which survives a rebase. Its ``+`` lines
     are patches with no equivalent upstream.
  2. For each of those, the files the patch ADDS are checked against main. A patch
     that adds a file main does not have cannot be upstream under any spelling — that
     is the signal. A patch whose added files are all present is almost certainly
     upstream in squashed form, and is reported separately rather than as a loss.

A patch that adds no files at all cannot be classified this way, and is reported as
UNKNOWN rather than quietly counted as safe. Silence about what a check cannot see is
how the original loss stayed invisible.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field

_BASE = "main"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def local_branches(base: str = _BASE) -> list[str]:
    out = _git("for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b for b in out.split() if b and b != base]


def absent_patches(branch: str, base: str = _BASE) -> list[str]:
    """Commits on ``branch`` whose patch has no equivalent in ``base`` (git cherry '+')."""
    out = _git("cherry", base, branch)
    return [
        line.split()[1]
        for line in out.splitlines()
        if line.startswith("+") and len(line.split()) > 1
    ]


def files_added(sha: str) -> list[str]:
    out = _git("show", "--diff-filter=A", "--name-only", "--format=", sha)
    return [f for f in out.splitlines() if f.strip()]


def exists_in_base(path: str, base: str = _BASE) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{path}"],
        capture_output=True, check=False,
    ).returncode == 0


def classify(added: list[str], present: list[bool]) -> str:
    """LOST when the patch adds a file base lacks; UPSTREAM when all are present.

    UNKNOWN when the patch adds nothing: it may be upstream in squashed form or may
    be genuinely absent, and this check cannot tell. Reported, never assumed safe.
    """
    if not added:
        return "UNKNOWN"
    if not all(present):
        return "LOST"
    return "UPSTREAM"


@dataclass
class BranchReport:
    branch: str
    lost: list[tuple[str, str]] = field(default_factory=list)      # (sha, subject)
    upstream: list[tuple[str, str]] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)

    @property
    def interesting(self) -> bool:
        return bool(self.lost or self.unknown)


def audit(base: str = _BASE) -> list[BranchReport]:
    reports: list[BranchReport] = []
    for branch in sorted(local_branches(base)):
        report = BranchReport(branch=branch)
        for sha in absent_patches(branch, base):
            subject = _git("log", "-1", "--format=%s", sha).strip()
            added = files_added(sha)
            verdict = classify(added, [exists_in_base(f, base) for f in added])
            missing = [f for f in added if not exists_in_base(f, base)]
            detail = subject if verdict != "LOST" else f"{subject}  [missing: {missing[0]}]"
            getattr(report, verdict.lower()).append((sha[:9], detail))
        if report.lost or report.upstream or report.unknown:
            reports.append(report)
    return reports


def main() -> int:
    strict = "--strict" in sys.argv
    base = _BASE
    reports = audit(base)

    lost_total = sum(len(r.lost) for r in reports)
    unknown_total = sum(len(r.unknown) for r in reports)
    upstream_total = sum(len(r.upstream) for r in reports)

    for report in reports:
        if not report.interesting:
            continue
        print(f"\n{report.branch}")
        for sha, detail in report.lost:
            print(f"  LOST     {sha} {detail}")
        for sha, detail in report.unknown:
            print(f"  UNKNOWN  {sha} {detail}  (adds no file; cannot be classified here)")
        if report.upstream:
            print(f"  ({len(report.upstream)} further patch(es) upstream in squashed form)")

    print(
        f"\n{len(local_branches(base))} local branches, {len(reports)} with patches absent "
        f"from {base} by patch-id:",
    )
    print(f"  LOST     {lost_total}  — add a file {base} does not have")
    print(f"  UNKNOWN  {unknown_total}  — add no file; check by hand")
    print(f"  UPSTREAM {upstream_total}  — every added file is already in {base}")
    if not lost_total and not unknown_total:
        print(f"\nNothing is stranded outside {base}.")
    return 1 if strict and (lost_total or unknown_total) else 0


if __name__ == "__main__":
    raise SystemExit(main())
