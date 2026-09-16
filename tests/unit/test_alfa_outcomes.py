"""Phase 5.2.C2: ``alfa_outcome``, its append-only repo and the outcome math.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (the table) and §4
(C2: the counterfactual outcome, identical for log and pas).

Hermetic: a tmp sqlite file, no network, no env.

Pins:
  - the table carries exactly the §2 columns, keyed (card_id, horizon_days),
    with only the columns a ``bekliyor`` row cannot fill left nullable;
  - the repo has no rewrite, reset or drop path, and no public name reads like
    one;
  - a stored row is never overwritten: a second write of the same key changes
    nothing, and a final row is immutable;
  - ``bekliyor`` advances to a final status EXACTLY once (§2);
  - a status and its fields can never disagree (``hesaplandı`` without a
    measurement, or ``veri yok`` with one, is refused);
  - the primary number is the journal's own method: ``direction sign x
    ((U_h/U_0 - 1) - (SPY_h/SPY_0 - 1))``, checked against
    ``webapp.journal.directional_excess`` on the same four closes;
  - a missing close is never a zero;
  - the count gate at ``fills.min_n_for_stats``, in both directions, with every
    statistic None below it, on a hand-computed quartile fixture;
  - every frozen and generated string passes ``honesty.ensure_clean``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board import outcomes as outcomes_module
from webapp.board.db import make_engine
from webapp.board.honesty import ensure_clean, forbidden_words
from webapp.board.outcomes import (
    COMPUTED,
    FINAL_STATUSES,
    NO_DATA,
    OUTCOME_COPY,
    PENDING,
    STATUSES,
    AlfaOutcome,
    ExcessStats,
    FinalOutcome,
    Outcome,
    OutcomeMeasurement,
    OutcomeRepo,
    direction_sign,
    ensure_outcome_tables,
    excess_stats,
    horizon_text,
    measure,
    price_text,
    signed_pct_text,
    template_context,
)
from webapp.board.settings import load_board_settings
from webapp.journal import TradeRow, directional_excess

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_MIN_N = _SETTINGS.fills.min_n_for_stats  # D8: the gate is profile-driven, never a literal

_CONTRACT_COLUMNS = {
    "card_id",
    "horizon_days",
    "computed_at",
    "underlying_close_at_card_day",
    "underlying_close_at_horizon",
    "spy_close_at_card_day",
    "spy_close_at_horizon",
    "market_neutral_excess",
    "option_symbol",
    "option_bid_at_horizon",
    "status",
}

# Any of these in a public name would be a way to rewrite a recorded outcome.
_DESTRUCTIVE_NAMES = ("delete", "drop", "reset", "truncate", "purge", "wipe", "clear")

_T0 = datetime(2026, 9, 16, 21, 30, tzinfo=UTC)
_SYMBOL = "SPY260918C00760000"
# The name rose 2%, SPY rose 0.5%: a ``yukarı`` card is +1.5% market-neutral.
_MEASUREMENT = OutcomeMeasurement(
    underlying_at_card_day=100.0,
    underlying_at_horizon=102.0,
    spy_at_card_day=400.0,
    spy_at_horizon=402.0,
    excess_pct=1.5,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'outcomes.db'}")
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine: Engine) -> OutcomeRepo:
    ticks = iter(_T0 + timedelta(seconds=i) for i in range(1000))
    return OutcomeRepo(engine, clock=lambda: next(ticks))


def _outcome(
    *, card_id: str = "card-1", horizon_days: int = 1, status: str = COMPUTED,
    excess: float | None = 1.5,
) -> Outcome:
    return Outcome(
        card_id=card_id,
        horizon_days=horizon_days,
        computed_at=_T0,
        underlying_close_at_card_day=100.0,
        underlying_close_at_horizon=102.0,
        spy_close_at_card_day=400.0,
        spy_close_at_horizon=402.0,
        market_neutral_excess=excess,
        option_symbol=_SYMBOL,
        option_bid_at_horizon=None,
        status=status,
    )


def _sample(n: int, *, start: float = 1.0) -> list[Outcome]:
    return [
        _outcome(card_id=f"card-{i}", excess=start + i) for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Table and repo (contract §2: append-only)
# ---------------------------------------------------------------------------


def test_table_has_exactly_the_contract_columns() -> None:
    assert AlfaOutcome.__tablename__ == "alfa_outcome"
    assert set(AlfaOutcome.__table__.columns.keys()) == _CONTRACT_COLUMNS
    assert [c.name for c in AlfaOutcome.__table__.primary_key] == ["card_id", "horizon_days"]
    nullable = {c.name for c in AlfaOutcome.__table__.columns if c.nullable}
    # §4 requires a row that exists while its closes do not ("Missing closes leave
    # the row bekliyor"), so exactly those columns are nullable. The key, the
    # stamp and the status never are.
    assert nullable == {
        "underlying_close_at_card_day",
        "underlying_close_at_horizon",
        "spy_close_at_card_day",
        "spy_close_at_horizon",
        "market_neutral_excess",
        "option_symbol",
        "option_bid_at_horizon",
    }


def test_ensure_outcome_tables_is_idempotent(engine: Engine) -> None:
    ensure_outcome_tables(engine)
    ensure_outcome_tables(engine)  # checkfirst: never alters or drops what exists
    assert OutcomeRepo(engine).list_outcomes() == ()


def test_repo_exposes_no_rewrite_path() -> None:
    public = {name for name in vars(OutcomeRepo) if not name.startswith("_")}
    assert public == {
        "engine", "write_pending", "write_final", "advance", "get_outcome", "list_outcomes",
    }
    module_public = [name for name in vars(outcomes_module) if not name.startswith("_")]
    for name in list(public) + module_public:
        folded = name.lower()
        assert not any(bad in folded for bad in _DESTRUCTIVE_NAMES), name
    # ``advance`` is the one guarded transition §2 allows; nothing else may update.
    assert "update" not in module_public


def test_the_three_contract_statuses_are_the_stored_words() -> None:
    assert STATUSES == ("bekliyor", "hesaplandı", "veri yok")
    assert FINAL_STATUSES == (COMPUTED, NO_DATA)
    assert PENDING not in FINAL_STATUSES


def test_a_pending_row_round_trips(repo: OutcomeRepo) -> None:
    assert repo.write_pending(card_id="card-1", horizon_days=5, option_symbol=_SYMBOL) is True
    stored = repo.get_outcome("card-1", 5)
    assert stored is not None
    assert (stored.card_id, stored.horizon_days, stored.status) == ("card-1", 5, PENDING)
    assert stored.computed_at == _T0
    assert stored.option_symbol == _SYMBOL
    # A missing close is None, never a zero (§4).
    assert stored.underlying_close_at_card_day is None
    assert stored.spy_close_at_horizon is None
    assert stored.market_neutral_excess is None
    assert stored.is_final is False


def test_a_final_row_round_trips(repo: OutcomeRepo) -> None:
    final = FinalOutcome(status=COMPUTED, measurement=_MEASUREMENT, option_bid_at_horizon=1.25)
    assert repo.write_final(card_id="c", horizon_days=1, final=final, option_symbol=_SYMBOL) is True
    stored = repo.get_outcome("c", 1)
    assert stored is not None
    assert stored.status == COMPUTED
    assert stored.is_final is True
    assert stored.underlying_close_at_card_day == 100.0
    assert stored.underlying_close_at_horizon == 102.0
    assert stored.spy_close_at_card_day == 400.0
    assert stored.spy_close_at_horizon == 402.0
    assert stored.market_neutral_excess == 1.5
    assert stored.option_bid_at_horizon == 1.25


def test_a_second_write_never_overwrites_a_stored_row(repo: OutcomeRepo) -> None:
    assert repo.write_pending(card_id="c", horizon_days=1) is True
    assert repo.write_pending(card_id="c", horizon_days=1) is False
    other = FinalOutcome(status=COMPUTED, measurement=_MEASUREMENT)
    assert repo.write_final(card_id="c", horizon_days=1, final=other) is False
    stored = repo.get_outcome("c", 1)
    assert stored is not None
    assert stored.status == PENDING  # the stored row won


def test_advance_moves_a_pending_row_exactly_once(repo: OutcomeRepo) -> None:
    repo.write_pending(card_id="c", horizon_days=1, option_symbol=_SYMBOL)
    final = FinalOutcome(status=COMPUTED, measurement=_MEASUREMENT, option_bid_at_horizon=0.80)
    assert repo.advance(card_id="c", horizon_days=1, final=final) is True
    moved = repo.get_outcome("c", 1)
    assert moved is not None
    assert moved.status == COMPUTED
    assert moved.market_neutral_excess == 1.5
    assert moved.option_bid_at_horizon == 0.80
    assert moved.option_symbol == _SYMBOL  # the pending row's symbol is kept
    # A second advance (a restart, a re-run) changes nothing at all.
    again = FinalOutcome(status=NO_DATA)
    assert repo.advance(card_id="c", horizon_days=1, final=again) is False
    assert repo.get_outcome("c", 1) == moved


def test_advance_never_touches_a_row_that_was_born_final(repo: OutcomeRepo) -> None:
    repo.write_final(
        card_id="c", horizon_days=1, final=FinalOutcome(status=COMPUTED, measurement=_MEASUREMENT),
    )
    before = repo.get_outcome("c", 1)
    assert repo.advance(card_id="c", horizon_days=1, final=FinalOutcome(status=NO_DATA)) is False
    assert repo.get_outcome("c", 1) == before


def test_advance_is_false_for_a_row_that_does_not_exist(repo: OutcomeRepo) -> None:
    assert repo.advance(card_id="nope", horizon_days=1, final=FinalOutcome(status=NO_DATA)) is False
    assert repo.get_outcome("nope", 1) is None


def test_veri_yok_is_a_final_status_with_no_numbers(repo: OutcomeRepo) -> None:
    repo.write_pending(card_id="c", horizon_days=5)
    assert repo.advance(card_id="c", horizon_days=5, final=FinalOutcome(status=NO_DATA)) is True
    stored = repo.get_outcome("c", 5)
    assert stored is not None
    assert (stored.status, stored.is_final) == (NO_DATA, True)
    assert stored.market_neutral_excess is None
    assert stored.underlying_close_at_horizon is None


@pytest.mark.parametrize(
    ("status", "measurement"),
    [
        (COMPUTED, None),  # a computed outcome with nothing behind it
        (NO_DATA, _MEASUREMENT),  # "no data" that carries data
        (PENDING, None),  # bekliyor is not a final status
        (PENDING, _MEASUREMENT),
    ],
)
def test_a_status_and_its_fields_can_never_disagree(
    status: str, measurement: OutcomeMeasurement | None,
) -> None:
    with pytest.raises(ValueError, match=r"status|final"):
        FinalOutcome(status=status, measurement=measurement)  # type: ignore[arg-type]


@pytest.mark.parametrize(("card_id", "horizon"), [("", 1), ("c", 0), ("c", -5)])
def test_a_write_refuses_a_blank_card_or_a_non_positive_horizon(
    repo: OutcomeRepo, card_id: str, horizon: int,
) -> None:
    with pytest.raises(ValueError, match=r"card|horizon"):
        repo.write_pending(card_id=card_id, horizon_days=horizon)
    assert repo.list_outcomes() == ()


def test_list_outcomes_filters_by_card_and_by_status(repo: OutcomeRepo) -> None:
    repo.write_pending(card_id="a", horizon_days=1)
    repo.write_pending(card_id="a", horizon_days=5)
    repo.write_final(
        card_id="b", horizon_days=1, final=FinalOutcome(status=COMPUTED, measurement=_MEASUREMENT),
    )
    assert len(repo.list_outcomes()) == 3
    assert {o.horizon_days for o in repo.list_outcomes(card_ids=["a"])} == {1, 5}
    assert [o.card_id for o in repo.list_outcomes(status=COMPUTED)] == ["b"]
    assert repo.list_outcomes(card_ids=[]) == ()
    assert repo.list_outcomes(card_ids=["ghost"]) == ()


# ---------------------------------------------------------------------------
# The measurement (contract §4: the journal's own method)
# ---------------------------------------------------------------------------


def test_direction_sign_is_the_cards_own_labels() -> None:
    assert direction_sign("yukarı") == 1.0
    assert direction_sign("aşağı") == -1.0
    with pytest.raises(ValueError, match="direction"):
        direction_sign("bullish")


def test_the_excess_is_the_journals_directional_excess_in_percent() -> None:
    """Contract §4: "the method the journal already uses" — checked, not asserted."""
    closes = {
        "underlying_at_card_day": 100.0,
        "underlying_at_horizon": 102.0,
        "spy_at_card_day": 400.0,
        "spy_at_horizon": 402.0,
    }
    for direction, thesis in (("yukarı", "bullish"), ("aşağı", "bearish")):
        found = measure(direction=direction, **closes)
        assert found is not None
        trade = TradeRow(
            direction=thesis,
            entry_underlying_px=closes["underlying_at_card_day"],
            exit_underlying_px=closes["underlying_at_horizon"],
            entry_spy_px=closes["spy_at_card_day"],
            exit_spy_px=closes["spy_at_horizon"],
        )
        journal_excess = directional_excess(trade)
        assert journal_excess is not None
        assert found.excess_pct == pytest.approx(journal_excess * 100.0)
    # Hand-computed: +2.0% - +0.5% = +1.5% for the up card, -1.5% for the down card.
    up = measure(direction="yukarı", **closes)
    down = measure(direction="aşağı", **closes)
    assert up is not None and down is not None
    assert up.excess_pct == pytest.approx(1.5)
    assert down.excess_pct == pytest.approx(-1.5)


def test_the_measurement_keeps_the_four_closes_it_used() -> None:
    found = measure(
        direction="yukarı", underlying_at_card_day=100.0, underlying_at_horizon=102.0,
        spy_at_card_day=400.0, spy_at_horizon=402.0,
    )
    assert (found.underlying_at_card_day, found.underlying_at_horizon) == (100.0, 102.0)
    assert (found.spy_at_card_day, found.spy_at_horizon) == (400.0, 402.0)
    assert found.excess_pct == pytest.approx(1.5)


@pytest.mark.parametrize(
    "closes",
    [
        {"underlying_at_card_day": 0.0},
        {"underlying_at_horizon": 0.0},
        {"spy_at_card_day": 0.0},
        {"spy_at_horizon": 0.0},
        {"underlying_at_card_day": -1.0},
    ],
)
def test_a_close_that_is_not_positive_measures_nothing(closes: dict[str, float]) -> None:
    """A price of zero is not a price. It must never become a -100% outcome."""
    fields = {
        "underlying_at_card_day": 100.0, "underlying_at_horizon": 102.0,
        "spy_at_card_day": 400.0, "spy_at_horizon": 402.0, **closes,
    }
    assert measure(direction="yukarı", **fields) is None


def test_an_unchanged_pair_measures_exactly_zero() -> None:
    found = measure(
        direction="yukarı", underlying_at_card_day=100.0, underlying_at_horizon=100.0,
        spy_at_card_day=400.0, spy_at_horizon=400.0,
    )
    assert found is not None
    assert found.excess_pct == 0.0


# ---------------------------------------------------------------------------
# The count gate (contract §4)
# ---------------------------------------------------------------------------


def test_the_quartiles_are_hand_computable() -> None:
    sample = [_outcome(card_id=f"c{i}", excess=value) for i, value in enumerate((1.0, 2.0, 3.0, 4.0))]
    stats = excess_stats(sample, min_n=4)
    assert (stats.n, stats.enough) == (4, True)
    assert stats.q1_pct == pytest.approx(1.75)
    assert stats.median_pct == pytest.approx(2.5)
    assert stats.q3_pct == pytest.approx(3.25)
    assert stats.iqr_pct == pytest.approx(1.5)


def test_a_single_value_is_its_own_quartiles() -> None:
    stats = excess_stats([_outcome(excess=2.0)], min_n=1)
    assert (stats.median_pct, stats.q1_pct, stats.q3_pct) == (2.0, 2.0, 2.0)
    assert stats.iqr_pct == 0.0


def test_below_the_profile_gate_only_the_count_survives() -> None:
    stats = excess_stats(_sample(_MIN_N - 1), min_n=_MIN_N)
    assert (stats.n, stats.min_n, stats.enough) == (_MIN_N - 1, _MIN_N, False)
    assert stats.median_pct is None
    assert stats.q1_pct is None
    assert stats.q3_pct is None
    assert stats.iqr_pct is None


def test_at_the_profile_gate_the_median_appears() -> None:
    stats = excess_stats(_sample(_MIN_N), min_n=_MIN_N)
    assert (stats.n, stats.enough) == (_MIN_N, True)
    assert stats.median_pct is not None
    assert stats.q1_pct is not None
    assert stats.q3_pct is not None


def test_an_unmeasured_row_is_not_a_zero_in_the_sample() -> None:
    """A bekliyor or veri yok row has no number; it must not dilute the median."""
    sample = [
        *_sample(3),
        _outcome(card_id="p", status=PENDING, excess=None),
        _outcome(card_id="n", status=NO_DATA, excess=None),
        # A row that claims "hesaplandı" with no number is still not a zero.
        _outcome(card_id="x", status=COMPUTED, excess=None),
    ]
    stats = excess_stats(sample, min_n=3)
    assert stats.n == 3
    assert stats.median_pct == pytest.approx(2.0)


def test_there_is_no_t_statistic_anywhere() -> None:
    stats = excess_stats(_sample(_MIN_N), min_n=_MIN_N)
    for banned in ("t_stat", "t_statistic", "p_value", "hit_rate", "win_rate", "mean", "average"):
        assert not hasattr(stats, banned), banned
    assert set(ExcessStats.__dataclass_fields__) == {
        "n", "min_n", "median_pct", "q1_pct", "q3_pct",
    }


# ---------------------------------------------------------------------------
# Text (contract R-WD1)
# ---------------------------------------------------------------------------


def test_signed_and_unknown_formatting() -> None:
    assert signed_pct_text(1.5) == "+%1.5"
    assert signed_pct_text(-1.5) == "-%1.5"
    assert signed_pct_text(0.0) == "+%0"
    assert signed_pct_text(None) == "bilinmiyor"
    assert price_text(1.25) == "$1.25"
    assert price_text(None) == "bilinmiyor"
    assert horizon_text(1) == "1 gün"
    assert horizon_text(5) == "5 gün"


def test_an_outcome_without_an_option_bid_says_veri_yok() -> None:
    stored = _outcome()
    assert stored.option_bid_at_horizon is None
    assert stored.option_bid_text == "veri yok"
    assert stored.excess_text == "+%1.5"
    assert stored.horizon_text == "1 gün"


def test_every_frozen_and_generated_string_is_clean() -> None:
    for text in OUTCOME_COPY.values():
        assert ensure_clean(text) == text
    generated = [
        horizon_text(5),
        signed_pct_text(-2.25),
        price_text(3.5),
        _outcome().option_bid_text,
        _outcome(status=NO_DATA, excess=None).excess_text,
    ]
    for text in generated:
        assert ensure_clean(text) == text
    assert forbidden_words(" ".join(OUTCOME_COPY.values())) == []


def test_the_template_context_carries_only_frozen_copy_and_helpers() -> None:
    context = template_context()
    assert context["outcome_copy"] is OUTCOME_COPY
    assert callable(context["outcome_signed_pct_text"])
    assert callable(context["outcome_price_text"])
    assert callable(context["outcome_horizon_text"])
