"""Dealer-gamma map — structural CONTEXT for the screener (Phase 4.10).

NOT a directional edge. The historical study (scripts/study_gamma_regime.py)
rejected gamma as a mechanical directional signal — the apparent edge was a
single-name squeeze (LCID), first-half only, and insignificant once
overlapping windows were removed. What survived is the vol/structure read:
extreme-gamma names move more, and the flip / walls mark where dealer hedging
amplifies vs pins. So this surfaces gamma as a SpotGamma-style daily context
map (regime, flip, call/put walls) to inform the discretionary read — clearly
labelled as context, never as a buy signal.

GEX is computed from the full option chain (OI x Black-Scholes gamma), the data
ThetaData gives us. UW's gamma endpoints are not in our tier (HTTP 401), so the
map is refreshed by a snapshot job (scripts/gamma_snapshot.py) wherever the
ThetaData chain is available, and the cloud screener just reads the result.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sqlalchemy import Float, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from webapp.repo import _normalize_url

_NORM = 1.0 / math.sqrt(2.0 * math.pi)


class _Base(DeclarativeBase):
    pass


class GammaRow(_Base):
    __tablename__ = "gamma_regime"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    as_of: Mapped[str] = mapped_column(String)  # snapshot date (ISO)
    spot: Mapped[float] = mapped_column(Float)
    net_gex: Mapped[float] = mapped_column(Float)  # >0 long (suppress), <0 short (amplify)
    flip: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_wall: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_wall: Mapped[float | None] = mapped_column(Float, nullable=True)


@dataclass(frozen=True)
class GammaContext:
    ticker: str
    as_of: str
    spot: float
    net_gex: float
    flip: float | None
    call_wall: float | None
    put_wall: float | None

    @property
    def regime(self) -> str:
        return "short" if self.net_gex < 0 else "long"

    @property
    def regime_label(self) -> str:
        return "amplifies moves" if self.net_gex < 0 else "suppresses moves"


def _bs_gamma_vec(spot: float, k: np.ndarray, t: np.ndarray, iv: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_t = iv * np.sqrt(t)
        d1 = (np.log(spot / k) + 0.5 * iv * iv * t) / vol_t
        g = (_NORM * np.exp(-0.5 * d1 * d1)) / (spot * vol_t)
    return np.where(np.isfinite(g), g, 0.0)


def compute_gamma(chain: pd.DataFrame, *, as_of: str) -> dict[str, float | None] | None:
    """Compute net GEX, flip and call/put walls from one chain snapshot.

    ``chain`` columns: strike, expiry, option_type, open_interest,
    implied_volatility, spot. Convention: GEX(S) = call gamma*OI - put gamma*OI
    (>0 dealers long gamma, suppress). Flip = the hypothetical spot S nearest
    the current spot at which GEX(S) crosses zero. Walls = the strike carrying
    the most call / put dollar-gamma at the current spot.
    """
    df = chain.copy()
    for col in ("strike", "open_interest", "implied_volatility", "spot"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["t"] = (df["expiry"] - pd.Timestamp(as_of)).dt.days / 365.0
    df = df[(df["t"] > 0) & (df["implied_volatility"] > 0) & (df["open_interest"] > 0)]
    df = df[(df["strike"] > 0) & (df["spot"] > 0)].dropna(
        subset=["strike", "open_interest", "implied_volatility", "spot", "t"],
    )
    if df.empty:
        return None

    spot = float(df["spot"].iloc[0])
    k = df["strike"].to_numpy(dtype=float)
    t = df["t"].to_numpy(dtype=float)
    iv = df["implied_volatility"].to_numpy(dtype=float)
    oi = df["open_interest"].to_numpy(dtype=float)
    sign = np.where(df["option_type"].str.lower().str.startswith("c"), 1.0, -1.0)

    def gex_at(s: float) -> float:
        return float(np.sum(sign * oi * _bs_gamma_vec(s, k, t, iv)))

    net_gex = gex_at(spot)

    # Flip: scan hypothetical spot, take the zero-crossing nearest current spot.
    grid = np.linspace(spot * 0.6, spot * 1.4, 161)
    vals = np.array([gex_at(s) for s in grid])
    crossings = [
        float(grid[i] - vals[i] * (grid[i] - grid[i - 1]) / (vals[i] - vals[i - 1]))
        for i in range(1, len(vals))
        if vals[i - 1] == 0 or (vals[i - 1] < 0) != (vals[i] < 0)
    ]
    flip = min(crossings, key=lambda s: abs(s - spot)) if crossings else None

    # Walls: dollar-gamma per strike at the current spot.
    dollar = oi * _bs_gamma_vec(spot, k, t, iv)
    walls = pd.DataFrame({"strike": k, "dollar": dollar, "call": sign > 0})
    calls = walls[walls["call"]].groupby("strike")["dollar"].sum()
    puts = walls[~walls["call"]].groupby("strike")["dollar"].sum()
    return {
        "spot": spot,
        "net_gex": net_gex,
        "flip": round(flip, 2) if flip is not None else None,
        "call_wall": float(calls.idxmax()) if not calls.empty else None,
        "put_wall": float(puts.idxmax()) if not puts.empty else None,
    }


class GammaRepo:
    def __init__(self, database_url: str | None = None) -> None:
        url = _normalize_url(
            database_url or os.environ.get("DATABASE_URL", "sqlite:///webapp/seed.db"),
        )
        self._engine = create_engine(url, future=True)
        _Base.metadata.create_all(self._engine)
        self._session = sessionmaker(self._engine, future=True)

    def upsert(self, ticker: str, as_of: str, m: dict[str, float | None]) -> None:
        with self._session() as s:
            row = s.get(GammaRow, ticker.upper()) or GammaRow(ticker=ticker.upper())
            row.as_of = as_of
            row.spot = float(m["spot"])  # type: ignore[arg-type]
            row.net_gex = float(m["net_gex"])  # type: ignore[arg-type]
            row.flip = m["flip"]
            row.call_wall = m["call_wall"]
            row.put_wall = m["put_wall"]
            s.add(row)
            s.commit()

    def latest(self) -> dict[str, GammaContext]:
        with self._session() as s:
            rows = s.execute(select(GammaRow)).scalars().all()
        return {
            r.ticker: GammaContext(
                ticker=r.ticker, as_of=r.as_of, spot=r.spot, net_gex=r.net_gex,
                flip=r.flip, call_wall=r.call_wall, put_wall=r.put_wall,
            )
            for r in rows
        }
