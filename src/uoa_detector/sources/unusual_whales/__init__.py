"""Unusual Whales source adapter.

Phase 3.3.3 added the production HTTP client, live WebSocket source,
and six provider implementations (dealer gamma, catalyst calendar,
IV history, sector map, peer flow, dark pool, open interest).

Phase 3.3.6 removed the Phase 2 stubs (``UnusualWhalesConfig``,
``UnusualWhalesFlowSource``) that previously lived in
``legacy_stub.py``. They were back-compat shims preserved through
Phase 3.3.3-3.3.5 while the real implementations stabilised. The
real ``UnusualWhalesLiveSource`` (in ``live.py``) is now the only
flow source; configuration flows through
``uoa_detector.calibration.profile.UnusualWhalesSettings``.

Public surface:
  - ``UnusualWhalesClient`` (Phase 3.3.3.2): authenticated HTTP client
  - ``UnusualWhalesAuthError`` / ``UnusualWhalesRateLimitError`` /
    ``UnusualWhalesTransientError``
  - ``UnusualWhalesError`` (base)
  - ``DEFAULT_BASE_URL``

For the live source, providers, and live observer wiring see the
sibling modules (``live``, ``providers``, ``client``).
"""

from uoa_detector.sources.unusual_whales.client import (
    DEFAULT_BASE_URL,
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "UnusualWhalesAuthError",
    "UnusualWhalesClient",
    "UnusualWhalesError",
    "UnusualWhalesRateLimitError",
    "UnusualWhalesTransientError",
]
