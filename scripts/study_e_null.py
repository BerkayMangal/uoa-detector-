"""Run the Study E shuffled-label null in resumable chunks, one JSON line per rep.

    uv run python scripts/study_e_null.py data/study_e/closes.csv out.jsonl 200

The protocol is **not** reimplemented here. This module imports
``scripts/study_e_signal.py`` and calls its own ``load_closes``, ``build_panel``
and ``run_once``, so the search stays byte-identical to the pre-registered one
(``docs/study-E-signal-preregistration.md``). What this file adds is only
bookkeeping: each rep is written and flushed the moment it finishes, and memory
is released between reps.

That bookkeeping is the reason the result exists. The first attempt held all
reps in memory and the OS killed it after twenty minutes, taking every completed
rep with it; it was killed twice more after that. Being resumable, the run
survived each kill and finished all 200 passes.

The seed is ``1000 + rep``, so any rep can be reproduced alone, and re-running a
completed file is a no-op rather than a duplicate.

Pass a fourth argument to point at a different copy of the protocol module;
without it the committed one next to this file is used.
"""
from __future__ import annotations

import gc
import importlib.util
import json
import pathlib
import sys
import time

import numpy as np


def _load_protocol(path: pathlib.Path) -> object:
    """Import the study module by path and register it, so its dataclasses resolve."""
    spec = importlib.util.spec_from_file_location("study_e_signal", path)
    if spec is None or spec.loader is None:
        msg = f"cannot import the protocol module at {path}"
        raise SystemExit(msg)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules, so register before exec.
    sys.modules["study_e_signal"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    closes_csv, out_path, total = sys.argv[1], sys.argv[2], int(sys.argv[3])
    protocol = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else (
        pathlib.Path(__file__).resolve().parent / "study_e_signal.py"
    )
    se = _load_protocol(protocol)

    wide = se.load_closes(pathlib.Path(closes_csv))  # type: ignore[attr-defined]
    panels = {h: se.build_panel(wide, h) for h in se.HORIZONS}  # type: ignore[attr-defined]

    out = pathlib.Path(out_path)
    done: set[int] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["rep"])
            except (ValueError, KeyError):
                continue  # a half-written line from a kill is simply redone
    print(f"resuming: {len(done)} reps already recorded", flush=True)

    with out.open("a", encoding="utf-8") as handle:
        for rep in range(total):
            if rep in done:
                continue
            started = time.perf_counter()
            row: dict[str, float | int] = {"rep": rep}
            for horizon, panel in panels.items():
                rng = np.random.default_rng(1000 + rep)
                # ``horizon`` is positional since 2026-09-20: the protocol module
                # purges the fold boundary and needs the label span to do it. This
                # call site is dynamically imported, so neither mypy nor the test
                # suite would have caught the stale signature — it would have failed
                # only when someone reran the null.
                results = se.run_once(panel, horizon, shuffle=True, rng=rng)  # type: ignore[attr-defined]
                row[f"h{horizon}"] = float(np.nanmean([r.ic for r in results]))
                del results
                gc.collect()
            row["seconds"] = round(time.perf_counter() - started, 1)
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            if (rep + 1) % 5 == 0:
                print(f"rep {rep + 1}/{total} took {row['seconds']}s", flush=True)
    print("null complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
