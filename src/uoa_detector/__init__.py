"""UOA + Convexity Detector v5 — discretionary options-flow screening + context tool.

Decision support, not a mechanical alpha engine: it surfaces unusual options
flow and structural context for a human's discretionary read, and never
auto-trades. The vol-premium edge was validated through a full cost / tail /
multiple-testing / walk-forward gate and rejected as untradeable
(see ``docs/edge_to_money.md``); the conditioning is kept only as context.
"""

from uoa_detector._version import __version__

__all__ = ["__version__"]
