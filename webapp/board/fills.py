"""Fill capture for the Alfa Board (Phase 5.2.C3).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (the ``alfa_fill``
table) and §5 (C3).

This is the live test of the cost assumption: the board tells the owner what a
contract should cost him at the quote it saw; a fill says what it actually
cost. The difference is slippage, and it is the only number in the system that
can falsify the cost model.

Three rules shape the whole module:

- **The assumed quote is the card's, never the fill's.** No source gives the
  NBBO at the moment of a manual fill — ``option-contracts`` carries no quote
  time and ``/flow`` is frozen at the last print (contract §5). So the quote
  read is the one frozen into the decision card, and it is always labelled with
  its age (``kart anındaki kotasyon, {n} sn yaşında``). Nothing here may ever
  render it as "NBBO at fill".
- **Append-only.** Like ``alfa_decision_card``, the table is created with
  ``checkfirst=True`` and the repository exposes no update and no delete path.
  A mis-typed fill is corrected by recording the truth next to it, not by
  editing the record.
- **Counts below the sample gate.** Under ``fills.min_n_for_stats`` fills, only
  a count is shown — :func:`slippage_stats` returns ``None`` for every
  statistic in that case, so a template cannot leak a median by accident. At or
  above it, the median and the interquartile range are shown, and no
  significance claim is made anywhere (contract §5).

Sign convention: slippage is **positive when the fill was worse than the card's
quote**. Entry slippage is ``fill − assumed_ask`` and exit slippage is
``assumed_bid − fill`` (contract §5), so both sides read the same way — a plus
is money lost against the assumption.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, cast

from sqlalchemy import DateTime, Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.board.tradability import format_pct, spread_pct_of_mid

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from webapp.board.cards import Json

# Contract §2: the stored side values are the board's own Turkish labels.
Side = Literal["giriş", "çıkış"]
ENTRY: Final[Side] = "giriş"
EXIT: Final[Side] = "çıkış"
SIDES: Final[tuple[Side, ...]] = (ENTRY, EXIT)

_DEFAULT_LIST_LIMIT: Final = 500

_PERCENT: Final = Decimal(100)
_CONTRACT_MULTIPLIER: Final = Decimal(100)  # one contract is 100 shares of premium
_LEGS: Final = Decimal(2)
_MIN_FOR_QUARTILES: Final = 2

# Frozen Turkish copy for fill capture (rule R-WD1). Every generated string is
# formatted from these templates, and a test passes each one through
# ``webapp.board.honesty.ensure_clean``.
FILL_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "form_title": "Dolum gir",
        "form_help": "Gerçekleşen dolumu kaydeder; tahta emir açmaz, tavsiye vermez.",
        "side_label": "Taraf",
        "side_entry": ENTRY,
        "side_exit": EXIT,
        "price_label": "Dolum fiyatı (prim/hisse)",
        "contracts_label": "Kontrat sayısı",
        "submit": "Dolumu kaydet",
        # Contract §5, byte for byte: the assumed quote is the CARD's quote, with its age.
        "assumed_quote": "kart anındaki kotasyon, {n} sn yaşında",
        "assumed_quote_not_nbbo": (
            "Dolum anındaki NBBO hiçbir kaynakta yok; karşılaştırma kart anındaki "
            "kotasyonla yapılır."
        ),
        "assumed_spread": "Kart anındaki makas",
        "entry_formula": "Giriş kayması = dolum - kart anındaki ask",
        "exit_formula": "Çıkış kayması = kart anındaki bid - dolum",
        "sign_help": (
            "Artı değer kart kotasyonundan kötü dolum, eksi değer kotasyondan iyi "
            "dolum demek."
        ),
        "slippage_usd": "Kayma (kontrat başına)",
        "slippage_pct": "Kayma (orta fiyata oran)",
        "median": "Medyan kayma",
        "iqr": "Çeyrekler arası aralık",
        "no_significance": "Bu bir anlamlılık iddiası değildir.",
        # Contract §5's example line, with the count filled in.
        "counts_only": "{n} dolum kaydı; istatistik için yetersiz örnek",
        "stats_title": "Kayma özeti",
        "fills_title": "Dolum kayıtları",
        "fills_empty": "Bu kart için dolum kaydı yok.",
        "sample_size": "{n} dolum kaydı",
        "recorded": "Dolum kaydedildi: {ticker} {side} · {contracts} kontrat · {price}",
        "card_title": "Karar kartı",
        "card_meta": "{ticker} {direction} · {created} · kart {card_id}",
        "card_missing": "Kart bulunamadı.",
        "trade_link": "Bağlı işlem: {trade_id}",
        "trade_unlinked": "Bu karta bağlı işlem yok.",
        "open_card": "Dolum gir (karar kartı)",
        "decision_log": "loglandı",
        "decision_pas": "pas geçildi",
        # Refusals. Every one of them says that nothing was written.
        "card_not_found": "Kart bulunamadı; hiçbir şey yazılmadı.",
        "not_logged": "Bu kart pas kaydı; dolum yalnızca loglanan karta yazılır.",
        "unknown_side": "Bilinmeyen taraf; hiçbir şey yazılmadı.",
        "bad_numbers": (
            "Dolum fiyatı ve kontrat sayısı sıfırdan büyük olmalı; hiçbir şey yazılmadı."
        ),
        "no_assumed_quote": (
            "Kart anında kotasyon yok; kayma hesaplanamaz, dolum kaydı yazılmadı."
        ),
        # The same failure the card POST names: a write that did not happen must
        # never read as a record. A fill nobody wrote cannot be reconstructed.
        "write_failed": "Dolum yazılamadı; hiçbir şey kaydedilmedi — tekrar dene.",
    },
)

_UNKNOWN: Final = "bilinmiyor"


class AlfaFill(AlfaBase):
    """``alfa_fill``: one append-only row per recorded fill (contract §2)."""

    __tablename__ = "alfa_fill"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    card_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Nullable: a card whose journal trade was never saved still has real fills.
    trade_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String, nullable=False)
    fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    contracts: Mapped[float] = mapped_column(Float, nullable=False)
    # The card snapshot's quote, copied in on write: not nullable, because a fill
    # with no assumption to test is not a slippage record. The route refuses it.
    assumed_ask: Mapped[float] = mapped_column(Float, nullable=False)
    assumed_bid: Mapped[float] = mapped_column(Float, nullable=False)
    assumed_mid: Mapped[float] = mapped_column(Float, nullable=False)
    quote_age_seconds_at_card: Mapped[int] = mapped_column(Integer, nullable=False)


def ensure_fill_tables(engine: Engine) -> None:
    """Create ``alfa_fill`` when it is missing. Never alters anything that exists."""
    cast("Table", AlfaFill.__table__).create(engine, checkfirst=True)


def side_for(value: str) -> Side | None:
    """The side a form value names, or ``None`` when it names neither."""
    for side in SIDES:
        if value == side:
            return side
    return None


# ---------------------------------------------------------------------------
# The assumed quote: read from the card snapshot, never from the client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssumedQuote:
    """The card's frozen quote, the only thing a manual fill can be measured against."""

    bid: float
    ask: float
    mid: float
    age_seconds: int

    @property
    def spread_pct(self) -> float | None:
        """The card's spread as % of mid, by the board's own formula."""
        return spread_pct_of_mid(self.bid, self.ask)


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _number(value: Json) -> float | None:
    """A JSON value as a float, or ``None``; ``True``/``False`` are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _mapping(value: Json, key: str) -> Json:
    return value.get(key) if isinstance(value, dict) else None


def assumed_quote_from_card(card: Json) -> AssumedQuote | None:
    """The quote frozen into a decision card, or ``None`` when it holds no usable pair.

    Reads ``card_json`` → ``row_view`` → ``chip``, the board's own tradability
    read of the row's dominant contract. ``None`` (and the route then refuses to
    write) when the snapshot has no bid, no ask, no quote age, or a mid that is
    not positive: slippage against an invented quote is not evidence, and a
    written fill can never be corrected.
    """
    chip = _mapping(_mapping(card, "row_view"), "chip")
    bid = _number(_mapping(chip, "bid"))
    ask = _number(_mapping(chip, "ask"))
    age = _number(_mapping(chip, "quote_age_seconds"))
    if bid is None or ask is None or age is None:
        return None
    mid = float((_dec(bid) + _dec(ask)) / _LEGS)
    if mid <= 0:
        return None
    return AssumedQuote(bid=bid, ask=ask, mid=mid, age_seconds=int(age))


def assumed_quote_text(quote: AssumedQuote) -> str:
    """``kart anındaki kotasyon, {n} sn yaşında`` — never "NBBO at fill" (contract §5)."""
    return FILL_COPY["assumed_quote"].format(n=quote.age_seconds)


# ---------------------------------------------------------------------------
# Slippage (pure)
# ---------------------------------------------------------------------------


def _raw_slippage(side: str, fill_price: float, quote: AssumedQuote) -> Decimal:
    """Contract §5: entry is ``fill − assumed_ask``, exit is ``assumed_bid − fill``."""
    if side == ENTRY:
        return _dec(fill_price) - _dec(quote.ask)
    if side == EXIT:
        return _dec(quote.bid) - _dec(fill_price)
    msg = f"unknown side {side!r}; expected one of {SIDES}"
    raise ValueError(msg)


def slippage_usd_per_contract(side: str, fill_price: float, quote: AssumedQuote) -> float:
    """Slippage in dollars per contract (100 shares of premium), positive when worse."""
    return float(_raw_slippage(side, fill_price, quote) * _CONTRACT_MULTIPLIER)


def slippage_pct_of_mid(side: str, fill_price: float, quote: AssumedQuote) -> float | None:
    """Slippage as % of the card's mid, or ``None`` when that mid is not positive."""
    if quote.mid <= 0:
        return None
    return float(_raw_slippage(side, fill_price, quote) / _dec(quote.mid) * _PERCENT)


# ---------------------------------------------------------------------------
# Stored fills
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """One stored fill, read back. Frozen: nothing reads a fill in order to change it."""

    id: str
    card_id: str
    trade_id: str | None
    created_at: datetime
    side: str
    fill_price: float
    contracts: float
    assumed_ask: float
    assumed_bid: float
    assumed_mid: float
    quote_age_seconds_at_card: int

    @property
    def quote(self) -> AssumedQuote:
        return AssumedQuote(
            bid=self.assumed_bid, ask=self.assumed_ask, mid=self.assumed_mid,
            age_seconds=self.quote_age_seconds_at_card,
        )

    @property
    def slippage_usd(self) -> float:
        return slippage_usd_per_contract(self.side, self.fill_price, self.quote)

    @property
    def slippage_pct(self) -> float | None:
        return slippage_pct_of_mid(self.side, self.fill_price, self.quote)

    @property
    def assumed_quote_text(self) -> str:
        return assumed_quote_text(self.quote)


def _to_fill(row: AlfaFill) -> Fill:
    created = row.created_at
    return Fill(
        id=row.id,
        card_id=row.card_id,
        trade_id=row.trade_id,
        # SQLite gives naive datetimes back; the column is written in UTC.
        created_at=created if created.tzinfo is not None else created.replace(tzinfo=UTC),
        side=row.side,
        fill_price=row.fill_price,
        contracts=row.contracts,
        assumed_ask=row.assumed_ask,
        assumed_bid=row.assumed_bid,
        assumed_mid=row.assumed_mid,
        quote_age_seconds_at_card=row.quote_age_seconds_at_card,
    )


class FillRepo:
    """Append-only access to ``alfa_fill``.

    The public API is ``write_fill``, ``get_fill`` and ``list_fills``. There is
    deliberately no update, delete, reset or drop path: a recorded fill is
    evidence of what the cost model got wrong, and evidence that can be edited
    later is not evidence.
    """

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        ensure_fill_tables(engine)
        self._sessions: sessionmaker[Session] = session_factory(engine)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def engine(self) -> Engine:
        return self._engine

    def write_fill(
        self,
        *,
        card_id: str,
        trade_id: str | None,
        side: Side,
        fill_price: float,
        contracts: float,
        quote: AssumedQuote,
    ) -> str:
        """Append one fill and return its id. Raises on a bad side, id or size."""
        if side not in SIDES:
            msg = f"unknown side {side!r}; expected one of {SIDES}"
            raise ValueError(msg)
        if not card_id:
            msg = "a fill must name the card it was measured against"
            raise ValueError(msg)
        if fill_price <= 0 or contracts <= 0:
            msg = "fill_price and contracts must both be positive"
            raise ValueError(msg)
        fill_id = uuid.uuid4().hex
        row = AlfaFill(
            id=fill_id,
            card_id=card_id,
            trade_id=trade_id or None,
            created_at=self._clock(),
            side=side,
            fill_price=fill_price,
            contracts=contracts,
            assumed_ask=quote.ask,
            assumed_bid=quote.bid,
            assumed_mid=quote.mid,
            quote_age_seconds_at_card=quote.age_seconds,
        )
        with self._sessions() as session:
            session.add(row)
            session.commit()
        return fill_id

    def get_fill(self, fill_id: str) -> Fill | None:
        with self._sessions() as session:
            row = session.get(AlfaFill, fill_id)
            return None if row is None else _to_fill(row)

    def list_fills(
        self,
        *,
        card_id: str | None = None,
        trade_id: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> tuple[Fill, ...]:
        """Fills newest first, optionally for one card or one trade."""
        stmt = select(AlfaFill).order_by(AlfaFill.created_at.desc(), AlfaFill.id.desc())
        if card_id is not None:
            stmt = stmt.where(AlfaFill.card_id == card_id)
        if trade_id is not None:
            stmt = stmt.where(AlfaFill.trade_id == trade_id)
        with self._sessions() as session:
            rows: Sequence[AlfaFill] = session.execute(stmt.limit(limit)).scalars().all()
            return tuple(_to_fill(row) for row in rows)


# ---------------------------------------------------------------------------
# Count-gated display (contract §5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlippageStats:
    """Median and interquartile range of slippage — or nothing, below the sample gate.

    Below ``min_n`` fills every statistic is ``None``, so a template cannot show
    a median, an average or a spread of one by accident: the only thing it can
    render is the count. There is no t-statistic and no significance claim here,
    at any sample size (contract §5).
    """

    n: int
    min_n: int
    median_usd: float | None = None
    q1_usd: float | None = None
    q3_usd: float | None = None
    median_pct: float | None = None
    q1_pct: float | None = None
    q3_pct: float | None = None

    @property
    def enough(self) -> bool:
        """True at or above the profile's ``fills.min_n_for_stats``."""
        return self.n >= self.min_n

    @property
    def iqr_usd(self) -> float | None:
        if self.q1_usd is None or self.q3_usd is None:
            return None
        return self.q3_usd - self.q1_usd

    @property
    def iqr_pct(self) -> float | None:
        if self.q1_pct is None or self.q3_pct is None:
            return None
        return self.q3_pct - self.q1_pct

    @property
    def counts_only_text(self) -> str:
        return FILL_COPY["counts_only"].format(n=self.n)

    @property
    def sample_text(self) -> str:
        return FILL_COPY["sample_size"].format(n=self.n)


def _quartiles(values: Sequence[float]) -> tuple[float, float, float]:
    """(q1, median, q3); with a single value all three are that value."""
    median = statistics.median(values)
    if len(values) < _MIN_FOR_QUARTILES:
        return (values[0], median, values[0])
    q1, _med, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return (q1, median, q3)


def slippage_stats(fills: Sequence[Fill], *, min_n: int) -> SlippageStats:
    """Count-gated slippage statistics over ``fills`` (contract §5).

    ``min_n`` is ``fills.min_n_for_stats`` from the board profile — never a
    literal here. Below it, only ``n`` is set. The percent statistics need their
    own sample: a fill whose card had no positive mid contributes a dollar
    figure and no percentage, and a thin percent sample stays unreported.
    """
    n = len(fills)
    if n < min_n:
        return SlippageStats(n=n, min_n=min_n)
    usd = [f.slippage_usd for f in fills]
    pct = [p for f in fills if (p := f.slippage_pct) is not None]
    q1_usd, median_usd, q3_usd = _quartiles(usd)
    stats = SlippageStats(
        n=n, min_n=min_n, median_usd=median_usd, q1_usd=q1_usd, q3_usd=q3_usd,
    )
    if len(pct) < min_n:
        return stats
    q1_pct, median_pct, q3_pct = _quartiles(pct)
    return SlippageStats(
        n=n, min_n=min_n, median_usd=median_usd, q1_usd=q1_usd, q3_usd=q3_usd,
        median_pct=median_pct, q1_pct=q1_pct, q3_pct=q3_pct,
    )


@dataclass(frozen=True)
class SideSummary:
    """One side's fills and their count-gated statistics."""

    side: str
    stats: SlippageStats

    @property
    def formula_text(self) -> str:
        return FILL_COPY["entry_formula" if self.side == ENTRY else "exit_formula"]


def side_summaries(fills: Sequence[Fill], *, min_n: int) -> tuple[SideSummary, ...]:
    """One summary per side, in ``SIDES`` order.

    Entry and exit slippage are different quantities (one is measured against
    the ask, the other against the bid), so they are never pooled: each side
    carries its own sample and its own count gate.
    """
    return tuple(
        SideSummary(side=side, stats=slippage_stats([f for f in fills if f.side == side], min_n=min_n))
        for side in SIDES
    )


# ---------------------------------------------------------------------------
# Formatting (the only place a fill number becomes text)
# ---------------------------------------------------------------------------


def usd_text(value: float | None) -> str:
    """``$1.23``, or ``bilinmiyor``."""
    return _UNKNOWN if value is None else f"${value:.2f}"


def signed_usd_text(value: float | None) -> str:
    """``+$2.00`` / ``-$2.00``: the sign is the whole point of a slippage number."""
    if value is None:
        return _UNKNOWN
    return f"{'+' if value >= 0 else '-'}${abs(value):.2f}"


def signed_pct_text(value: float | None) -> str:
    """``+%1.5`` / ``-%1.5``, with the board's own percent formatting."""
    if value is None:
        return _UNKNOWN
    return f"{'+' if value >= 0 else '-'}%{format_pct(abs(value))}"


def pct_text(value: float | None) -> str:
    """``%5.2``, or ``bilinmiyor`` (used for the card's assumed spread)."""
    return _UNKNOWN if value is None else f"%{format_pct(value)}"


def contracts_text(value: float) -> str:
    """``3`` for a whole number of contracts, ``2.5`` otherwise."""
    return f"{value:g}"


def recorded_text(fill: Fill, *, ticker: str) -> str:
    """The board's confirmation line for a recorded fill (frozen copy)."""
    return FILL_COPY["recorded"].format(
        ticker=ticker,
        side=fill.side,
        contracts=contracts_text(fill.contracts),
        price=usd_text(fill.fill_price),
    )


def template_context() -> dict[str, object]:
    """Frozen copy and formatting helpers for the fill templates."""
    return {
        "fill_copy": FILL_COPY,
        "fill_sides": SIDES,
        "fill_usd_text": usd_text,
        "fill_signed_usd_text": signed_usd_text,
        "fill_signed_pct_text": signed_pct_text,
        "fill_pct_text": pct_text,
        "fill_contracts_text": contracts_text,
        "fill_assumed_quote_text": assumed_quote_text,
    }
