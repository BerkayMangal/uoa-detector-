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
        # First load must succeed — there's no prior state to retain.
        # reload() returns False on failure rather than raising so subsequent
        # reloads never crash the pipeline; the bootstrap path must explicitly
        # convert that into a hard error.
        if not self.reload():
            msg = (
                f"failed to load default profile {default_profile_filename!r} "
                f"from {self._profiles_dir}"
            )
            raise ConfigurationError(msg)

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

    def reload(self) -> bool:
        """Re-scan the profiles directory and rebuild the cache.

        Transactional: builds the new cache into local variables first, then
        swaps in atomically under the lock. If any error occurs (default
        missing or malformed, or any non-default profile fails fatally — see
        below), the swap is skipped and the previously-loaded state is
        retained intact. The pipeline continues processing on the old
        snapshot without disruption.

        Returns ``True`` if the swap occurred, ``False`` if the existing
        state was retained.

        Per-profile error handling:
          - **Default profile** failure (missing, unreadable, malformed,
            validator rejection) → reload aborts, returns ``False``, logs
            ``calibration_resolver_reload_aborted``.
          - **Ticker/regime** profile failure → that single profile is
            dropped from the new cache; reload continues with the rest.
            Logged as ``calibration_profile_load_failed``.
        """
        default_path = self._profiles_dir / self._default_filename
        try:
            new_default = load_profile(default_path, profiles_dir=self._profiles_dir)
        except Exception as e:
            # YAML libs raise their own exception subclasses
            # (ruamel.yaml.scanner.ScannerError, parser errors, etc.) that
            # don't inherit from our ConfigurationError. The spec says:
            # malformed YAML → log + continue; preserve old state. So we catch
            # broad here and let the failure logging cover the diagnosis.
            _logger.warning(
                "calibration_resolver_reload_aborted",
                reason=str(e),
                default_path=str(default_path),
            )
            return False

        new_tickers = self._load_dir(self._profiles_dir / "tickers", upper=True)
        new_regimes = self._load_dir(self._profiles_dir / "regimes", upper=False)

        with self._lock:
            old_default_hash = (
                self._default.content_hash() if hasattr(self, "_default") else None
            )
            self._default = new_default
            self._tickers = new_tickers
            self._regimes = new_regimes

        # Only log "changed" if the default's content actually shifted.
        # Identical-reload (same content) is silent so dashboards / log-
        # parsers don't see spurious churn from periodic reloads.
        if old_default_hash != new_default.content_hash():
            _logger.info(
                "calibration_resolver_reloaded",
                default_profile=new_default.profile_id,
                ticker_count=len(new_tickers),
                regime_count=len(new_regimes),
            )
        return True

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
