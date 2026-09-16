"""Position size per Alfa Board row (Phase 5.2.B1, contract §6 B1). Pure functions.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §6 B1 ("Cell", "Risk
bucket line", "Disclosure", "Unknowns"), §4.3 (owner-only values) and §2 R-CO1;
decision P15 (disclosed defaults).

The cell answers "how much of the account is one lot, and how many lots does the
profile's risk bucket allow" — never "how many should be taken". It states
nothing about outcome.

- **1 lot** = ask x 100 dollars, and that as a percent of ``sizing.capital_usd``.
- **Risk bucket line**: the source print's risk bucket and its ``max_r``,
  the dollar risk ``max_r x sizing.r_usd`` and the resulting lot count
  ``floor(risk / (ask x 100 + cost.commission_per_contract_usd))``.
  One commission: the entry leg. The exit leg's commission is already in the
  chip's round trip (R-CO1), and a lot count must not be inflated by pricing
  the exit twice.
- **Bucket and max_r** come from the row's source print: the largest print of
  the dominant contract, the same print the audit block and the penalty ledger
  describe. ``StoredSignal`` persists ``label`` and ``max_r``
  (``src/uoa_detector/backtest/store.py``); the bucket is the label's bucket,
  mapped exactly as ``uoa_detector.risk.sizer.RiskSizer`` maps it (a unit test
  pins the parity). Only ``max_r`` is a profile number, and it is persisted on
  the row, so nothing here reads a profile.
- **The ask** is the dominant contract's current ask, and only when it is
  executable (``tradability.executable_ask``): a null, crossed, zero or stale
  quote gives no lot count and no lot cost, and the cell reads ``bilinmiyor``
  with the chip's quote age still visible (review FA-01/FA-02, fix1).
- **Zero lots** is rendered, never hidden: one lot above the risk amount is a
  fact about the account, and the row stays on the board.
- While ``sizing.values_confirmed_by_owner`` is false, every size cell carries
  ``(varsayılan değer)`` (decision P15).
- **Audit only**: the sentence disclosing that ``max_r`` comes from the label,
  which the combined score drives, belongs in the row's ``Denetim`` block, with
  the score (R-EV2).

Every string comes from the frozen dictionary below (R-WD1). No numeric cutoff
lives here (D8): capital, R and the commission come from ``BoardSettings``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from uoa_detector.domain.labels import SignalLabel
from uoa_detector.domain.risk import RiskBucket
from webapp.board.tradability import executable_ask

if TYPE_CHECKING:
    from collections.abc import Mapping

    from uoa_detector.backtest.store import StoredSignal
    from webapp.board.settings import CostSettings, SizingSettings
    from webapp.board.tradability import TradabilityRead

# Market structure, not a cutoff: one US equity option contract covers 100 shares.
_CONTRACT_MULTIPLIER: Final = Decimal(100)
_PERCENT: Final = Decimal(100)

# ---------------------------------------------------------------------------
# Frozen copy (contract §6 B1)
# ---------------------------------------------------------------------------

SIZE_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "title": "Pozisyon boyutu",
        "lot": "1 lot ${cost} · sermayenin %{pct} kadarı",
        "lot_unknown": "1 lot: bilinmiyor (işlem yapılabilir kotasyon yok)",
        # Contract §6 B1, byte for byte.
        "risk_bucket": "Profil risk kovası: {bucket}, max_r {x} → {x} × R = ${y} → {n} lot",  # noqa: RUF001
        "risk_bucket_no_lots": (
            "Profil risk kovası: {bucket}, max_r {x} → {x} × R = ${y} → lot sayısı bilinmiyor"  # noqa: RUF001
        ),
        "risk_bucket_unknown": "Profil risk kovası: bilinmiyor (kaynak baskının kaydı okunamadı)",
        "zero_lots": "1 lot risk tutarını aşıyor: 0 lot",
        "audit": (
            "max_r satırın {label} etiketinden gelir; etiketi birleşik skor belirler "
            "(skor bu blokta)."
        ),
        "audit_unknown": (
            "max_r satırın etiketinden gelir; etiketi birleşik skor belirler "
            "(bu satırın kaydı okunamadı)."
        ),
        "default_value": "(varsayılan değer)",
    },
)

# Label → risk bucket, exactly as ``uoa_detector.risk.sizer.RiskSizer`` maps it.
# Only ``max_r`` is a profile number, and it is persisted on the signal row, so
# the board needs no profile to name the bucket. A unit test pins this parity.
LABEL_BUCKETS: Final[Mapping[SignalLabel, RiskBucket]] = MappingProxyType(
    {
        SignalLabel.HIGH_CONVICTION_SEQUENCE: RiskBucket.HIGH_CONVICTION_SEQUENCE,
        SignalLabel.SWEEP_UOA: RiskBucket.SWEEP_UOA,
        SignalLabel.PRE_CATALYST_FLOW: RiskBucket.PRE_CATALYST_FLOW,
        SignalLabel.STANDARD_UOA: RiskBucket.STANDARD_UOA,
        SignalLabel.CONVEXITY_CLUSTER: RiskBucket.CONVEXITY_CLUSTER,
        SignalLabel.CONVEXITY_BURST: RiskBucket.CONVEXITY_CLUSTER,
        SignalLabel.CONVEXITY_WATCH: RiskBucket.CONVEXITY_WATCH,
        SignalLabel.LEAP_POSITIONING: RiskBucket.LEAP_POSITIONING,
        SignalLabel.REJECTED: RiskBucket.REJECTED,
        SignalLabel.IGNORE_NOISE: RiskBucket.DISCARD,
        SignalLabel.LIKELY_CLOSING_OR_NOISE: RiskBucket.DISCARD,
        SignalLabel.POST_EVENT_NOISE: RiskBucket.DISCARD,
        SignalLabel.PENALIZED_BELOW_THRESHOLD: RiskBucket.DISCARD,
        SignalLabel.OPENING_UNCONFIRMED: RiskBucket.DISCARD,
        SignalLabel.SECTOR_FLOW_CLUSTER: RiskBucket.DISCARD,
        SignalLabel.CONFIRMED_OPENING_FLOW: RiskBucket.DISCARD,
        SignalLabel.OPTIONS_EQUITY_TAPE_CONFIRMATION: RiskBucket.DISCARD,
        SignalLabel.GAMMA_ACCELERATION_RISK: RiskBucket.DISCARD,
    },
)


@dataclass(frozen=True)
class SizeRead:
    """One row's size cell: the lot, the risk amount and the lot count."""

    bucket: str | None  # the source print's risk bucket name
    max_r: float | None
    r_usd: float
    capital_usd: float
    risk_usd: float | None  # max_r x r_usd
    ask: float | None  # executable ask only
    lot_cost_usd: float | None  # ask x 100
    lot_pct_capital: float | None
    lots: int | None
    values_confirmed: bool

    @property
    def no_lots(self) -> bool:
        """One lot does not fit inside the risk amount. Rendered, never hidden."""
        return self.lots == 0


@dataclass(frozen=True)
class SizeText:
    """Display strings for one size cell, formatted from the frozen templates."""

    title: str
    lot: str
    risk_bucket: str
    zero_note: str | None
    audit_note: str
    default_marker: str | None


def bucket_for_label(label: SignalLabel) -> RiskBucket:
    """The risk bucket a label sizes into (``RiskSizer``'s mapping)."""
    return LABEL_BUCKETS[label]


def lots_for(risk_usd: float | None, ask: float | None, commission_per_contract_usd: float) -> int | None:
    """``floor(risk / (ask x 100 + commission))``; None when risk or the ask is unknown."""
    if risk_usd is None or ask is None:
        return None
    if not math.isfinite(risk_usd) or not math.isfinite(ask) or risk_usd < 0 or ask <= 0:
        return None
    per_lot = _dec(ask) * _CONTRACT_MULTIPLIER + _dec(commission_per_contract_usd)
    if per_lot <= 0:
        return None
    return int(_dec(risk_usd) // per_lot)


def build_size(
    *,
    signal: StoredSignal | None,
    chip: TradabilityRead,
    sizing: SizingSettings,
    cost: CostSettings,
) -> SizeRead:
    """The size cell of one row, from its source print and its tradability chip."""
    ask = executable_ask(chip)
    max_r = signal.max_r if signal is not None else None
    bucket = bucket_for_label(signal.label).value if signal is not None else None
    risk = max_r * sizing.r_usd if max_r is not None else None
    return SizeRead(
        bucket=bucket,
        max_r=max_r,
        r_usd=sizing.r_usd,
        capital_usd=sizing.capital_usd,
        risk_usd=risk,
        ask=ask,
        lot_cost_usd=float(_dec(ask) * _CONTRACT_MULTIPLIER) if ask is not None else None,
        lot_pct_capital=(
            float(_dec(ask) * _CONTRACT_MULTIPLIER / _dec(sizing.capital_usd) * _PERCENT)
            if ask is not None
            else None
        ),
        lots=lots_for(risk, ask, cost.commission_per_contract_usd),
        values_confirmed=sizing.values_confirmed_by_owner,
    )


def size_text(read: SizeRead) -> SizeText:
    """The size cell's strings; every unknown is labelled, never blank."""
    lot = (
        SIZE_COPY["lot"].format(cost=_usd(read.lot_cost_usd), pct=_share_pct(read.lot_pct_capital))
        if read.lot_cost_usd is not None and read.lot_pct_capital is not None
        else SIZE_COPY["lot_unknown"]
    )
    return SizeText(
        title=SIZE_COPY["title"],
        lot=lot,
        risk_bucket=_risk_bucket_text(read),
        zero_note=SIZE_COPY["zero_lots"] if read.no_lots else None,
        audit_note=(
            SIZE_COPY["audit"].format(label=read.bucket)
            if read.bucket is not None
            else SIZE_COPY["audit_unknown"]
        ),
        default_marker=None if read.values_confirmed else SIZE_COPY["default_value"],
    )


def _risk_bucket_text(read: SizeRead) -> str:
    if read.bucket is None or read.max_r is None or read.risk_usd is None:
        return SIZE_COPY["risk_bucket_unknown"]
    values = {"bucket": read.bucket, "x": _plain(read.max_r), "y": _usd(read.risk_usd)}
    if read.lots is None:
        return SIZE_COPY["risk_bucket_no_lots"].format(**values)
    return SIZE_COPY["risk_bucket"].format(**values, n=read.lots)


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _plain(value: float) -> str:
    """A profile number as written: 0.75, 1, 0."""
    return f"{value:g}"


def _usd(value: float) -> str:
    """Dollars with thousands separators; cents only when they are not zero."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _share_pct(value: float) -> str:
    """Two decimals, trailing zeros dropped: a 1-lot share is usually below 1% of capital."""
    return f"{round(value, 2):g}"
