# Phase5.62.1 — explicit source budget

Both original native session attempts safely returned409/source_limit_exceeded;
zero datasets or market results were produced. This delivery makes the already
computed narrow source metadata inspectable before choosing a bounded export.

The default16MiB source-character budget remains. A typed strict-integer request
may explicitly specify1..64MiB after source metadata is reviewed; the selected
budget is recorded in the export envelope.256-scan,8MiB-response, frozen adapter
limits and single-export lock remain. Metadata-only requests neither materialize
snapshots nor call the adapter. No threshold, frozen research identity or trading
behavior changed, and a hard-cap refusal is still an unknown/blocked dataset.

Full unchanged gate: **6196 passed,52 key-gated skips,two existing warnings**,
151.94s; mypy336 files,Ruff,lock,whitespace clean.25 focused cases pass, including
eight new metadata/budget cases. Existing default refusal, exact complete-source
export, invalid/bool/above-hard-budget rejection and private metadata behavior
are verified. Exact CI/native source-size decisions and actual pilot evidence
are appended to the release PR after publication; no unexecuted result is claimed.
