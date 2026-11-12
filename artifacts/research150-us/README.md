# Research150 US — frozen research record

This archive preserves seven original research files byte for byte. It contains
the 150-idea feasibility inventory and the fixed specification for the first
three-family US stock pilot. A deterministic `engine-source.zip` separately
preserves the seven engine/test Python files named by `engine-code-hashes.json`.
It contains no raw prices, native provider packets, news articles or credentials.
The `development/` and `evaluation/` subdirectories add byte-preserved
summary/audit evidence and deterministic gzip files of all 324 per-policy/cost
summary rows for each phase, with separate manifests recording both compressed
and original digests. No feature matrices, trade-by-trade price rows or raw data
accompany those results.
The `coverage/` directory retains acquisition and coverage summaries; its compact
exclusion audit keeps original source hashes without republishing native volume
rows or private raw-source paths. Excluded sessions have invalid volume, not
missing native price timestamps.
The `evidence/` directory contains independent mechanical verification and an
undeployed news-export proposal with synthetic validation. Its compact news
coverage summary excludes article text, raw packets and individual records.
The small news prototype ZIP retains its unchanged source/test/profile files and
an internal SHA256 manifest; it does not install a route or change the live app.
The `supplement-s1/` directory preserves the separate exploratory five-family
specification; see its [archival addendum](../../docs/phase-5.69-S1-addendum-record.md).
S1's separately reviewed source, pre-run seal and complete results are archived.
Across both runs, 135 source-directed candidates and nine separate controls are
negative in development at every cost. The later period has 3/135 positives at
5 bps and none at 10/20 bps; all controls remain negative. No validated edge or
promotion follows. Full outcomes and independent review are retained.

The inventory records **13 feasible with available OHLCV, 58 requiring a new
adaptation, and 79 unavailable with the exact required data**. These are data
feasibility counts, not 150 backtests or positive findings. The first pilot has
108 registered policies (54 candidates and 54 baselines), displayed at three
fixed cost assumptions for 324 policy-cost views.
The 13 ideas have OHLCV-observable components; precise rules and any additional
dependencies still need to be specified. They are not 13 ready strategies.

Some large original files use lossless gzip or XZ transport to keep the review
payload below 2 MB. The [transport record](../../docs/phase-5.69-archive-transport.md)
maps logical filenames to delivered paths. Each directory's archive manifest
records compressed and original digests; decompression restores the exact
original bytes, without minification. The v2 registry remains plain JSON to
preserve the frozen contract's existing link.

See [the archival contract](../../docs/phase-5.69-us-intraday-research-acceptance.md)
and [the result record](../../docs/phase-5.69-results.md). The protocol, registry,
and two pre-outcome clarifications govern the analysis; this README is not a
replacement specification.

`archive-manifest.json` records byte sizes, SHA256 digests, and observed original
filesystem modification times. `pilot-freeze-v2.json` binds the protocol and
registry at its embedded timestamp; `engine-code-hashes.json` binds the later
clarifications and reviewed implementation. A locally recorded timestamp is not
an independently authenticated timestamp. The inventory's archived digest is
not retroactively asserted to have been part of those earlier seals.

Original files contain local workspace paths as historical provenance. They are
retained solely to preserve their byte identity. They are not public download
locations or required checkout paths. Use the sibling artifact filenames in this
archive. The source snapshot is research machinery, not application runtime code.
Licensed raw inputs remain outside this package, so a market replay requires the
private coverage manifest and hash-matched canonical files. A metadata-only
dataset plan is provided under `coverage/` for synthetic S1 reproduction.

History reused in earlier UOA studies remains burned. News is context only in
this pilot. ThetaData access is awaiting a real Terminal response; these stock
results cannot establish option returns or successful provider integration.

## Source verification and synthetic reproduction

Work from a private copy of this artifact directory, treated as the research
root. First restore the logical files from compressed transport as described in
the transport record; the existing plain originals must remain identical.
The v2 ZIP has exactly seven sorted, basename-only regular files, with fixed
headers, no extra entries, no symlinks and no `__pycache__`. Its SHA256 is
`748bd66901de840506e57517a775fbb8adaf315ee7288af84c1b2aa045d0e57b`.
Verify the archive and each source entry before extraction:

```python
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

root = Path.cwd()
manifest = json.loads((root / "archive-manifest.json").read_text())
zip_record = next(x for x in manifest["derived_artifacts"]
                  if x["file"] == "engine-source.zip")
payload = (root / "engine-source.zip").read_bytes()
assert hashlib.sha256(payload).hexdigest() == zip_record["sha256"]
expected = json.loads((root / "engine-code-hashes.json").read_text())["code"]
with ZipFile(root / "engine-source.zip") as snapshot:
    assert len(snapshot.infolist()) == len(expected) == 7
    assert snapshot.namelist() == sorted(expected)
    for item in snapshot.infolist():
        assert Path(item.filename).name == item.filename
        assert item.filename.endswith(".py") and not item.is_dir()
        assert (item.external_attr >> 16) == 0o100644
        assert hashlib.sha256(snapshot.read(item)).hexdigest() == expected[item.filename]
    snapshot.extractall(root / "engine")
```

The reviewed runtime recorded Python 3.12.14, NumPy 2.4.4, pandas 3.0.2 and
PyArrow 24.0.0. Use an isolated Python environment with those dependencies, then
run from the same research root:

```bash
python -m unittest discover -s engine -p 'selftest_*.py' -v
```

This exercises synthetic prices only. The frozen review record reports 20
passing synthetic tests; the same 20 tests also pass from a clean extraction of
this source ZIP with the recorded dependencies. A synthetic pass is not a market
result.

## Private market replay

Copy `coverage/dataset-plan.json` unchanged under `bulk/`. Supply its matching
coverage manifest and the canonical files referenced by `canonical_path`; file
SHA256 values must agree with the manifest. The exact originals, documented
point-in-time limitations, and authorized data scope are required. The package
does not fetch data or provide protected study labels. If paths must be moved,
use a separately recorded relocation manifest retaining the original evidence
and canonical hashes; do not rewrite an original seal or claim byte-identical
metadata reproduction.

Seal metadata before any price-label read into a new, previously unused output
directory, inspect the seal, then run that sealed development phase:

```bash
python engine/run_pilot.py --seal-only --phase development --manifest bulk/development-coverage-manifest.json --output runs/development-replay
python engine/run_pilot.py --run --output runs/development-replay
```

The chronological phase uses `--phase chronological_evaluation`, its matching
manifest and a different output directory. Existing outcome directories must not
be overwritten. New timestamps and filesystem paths give a new run-seal hash;
the source/document/input identities and numerical results are what can be
compared. Repeating a run does not create fresh evidence or erase prior exposure.

## Separate S1 source snapshot

S1's four-file ZIP and code manifest are under `supplement-s1/`. Verify them
against that directory's archive manifest and the `code` mapping in
`supplement-code-hashes.json`, using the same safe basename/hash checks as above.
In the private research-root copy, the complete frozen layout is:

```text
pilot-*                         original v2 documents in the root
engine-code-hashes.json          original v2 source hashes
engine/                         seven files from engine-source.zip
supplement-proposal.md           copy from supplement-s1/
supplement-proposal.json         copy from supplement-s1/
supplement-registry-s1.json       copy from supplement-s1/
supplement-freeze-s1.json         copy from supplement-s1/
supplement-code-hashes.json      copy from supplement-s1/
supplement_engine/              four files from supplement-engine-source.zip
bulk/dataset-plan.json           copy from coverage/; metadata, no prices
```

After verifying and extracting the S1 source ZIP under `supplement_engine/`,
and placing those exact documents, run synthetic checks from the research root:

```bash
python -m unittest discover -s supplement_engine -p 'selftest_supplement_*.py' -q
```

The final S1 review records 13 passing checks with the recorded repository
dependencies. All 13 also pass from a clean extraction of the public source
archives and metadata plan; the S1 archive manifest records that verification.
A market replay additionally requires complete private input
manifests/canonical files and both original v2 run directories, including their
daily proxy matrices, per-trial JSON, reports and seals. Those matrices are not
included here. With those originals available, the distinct S1 commands are:

```bash
python supplement_engine/run_supplement.py --seal-only --phase all --manifest bulk/coverage-manifest.json --v2-run runs/development-replay --v2-run runs/evaluation-replay --output runs/s1-replay
python supplement_engine/run_supplement.py --run --output runs/s1-replay
```

Preserve all original run directories. Any path-relocation manifest or replay
seal is a new artifact; it cannot replace the original seals or make previously
observed outcomes fresh.
