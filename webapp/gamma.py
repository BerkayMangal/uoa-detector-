"""Dealer-gamma map — structural CONTEXT for the screener (Phase 4.10).

NOT a directional edge, and NOT a validated signal of any kind. The studies
rejected BOTH tradeable gamma claims: direction (study_gamma_regime — the
apparent edge was an LCID squeeze, first-half only, non-overlap t=0.94) and
pinning (study_gamma_pinning — corr ~0, sign-flips across halves). The vol
effect (H1, "extreme-gamma names move more") looks significant full-sample
(t=2.60) but does NOT survive the same non-overlap gauntlet that killed the
others (t=1.08) — so it is NOT robustly established either. The ONE thing that
survived robustness is a separate, different claim: the vol-RISK-premium
(sell vol in long-gamma + high-IV names, study_vol_premium, non-overlap t=2.64,
small and tail-risky). So this map is purely SpotGamma-style structural context
(regime, flip, walls, IV rank) to inform a discretionary read — NEVER a signal.

GEX is computed from per-strike dealer exposure: live from UW greek-exposure
(webapp/gamma_live.py, full-tier key) refreshed in-process, OR offline from the
ThetaData option chain (OI x Black-Scholes gamma; scripts/gamma_snapshot.py).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date

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
    atm_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    iv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0..1 within own history
    realized_vol: Mapped[float | None] = mapped_column(Float, nullable=True)  # annualised, ~21d
    next_earnings: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO date
    next_catalyst: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO date, any kind
    catalyst_kind: Mapped[str | None] = mapped_column(String, nullable=True)  # earnings/fomc/fda/...


@dataclass(frozen=True)
class GammaContext:
    ticker: str
    as_of: str
    spot: float
    net_gex: float
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    atm_iv: float | None = None
    iv_pct: float | None = None
    realized_vol: float | None = None
    next_earnings: date | None = None
    next_catalyst: date | None = None
    catalyst_kind: str | None = None

    @property
    def vrp_pct(self) -> float | None:
        """Implied minus realized vol, in vol points (%). >0 = implied richer
        than recent realised — the vol premium, made visible. Descriptive."""
        if self.atm_iv is None or self.realized_vol is None:
            return None
        return round((self.atm_iv - self.realized_vol) * 100, 1)

    @property
    def regime(self) -> str:
        return "short" if self.net_gex < 0 else "long"

    @property
    def regime_label(self) -> str:
        return "amplifies moves" if self.net_gex < 0 else "suppresses moves"

    @property
    def vol_signal(self) -> str:
        """The study-backed vol read: sell vol in long-gamma + rich IV; buy in
        short-gamma + cheap IV. Only the sell side survived robustness — buy is
        shown as a weak/watch lead. Returns 'sell' | 'buy' | 'neutral'."""
        if self.iv_pct is None:
            return "neutral"
        if self.regime == "long" and self.iv_pct >= 0.75:
            return "sell"
        if self.regime == "short" and self.iv_pct <= 0.25:
            return "buy"
        return "neutral"

    @property
    def vol_label(self) -> str:
        return {
            "sell": "Options rich — vol-selling candidate (defined-risk)",
            "buy": "Options cheap — vol-buying lead (weak, watch)",
            "neutral": "Vol fairly priced",
        }[self.vol_signal]


def atm_iv(chain: pd.DataFrame, *, as_of: str) -> float | None:
    """Median ATM implied vol (DTE 10-45, within 5% of spot) — the market's
    annualised vol charge. Used for the vol-premium read + IV percentile."""
    df = chain.copy()
    for c in ("strike", "implied_volatility", "spot"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["dte"] = (df["expiry"] - pd.Timestamp(as_of)).dt.days
    spot = df["spot"].dropna()
    if spot.empty:
        return None
    s = float(spot.iloc[0])
    df = df[(df["dte"] >= 10) & (df["dte"] <= 45) & (df["implied_volatility"] > 0)]
    df = df[(df["strike"] - s).abs() <= 0.05 * s]
    return float(df["implied_volatility"].median()) if not df.empty else None


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
    # Vectorised over the full (grid x contracts) broadcast — one exp instead of
    # 161 Python-loop calls (compute_gamma runs ~6000x in the studies). Same math
    # as gex_at, batched.
    grid = np.linspace(spot * 0.6, spot * 1.4, 161)
    s_col = grid[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_t = iv[None, :] * np.sqrt(t[None, :])
        d1 = (np.log(s_col / k[None, :]) + 0.5 * iv[None, :] ** 2 * t[None, :]) / vol_t
        g_grid = (_NORM * np.exp(-0.5 * d1 * d1)) / (s_col * vol_t)
    g_grid = np.where(np.isfinite(g_grid), g_grid, 0.0)
    vals = np.sum(sign[None, :] * oi[None, :] * g_grid, axis=1)
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
        "atm_iv": atm_iv(chain, as_of=as_of),
    }


class GammaRepo:
    def __init__(self, database_url: str | None = None) -> None:
        url = _normalize_url(
            database_url or os.environ.get("DATABASE_URL", "sqlite:///webapp/seed.db"),
        )
        self._engine = create_engine(url, future=True)
        _Base.metadata.create_all(self._engine)
        self._session = sessionmaker(self._engine, future=True)

    def reset(self) -> None:
        """Drop + recreate the table — the snapshot is a full daily rebuild, and
        this picks up schema changes (new columns) without a migration."""
        GammaRow.__table__.drop(self._engine, checkfirst=True)
        _Base.metadata.create_all(self._engine)

    def upsert(self, ticker: str, as_of: str, m: dict[str, float | None]) -> None:
        with self._session() as s:
            row = s.get(GammaRow, ticker.upper()) or GammaRow(ticker=ticker.upper())
            row.as_of = as_of
            row.spot = float(m["spot"])  # type: ignore[arg-type]
            row.net_gex = float(m["net_gex"])  # type: ignore[arg-type]
            row.flip = m["flip"]
            row.call_wall = m["call_wall"]
            row.put_wall = m["put_wall"]
            row.atm_iv = m.get("atm_iv")
            row.iv_pct = m.get("iv_pct")
            row.realized_vol = m.get("realized_vol")
            row.next_earnings = m.get("next_earnings")  # type: ignore[assignment]
            row.next_catalyst = m.get("next_catalyst")  # type: ignore[assignment]
            row.catalyst_kind = m.get("catalyst_kind")  # type: ignore[assignment]
            s.add(row)
            s.commit()

    def latest(self) -> dict[str, GammaContext]:
        with self._session() as s:
            rows = s.execute(select(GammaRow)).scalars().all()
        return {
            r.ticker: GammaContext(
                ticker=r.ticker, as_of=r.as_of, spot=r.spot, net_gex=r.net_gex,
                flip=r.flip, call_wall=r.call_wall, put_wall=r.put_wall,
                atm_iv=r.atm_iv, iv_pct=r.iv_pct, realized_vol=r.realized_vol,
                next_earnings=date.fromisoformat(r.next_earnings) if r.next_earnings else None,
                next_catalyst=date.fromisoformat(r.next_catalyst) if r.next_catalyst else None,
                catalyst_kind=r.catalyst_kind,
            )
            for r in rows
        }
