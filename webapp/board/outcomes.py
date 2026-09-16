"""Counterfactual outcomes for the decision cards (Phase 5.2.C2).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (the
``alfa_outcome`` table) and §4 (C2, the pass ledger).

A decision card records what the owner did. This table records what the market
then did — for the cards he took (``log``) and the cards he passed on (``pas``),
with **identical math**, which is the only thing that makes the two comparable
at all (§4). Without it the pass ledger would be a list of regrets rather than
a measurement.

The primary number is market-neutral excess:
``direction sign × ((U_h / U_0 − 1) − (SPY_h / SPY_0 − 1))`` — the method the
journal already uses (``webapp/journal.py`` ``directional_excess``) — read out
of ``alfa_daily_close``. It is stored in PERCENT units (``1.5`` means +1.5%),
which is what ``webapp.board.daily_close.pct_move_between`` returns and what
every percent key in the board profile means. A test pins it against
``directional_excess`` on the same four closes, so the "same method" claim is
checked rather than asserted.

Append-only, like every other ``alfa_`` table:

- the table is created with ``checkfirst=True`` and is never altered, reset or
  dropped (contract §2, "append-only; never dropped, reset or rewritten");
- a row's status advances from ``bekliyor`` to a final status **exactly once**
  (§2). :meth:`OutcomeRepo.advance` is guarded on the stored status inside one
  statement, so a second advance — or a restart that runs the job twice —
  changes nothing;
- a final row is never rewritten, and a missing close leaves the row
  ``bekliyor``: it never becomes a zero (§4).

Nullability. §2 marks only ``option_bid_at_horizon`` nullable, but §4 requires a
row that exists while its closes do not ("Missing closes leave the row
``bekliyor``"). A ``bekliyor`` row cannot carry closes, so the four close
columns and the excess are nullable here; the COLUMN SET is exactly §2's.
``option_symbol`` is nullable for the reason ``alfa_decision_card`` already
gives: a card whose dominant contract has no recorded OCC symbol is still a
decision the owner made.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, cast

import sqlalchemy as sa
from sqlalchemy import DateTime, Float, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.board.direction import DIRECTION_LABELS
from webapp.board.tradability import CHIP_COPY, format_pct

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

# Contract §2: the three stored status values, in the board's own Turkish.
Status = Literal["bekliyor", "hesaplandı", "veri yok"]
PENDING: Final[Status] = "bekliyor"
COMPUTED: Final[Status] = "hesaplandı"
NO_DATA: Final[Status] = "veri yok"
STATUSES: Final[tuple[Status, ...]] = (PENDING, COMPUTED, NO_DATA)
# The two a row may advance TO, exactly once (§2).
FINAL_STATUSES: Final[tuple[Status, ...]] = (COMPUTED, NO_DATA)

_DEFAULT_LIST_LIMIT: Final = 2000
_MIN_FOR_QUARTILES: Final = 2
_PERCENT: Final = 100.0  # the excess is stored in percent units (1.5 means +1.5%)
_UNKNOWN: Final = CHIP_COPY["unknown"]

# Frozen Turkish copy for outcomes (rule R-WD1). Every generated string is
# formatted from these templates, and a test passes each one through
# ``webapp.board.honesty.ensure_clean``.
OUTCOME_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        # The statuses are the contract's own words, byte for byte.
        "status_pending": PENDING,
        "status_computed": COMPUTED,
        "status_no_data": NO_DATA,
        # A (card, horizon) with no row at all: the horizon has not passed, or the
        # job has not reached it yet. Never rendered as a zero or as a result.
        "not_computed": "henüz hesaplanmadı",
        "horizon": "{n} gün",
        "excess": "Piyasa-nötr fark",
        "excess_help": (
            "Dayanak varlığın SPY'a göre, kartın yönündeki kapanış-kapanış hareketi."
        ),
        "option_bid": "Ufuk günündeki bid",
        "option_missing": NO_DATA,
        "card_day": "Kart günü kapanışı",
        "horizon_day": "Ufuk günü kapanışı",
        "no_significance": "Bu bir anlamlılık iddiası değildir.",
        "same_math": "Log ve pas kartları aynı hesapla ölçülür.",
    },
)


class AlfaOutcome(AlfaBase):
    """``alfa_outcome``: one row per (card, horizon), append-only (contract §2)."""

    __tablename__ = "alfa_outcome"

    card_id: Mapped[str] = mapped_column(String, primary_key=True)
    horizon_days: Mapped[int] = mapped_column(Integer, primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Nullable: a ``bekliyor`` row exists before its closes do (§4). None is
    # "not known", and it is rendered as such — never as a zero.
    underlying_close_at_card_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    underlying_close_at_horizon: Mapped[float | None] = mapped_column(Float, nullable=True)
    spy_close_at_card_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    spy_close_at_horizon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Direction-signed, in percent units (1.5 means +1.5%).
    market_neutral_excess: Mapped[float | None] = mapped_column(Float, nullable=True)
    option_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    option_bid_at_horizon: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)


def ensure_outcome_tables(engine: Engine) -> None:
    """Create ``alfa_outcome`` when it is missing. Never alters anything that exists."""
    cast("Table", AlfaOutcome.__table__).create(engine, checkfirst=True)


def horizon_text(horizon_days: int) -> str:
    """``1 gün`` / ``5 gün`` — the label of one horizon (frozen copy)."""
    return OUTCOME_COPY["horizon"].format(n=horizon_days)


def signed_pct_text(value: float | None) -> str:
    """``+%1.42`` / ``-%1.42``, or ``bilinmiyor``. The sign is the whole point."""
    if value is None:
        return _UNKNOWN
    return f"{'+' if value >= 0 else '-'}%{format_pct(abs(value))}"


def price_text(value: float | None) -> str:
    """``$1.23``, or ``bilinmiyor`` (a close or an option bid that is not known)."""
    return _UNKNOWN if value is None else f"${value:.2f}"


# ---------------------------------------------------------------------------
# The measurement (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeMeasurement:
    """The four closes behind one outcome and the direction-signed excess.

    ``excess_pct`` is in percent units and carries the card's direction sign, so
    a plus always means the name moved the way the card said, beyond the market.
    """

    underlying_at_card_day: float
    underlying_at_horizon: float
    spy_at_card_day: float
    spy_at_horizon: float
    excess_pct: float


def direction_sign(direction: str) -> float:
    """``+1`` for a ``yukarı`` card, ``-1`` for an ``aşağı`` one (contract §2's labels).

    The sign is what makes a taken card and a passed card comparable: both are
    measured in the direction the card claimed, so a plus always means the name
    moved the way the owner read it.
    """
    for key, label in DIRECTION_LABELS.items():
        if direction == label:
            return 1.0 if key == "up" else -1.0
    msg = f"unknown direction {direction!r}; expected one of {tuple(DIRECTION_LABELS.values())}"
    raise ValueError(msg)


def measure(
    *,
    direction: str,
    underlying_at_card_day: float,
    underlying_at_horizon: float,
    spy_at_card_day: float,
    spy_at_horizon: float,
) -> OutcomeMeasurement | None:
    """Contract §4's primary number, from four closes, in percent units.

    ``direction sign × ((U_h/U_0 − 1) − (SPY_h/SPY_0 − 1))`` — identical for a
    ``log`` card and a ``pas`` card, which is the only reason the two can be
    compared (§4).

    ``None`` when any of the four closes is not positive: a price of zero is not
    a price, and it must never turn into a −100% outcome in an append-only
    table. The caller then leaves the row ``bekliyor``.
    """
    closes = (underlying_at_card_day, underlying_at_horizon, spy_at_card_day, spy_at_horizon)
    if any(close <= 0 for close in closes):
        return None
    underlying_return = underlying_at_horizon / underlying_at_card_day - 1.0
    market_return = spy_at_horizon / spy_at_card_day - 1.0
    return OutcomeMeasurement(
        underlying_at_card_day=underlying_at_card_day,
        underlying_at_horizon=underlying_at_horizon,
        spy_at_card_day=spy_at_card_day,
        spy_at_horizon=spy_at_horizon,
        excess_pct=direction_sign(direction) * (underlying_return - market_return) * _PERCENT,
    )


@dataclass(frozen=True)
class FinalOutcome:
    """A finished outcome: either a measurement, or the honest absence of one.

    ``hesaplandı`` carries a measurement; ``veri yok`` carries none. Anything
    else is rejected on write, so a row can never claim a status its own fields
    contradict.
    """

    status: Status
    measurement: OutcomeMeasurement | None = None
    option_bid_at_horizon: float | None = None

    def __post_init__(self) -> None:
        if self.status not in FINAL_STATUSES:
            msg = f"{self.status!r} is not a final status; expected one of {FINAL_STATUSES}"
            raise ValueError(msg)
        if (self.status == COMPUTED) != (self.measurement is not None):
            msg = f"status {self.status!r} and measurement {self.measurement!r} disagree"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Stored outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """One stored outcome, read back. Frozen: nothing reads an outcome to change it."""

    card_id: str
    horizon_days: int
    computed_at: datetime
    underlying_close_at_card_day: float | None
    underlying_close_at_horizon: float | None
    spy_close_at_card_day: float | None
    spy_close_at_horizon: float | None
    market_neutral_excess: float | None
    option_symbol: str | None
    option_bid_at_horizon: float | None
    status: str

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def excess_text(self) -> str:
        return signed_pct_text(self.market_neutral_excess)

    @property
    def option_bid_text(self) -> str:
        """The horizon day's bid, or ``veri yok`` — never an invented option price."""
        if self.option_bid_at_horizon is None:
            return OUTCOME_COPY["option_missing"]
        return price_text(self.option_bid_at_horizon)

    @property
    def horizon_text(self) -> str:
        return horizon_text(self.horizon_days)


def _to_outcome(row: AlfaOutcome) -> Outcome:
    computed = row.computed_at
    return Outcome(
        card_id=row.card_id,
        horizon_days=row.horizon_days,
        # SQLite gives naive datetimes back; the column is written in UTC.
        computed_at=computed if computed.tzinfo is not None else computed.replace(tzinfo=UTC),
        underlying_close_at_card_day=row.underlying_close_at_card_day,
        underlying_close_at_horizon=row.underlying_close_at_horizon,
        spy_close_at_card_day=row.spy_close_at_card_day,
        spy_close_at_horizon=row.spy_close_at_horizon,
        market_neutral_excess=row.market_neutral_excess,
        option_symbol=row.option_symbol,
        option_bid_at_horizon=row.option_bid_at_horizon,
        status=row.status,
    )


class OutcomeRepo:
    """Append-only access to ``alfa_outcome``.

    The public API is ``write_pending``, ``write_final``, ``advance``,
    ``get_outcome`` and ``list_outcomes``. There is deliberately no rewrite, no
    reset and no drop path: an outcome is the evidence that says whether the
    owner's passes were better than his trades, and evidence that can be edited
    afterwards is not evidence.

    ``advance`` is the single exception the contract grants (§2, "moves from
    ``bekliyor`` to a final status exactly once"). It is one guarded statement,
    so a concurrent or repeated call finds nothing to advance and reports
    ``False``.
    """

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        ensure_outcome_tables(engine)
        self._sessions: sessionmaker[Session] = session_factory(engine)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def engine(self) -> Engine:
        return self._engine

    def write_pending(
        self, *, card_id: str, horizon_days: int, option_symbol: str | None = None,
    ) -> bool:
        """Append a ``bekliyor`` row. ``False`` when one already exists for this key.

        Written when a horizon has passed but its closes are not stored yet, so
        the ledger shows the card is being tracked rather than silently nothing.
        """
        return self._insert(
            card_id=card_id,
            horizon_days=horizon_days,
            status=PENDING,
            option_symbol=option_symbol,
            measurement=None,
            option_bid=None,
        )

    def write_final(
        self,
        *,
        card_id: str,
        horizon_days: int,
        final: FinalOutcome,
        option_symbol: str | None = None,
    ) -> bool:
        """Append a finished row. ``False`` when any row already exists for this key.

        The usual path: the job runs after the horizon and the closes are there,
        so the row is born final. A key that already holds a row is never
        overwritten — advancing a ``bekliyor`` row is :meth:`advance`'s job.
        """
        return self._insert(
            card_id=card_id,
            horizon_days=horizon_days,
            status=final.status,
            option_symbol=option_symbol,
            measurement=final.measurement,
            option_bid=final.option_bid_at_horizon,
        )

    def advance(self, *, card_id: str, horizon_days: int, final: FinalOutcome) -> bool:
        """Move one ``bekliyor`` row to a final status. ``True`` when this call moved it.

        Guarded on the stored status inside the UPDATE itself, so the transition
        happens exactly once (§2): a row that is already final, or absent, is
        left exactly as it is and the answer is ``False``.
        """
        measurement = final.measurement
        statement = (
            # ``sa.update`` rather than a ``from sqlalchemy import update``: no
            # public name in this module may read as a way to rewrite an
            # append-only table, and a test pins that. This is the one guarded
            # transition the contract allows.
            sa.update(AlfaOutcome)
            .where(
                AlfaOutcome.card_id == card_id,
                AlfaOutcome.horizon_days == horizon_days,
                AlfaOutcome.status == PENDING,
            )
            .values(
                computed_at=self._clock(),
                status=final.status,
                option_bid_at_horizon=final.option_bid_at_horizon,
                underlying_close_at_card_day=(
                    None if measurement is None else measurement.underlying_at_card_day
                ),
                underlying_close_at_horizon=(
                    None if measurement is None else measurement.underlying_at_horizon
                ),
                spy_close_at_card_day=(
                    None if measurement is None else measurement.spy_at_card_day
                ),
                spy_close_at_horizon=(
                    None if measurement is None else measurement.spy_at_horizon
                ),
                market_neutral_excess=(None if measurement is None else measurement.excess_pct),
            )
        )
        with self._sessions() as session:
            # ``session.connection()`` rather than ``session.execute``: the Core
            # result carries ``rowcount``, which is how "exactly once" is READ OFF
            # the guarded UPDATE instead of being trusted.
            result = session.connection().execute(statement)
            session.commit()
            return result.rowcount == 1

    def get_outcome(self, card_id: str, horizon_days: int) -> Outcome | None:
        with self._sessions() as session:
            row = session.get(AlfaOutcome, (card_id, horizon_days))
            return None if row is None else _to_outcome(row)

    def list_outcomes(
        self,
        *,
        card_ids: Sequence[str] | None = None,
        status: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> tuple[Outcome, ...]:
        """Stored outcomes, optionally for a set of cards or one status. Read-only."""
        if card_ids is not None:
            wanted = [card_id for card_id in card_ids if card_id]
            if not wanted:
                return ()
        statement = select(AlfaOutcome).order_by(
            AlfaOutcome.card_id, AlfaOutcome.horizon_days,
        )
        if card_ids is not None:
            statement = statement.where(AlfaOutcome.card_id.in_(wanted))
        if status is not None:
            statement = statement.where(AlfaOutcome.status == status)
        with self._sessions() as session:
            rows: Sequence[AlfaOutcome] = session.execute(statement.limit(limit)).scalars().all()
            return tuple(_to_outcome(row) for row in rows)

    def _insert(
        self,
        *,
        card_id: str,
        horizon_days: int,
        status: Status,
        option_symbol: str | None,
        measurement: OutcomeMeasurement | None,
        option_bid: float | None,
    ) -> bool:
        if not card_id:
            msg = "an outcome must name the card it measures"
            raise ValueError(msg)
        if horizon_days <= 0:
            msg = f"horizon_days must be positive, got {horizon_days!r}"
            raise ValueError(msg)
        row = AlfaOutcome(
            card_id=card_id,
            horizon_days=horizon_days,
            computed_at=self._clock(),
            underlying_close_at_card_day=(
                None if measurement is None else measurement.underlying_at_card_day
            ),
            underlying_close_at_horizon=(
                None if measurement is None else measurement.underlying_at_horizon
            ),
            spy_close_at_card_day=None if measurement is None else measurement.spy_at_card_day,
            spy_close_at_horizon=None if measurement is None else measurement.spy_at_horizon,
            market_neutral_excess=None if measurement is None else measurement.excess_pct,
            option_symbol=option_symbol,
            option_bid_at_horizon=option_bid,
            status=status,
        )
        with self._sessions() as session:
            if session.get(AlfaOutcome, (card_id, horizon_days)) is not None:
                return False
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Another writer stored this key between the read and the commit.
                # The stored row wins: nothing here may overwrite it.
                session.rollback()
                return False
            return True


# ---------------------------------------------------------------------------
# Count-gated statistics (contract §4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcessStats:
    """Median and interquartile range of market-neutral excess — or nothing.

    Below ``min_n`` measured cards every statistic is ``None``, so a template
    cannot show a median, an average or a spread by accident: the only thing it
    can render is the count. There is no hit rate, no t-statistic and no
    significance claim here at any sample size (contract §4).
    """

    n: int
    min_n: int
    median_pct: float | None = None
    q1_pct: float | None = None
    q3_pct: float | None = None

    @property
    def enough(self) -> bool:
        """True at or above the profile's ``fills.min_n_for_stats``."""
        return self.n >= self.min_n

    @property
    def iqr_pct(self) -> float | None:
        if self.q1_pct is None or self.q3_pct is None:
            return None
        return self.q3_pct - self.q1_pct


def _quartiles(values: Sequence[float]) -> tuple[float, float, float]:
    """(q1, median, q3); with a single value all three are that value."""
    median = statistics.median(values)
    if len(values) < _MIN_FOR_QUARTILES:
        return (values[0], median, values[0])
    q1, _median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return (q1, median, q3)


def excess_stats(outcomes: Sequence[Outcome], *, min_n: int) -> ExcessStats:
    """Count-gated statistics over the measured excesses in ``outcomes``.

    ``min_n`` is ``fills.min_n_for_stats`` from the board profile — never a
    literal here. Only rows that actually carry an excess count towards the
    sample: a ``bekliyor`` or ``veri yok`` row is not a zero (§4).
    """
    measured = [
        outcome.market_neutral_excess
        for outcome in outcomes
        if outcome.status == COMPUTED and outcome.market_neutral_excess is not None
    ]
    n = len(measured)
    if n < min_n:
        return ExcessStats(n=n, min_n=min_n)
    q1, median, q3 = _quartiles(measured)
    return ExcessStats(n=n, min_n=min_n, median_pct=median, q1_pct=q1, q3_pct=q3)


def template_context() -> dict[str, object]:
    """Frozen copy and formatting helpers for the outcome templates."""
    return {
        "outcome_copy": OUTCOME_COPY,
        "outcome_signed_pct_text": signed_pct_text,
        "outcome_price_text": price_text,
        "outcome_horizon_text": horizon_text,
    }
