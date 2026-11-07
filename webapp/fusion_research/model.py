"""Immutable multimodal rows and explicit receipt/maturity boundaries."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import product
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

Direction = Literal["up", "down"]
Modality = Literal["technical", "flow", "news", "fundamental", "market"]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, default=str, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware clock required")
    return value.astimezone(UTC)


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Observation(Frozen):
    name: str
    value: float | bool | str | None = None
    known_at: datetime | None = None
    source: str = ""
    source_hash: str = ""
    missing: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> Self:
        if not self.name:
            raise ValueError("feature name required")
        if self.known_at is not None:
            aware(self.known_at)
        if self.value is None:
            if not self.missing:
                raise ValueError("unknown value requires a reason")
        elif self.known_at is None or not self.source or not self.source_hash or self.missing:
            raise ValueError("known feature requires clock and provenance without missingness")
        return self


class FeatureRow(Frozen):
    event_id: str
    ticker: str
    session: date
    event_at: datetime
    known_at: datetime
    technical: tuple[Observation, ...] = ()
    flow: tuple[Observation, ...] = ()
    news: tuple[Observation, ...] = ()
    fundamental: tuple[Observation, ...] = ()
    market: tuple[Observation, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> Self:
        if not self.event_id or not self.ticker or aware(self.event_at) > aware(self.known_at):
            raise ValueError("event identity/chronology invalid")
        for modality in ("technical", "flow", "news", "fundamental", "market"):
            observations: tuple[Observation, ...] = getattr(self, modality)
            if len({o.name for o in observations}) != len(observations):
                raise ValueError("duplicate feature name")
            if any(
                o.known_at is not None and aware(o.known_at) > aware(self.known_at)
                for o in observations
            ):
                raise ValueError("row cannot precede a supplied feature receipt")
        return self

    def usable(
        self, at: datetime, required: tuple[tuple[Modality, str], ...]
    ) -> tuple[bool, tuple[str, ...]]:
        moment = aware(at)
        if aware(self.known_at) > moment or aware(self.event_at) > moment:
            return False, ("event_not_yet_known",)
        reasons = []
        for modality, name in required:
            values: tuple[Observation, ...] = getattr(self, modality)
            item = next((o for o in values if o.name == name), None)
            if item is None or item.value is None or item.known_at is None:
                reasons.append(f"{modality}.{name}:unknown")
            elif aware(item.known_at) > moment:
                reasons.append(f"{modality}.{name}:future_receipt")
        return not reasons, tuple(reasons)


class OutcomeRow(Frozen):
    event_id: str
    ticker: str
    session: date
    method: str
    instrument: str
    horizon: str
    state: Literal["observed", "missing", "not_applicable"]
    entry_at: datetime | None = None
    exit_at: datetime | None = None
    entry_known_at: datetime | None = None
    exit_known_at: datetime | None = None
    mature_at: datetime | None = None
    known_at: datetime | None = None
    net_usd: float | None = None
    excess_usd: float | None = None
    missing: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> Self:
        clocks = (
            self.entry_at,
            self.exit_at,
            self.entry_known_at,
            self.exit_known_at,
            self.mature_at,
            self.known_at,
        )
        for value in clocks:
            if value is not None:
                aware(value)
        if self.state == "observed":
            if (
                any(v is None for v in clocks)
                or self.net_usd is None
                or self.excess_usd is None
                or self.missing
            ):
                raise ValueError("observed label requires complete prices/receipts")
            assert (
                self.entry_at
                and self.exit_at
                and self.entry_known_at
                and self.exit_known_at
                and self.mature_at
                and self.known_at
            )
            if not (
                self.entry_at <= self.entry_known_at <= self.known_at
                and self.entry_at <= self.exit_at <= self.exit_known_at <= self.known_at
                and self.exit_at <= self.mature_at <= self.known_at
            ):
                raise ValueError("label chronology invalid")
        elif self.net_usd is not None or self.excess_usd is not None or not self.missing:
            raise ValueError("unknown label cannot manufacture a return")
        return self

    def available(self, at: datetime) -> bool:
        return bool(
            self.state == "observed"
            and self.known_at
            and self.mature_at
            and max(aware(self.known_at), aware(self.mature_at)) <= aware(at)
        )


@dataclass(frozen=True)
class Candidate:
    family: str
    lookback: int
    threshold: float
    volume: float
    regime: str
    direction: Direction
    horizon: int

    @property
    def key(self) -> str:
        return digest(
            (
                self.family,
                self.lookback,
                self.threshold,
                self.volume,
                self.regime,
                self.direction,
                self.horizon,
            )
        )[:20]

    @property
    def mask_key(self) -> tuple[str, int, float, float, str, str]:
        return self.family, self.lookback, self.threshold, self.volume, self.regime, self.direction


def grid() -> tuple[Candidate, ...]:
    from webapp.fusion_research.settings import settings

    cfg = settings()
    return tuple(
        Candidate(family, *args)
        for family, thresholds in cfg.families.items()
        for args in product(
            cfg.lookbacks, thresholds, cfg.volume_gates, cfg.regimes, cfg.directions, cfg.horizons
        )
    )


@dataclass(frozen=True)
class TickerEstimate:
    ticker: str
    mode: Literal["GLOBAL_FALLBACK", "SHRUNK_TICKER"]
    train_units: int
    validation_units: int
    train_days: int
    validation_days: int
    weight: float
    training_win_fraction: float | None
    training_mean_net_usd: float | None
    calibrated_success_probability: None = None


def ticker_estimates(
    train: tuple[OutcomeRow, ...], validation: tuple[OutcomeRow, ...], asof: datetime
) -> tuple[TickerEstimate, ...]:
    from webapp.fusion_research.settings import settings

    cfg = settings()
    training = [r for r in train if r.available(asof)]
    valid = [r for r in validation if r.available(asof)]
    keys = [r.event_id for r in training + valid]
    if len(set(keys)) != len(keys):
        raise ValueError("training/validation labels overlap or duplicate")
    panels = {(r.method, r.instrument, r.horizon) for r in training + valid}
    if len(panels) > 1:
        raise ValueError("different label methods/instruments/horizons cannot be pooled")
    if training and valid and max(r.session for r in training) >= min(r.session for r in valid):
        raise ValueError("validation must follow training in whole-date order")
    global_p = sum((r.net_usd or 0) > 0 for r in training) / len(training) if training else None
    global_ev = sum(r.net_usd or 0 for r in training) / len(training) if training else None
    out = []
    for ticker in sorted({r.ticker for r in train + validation}):
        tr, va = (
            [r for r in training if r.ticker == ticker],
            [r for r in valid if r.ticker == ticker],
        )
        td, vd = len({r.session for r in tr}), len({r.session for r in va})
        supported = (
            len(tr) >= cfg.minimum_train_units
            and len(va) >= cfg.minimum_validation_units
            and td >= cfg.minimum_train_days
            and vd >= cfg.minimum_validation_days
        )
        w = len(tr) / (len(tr) + cfg.ticker_shrinkage_units) if supported else 0.0
        p = (
            w * sum((r.net_usd or 0) > 0 for r in tr) / len(tr) + (1 - w) * global_p
            if tr and global_p is not None
            else global_p
        )
        ev = (
            w * sum(r.net_usd or 0 for r in tr) / len(tr) + (1 - w) * global_ev
            if tr and global_ev is not None
            else global_ev
        )
        if p is not None and not math.isfinite(p):
            raise ValueError("nonfinite training estimate")
        out.append(
            TickerEstimate(
                ticker,
                "SHRUNK_TICKER" if supported else "GLOBAL_FALLBACK",
                len(tr),
                len(va),
                td,
                vd,
                w,
                p,
                ev,
            )
        )
    return tuple(out)
