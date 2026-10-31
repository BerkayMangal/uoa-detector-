"""Frozen research inputs, with explicit source and receipt times."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Settings(Frozen):
    version: str
    bucket_minutes: int
    opening_delay_minutes: int
    flow_window_seconds: int
    poll_gap_seconds: int
    normalization_sessions: int
    minimum_history_sessions: int
    price_acceptance_minutes: int
    price_max_age_seconds: int
    mark_tolerance_minutes: int
    max_units_per_day: int
    ratios: tuple[float, ...]
    dominance: tuple[float, ...]
    percentiles: tuple[float, ...]
    entry_delay_minutes: tuple[int, ...]
    holding_minutes: tuple[int, ...]
    arms: tuple[str, ...]
    slippage_bps: tuple[int, ...]
    minimum_sessions: int
    development_sessions: int
    training_sessions: tuple[int, ...]
    validation_sessions: int
    embargo_sessions: int
    minimum_train_units: int
    minimum_train_days: int
    minimum_validation_units: int
    minimum_validation_days: int
    shrinkage_units: int
    bootstrap_draws: int
    bootstrap_block_days: int
    bootstrap_seed: int
    risk_budget_usd: float
    outlay_cap_usd: float
    commission_per_contract_side: float
    quote_max_age_seconds: int
    row_limit: int
    probe_max_requests: int
    probe_timeout_seconds: int
    probe_quota_reserve: int


@cache
def settings() -> Settings:
    return Settings(
        **YAML(typ="safe").load((ROOT / "profiles/event_calibration_v1.yaml").read_text())
    )


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def identity() -> dict[str, str]:
    files = sorted(Path(__file__).parent.glob("*.py"))
    dependencies = [
        ROOT / p
        for p in (
            "webapp/alpha_lab/flow.py",
            "webapp/alpha_lab/settings.py",
            "profiles/alpha_lab_v1.yaml",
            "src/uoa_detector/live_alpha/calendar.py",
            "profiles/live_alpha_v1.yaml",
        )
    ]
    return {
        "profile_hash": hashlib.sha256(
            (ROOT / "profiles/event_calibration_v1.yaml").read_bytes()
        ).hexdigest(),
        "code_hash": hashlib.sha256(
            b"".join(p.name.encode() + p.read_bytes() for p in [*files, *dependencies])
        ).hexdigest(),
    }


class Timed(Frozen):
    at: datetime
    known_at: datetime

    @model_validator(mode="after")
    def chronology(self) -> Self:
        if self.at.tzinfo is None or self.known_at.tzinfo is None or self.at > self.known_at:
            raise ValueError("source and receipt must be aware and source <= receipt")
        return self


class Price(Timed):
    ticker: str
    value: float = Field(gt=0)


class Context(Timed):
    value: float


class Leg(Frozen):
    symbol: str
    strike: float = Field(gt=0)
    expiry: date
    right: Literal["call"]
    is_long: bool
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    fetched_at: datetime

    @model_validator(mode="after")
    def quote(self) -> Self:
        if self.ask < self.bid or self.fetched_at.tzinfo is None:
            raise ValueError("uncrossed, aware quotes required")
        return self


class Structure(Frozen):
    kind: Literal["long_call", "bull_call_debit"]
    legs: tuple[Leg, ...]
    multiplier: int = Field(gt=0)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.kind == "long_call":
            if len(self.legs) != 1 or not self.legs[0].is_long:
                raise ValueError("long call requires one long leg")
        elif (
            len(self.legs) != 2
            or not self.legs[0].is_long
            or self.legs[1].is_long
            or self.legs[0].strike >= self.legs[1].strike
        ):
            raise ValueError("call debit requires long lower and short higher strike")
        if len({leg.expiry for leg in self.legs}) != 1:
            raise ValueError("spread expiries differ")
        if len({leg.symbol for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate option leg")
        return self


class Decision(Frozen):
    key: str
    ticker: str
    at: datetime
    baseline: bool
    usable: bool = True
    source_reason: str = ""
    risk_fraction: float | None = Field(default=None, gt=0, lt=1)
    news: bool | None = None
    market: Context | None = None
    sector: Context | None = None
    gamma: Context | None = None
    options: tuple[Structure, ...] = ()

    @model_validator(mode="after")
    def receipt(self) -> Self:
        if self.at.tzinfo is None:
            raise ValueError("aware publication required")
        if any(c and c.known_at > self.at for c in (self.market, self.sector, self.gamma)):
            raise ValueError("context was unavailable at decision")
        if any(leg.fetched_at > self.at for structure in self.options for leg in structure.legs):
            raise ValueError("future quote")
        return self


class Alert(Frozen):
    ticker: str
    key: str
    known_at: datetime
    payload: dict[str, object]


class Poll(Frozen):
    ticker: str
    at: datetime
    saturated: bool


class Dataset(Frozen):
    version: Literal["observed_events_v1"] = "observed_events_v1"
    origin: str
    asof: datetime
    sessions: tuple[date, ...]
    decisions: tuple[Decision, ...] = ()
    prices: tuple[Price, ...] = ()
    alerts: tuple[Alert, ...] = ()
    polls: tuple[Poll, ...] = ()
    provenance: dict[str, str] = {}

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.asof.tzinfo is None or tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("aware asof and unique ordered sessions required")
        if len({d.key for d in self.decisions}) != len(self.decisions):
            raise ValueError("duplicate decision identity")
        if len({(p.ticker, p.at) for p in self.prices}) != len(self.prices):
            raise ValueError("conflicting/duplicate source price identity")
        for collection in (self.decisions, self.prices, self.alerts, self.polls):
            if len(collection) > settings().row_limit:
                raise ValueError("input bound exceeded")
            for row in collection:
                at = row.known_at if isinstance(row, Price | Alert) else row.at
                if at.tzinfo is None or at > self.asof:
                    raise ValueError("future or naive receipt")
        return self


def utc(at: datetime) -> datetime:
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def load(path: Path) -> Dataset:
    resolved = path.resolve()
    if (ROOT / "data/study_g").resolve() in resolved.parents:
        raise ValueError("reserved study is not an event input")
    body = json.loads(resolved.read_text())
    expected = body.pop("report_hash", None)
    if expected != digest(body):
        raise ValueError("missing or mismatched dataset manifest hash")
    return Dataset.model_validate(body)
