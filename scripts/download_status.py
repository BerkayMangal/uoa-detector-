"""Phase 3.5.2.1 — download progress / ETA reporter.

Reads the live ``.download_state.json`` produced by
``scripts/download_tier2.py`` (Phase 3.3.4) and prints a human-
readable status snapshot: per-state task counts, total rows so far,
elapsed wall-clock, observed throughput, ETA.

Usage:
  python scripts/download_status.py data/historical/                # one or more state dirs
  python scripts/download_status.py data/historical/dry-run/
  python scripts/download_status.py data/historical/tier1_anchor/ data/historical/tier2_starter/

The script is read-only — never touches the state file, never makes
network calls. Safe to run while a download is mid-flight.

Phase 3.5.3 operator workflow:
  - Run ``download_tier2.py`` in one terminal tab
  - Run ``download_status.py`` periodically in another tab
  - Watch ETA + throughput; if ``throughput < 1 ticker-month/hour``
    for 4+ hours, apply Phase 3.5 §"open question" escape hatch
    (cancel + restart with reduced universe).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    try:
        with state_path.open() as f:
            data: dict[str, Any] = json.load(f)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  WARNING: cannot read {state_path}: {exc}", file=sys.stderr)
        return None


def _summarise(state_dir: Path) -> None:
    state_path = state_dir / ".download_state.json"
    state = _load_state(state_path)
    if state is None:
        print(f"=== {state_dir} ===")
        print("  no state file found")
        print()
        return

    tasks: dict[str, dict[str, Any]] = state.get("tasks", {})
    counter: Counter[str] = Counter(t.get("status", "unknown") for t in tasks.values())
    total = len(tasks)
    done = counter.get("done", 0)
    failed = counter.get("failed", 0)
    in_progress = counter.get("in_progress", 0)
    pending = counter.get("pending", 0)

    rows = sum(t.get("row_count", 0) or 0 for t in tasks.values())

    started_iso = state.get("started_at")
    if started_iso:
        started = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        elapsed = (datetime.now(UTC) - started).total_seconds()
    else:
        elapsed = 0.0

    last_complete: datetime | None = None
    for t in tasks.values():
        c = t.get("completed_at")
        if c:
            ts = datetime.fromisoformat(c.replace("Z", "+00:00"))
            if last_complete is None or ts > last_complete:
                last_complete = ts

    print(f"=== {state_dir} ===")
    print(
        f"  total: {total:>4d}  done: {done:>4d}  "
        f"in_progress: {in_progress:>3d}  pending: {pending:>4d}  "
        f"failed: {failed:>3d}",
    )
    print(f"  rows accumulated: {rows:,}")
    if elapsed > 0:
        elapsed_h = elapsed / 3600.0
        print(f"  elapsed: {elapsed_h:.2f}h ({elapsed:.0f}s)")
        if done > 0:
            tasks_per_hour = done / elapsed_h
            print(
                f"  throughput: {tasks_per_hour:.2f} ticker-month/hour "
                f"({done} done in {elapsed_h:.2f}h)",
            )
            remaining = pending + in_progress
            if remaining > 0 and tasks_per_hour > 0:
                eta_hours = remaining / tasks_per_hour
                eta_days = eta_hours / 24.0
                eta_done = datetime.now(UTC).timestamp() + eta_hours * 3600
                eta_when = datetime.fromtimestamp(eta_done, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M UTC",
                )
                print(
                    f"  ETA: {eta_hours:.1f}h ({eta_days:.1f}d) "
                    f"→ ~{eta_when}",
                )
                if tasks_per_hour < 1.0:
                    print(
                        "  ⚠️  throughput < 1 ticker-month/hour. If sustained "
                        "for 4+h apply Phase 3.5 §'open question' rule "
                        "(reduce universe).",
                    )
    if last_complete is not None:
        idle_s = (datetime.now(UTC) - last_complete).total_seconds()
        print(
            f"  last task completed: {last_complete.strftime('%H:%M:%S UTC')} "
            f"({idle_s:.0f}s ago)",
        )

    if failed > 0:
        print("  recent failures:")
        for k, v in list(tasks.items())[:200]:
            if v.get("status") == "failed":
                err = (v.get("error") or "")[:120]
                print(f"    {k}: {err}")
                # Cap at first 3 displayed failures so the report stays scannable.
                break
    print()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download progress reporter.")
    p.add_argument(
        "state_dir",
        nargs="+",
        type=Path,
        help="One or more directories containing .download_state.json",
    )
    args = p.parse_args(argv)
    for d in args.state_dir:
        _summarise(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
