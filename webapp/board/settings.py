"""Alfa Board settings (Phase 5.2, contract §4.3).

Loaded from ``profiles/board_v1.yaml`` into strict, frozen models. The file sits
outside ``CalibrationProfile`` on purpose: a new calibration section would change
the content hash of every calibration profile, including the burned ones that
trace recorded verdicts.

Percent-valued keys use percent units (``15.0`` means 15%), matching
``penalty_triggers.spread_pct_threshold``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

FamilyKey = Literal[
    "flow", "dealer_gamma", "dark_pool", "sector", "price_confirmation", "open_interest",
]

DEFAULT_BOARD_PROFILE = Path("profiles/board_v1.yaml")

_CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceSettings(_Strict):
    counted_families: tuple[FamilyKey, ...] = Field(min_length=1)
    strong_min_supporting: int = Field(ge=1)
    moderate_min_supporting: int = Field(ge=1)
    max_unknown_for_strong: int = Field(ge=0)
    flow_net_premium_deadband_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> EvidenceSettings:
        if len(set(self.counted_families)) != len(self.counted_families):
            msg = "evidence.counted_families must not repeat a family"
            raise ValueError(msg)
        if self.moderate_min_supporting > self.strong_min_supporting:
            msg = "evidence.moderate_min_supporting must not exceed strong_min_supporting"
            raise ValueError(msg)
        return self


class TradabilitySettings(_Strict):
    max_tradable_spread_pct: float = Field(gt=0)
    min_exit_bid_size: int = Field(ge=0)
    max_quote_age_seconds: int = Field(gt=0)


class CostSettings(_Strict):
    commission_per_contract_usd: float = Field(ge=0)


class SizingSettings(_Strict):
    capital_usd: float = Field(gt=0)
    r_usd: float = Field(gt=0)
    values_confirmed_by_owner: bool


class SpotSettings(_Strict):
    """Phase 5.3 §3.3: the spot frame's cutoffs. Owner decision O1 fixes the multiple."""

    atr_period: int = Field(ge=2)
    atr_stop_multiple: float = Field(gt=0)
    atr_min_sessions: int = Field(ge=2)
    max_position_pct_of_capital: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _enough_sessions_for_the_window(self) -> SpotSettings:
        if self.atr_min_sessions < self.atr_period:
            msg = "spot: atr_min_sessions must be at least atr_period"
            raise ValueError(msg)
        return self


class AggregationSettings(_Strict):
    intentional_min_top_strike_share_pct: float = Field(gt=0, le=100)
    scattered_max_top_strike_share_pct: float = Field(gt=0, le=100)
    min_strikes_for_scattered: int = Field(ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> AggregationSettings:
        if self.scattered_max_top_strike_share_pct >= self.intentional_min_top_strike_share_pct:
            msg = "aggregation: scattered_max must be below intentional_min"
            raise ValueError(msg)
        return self


class CleanCandidateSettings(_Strict):
    min_supporting: int = Field(ge=1)
    max_against: int = Field(ge=0)
    max_unknown: int = Field(ge=0)


class ChaseSettings(_Strict):
    reasonable_max_pct: float = Field(ge=0)
    late_min_pct: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> ChaseSettings:
        if self.reasonable_max_pct >= self.late_min_pct:
            msg = "chase: reasonable_max_pct must be below late_min_pct"
            raise ValueError(msg)
        return self


class RefreshSettings(_Strict):
    cadence_seconds: int = Field(ge=60)
    exit_depth_top_k: int = Field(ge=0)
    max_symbols_per_request: int = Field(ge=1)
    atm_expiries: int = Field(ge=1, le=5)
    daily_request_soft_cap: int = Field(gt=0)
    daily_job_max_attempts: int = Field(ge=1)  # Phase 5.2.B-fix1
    closed_market_sleep_seconds: int = Field(ge=60)
    daily_limit_backoff_seconds: int = Field(ge=60)


class RegimeSettings(_Strict):
    tide_deadband_usd: float = Field(ge=0)
    tide_persistence_buckets: int = Field(ge=1)
    gamma_deadband_usd: float = Field(ge=0)
    iv_anchor_short_dte: int = Field(ge=1)
    iv_anchor_long_dte: int = Field(ge=2)
    iv_curve_deadband_vol_pts: float = Field(ge=0)
    exclude_event_hump_max_dte: int = Field(ge=0)
    max_source_age_seconds: int = Field(gt=0)
    # The vol board's IV-richness cutoff, as a fraction of the 1y range. It used to be
    # a function default in webapp/vol_board.py while the sentence the page prints
    # spelled the same number again, so the two could drift apart (registry REG-3).
    vol_rich_iv_pct: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> RegimeSettings:
        if self.iv_anchor_short_dte >= self.iv_anchor_long_dte:
            msg = "regime: iv_anchor_short_dte must be below iv_anchor_long_dte"
            raise ValueError(msg)
        return self


class OpeningClosingSettings(_Strict):
    confirm_open_min_ratio: float = Field(gt=0)
    confirm_close_max_ratio: float = Field(lt=0)
    job_time_et: str = Field(pattern=_CLOCK)


class CatalystSettings(_Strict):
    macro_event_names: tuple[str, ...] = Field(min_length=1)
    macro_horizon_days: int = Field(ge=1)


class DelayedSettings(_Strict):
    congress_late_days: int = Field(ge=1)
    congress_lookback_days: int = Field(ge=1)
    insider_lookback_days: int = Field(ge=1)
    short_interest_lookback_days: int = Field(ge=1)  # Phase 5.2.D3a
    ftd_lookback_days: int = Field(ge=1)  # Phase 5.2.D3a
    max_items_per_family: int = Field(ge=1)  # Phase 5.2.D1: display cap per family
    job_time_et: str = Field(pattern=_CLOCK)


class PortfolioSettings(_Strict):
    focused_etfs: tuple[str, ...]
    cluster_min_member_weight_pct: float = Field(gt=0, le=100)
    max_focused_holdings: int = Field(ge=1)
    share_class_aliases: dict[str, str]


class FillsSettings(_Strict):
    min_n_for_stats: int = Field(ge=1)
    # Phase 5.2.C32-fix2: how many fills the card page's slippage summary reads.
    max_fills_per_summary: int = Field(ge=1)


class LedgerSettings(_Strict):
    """Phase 5.2.C2a: the /defter page cap (the ledger itself is never trimmed)."""

    max_cards_per_page: int = Field(ge=1)


class OutcomesSettings(_Strict):
    horizons_trading_days: tuple[int, ...] = Field(min_length=1)
    job_time_et: str = Field(pattern=_CLOCK)
    max_cards_per_run: int = Field(ge=1)  # Phase 5.2.C2b: the daily job's scan bound


class TapeSettings(_Strict):
    """Phase 5.2.A3: the net-premium tape behind the Akış family."""

    max_age_seconds: int = Field(gt=0)


class NarrativeSettings(_Strict):
    """Phase 5.2.A5: the reason sentence and the mandatory counter-argument."""

    counter_min_unknown_families: int = Field(ge=1)


class BoardSettings(_Strict):
    """Every Alfa Board cutoff, cadence and owner-set value."""

    board_profile_id: str = Field(min_length=1)
    evidence: EvidenceSettings
    tape: TapeSettings
    narrative: NarrativeSettings
    tradability: TradabilitySettings
    cost: CostSettings
    sizing: SizingSettings
    aggregation: AggregationSettings
    clean_candidate: CleanCandidateSettings
    chase: ChaseSettings
    refresh: RefreshSettings
    regime: RegimeSettings
    spot: SpotSettings
    opening_closing: OpeningClosingSettings
    catalyst: CatalystSettings
    delayed: DelayedSettings
    portfolio: PortfolioSettings
    fills: FillsSettings
    ledger: LedgerSettings
    outcomes: OutcomesSettings

    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON dump; stored on decision cards (FAZ C)."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_board_settings(path: Path = DEFAULT_BOARD_PROFILE) -> BoardSettings:
    """Load and validate the board profile. Raises on a missing file or invalid content."""
    raw = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"board profile {path} must be a mapping"
        raise ValueError(msg)
    return BoardSettings.model_validate(raw)
