"""``CalibrationResolver`` — resolve the active profile per (ticker, regime).

Resolution order (default implementation):
  1. ``profiles/tickers/<TICKER>.yaml`` if present
  2. ``profiles/regimes/<regime>.yaml`` if regime tag passed and the file exists
  3. ``profiles/v5_default.yaml`` fallback (always loaded)

Hot-swap: ``reload()`` re-reads every YAML in ``profiles_dir``. The pipeline
finishes the in-flight event on the old profile snapshot; the next ``resolve``
call sees the new one. Lock-protected so concurrent stages don't observe a
half-rebuilt cache.
"""

from __future__ import annotations

import threading
from pathlib import Path

import structlog

from uoa_detector.calibration.loader import load_profile
from uoa_detector.calibration.profile import CalibrationProfile
from uoa_detector.errors import ConfigurationError

_logger = structlog.get_logger(__name__)


class CalibrationResolver:
    """Holds the loaded profile cache and resolves which profile applies."""

    def __init__(
        self,
        profiles_dir: Path | str = "profiles",
        *,
        default_profile_filename: str = "v5_default.yaml",
    ) -> None:
        self._profiles_dir = Path(profiles_dir).expanduser().resolve()
        self._default_filename = default_profile_filename
        self._lock = threading.RLock()
        self._default: CalibrationProfile
        self._tickers: dict[str, CalibrationProfile] = {}
        self._regimes: dict[str, CalibrationProfile] = {}
        self.reload()

    @property
    def profiles_dir(self) -> Path:
        """The directory this resolver scans on ``reload()``."""
        return self._profiles_dir

    def default(self) -> CalibrationProfile:
        """Return the default (v5) profile."""
        with self._lock:
            return self._default

    def resolve(
        self,
        ticker: str | None = None,
        regime: str | None = None,
    ) -> CalibrationProfile:
        """Return the active profile for ``(ticker, regime)``.

        Ticker overrides regime; regime overrides default. ``None`` for both
        returns the default.
        """
        with self._lock:
            if ticker is not None:
                key = ticker.strip().upper()
                if key in self._tickers:
                    return self._tickers[key]
            if regime is not None:
                rkey = regime.strip().lower()
                if rkey in self._regimes:
                    return self._regimes[rkey]
            return self._default

    def reload(self) -> None:
        """Re-scan the profiles directory and rebuild the cache.

        The default profile is required; ticker/regime profiles are optional.
        Errors loading a non-default profile log a warning but do not abort
        the reload — the existing entry for that profile is dropped.
        """
        with self._lock:
            default_path = self._profiles_dir / self._default_filename
            if not default_path.exists():
                msg = f"default profile not found at {default_path}"
                raise ConfigurationError(msg)
            self._default = load_profile(default_path, profiles_dir=self._profiles_dir)

            self._tickers = self._load_dir(self._profiles_dir / "tickers", upper=True)
            self._regimes = self._load_dir(self._profiles_dir / "regimes", upper=False)

            _logger.info(
                "calibration_resolver_reloaded",
                default_profile=self._default.profile_id,
                ticker_count=len(self._tickers),
                regime_count=len(self._regimes),
            )

    def _load_dir(
        self,
        directory: Path,
        *,
        upper: bool,
    ) -> dict[str, CalibrationProfile]:
        """Load every ``*.yaml`` in ``directory`` keyed by stem (uppercased for tickers)."""
        out: dict[str, CalibrationProfile] = {}
        if not directory.exists():
            return out
        for path in sorted(directory.glob("*.yaml")):
            stem = path.stem
            key = stem.upper() if upper else stem.lower()
            try:
                out[key] = load_profile(path, profiles_dir=self._profiles_dir)
            except ConfigurationError as e:
                _logger.warning(
                    "calibration_profile_load_failed",
                    path=str(path),
                    error=str(e),
                )
        return out
