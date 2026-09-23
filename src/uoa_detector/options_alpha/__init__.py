"""Options Alpha v1 (phase 5.24) — option-first candidate and PAPER signal engine.

Pure, deterministic modules: given the same quotes and the same profile they
return the same decision, so the research path and the live path can share one
implementation instead of drifting into two.

Nothing here sends an order. The scope is PAPER only.
"""
