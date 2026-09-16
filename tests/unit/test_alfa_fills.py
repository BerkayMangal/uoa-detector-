"""Phase 5.2.C3: the ``alfa_fill`` table, its append-only repo and the slippage math.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §2 (columns,
append-only) and §5 (C3: slippage, the assumed quote, the count gate).

Pins:
  - the table carries exactly the §2 columns, under the ``alfa_`` prefix;
  - the repo exposes no update, delete, drop, reset or truncate path, and a
    second write appends rather than rewriting;
  - slippage on hand-computed fixtures, both sides, both signs, in dollars per
    contract and as % of mid — computed in ``Decimal`` so a fixture lands
    exactly on its expected value;
  - the assumed quote is read out of a REAL frozen ``TradabilityRead``, so a
    renamed chip field breaks this test instead of silently zeroing slippage;
  - its label is the card's own age line, never a claim about the NBBO at the
    fill;
  - the count gate at ``fills.min_n_for_stats`` in both directions: one fill
    below shows a count and no statistic at all, exactly at it the median and
    the interquartile range appear;
  - every frozen and generated Turkish string passes ``ensure_clean``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board import fills as fills_module
from webapp.board.cards import snapshot
from webapp.board.db import make_engine
from webapp.board.fills import (
    ENTRY,
    EXIT,
    FILL_COPY,
    SIDES,
    AlfaFill,
    AssumedQuote,
    Fill,
    FillRepo,
    assumed_quote_from_card,
    assumed_quote_text,
    contracts_text,
    ensure_fill_tables,
    recorded_text,
    side_for,
    side_summaries,
    signed_pct_text,
    signed_usd_text,
    slippage_pct_of_mid,
    slippage_stats,
    slippage_usd_per_contract,
    template_context,
    usd_text,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings
from webapp.board.tradability import QuoteView, assess_tradability

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO_ROOT / "profiles" / "board_v1.yaml")
_MIN_N = _SETTINGS.fills.min_n_for_stats  # D8: the gate is profile-driven, never a literal

_CONTRACT_COLUMNS = {
    "id",
    "card_id",
    "trade_id",
    "created_at",
    "side",
    "fill_price",
    "contracts",
    "assumed_ask",
    "assumed_bid",
    "assumed_mid",
    "quote_age_seconds_at_card",
}

# Any of these in a public name would be a way to destroy a recorded fill.
_DESTRUCTIVE_NAMES = ("update", "delete", "drop", "reset", "truncate", "purge", "wipe", "clear")

_T0 = datetime(2026, 9, 16, 14, 30, tzinfo=UTC)
# bid 0.90 / ask 1.10 → mid 1.00, so a five-cent miss is exactly $5 and exactly 5%.
_QUOTE = AssumedQuote(bid=0.90, ask=1.10, mid=1.00, age_seconds=41)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'fills.db'}")
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine: Engine) -> FillRepo:
    ticks = iter(_T0 + timedelta(seconds=i) for i in range(1000))
    return FillRepo(engine, clock=lambda: next(ticks))


def _write(repo: FillRepo, **overrides: object) -> str:
    fields: dict[str, object] = {
        "card_id": "card-1",
        "trade_id": "trade-1",
        "side": ENTRY,
        "fill_price": 1.15,
        "contracts": 2.0,
        "quote": _QUOTE,
    }
    fields.update(overrides)
    return repo.write_fill(**fields)  # type: ignore[arg-type]


def _fill(
    *, side: str = ENTRY, fill_price: float = 1.15, bid: float = 0.90, ask: float = 1.10,
    fill_id: str = "f", card_id: str = "card-1",
) -> Fill:
    return Fill(
        id=fill_id, card_id=card_id, trade_id=None, created_at=_T0, side=side,
        fill_price=fill_price, contracts=1.0, assumed_ask=ask, assumed_bid=bid,
        assumed_mid=(bid + ask) / 2, quote_age_seconds_at_card=41,
    )


# ---------------------------------------------------------------------------
# Table and repo (contract §2: append-only)
# ---------------------------------------------------------------------------


def test_table_has_exactly_the_contract_columns() -> None:
    assert AlfaFill.__tablename__ == "alfa_fill"
    assert set(AlfaFill.__table__.columns.keys()) == _CONTRACT_COLUMNS
    assert [c.name for c in AlfaFill.__table__.primary_key] == ["id"]
    nullable = {c.name for c in AlfaFill.__table__.columns if c.nullable}
    # Contract §2 marks only the trade link nullable: a fill with no assumed quote
    # is not a slippage record, and the route refuses to write one.
    assert nullable == {"trade_id"}


def test_ensure_fill_tables_is_idempotent(engine: Engine) -> None:
    ensure_fill_tables(engine)
    ensure_fill_tables(engine)  # checkfirst: never alters or drops what exists
    assert FillRepo(engine).list_fills() == ()


def test_repo_exposes_no_update_or_delete_path() -> None:
    public = {name for name in vars(FillRepo) if not name.startswith("_")}
    assert public == {"engine", "write_fill", "get_fill", "list_fills"}
    module_public = [name for name in vars(fills_module) if not name.startswith("_")]
    for name in list(public) + module_public:
        folded = name.lower()
        assert not any(bad in folded for bad in _DESTRUCTIVE_NAMES), name


def test_written_fill_round_trips(repo: FillRepo) -> None:
    fill_id = _write(repo)
    stored = repo.get_fill(fill_id)
    assert stored is not None
    assert stored.id == fill_id
    assert len(fill_id) == 32
    assert uuid.UUID(hex=fill_id).hex == fill_id
    assert stored.card_id == "card-1"
    assert stored.trade_id == "trade-1"
    assert stored.created_at == _T0
    assert stored.side == ENTRY
    assert stored.fill_price == 1.15
    assert stored.contracts == 2.0
    # The assumed quote is copied off the card snapshot, in full.
    assert (stored.assumed_bid, stored.assumed_ask, stored.assumed_mid) == (0.90, 1.10, 1.00)
    assert stored.quote_age_seconds_at_card == 41
    assert stored.quote == _QUOTE
    assert repo.get_fill("no-such-fill") is None


def test_a_second_fill_appends_rather_than_rewriting(repo: FillRepo) -> None:
    first = _write(repo, fill_price=1.15)
    second = _write(repo, fill_price=1.30)
    assert first != second
    stored = repo.list_fills()
    assert [f.id for f in stored] == [second, first]  # newest first
    assert [f.fill_price for f in stored] == [1.30, 1.15]


def test_write_fill_refuses_a_bad_side_id_or_size(repo: FillRepo) -> None:
    with pytest.raises(ValueError, match="unknown side"):
        _write(repo, side="entry")
    with pytest.raises(ValueError, match="must name the card"):
        _write(repo, card_id="")
    for bad in ({"fill_price": 0.0}, {"fill_price": -1.0}, {"contracts": 0.0}, {"contracts": -2.0}):
        with pytest.raises(ValueError, match="must both be positive"):
            _write(repo, **bad)
    assert repo.list_fills() == ()


def test_list_fills_filters_by_card_and_trade(repo: FillRepo) -> None:
    a = _write(repo, card_id="card-1", trade_id="trade-1")
    b = _write(repo, card_id="card-2", trade_id=None, side=EXIT)
    c = _write(repo, card_id="card-1", trade_id="trade-9")

    assert [f.id for f in repo.list_fills()] == [c, b, a]
    assert [f.id for f in repo.list_fills(card_id="card-1")] == [c, a]
    assert [f.id for f in repo.list_fills(card_id="card-2")] == [b]
    assert [f.id for f in repo.list_fills(trade_id="trade-1")] == [a]
    assert [f.id for f in repo.list_fills(limit=1)] == [c]
    stored_b = repo.get_fill(b)
    assert stored_b is not None
    assert stored_b.trade_id is None  # a logged card whose journal trade was never saved


def test_side_for_reads_only_the_two_stored_labels() -> None:
    assert SIDES == ("giriş", "çıkış")
    assert side_for("giriş") == ENTRY
    assert side_for("çıkış") == EXIT
    for unknown in ("entry", "exit", "GİRİŞ", "", "log"):
        assert side_for(unknown) is None


# ---------------------------------------------------------------------------
# Slippage on hand-computed fixtures (contract §5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "fill_price", "usd", "pct"),
    [
        # Entry: fill − assumed_ask. Paying 1.15 against a 1.10 ask costs 5c a share.
        (ENTRY, 1.15, 5.0, 5.0),
        (ENTRY, 1.10, 0.0, 0.0),      # filled exactly at the assumed ask
        (ENTRY, 1.05, -5.0, -5.0),    # better than the assumption: negative slippage
        # Exit: assumed_bid − fill. Selling at 0.85 against a 0.90 bid costs 5c a share.
        (EXIT, 0.85, 5.0, 5.0),
        (EXIT, 0.90, 0.0, 0.0),
        (EXIT, 0.95, -5.0, -5.0),
    ],
    ids=["entry worse", "entry at ask", "entry better",
         "exit worse", "exit at bid", "exit better"],
)
def test_slippage_both_sides_in_dollars_per_contract_and_pct_of_mid(
    side: str, fill_price: float, usd: float, pct: float,
) -> None:
    assert slippage_usd_per_contract(side, fill_price, _QUOTE) == usd
    assert slippage_pct_of_mid(side, fill_price, _QUOTE) == pct
    stored = _fill(side=side, fill_price=fill_price)
    assert stored.slippage_usd == usd
    assert stored.slippage_pct == pct


def test_slippage_is_a_hundred_dollars_per_point_of_premium() -> None:
    """One contract is 100 shares of premium — the multiplier the cost model uses."""
    assert slippage_usd_per_contract(ENTRY, 2.10, AssumedQuote(1.0, 2.0, 1.5, 30)) == 10.0
    assert slippage_usd_per_contract(EXIT, 0.90, AssumedQuote(1.0, 2.0, 1.5, 30)) == 10.0


def test_slippage_refuses_an_unknown_side() -> None:
    for unknown in ("entry", "exit", "giris"):
        with pytest.raises(ValueError, match="unknown side"):
            slippage_usd_per_contract(unknown, 1.15, _QUOTE)


def test_a_mid_that_is_not_positive_has_no_percentage() -> None:
    zero = AssumedQuote(bid=0.0, ask=0.0, mid=0.0, age_seconds=10)
    assert slippage_pct_of_mid(ENTRY, 0.05, zero) is None
    assert slippage_usd_per_contract(ENTRY, 0.05, zero) == 5.0


# ---------------------------------------------------------------------------
# The assumed quote comes from the card snapshot (contract §5)
# ---------------------------------------------------------------------------


def _chip_snapshot(bid: float | None, ask: float | None, *, age_seconds: int = 41) -> object:
    """A real ``TradabilityRead``, frozen the way a decision card freezes it."""
    now = datetime(2026, 9, 16, 14, 5, tzinfo=UTC)
    quote = (
        None
        if bid is None or ask is None
        else QuoteView(
            option_symbol="SPY260918C00760000", nbbo_bid=bid, nbbo_ask=ask, volume=500,
            last_tape_time=None, fetched_at=now - timedelta(seconds=age_seconds), returned=True,
        )
    )
    read = assess_tradability(
        "SPY", quote, None, tradability=_SETTINGS.tradability, spread_cutoff_pct=15.0,
        cost=_SETTINGS.cost, sizing=_SETTINGS.sizing, now=now,
    )
    return {"row_view": {"chip": snapshot(read)}}


def test_the_assumed_quote_is_read_out_of_a_real_frozen_chip() -> None:
    card = _chip_snapshot(0.90, 1.10)
    quote = assumed_quote_from_card(card)  # type: ignore[arg-type]
    assert quote == AssumedQuote(bid=0.90, ask=1.10, mid=1.00, age_seconds=41)
    assert quote.spread_pct == 20.0  # (1.10 − 0.90) / 1.00 × 100, the board's own formula


@pytest.mark.parametrize(
    "card",
    [
        None,
        {},
        {"row_view": {}},
        {"row_view": {"chip": {}}},
        {"row_view": {"chip": {"bid": 0.9, "ask": None, "quote_age_seconds": 41}}},
        {"row_view": {"chip": {"bid": None, "ask": 1.1, "quote_age_seconds": 41}}},
        {"row_view": {"chip": {"bid": 0.9, "ask": 1.1, "quote_age_seconds": None}}},
        {"row_view": {"chip": {"bid": True, "ask": True, "quote_age_seconds": 41}}},
        {"row_view": {"chip": {"bid": 0.0, "ask": 0.0, "quote_age_seconds": 41}}},
    ],
    ids=["no card", "empty", "no chip", "empty chip", "no ask", "no bid", "no age",
         "booleans are not prices", "mid is not positive"],
)
def test_a_card_without_a_usable_quote_yields_no_assumption(card: object) -> None:
    assert assumed_quote_from_card(card) is None  # type: ignore[arg-type]


def test_a_card_whose_contract_did_not_trade_yields_no_assumption() -> None:
    """A null NBBO reads ``kotasyon yok`` on the board; it is no basis for slippage."""
    assert assumed_quote_from_card(_chip_snapshot(None, None)) is None  # type: ignore[arg-type]


def test_the_quote_label_is_the_cards_age_never_the_nbbo_at_the_fill() -> None:
    assert assumed_quote_text(_QUOTE) == "kart anındaki kotasyon, 41 sn yaşında"
    assert _fill().assumed_quote_text == "kart anındaki kotasyon, 41 sn yaşında"
    # The only mention of the NBBO anywhere in the copy says it does not exist.
    mentions = [text for text in FILL_COPY.values() if "NBBO" in text]
    assert mentions == [FILL_COPY["assumed_quote_not_nbbo"]]
    assert mentions[0].startswith("Dolum anındaki NBBO hiçbir kaynakta yok")


# ---------------------------------------------------------------------------
# The count gate, at the boundary, in both directions (contract §5)
# ---------------------------------------------------------------------------


def _sample(n: int) -> list[Fill]:
    """``n`` entry fills whose slippage is 1, 2, 3 ... dollars per contract."""
    return [
        _fill(fill_price=round(1.10 + (i + 1) / 100, 2), bid=1.10, ask=1.10, fill_id=f"f{i}")
        for i in range(n)
    ]


def test_below_the_gate_only_the_count_is_available() -> None:
    stats = slippage_stats(_sample(_MIN_N - 1), min_n=_MIN_N)
    assert stats.n == _MIN_N - 1
    assert stats.enough is False
    assert (stats.median_usd, stats.q1_usd, stats.q3_usd) == (None, None, None)
    assert (stats.median_pct, stats.q1_pct, stats.q3_pct) == (None, None, None)
    assert (stats.iqr_usd, stats.iqr_pct) == (None, None)
    assert stats.counts_only_text == f"{_MIN_N - 1} dolum kaydı; istatistik için yetersiz örnek"


def test_at_the_gate_the_median_and_interquartile_range_appear() -> None:
    stats = slippage_stats(_sample(_MIN_N), min_n=_MIN_N)
    assert stats.n == _MIN_N
    assert stats.enough is True
    # Slippage runs 1, 2, ... n dollars, so the median is the midpoint of that run.
    assert stats.median_usd == (1 + _MIN_N) / 2
    assert stats.q1_usd is not None
    assert stats.q3_usd is not None
    assert stats.q1_usd < stats.median_usd < stats.q3_usd
    assert stats.iqr_usd == stats.q3_usd - stats.q1_usd
    assert stats.median_pct is not None  # mid is 1.10, so a cent is 0.909...%


def test_the_gate_is_hand_computable_on_a_four_fill_sample() -> None:
    sample = _sample(4)
    assert [f.slippage_usd for f in sample] == [1.0, 2.0, 3.0, 4.0]
    assert slippage_stats(sample, min_n=5).median_usd is None       # one short
    exact = slippage_stats(sample, min_n=4)                          # exactly at the gate
    assert (exact.q1_usd, exact.median_usd, exact.q3_usd) == (1.75, 2.5, 3.25)
    assert exact.iqr_usd == 1.5


def test_a_single_fill_at_a_gate_of_one_reports_no_spread_of_values() -> None:
    stats = slippage_stats(_sample(1), min_n=1)
    assert stats.median_usd == 1.0
    assert stats.iqr_usd == 0.0


def test_the_two_sides_are_never_pooled() -> None:
    entries = [_fill(side=ENTRY, fill_price=1.15, fill_id="e")] * 3
    exits = [_fill(side=EXIT, fill_price=0.85, fill_id="x")] * 2
    entry, exit_ = side_summaries([*entries, *exits], min_n=3)
    assert (entry.side, exit_.side) == (ENTRY, EXIT)
    assert (entry.stats.n, exit_.stats.n) == (3, 2)
    assert entry.stats.enough is True
    assert entry.stats.median_usd == 5.0
    assert exit_.stats.enough is False          # its own sample is short, on its own gate
    assert exit_.stats.median_usd is None
    assert entry.formula_text == FILL_COPY["entry_formula"]
    assert exit_.formula_text == FILL_COPY["exit_formula"]


def test_no_statistic_claims_significance() -> None:
    stats = slippage_stats(_sample(_MIN_N), min_n=_MIN_N)
    assert not hasattr(stats, "t_stat")
    assert not hasattr(stats, "p_value")
    for text in FILL_COPY.values():
        for claim in ("anlamlı", "t=", "olasılık", "kâr"):
            assert claim not in text or text == FILL_COPY["no_significance"], text


# ---------------------------------------------------------------------------
# Formatting and honesty (rule R-WD1)
# ---------------------------------------------------------------------------


def test_the_sign_survives_formatting() -> None:
    assert signed_usd_text(5.0) == "+$5.00"
    assert signed_usd_text(-5.0) == "-$5.00"
    assert signed_usd_text(0.0) == "+$0.00"
    assert signed_usd_text(None) == "bilinmiyor"
    assert signed_pct_text(5.0) == "+%5"
    assert signed_pct_text(-5.25) == "-%5.2"
    assert signed_pct_text(None) == "bilinmiyor"
    assert usd_text(1.5) == "$1.50"
    assert usd_text(None) == "bilinmiyor"
    assert contracts_text(3.0) == "3"
    assert contracts_text(2.5) == "2.5"


def test_every_frozen_and_generated_string_is_clean() -> None:
    for text in FILL_COPY.values():
        assert ensure_clean(text) == text
    generated = [
        assumed_quote_text(_QUOTE),
        recorded_text(_fill(), ticker="SPY"),
        slippage_stats(_sample(2), min_n=_MIN_N).counts_only_text,
        slippage_stats(_sample(2), min_n=_MIN_N).sample_text,
        *(summary.formula_text for summary in side_summaries(_sample(2), min_n=_MIN_N)),
    ]
    for text in generated:
        assert ensure_clean(text) == text


def test_the_recorded_line_names_the_fill_the_owner_entered() -> None:
    stored = _fill(side=EXIT, fill_price=0.85)
    assert recorded_text(stored, ticker="NVDA") == (
        "Dolum kaydedildi: NVDA çıkış · 1 kontrat · $0.85"
    )


def test_the_template_context_carries_only_frozen_copy_and_helpers() -> None:
    context = template_context()
    assert context["fill_copy"] is FILL_COPY
    assert context["fill_sides"] == SIDES
    for key in ("fill_usd_text", "fill_signed_usd_text", "fill_signed_pct_text",
                "fill_pct_text", "fill_contracts_text", "fill_assumed_quote_text"):
        assert callable(context[key]), key
    with pytest.raises(TypeError):
        FILL_COPY["form_title"] = "değişti"  # type: ignore[index]
