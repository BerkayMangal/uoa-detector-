"""Phase 5.2.C1a: the decision-card table, its append-only repo and the snapshot serializer.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (columns, append-only)
and §3 (C1: the frozen view, the ``trade_id`` link set once).

Pins:
  - the table carries exactly the §2 columns, under the ``alfa_`` prefix;
  - card ids are uuid hex;
  - the repo exposes no update, delete, drop, reset or truncate path;
  - ``link_trade`` links once, only on a card that has none, and never touches
    ``card_json``;
  - the serializer is generic: a field added to a view model later is frozen
    with no change to ``webapp/board/cards.py`` — this is what makes a card
    written today still complete after FAZ B lands its row fields;
  - Decimal, datetime, date, Enum, tuple, mapping and Pydantic values survive a
    JSON round trip as exact text.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel
from webapp.board import cards
from webapp.board.cards import AlfaDecisionCard, CardRepo, snapshot, snapshot_json
from webapp.board.db import make_engine

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

_CONTRACT_COLUMNS = {
    "id",
    "created_at",
    "decision",
    "ticker",
    "direction",
    "run_id",
    "dominant_option_symbol",
    "card_json",
    "board_version",
    "board_profile_hash",
    "calibration_profile_hash",
    "trade_id",
    "note",
}

# Any of these in a public name would be a way to destroy a written card.
_DESTRUCTIVE_NAMES = ("update", "delete", "drop", "reset", "truncate", "purge", "wipe", "clear")

_T0 = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'cards.db'}")
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine: Engine) -> CardRepo:
    ticks = iter(_T0 + timedelta(seconds=i) for i in range(1000))
    return CardRepo(engine, clock=lambda: next(ticks))


def _write(repo: CardRepo, **overrides: object) -> str:
    fields: dict[str, object] = {
        "decision": "pas",
        "ticker": "SPY",
        "direction": "yukarı",
        "run_id": "live-2026-09-16",
        "dominant_option_symbol": "SPY260918C00760000",
        "card": {"row_view": {"ticker": "SPY"}, "constituents": [["live-2026-09-16", "e1"]]},
        "board_profile_hash": "board-hash",
        "calibration_profile_hash": "calib-hash",
    }
    fields.update(overrides)
    return repo.write_card(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Table and repo
# ---------------------------------------------------------------------------


def test_table_has_exactly_the_contract_columns() -> None:
    assert AlfaDecisionCard.__tablename__ == "alfa_decision_card"
    assert set(AlfaDecisionCard.__table__.columns.keys()) == _CONTRACT_COLUMNS
    assert [c.name for c in AlfaDecisionCard.__table__.primary_key] == ["id"]
    nullable = {c.name for c in AlfaDecisionCard.__table__.columns if c.nullable}
    # Only the links and the two values a row may genuinely not have.
    assert nullable == {"dominant_option_symbol", "calibration_profile_hash", "trade_id", "note"}


def test_card_ids_are_uuid_hex_and_unique(repo: CardRepo) -> None:
    ids = {_write(repo) for _ in range(5)}
    assert len(ids) == 5
    for card_id in ids:
        assert len(card_id) == 32
        assert uuid.UUID(hex=card_id).hex == card_id


def test_repo_exposes_no_update_or_delete_path() -> None:
    public = {name for name in vars(CardRepo) if not name.startswith("_")}
    assert public == {"engine", "write_card", "get_card", "list_cards", "link_trade"}
    module_public = [name for name in vars(cards) if not name.startswith("_")]
    for name in list(public) + module_public:
        folded = name.lower()
        assert not any(bad in folded for bad in _DESTRUCTIVE_NAMES), name


def test_written_card_round_trips(repo: CardRepo) -> None:
    card_id = _write(repo, decision="log", note="ilk kart")
    stored = repo.get_card(card_id)
    assert stored is not None
    assert stored.id == card_id
    assert stored.created_at == _T0
    assert stored.decision == "log"
    assert stored.ticker == "SPY"
    assert stored.direction == "yukarı"
    assert stored.run_id == "live-2026-09-16"
    assert stored.dominant_option_symbol == "SPY260918C00760000"
    assert stored.board_profile_hash == "board-hash"
    assert stored.calibration_profile_hash == "calib-hash"
    assert stored.trade_id is None
    assert stored.note == "ilk kart"
    assert stored.card == {
        "row_view": {"ticker": "SPY"},
        "constituents": [["live-2026-09-16", "e1"]],
    }
    assert repo.get_card("no-such-card") is None


def test_board_version_is_the_railway_sha_or_unknown(
    repo: CardRepo, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    unknown = repo.get_card(_write(repo))
    assert unknown is not None
    assert unknown.board_version == cards.UNKNOWN_BOARD_VERSION

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456")
    stamped = repo.get_card(_write(repo))
    assert stamped is not None
    assert stamped.board_version == "abc123def456"

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "   ")
    blank = repo.get_card(_write(repo))
    assert blank is not None
    assert blank.board_version == cards.UNKNOWN_BOARD_VERSION


def test_write_card_rejects_an_unknown_decision_or_direction(repo: CardRepo) -> None:
    with pytest.raises(ValueError, match="unknown decision"):
        _write(repo, decision="belki")
    with pytest.raises(ValueError, match="unknown direction"):
        _write(repo, direction="up")
    assert repo.list_cards() == ()


def test_list_cards_is_newest_first_and_filters(repo: CardRepo) -> None:
    first = _write(repo, decision="pas", ticker="SPY")
    second = _write(repo, decision="log", ticker="NVDA")
    third = _write(repo, decision="pas", ticker="NVDA")

    assert [c.id for c in repo.list_cards()] == [third, second, first]
    assert [c.id for c in repo.list_cards(decision="pas")] == [third, first]
    assert [c.id for c in repo.list_cards(ticker="nvda")] == [third, second]
    assert [c.id for c in repo.list_cards(decision="log", ticker="NVDA")] == [second]
    assert [c.id for c in repo.list_cards(limit=1)] == [third]


# ---------------------------------------------------------------------------
# link_trade: the one allowed write to an existing card
# ---------------------------------------------------------------------------


def test_link_trade_links_once_and_leaves_the_snapshot_alone(repo: CardRepo) -> None:
    card_id = _write(repo, decision="log")
    before = repo.get_card(card_id)
    assert before is not None

    assert repo.link_trade(card_id, "trade-1") is True
    linked = repo.get_card(card_id)
    assert linked is not None
    assert linked.trade_id == "trade-1"
    assert linked.card_json == before.card_json

    # A second link never overwrites the first.
    assert repo.link_trade(card_id, "trade-2") is False
    again = repo.get_card(card_id)
    assert again is not None
    assert again.trade_id == "trade-1"
    assert again.card_json == before.card_json
    assert again.created_at == before.created_at


def test_link_trade_is_false_for_a_missing_card_or_blank_ids(repo: CardRepo) -> None:
    card_id = _write(repo)
    assert repo.link_trade("no-such-card", "trade-1") is False
    assert repo.link_trade(card_id, "") is False
    assert repo.link_trade("", "trade-1") is False
    stored = repo.get_card(card_id)
    assert stored is not None
    assert stored.trade_id is None


# ---------------------------------------------------------------------------
# The generic snapshot serializer
# ---------------------------------------------------------------------------


class _Side(Enum):
    BUY = "alım"
    SELL = "satım"


class _Quote(BaseModel):
    symbol: str
    bid: float | None


@dataclass(frozen=True)
class _Leaf:
    label: str
    premium: Decimal
    seen_at: datetime


@dataclass(frozen=True)
class _View:
    ticker: str
    side: _Side
    leaves: tuple[_Leaf, ...]
    by_day: dict[tuple[str, date], int]
    quote: _Quote
    tags: frozenset[str]
    missing: str | None = None


def _view() -> _View:
    return _View(
        ticker="SPY",
        side=_Side.BUY,
        leaves=(
            _Leaf(label="a", premium=Decimal("400000.50"), seen_at=datetime(2026, 9, 16, 13, 30, tzinfo=UTC)),
            _Leaf(label="b", premium=Decimal("0.0001"), seen_at=datetime(2026, 9, 16, 14, 0, tzinfo=UTC)),
        ),
        by_day={("SPY", date(2026, 9, 16)): 2},
        quote=_Quote(symbol="SPY260918C00760000", bid=None),
        tags=frozenset({"b", "a"}),
    )


def test_snapshot_freezes_a_nested_view_exactly() -> None:
    frozen = snapshot(_view())
    assert frozen == {
        "ticker": "SPY",
        "side": "alım",
        "leaves": [
            {"label": "a", "premium": "400000.50", "seen_at": "2026-09-16T13:30:00+00:00"},
            {"label": "b", "premium": "0.0001", "seen_at": "2026-09-16T14:00:00+00:00"},
        ],
        "by_day": {'["SPY","2026-09-16"]': 2},
        "quote": {"symbol": "SPY260918C00760000", "bid": None},
        "tags": ["a", "b"],  # sets are ordered so two snapshots of one view match
        "missing": None,
    }


def test_snapshot_survives_a_json_round_trip() -> None:
    frozen = snapshot(_view())
    assert json.loads(snapshot_json(_view())) == frozen
    # Decimal keeps its exact text, not a float: a premium is money.
    assert json.loads(snapshot_json(_view()))["leaves"][1]["premium"] == "0.0001"


def test_snapshot_picks_up_a_field_added_later() -> None:
    """A parallel branch adding a row field must be frozen without touching cards.py."""

    @dataclass(frozen=True)
    class _Extended(_View):
        break_even: Decimal = Decimal("761.25")

    frozen = snapshot(_Extended(**{f: getattr(_view(), f) for f in _View.__dataclass_fields__}))
    assert frozen["break_even"] == "761.25"  # type: ignore[index,call-overload]
    assert frozen["ticker"] == "SPY"  # type: ignore[index,call-overload]


def test_snapshot_names_a_value_it_cannot_serialize() -> None:
    class _Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    frozen = snapshot({"thing": _Opaque()})
    assert frozen == {"thing": {cards.UNSERIALIZED_KEY: "_Opaque", "repr": "<opaque>"}}


def test_snapshot_handles_scalars_and_empty_containers() -> None:
    assert snapshot(None) is None
    assert snapshot(True) is True
    assert snapshot(3) == 3
    assert snapshot(2.5) == 2.5
    assert snapshot("x") == "x"
    assert snapshot(()) == []
    assert snapshot({}) == {}
    assert snapshot(date(2026, 9, 16)) == "2026-09-16"
