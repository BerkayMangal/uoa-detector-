"""Card construction rules that must not quietly regress.

Each test names the specific way a card could mislead if the rule were missing.
"""

from __future__ import annotations

import pathlib
from datetime import date
from decimal import Decimal

import pytest

from uoa_detector.options_alpha.card import (
    DataOrigin,
    OpportunityStatus,
    ResearchStatus,
    build_card,
)
from uoa_detector.options_alpha.selection import Candidate
from uoa_detector.options_alpha.settings import OptionsAlphaSettings, load_settings
from uoa_detector.options_alpha.structures import (
    ContractQuote,
    Leg,
    Right,
    price_structure,
    size_position,
)

SESSION = date(2026, 9, 22)
EXPIRY = date(2026, 10, 9)


@pytest.fixture(scope="module")
def settings() -> OptionsAlphaSettings:
    return load_settings(pathlib.Path("profiles/options_alpha_v1.yaml"))


def _quote(symbol: str, strike: str, bid: str, ask: str) -> ContractQuote:
    return ContractQuote(
        option_symbol=symbol,
        underlying="SPY",
        right=Right.CALL,
        strike=Decimal(strike),
        expiry=EXPIRY,
        bid=Decimal(bid),
        ask=Decimal(ask),
        as_of=SESSION,
    )


def _spread_candidate(settings: OptionsAlphaSettings) -> Candidate:
    """The real SPY 775/776 spread this engine produced on 2026-09-22."""
    legs = (
        Leg(_quote("SPY261009C00775000", "775", "7.26", "7.29"), is_long=True),
        Leg(_quote("SPY261009C00776000", "776", "6.72", "6.74"), is_long=False),
    )
    kind, price = price_structure(legs, settings)
    return Candidate(
        kind=kind,
        legs=legs,
        price=price,
        sizing=size_position(price, settings),
        direction="up",
        chose_spread_because="test",
    )


def _build(settings: OptionsAlphaSettings, origin: DataOrigin) -> object:
    return build_card(
        _spread_candidate(settings),
        hypothesis_id="H00_pipeline_smoke",
        research_status=ResearchStatus.RESEARCH_ONLY,
        settings=settings,
        session=SESSION,
        underlying="SPY",
        trigger="test",
        counter_argument="test",
        data_origin=origin,
    )


def test_a_target_can_never_exceed_the_structures_own_ceiling(
    settings: OptionsAlphaSettings,
) -> None:
    """The first real card asked +100% of a 60.60 debit on a 39.40-max spread.

    A defined-risk structure has a payoff ceiling. A target above it cannot be
    reached even in the best case, so printing one tells the reader to wait for
    something that cannot happen. The target must be capped at the ceiling and
    the capping must be visible in the card's own wording.
    """
    card = _build(settings, DataOrigin.REPLAY)

    assert card.max_profit_usd == Decimal("39.40")
    assert card.structural_max_loss_usd == Decimal("60.60")
    assert card.target_usd == Decimal("39.40")
    assert card.target_usd <= card.max_profit_usd
    assert "sinirlandi" in card.target_meaning


def test_a_replay_card_is_never_offered_as_an_entry(settings: OptionsAlphaSettings) -> None:
    """Replay prices come from a closed session's end-of-day snapshot.

    Showing them as PAPER_ENTRY_READY would present an old card as "buy now",
    which is the presentation failure the scope explicitly forbids.
    """
    card = _build(settings, DataOrigin.REPLAY)

    assert card.data_origin is DataOrigin.REPLAY
    assert card.opportunity_status is OpportunityStatus.WATCH


def test_research_status_and_opportunity_status_are_independent(
    settings: OptionsAlphaSettings,
) -> None:
    """An unvalidated strategy may still produce an actionable card, and say so.

    Collapsing the two axes is how RESEARCH_ONLY work ends up looking validated.
    """
    card = _build(settings, DataOrigin.LIVE)

    assert card.research_status is ResearchStatus.RESEARCH_ONLY
    assert card.opportunity_status is OpportunityStatus.PAPER_ENTRY_READY


def test_the_same_structure_on_the_same_session_is_one_opportunity(
    settings: OptionsAlphaSettings,
) -> None:
    """Two hypotheses firing on one economic event must not read as two trades."""
    first = _build(settings, DataOrigin.REPLAY)
    second = build_card(
        _spread_candidate(settings),
        hypothesis_id="H99_other_hypothesis",
        research_status=ResearchStatus.RESEARCH_ONLY,
        settings=settings,
        session=SESSION,
        underlying="SPY",
        trigger="test",
        counter_argument="test",
        data_origin=DataOrigin.REPLAY,
    )

    assert first.opportunity_id == second.opportunity_id
    assert first.signal_id != second.signal_id


def test_every_leg_carries_the_identity_a_ticket_would_need(
    settings: OptionsAlphaSettings,
) -> None:
    card = _build(settings, DataOrigin.REPLAY)

    assert len(card.legs) == 2
    for leg in card.legs:
        assert leg.occ_symbol.startswith("SPY26")
        assert leg.multiplier == 100
        assert leg.expiry == EXPIRY
        assert leg.quote_as_of == SESSION
        assert leg.bid is not None and leg.ask is not None
    assert [leg.is_long for leg in card.legs] == [True, False]
