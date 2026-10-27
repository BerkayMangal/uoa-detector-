# Phase 5.34.1 — release cache and monitor status

Frozen before code, 2026-10-27. Covered by the owner's continuing instruction to
complete and deploy the desk. No policy, artifact or research threshold changes.

The new calibration report adds a generated CSS utility, while public CSS has a
one-day browser cache. Advance the stylesheet URL version so an already-open desk
loads the matching stylesheet after navigation. Also expose only the calibration
monitor's safe status string in the existing public live-cycle summary, allowing
release verification without production Basic credentials or trade data.

Keep private routes authenticated, report hashes valid and all existing tests.
Run the full gate, green PR/merge CI and verify live SHA plus safe desk/calibration
status. No extra vendor calls, broker action or frozen report rewrite.
