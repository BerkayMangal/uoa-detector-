"""Typed reader for ``profiles/options_alpha_v1.yaml``.

D8: no numeric threshold may live in code. Every cutoff the option candidate,
construction, cost and risk path uses is loaded through here, so a number can
only change by editing the profile — a visible, reviewable, hashable event.

The hash travels with the settings on purpose. Section 10 of the task requires
every result to record the profile that produced it; carrying
:attr:`OptionsAlphaSettings.profile_sha256` means a result can never be quietly
re-attributed to a different set of numbers than the one that generated it.

Strict by construction: unknown keys are rejected rather than ignored, because a
typo in a threshold name that silently falls back to a default is exactly the
failure this project has paid for before.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

# One parser instance, the way `calibration/loader.py` already does it.
_YAML = YAML(typ="safe")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QualitySettings(_Strict):
    """Which evidence tier this data can support — see the task's section 7."""

    tier: Literal["A", "B", "C"]
    tier_reason: str
    research_window_start: str


class UniverseSettings(_Strict):
    tickers: tuple[str, ...]
    use_unusual_preset: bool


class ContractSettings(_Strict):
    min_dte: int = Field(ge=0)
    max_dte: int = Field(ge=0)
    min_abs_delta: float = Field(ge=0.0, le=1.0)
    max_abs_delta: float = Field(ge=0.0, le=1.0)
    min_open_interest: int = Field(ge=0)
    min_volume: int = Field(ge=0)
    max_spread_pct_of_mid: float = Field(gt=0.0)
    min_nbbo_bid: float = Field(ge=0.0)
    require_standard_multiplier: bool
    standard_multiplier: int = Field(gt=0)


class DebitSpreadSettings(_Strict):
    width_strikes: int = Field(ge=1)
    min_debit_reduction_pct: float = Field(ge=0.0)
    require_same_expiry: bool
    require_same_session: bool


class StructureSettings(_Strict):
    enabled: tuple[str, ...]
    debit_spread: DebitSpreadSettings


class CostSettings(_Strict):
    entry_long_side: Literal["ask", "bid"]
    exit_long_side: Literal["ask", "bid"]
    entry_short_side: Literal["ask", "bid"]
    exit_short_side: Literal["ask", "bid"]
    commission_per_contract_per_leg_usd: float = Field(ge=0.0)
    commission_is_simulation_default: bool
    extra_slippage_pct: float = Field(ge=0.0)


class RiskSettings(_Strict):
    capital_usd: float = Field(gt=0.0)
    r_usd: float = Field(gt=0.0)
    is_simulation_account: bool
    allow_fractional: bool
    max_structures_per_signal: int = Field(ge=1)
    max_open_premium_pct_of_capital: float = Field(gt=0.0)
    max_positions_per_ticker: int = Field(ge=1)
    max_open_positions: int = Field(ge=1)


class ExitVariant(_Strict):
    id: str
    description: str


class ExitSettings(_Strict):
    primary_hold_trading_days: int = Field(ge=1)
    secondary_hold_trading_days: int = Field(ge=1)
    close_at_dte: int = Field(ge=0)
    variants: tuple[ExitVariant, ...]
    stop_pct_of_entry_debit: float = Field(gt=0.0)
    target_pct_of_entry_debit: float = Field(gt=0.0)
    tracks: Literal["structure_exit_value", "underlying"]


class FreshnessSettings(_Strict):
    max_quote_age_seconds: int = Field(ge=0)
    replay_cards_are_labelled: bool


class OptionsAlphaSettings(_Strict):
    """The whole numeric surface of phase 5.24, plus the hash that identifies it."""

    version: int
    scope: str
    quality: QualitySettings
    universe: UniverseSettings
    contract: ContractSettings
    structures: StructureSettings
    costs: CostSettings
    risk: RiskSettings
    exit: ExitSettings
    freshness: FreshnessSettings
    profile_sha256: str

    def model_post_init(self, _context: object) -> None:
        # Ranges that are individually valid but jointly nonsense are worth
        # refusing at load time rather than producing an empty candidate list
        # that looks like "no opportunities today".
        if self.contract.min_dte > self.contract.max_dte:
            raise ValueError("contract.min_dte cannot exceed contract.max_dte")
        if self.contract.min_abs_delta > self.contract.max_abs_delta:
            raise ValueError("contract.min_abs_delta cannot exceed contract.max_abs_delta")


DEFAULT_PROFILE = pathlib.Path("profiles/options_alpha_v1.yaml")


def load_settings(path: pathlib.Path | str = DEFAULT_PROFILE) -> OptionsAlphaSettings:
    """Parse the profile and stamp it with the sha256 of the bytes that were read.

    The hash is taken over the raw file, not the parsed object, so it changes
    with a comment edit too. That is deliberate: the comments in this profile
    carry the reasoning for each threshold, and a result that cites a hash should
    cite the exact text a reader would open.
    """
    file_path = pathlib.Path(path)
    raw = file_path.read_bytes()
    # ruamel, not PyYAML: it is already a declared dependency and already has the
    # mypy override this repo configured for it. Adding a second YAML library for
    # one file would change uv.lock for no gain.
    data: Any = YAML(typ="safe").load(io.BytesIO(raw))
    if not isinstance(data, dict):
        raise ValueError(f"{file_path}: profil bir sozluk degil")
    return OptionsAlphaSettings(**data, profile_sha256=hashlib.sha256(raw).hexdigest())
