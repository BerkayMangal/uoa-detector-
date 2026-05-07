"""Historical bulk-download orchestration.

Phase 3.3.4: ties together the per-contract
``ThetaDataHistoricalDownloader`` (Phase 3.3.2.4) with universe
selection, state tracking, validation, and manifest production.

The intent is that ``scripts/download_tier2.py`` is a thin CLI
wrapper; all the testable logic lives here.

Modules (rolled in across sub-commits):
  - universe.py     — CSV reader for tier1/tier2 universes
  - state.py        — ``.download_state.json`` atomic I/O + resume
  - orchestrator.py — drives the downloader with state + concurrency
  - validation.py   — per-ticker counts, schema check, gap detection
  - manifest.py     — ``.manifest.json`` summary generator
"""
