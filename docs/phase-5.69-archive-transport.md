# Phase 5.69 — lossless archive transport record

Recorded 12 November 2026 after the completed v2/S1 computations. This describes
delivery packaging only. Frozen protocols, registries, source files, rules and
original result bytes are unchanged. It does not revise the original acceptance
contract or create a new pre-outcome claim.

Large files are transported with gzip or XZ so the complete review payload fits
below 2 MB while retaining every field and failed arm. Per-directory
`archive-manifest.json` files record the delivered filename/size/SHA256 and,
where compressed, `original_file`, `original_bytes` and `original_sha256`.
Decompression is verified against the exact original bytes; there is no JSON
minification or selected-row subset in the full result archives.

Paths below are relative to `artifacts/research150-us/`:

| Original logical file | Delivered transport |
|---|---|
| `inventory.json` | `inventory.json.gz` |
| `supplement-s1/supplement-registry-s1.json` | `supplement-s1/supplement-registry-s1.json.gz` |
| `coverage/missingness-report.json` | `coverage/missingness-report.json.gz` |
| `coverage/coverage-verification.json` | `coverage/coverage-verification.json.gz` |
| `development/per-trial-results.json` | `development/per-trial-results.json.gz` |
| `evaluation/per-trial-results.json` | `evaluation/per-trial-results.json.gz` |
| `supplement-s1/per-policy-results.json` | `supplement-s1/per-policy-results.json.xz` |
| `supplement-s1/paired-contrasts.json` | `supplement-s1/all-126-paired-contrasts-all-costs-periods.json.gz` |
| `evidence/final-all-policy-checks.csv` | `evidence/final-all-policy-checks.csv.gz` |

`pilot-registry-v2.json` stays uncompressed at its original path, preserving the
frozen acceptance document's direct link. The S1 producer's original
`public-results-manifest.json` also describes an alternative gzip for the full
648-row JSON; this package ships the smaller XZ representation of the same
8,041,309 original bytes. Its original SHA256 remains
`038119d54418885bcaa11b976ad9694549d26ae9dc00f8449ca96df02f739bb8`.
The local `archive-manifest.json` describes the actual delivered transport.

From a **private copy** of the artifact directory, the following Python
standard-library procedure verifies and restores all logical originals without
overwriting different existing files:

```python
import gzip
import hashlib
import json
import lzma
from pathlib import Path

root = Path.cwd()
for manifest_path in sorted(root.rglob("archive-manifest.json")):
    manifest = json.loads(manifest_path.read_text())
    for record in manifest["files"] + manifest.get("derived_artifacts", []):
        delivered = manifest_path.parent / record["file"]
        payload = delivered.read_bytes()
        assert len(payload) == record["bytes"]
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        if "original_file" not in record:
            continue
        name = record["original_file"]
        assert Path(name).name == name
        if delivered.suffix == ".gz":
            raw = gzip.decompress(payload)
        elif delivered.suffix == ".xz":
            raw = lzma.decompress(payload)
        else:
            raise ValueError("Unknown archive transport")
        assert len(raw) == record["original_bytes"]
        assert hashlib.sha256(raw).hexdigest() == record["original_sha256"]
        destination = manifest_path.parent / name
        if destination.exists():
            assert destination.read_bytes() == raw
        else:
            destination.write_bytes(raw)
```

The [artifact README](../artifacts/research150-us/README.md) then describes safe
source-ZIP extraction, the exact combined directory layout, synthetic checks and
private market replay prerequisites. Raw market prices, provider packets, news
articles and credentials are not supplied. Compression creates no new evidence
or independent trials and does not make previously observed outcomes fresh.

The archive-local `.gitattributes` preserves original CSV bytes (`-text`) and
recognizes their standard CRLF record endings during Git whitespace checks.
It does not normalize the files or change their sealed digests; other trailing
whitespace checks remain enabled. Staged Git blob bytes are checked against
the working-file hashes before delivery.
