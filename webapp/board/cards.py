"""Decision cards for the Alfa Board (Phase 5.2.C1a).

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (the
``alfa_decision_card`` table) and §3 (C1).

A decision card is the only irreversible thing the board produces. When the
owner presses ``Logla`` or ``Pas geç`` on a row, what that row said at that
moment is frozen into ``card_json``; a pass that is not written today cannot
be reconstructed tomorrow, because the quotes, the evidence reads and the tape
behind it are gone. So this module is deliberately small, and append-only:

- the table is created with ``checkfirst=True`` and is never altered, reset or
  dropped (contract §2, "append-only; never dropped, reset or rewritten");
- the repository exposes no update and no delete path. The single exception
  the contract allows is :meth:`CardRepo.link_trade`, which sets ``trade_id``
  once on a card that has none and never touches the snapshot;
- :func:`snapshot` is GENERIC — it walks dataclasses, Pydantic models,
  mappings, sequences, ``Decimal``, ``date``/``datetime`` and ``Enum`` — so a
  field a later branch adds to the row view model is frozen automatically.
  There is no hand-written field list that could silently go stale, which is
  what a card written against a half-frozen view would be worth: nothing.

``direction`` is stored as the board's own Turkish label (``yukarı`` /
``aşağı``, contract §2). The machine-readable key (``up`` / ``down``) stays
inside ``card_json`` with the rest of the row view, so nothing is lost.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, cast

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from webapp.board.db import AlfaBase, session_factory
from webapp.board.direction import DIRECTION_LABELS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from webapp.board.alfa_page import AlfaPage, AlfaRowView
    from webapp.board.direction import Direction

# A JSON document, as it is written to ``card_json`` and read back.
Json = str | int | float | bool | None | dict[str, "Json"] | list["Json"]

Decision = Literal["log", "pas"]
DECISIONS: Final[tuple[Decision, ...]] = ("log", "pas")

# The only decision a journal trade may be linked onto: contract §3 places the
# ``trade_id`` link in the Log flow. A passed card never takes one.
_LINKABLE_DECISION: Final[Decision] = "log"

# Contract §2: the stored direction values are the board's own labels.
DIRECTION_VALUES: Final[tuple[str, ...]] = tuple(DIRECTION_LABELS.values())

_BOARD_VERSION_ENV: Final = "RAILWAY_GIT_COMMIT_SHA"
UNKNOWN_BOARD_VERSION: Final = "unknown"

# Frozen Turkish copy for the card buttons and their answers (rule R-WD1).
# These record the owner's own decision; none of them may read as advice, and
# a test passes every one of them through ``webapp.board.honesty.ensure_clean``.
CARD_COPY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "log_button": "Logla",
        "pas_button": "Pas geç",
        "buttons_help": "Bu düğmeler senin kararını kaydeder; tahta tavsiye vermez.",
        "pas_recorded": "Pas kaydedildi: {ticker} {direction} · kart {card_id}",
        "forbidden_origin": "Bu istek tahtadan gelmedi; hiçbir şey yazılmadı.",
        "unknown_decision": "Bilinmeyen karar; hiçbir şey yazılmadı.",
        "row_not_found": "Satır bu çalışmada bulunamadı; hiçbir şey yazılmadı.",
        # The one failure the board must never paper over. The app's generic error
        # page says "Nothing is lost", which is true of a read-only view and false
        # of a decision: an unrecorded pass is gone. So the press gets this line.
        "write_failed": "Karar yazılamadı; hiçbir şey kaydedilmedi — tekrar dene.",
    },
)

# What a value the serializer does not understand is written as: named, never
# dropped silently, so a reader can see exactly what was not frozen.
UNSERIALIZED_KEY: Final = "__unserialized__"

_DEFAULT_LIST_LIMIT: Final = 200


class AlfaDecisionCard(AlfaBase):
    """``alfa_decision_card``: one append-only row per owner decision (contract §2)."""

    __tablename__ = "alfa_decision_card"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable: a row whose dominant contract has no recorded OCC symbol is still
    # a decision the owner made. Refusing to record it would lose the one thing
    # that cannot be rebuilt later.
    dominant_option_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    card_json: Mapped[str] = mapped_column(Text, nullable=False)
    board_version: Mapped[str] = mapped_column(String, nullable=False)
    board_profile_hash: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable for the same reason: a legacy print, or a failed hash read, must
    # not cost the card. The absence is then explicit rather than invented.
    calibration_profile_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    trade_id: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


def ensure_card_tables(engine: Engine) -> None:
    """Create ``alfa_decision_card`` when it is missing. Never alters anything that exists."""
    cast("Table", AlfaDecisionCard.__table__).create(engine, checkfirst=True)


def board_version() -> str:
    """The deployed commit (``RAILWAY_GIT_COMMIT_SHA``), or ``unknown`` (contract §2)."""
    return os.environ.get(_BOARD_VERSION_ENV, "").strip() or UNKNOWN_BOARD_VERSION


def direction_value(direction: Direction) -> str:
    """The stored value for a board direction key (``up`` → ``yukarı``)."""
    return DIRECTION_LABELS[direction]


def decision_for(value: str) -> Decision | None:
    """The decision a form value names, or ``None`` when it names none of them."""
    for decision in DECISIONS:
        if value == decision:
            return decision
    return None


def is_linkable(card: DecisionCard) -> bool:
    """True when a saved journal trade may still be linked onto ``card``.

    Contract §3 puts the ``trade_id`` link inside the **Log flow**: it is set
    once, on a card that has none. So a ``pas`` card is never linkable — a pass
    that acquired a trade id would read as a taken trade forever (the table is
    append-only; there is no repair path) and would corrupt the log-vs-pas
    comparison the pass ledger exists to make (§4).

    :meth:`CardRepo.link_trade` acts on exactly this answer, so nothing can
    promise the owner a link that the write would then refuse.
    """
    return card.decision == _LINKABLE_DECISION and card.trade_id is None


def is_logged(card: DecisionCard) -> bool:
    """True when the card records a taken decision (``log``).

    A fill belongs only to a taken decision: a pass was never traded, so it has
    no fill price, and a fill hanging off a ``pas`` card would corrupt the very
    log-vs-pas comparison the pass ledger exists to make (contract §4).
    """
    return card.decision == _LINKABLE_DECISION


def cards_for_trades(engine: Engine, trade_ids: Sequence[str]) -> Mapping[str, DecisionCard]:
    """The card linked onto each of ``trade_ids``, keyed by trade id (read only).

    One query for the whole journal page rather than one per trade. Reads no
    table it does not find: a caller on a database without the card table sees
    the read fail, and the journal renders exactly as it did before FAZ C.
    """
    wanted = [trade_id for trade_id in trade_ids if trade_id]
    if not wanted:
        return {}
    stmt = select(AlfaDecisionCard).where(AlfaDecisionCard.trade_id.in_(wanted))
    with session_factory(engine)() as session:
        rows: Sequence[AlfaDecisionCard] = session.execute(stmt).scalars().all()
    return {row.trade_id: _to_card(row) for row in rows if row.trade_id}


def pas_recorded_text(card: DecisionCard) -> str:
    """The board's confirmation line for a recorded pass (frozen copy, contract §3)."""
    return CARD_COPY["pas_recorded"].format(
        ticker=card.ticker, direction=card.direction, card_id=card.id,
    )


# ---------------------------------------------------------------------------
# Generic snapshot serializer
# ---------------------------------------------------------------------------


_COMPACT: Final = (",", ":")


def _json_key(key: object) -> str:
    """A mapping key as a JSON object key: strings as they are, anything else as its JSON text."""
    if isinstance(key, str):
        return key
    return json.dumps(snapshot(key), ensure_ascii=False, sort_keys=True, separators=_COMPACT)


def _sort_key(item: Json) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=_COMPACT)


def snapshot(value: object) -> Json:
    """A JSON-ready deep copy of ``value``, whatever shape it has.

    Recursive over dataclasses, Pydantic models, mappings, lists, tuples and
    sets; ``Decimal`` and ``date``/``datetime`` become their exact text,
    ``Enum`` becomes its value. Nothing here names a board field, so a field
    added to the row view model by another branch is frozen without a change
    to this module.

    A value of an unknown type is written as
    ``{"__unserialized__": <type>, "repr": <repr>}`` rather than dropped: a
    card that quietly lost a field would be worse than one that says so.
    """
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return snapshot(value.value)
    if isinstance(value, BaseModel):
        return snapshot(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: snapshot(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {_json_key(k): snapshot(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((snapshot(item) for item in value), key=_sort_key)
    if isinstance(value, list | tuple):
        return [snapshot(item) for item in value]
    return {UNSERIALIZED_KEY: type(value).__name__, "repr": repr(value)}


def snapshot_json(value: object) -> str:
    """``snapshot(value)`` as JSON text, the form stored in ``card_json``."""
    return json.dumps(snapshot(value), ensure_ascii=False)


# The row containers are the only thing a frozen page context leaves out: the row
# this card is about is already frozen under ``row_view``, and the board's other
# rows are not this decision. Everything else the page model holds is frozen
# generically, with no field list — the cost-gate state, the session date, the
# freshness line, the regime sentence and whatever else a later branch hangs off
# the page, captured the day it lands rather than the day someone remembers to
# extend a list here.
_PAGE_ROW_FIELDS: Final[frozenset[str]] = frozenset({"rows", "views", "sections"})


def page_context(page: AlfaPage) -> Json:
    """Everything the page model carries around the row, frozen (contract §3).

    This names no field: the names come from the dataclass itself and
    :func:`snapshot` walks whatever each value turns out to be.
    """
    return {
        field.name: snapshot(getattr(page, field.name))
        for field in dataclasses.fields(page)
        if field.name not in _PAGE_ROW_FIELDS
    }


def build_card_view(
    row_view: AlfaRowView,
    *,
    board_profile_hash: str,
    calibration_profile_hash: str | None,
    page: AlfaPage,
) -> Json:
    """The frozen view of one board row and the page it sat on (contract §3).

    Everything the row view model carries — evidence, tradability, narrative,
    ledger and whatever a later branch adds — plus the constituent
    ``(run_id, event_id)`` list, both profile hashes, and the page context the
    row was read in.

    ``page`` is required rather than optional on purpose: §3 asks the frozen
    view for things that live on the page and not on the row, so a call site
    that quietly omitted it would write a card missing half of what the owner
    was looking at — and a card can never be repaired afterwards.
    """
    return {
        "row_view": snapshot(row_view),
        "constituents": snapshot(
            tuple((p.run_id, p.event_id) for p in row_view.row.prints),
        ),
        "board_profile_hash": board_profile_hash,
        "calibration_profile_hash": calibration_profile_hash,
        "page_context": page_context(page),
    }


# ---------------------------------------------------------------------------
# Repository (append-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionCard:
    """One stored card, read back. Frozen: nothing reads a card in order to change it."""

    id: str
    created_at: datetime
    decision: str
    ticker: str
    direction: str
    run_id: str
    dominant_option_symbol: str | None
    card_json: str
    board_version: str
    board_profile_hash: str
    calibration_profile_hash: str | None
    trade_id: str | None
    note: str | None

    @property
    def card(self) -> Json:
        """The frozen row view, parsed."""
        return cast("Json", json.loads(self.card_json))


def _to_card(row: AlfaDecisionCard) -> DecisionCard:
    created = row.created_at
    return DecisionCard(
        id=row.id,
        # SQLite gives naive datetimes back; the column is written in UTC.
        created_at=created if created.tzinfo is not None else created.replace(tzinfo=UTC),
        decision=row.decision,
        ticker=row.ticker,
        direction=row.direction,
        run_id=row.run_id,
        dominant_option_symbol=row.dominant_option_symbol,
        card_json=row.card_json,
        board_version=row.board_version,
        board_profile_hash=row.board_profile_hash,
        calibration_profile_hash=row.calibration_profile_hash,
        trade_id=row.trade_id,
        note=row.note,
    )


class CardRepo:
    """Append-only access to ``alfa_decision_card``.

    The public API is ``write_card``, ``get_card``, ``list_cards`` and
    ``link_trade``. There is deliberately no update, delete, reset or drop
    path: a written card is evidence, and evidence that can be edited later is
    not evidence. ``link_trade`` is the one write to an existing row the
    contract allows, and it touches only ``trade_id``.
    """

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        ensure_card_tables(engine)
        self._sessions: sessionmaker[Session] = session_factory(engine)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def engine(self) -> Engine:
        return self._engine

    def write_card(
        self,
        *,
        decision: Decision,
        ticker: str,
        direction: str,
        run_id: str,
        dominant_option_symbol: str | None,
        card: Json,
        board_profile_hash: str,
        calibration_profile_hash: str | None,
        note: str | None = None,
    ) -> str:
        """Append one card and return its id. Raises on an unknown decision or direction."""
        if decision not in DECISIONS:
            msg = f"unknown decision {decision!r}; expected one of {DECISIONS}"
            raise ValueError(msg)
        if direction not in DIRECTION_VALUES:
            msg = f"unknown direction {direction!r}; expected one of {DIRECTION_VALUES}"
            raise ValueError(msg)
        card_id = uuid.uuid4().hex
        # One canonical spelling, decided on write. The pass ledger filters cards
        # by ticker (§4) and vendor tickers are not normalised on their way into a
        # BoardRow, so a ticker stored verbatim could count in the totals while
        # being invisible in its own ticker view — the one comparison the ledger
        # exists to make. The row view under card_json keeps the original text.
        row = AlfaDecisionCard(
            id=card_id,
            created_at=self._clock(),
            decision=decision,
            ticker=ticker.strip().upper(),
            direction=direction,
            run_id=run_id,
            dominant_option_symbol=dominant_option_symbol,
            card_json=json.dumps(card, ensure_ascii=False),
            board_version=board_version(),
            board_profile_hash=board_profile_hash,
            calibration_profile_hash=calibration_profile_hash,
            trade_id=None,
            note=note,
        )
        with self._sessions() as session:
            session.add(row)
            session.commit()
        return card_id

    def get_card(self, card_id: str) -> DecisionCard | None:
        with self._sessions() as session:
            row = session.get(AlfaDecisionCard, card_id)
            return None if row is None else _to_card(row)

    def list_cards(
        self,
        *,
        decision: str | None = None,
        ticker: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> tuple[DecisionCard, ...]:
        """Cards newest first, optionally filtered by decision and ticker (contract §4)."""
        stmt = select(AlfaDecisionCard).order_by(
            AlfaDecisionCard.created_at.desc(), AlfaDecisionCard.id.desc(),
        )
        if decision is not None:
            stmt = stmt.where(AlfaDecisionCard.decision == decision)
        if ticker is not None:
            stmt = stmt.where(AlfaDecisionCard.ticker == ticker.strip().upper())
        with self._sessions() as session:
            rows: Sequence[AlfaDecisionCard] = session.execute(stmt.limit(limit)).scalars().all()
            return tuple(_to_card(row) for row in rows)

    def link_trade(self, card_id: str, trade_id: str) -> bool:
        """Set ``trade_id`` on a ``log`` card that has none; ``True`` when this call linked it.

        The only write to an existing card the contract allows (§3, "the
        ``trade_id`` link is set once, on a card that has none"), and the
        contract places it inside the Log flow: :func:`is_linkable` is the rule,
        so a ``pas`` card is refused as firmly as a missing one. The snapshot is
        not touched: a card already linked, passed, or not found, returns
        ``False`` and nothing changes.
        """
        if not card_id or not trade_id:
            return False
        with self._sessions() as session:
            # One guarded statement, the shape oi_confirm and outcomes already use
            # for their append-only transitions. The read-then-write it replaces
            # let two concurrent journal POSTs both pass is_linkable and both write,
            # so the second silently re-pointed a card that was already linked —
            # on a table with no repair path (audit 2026-09-19). The WHERE carries
            # is_linkable's own condition, so the loser gets False, as the contract
            # promises, instead of overwriting the winner.
            statement = (
                # ``sa.update`` rather than a bare ``update`` import: no public name
                # in this module may read as a way to rewrite an append-only table,
                # and test_repo_exposes_no_update_or_delete_path pins exactly that.
                # outcomes.py made the same choice for the same reason.
                sa.update(AlfaDecisionCard)
                .where(
                    AlfaDecisionCard.id == card_id,
                    AlfaDecisionCard.trade_id.is_(None),
                    AlfaDecisionCard.decision == _LINKABLE_DECISION,
                )
                .values(trade_id=trade_id)
            )
            # ``session.connection()`` rather than ``session.execute``: the Core
            # result carries ``rowcount``, which is how "exactly once" is read off
            # the guarded UPDATE instead of being trusted.
            result = session.connection().execute(statement)
            session.commit()
            return result.rowcount == 1
