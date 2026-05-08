"""Phase 3.4.5.2 tests for ``SectorPeerStage`` (Module 25).

Pins (acceptance doc §3.4.5):
  Pure _score_from_peer_flow function:
    - empty events → empty_peer_flow (0.5)
    - call + 100% bullish peers → strong (1.0)
    - put + 100% bearish peers → strong (1.0)
    - call + 50% bullish 50% bearish → moderate
    - call + 30% bullish → weak
    - call + 0% bullish → contrarian (0.0)
    - all-neutral peers → empty_peer_flow
    - mixed neutral + bullish → neutral excluded from denominator
    - unknown option_type → empty_peer_flow with distinct branch
    - profile threshold operator override changes branch

  Stage async:
    - Idempotency-on-preset
    - Sector provider timeout → branch=timeout, source=sector_map
    - Peer flow provider timeout → branch=timeout, source=peer_flow
    - No peers (sector unknown) → no_sector branch
    - Small sector (3 peers, peer_count=5) → uses 3
    - Default constructor uses NoOp providers
    - End-to-end strong / contrarian / moderate
    - peers truncated to top peer_count

  Telemetry:
    - last_execution_metadata round-trips into StageExecutionEntry
    - alignment_pct surfaced for inspection
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from uoa_detector.calibration import load_default_profile
from uoa_detector.calibration.profile import M25Settings
from uoa_detector.domain.agreement import SourceAgreement
from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
from uoa_detector.pipeline.stage import PipelineContext
from uoa_detector.pipeline.stages.m25_sector_peer import (
    SectorPeerStage,
    _score_from_peer_flow,
)
from uoa_detector.providers.sector_map import (
    NoOpPeerFlowProvider,
    NoOpSectorMapProvider,
    PeerFlowEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    ticker: str = "AAPL",
    option_type: str = "call",
    timestamp: datetime | None = None,
    sector_confirmation_score: float | None = None,
) -> EnrichedEvent:
    if timestamp is None:
        timestamp = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
    op = OptionsPrint(
        event_id="e1",
        timestamp=timestamp,
        ticker=ticker,
        option_type=option_type,  # type: ignore[arg-type]
        strike=Decimal("150.00"),
        expiry=date(2024, 2, 16),
        dte=32,
        spot_price=Decimal("150.00"),
        premium_paid=Decimal("100000"),
        option_price=Decimal("1.50"),
        implied_volatility=0.25,
        bid=Decimal("1.45"),
        ask=Decimal("1.55"),
        fill_side="at_ask",
        exchange="CBOE",
        is_iso=False,
        open_interest=1000,
        source_agreement=SourceAgreement(
            sources_seen=("synthetic",),
            premium_disagreement=Decimal("0"),
            timestamp_skew_ms=0,
            classification_disagreement=False,
            confidence_tier="single",
        ),
    )
    e = EnrichedEvent(print=op)
    if sector_confirmation_score is not None:
        e.sector_confirmation_score = sector_confirmation_score
    return e


def _peer(direction: str, ticker: str = "MSFT") -> PeerFlowEvent:
    return PeerFlowEvent(  # type: ignore[arg-type]
        ticker=ticker,
        direction=direction,
        when=datetime(2024, 1, 15, 15, 15, tzinfo=UTC),
    )


class _StubSectorProvider:
    def __init__(
        self,
        *,
        peers: Sequence[str] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        self._peers = peers
        self._delay = delay_seconds
        self.peers_call_count = 0

    async def sector_of(self, ticker: str) -> str | None:
        del ticker
        return "Technology" if self._peers else None

    async def peers_of(self, ticker: str) -> Sequence[str]:
        del ticker
        self.peers_call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._peers


class _StubPeerFlowProvider:
    def __init__(
        self,
        *,
        events: Sequence[PeerFlowEvent] = (),
        delay_seconds: float = 0.0,
    ) -> None:
        self._events = events
        self._delay = delay_seconds
        self.recent_flow_call_count = 0
        self.last_tickers: list[str] | None = None

    async def recent_flow(
        self,
        tickers: Sequence[str],
        before: datetime,
        window: timedelta,
    ) -> Sequence[PeerFlowEvent]:
        del before, window
        self.recent_flow_call_count += 1
        self.last_tickers = list(tickers)
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._events


# ---------------------------------------------------------------------------
# Pure _score_from_peer_flow function
# ---------------------------------------------------------------------------


def test_score_empty_events_returns_empty_peer_flow() -> None:
    s = M25Settings()
    score, branch, alignment = _score_from_peer_flow(
        peer_events=(),
        option_type="call",
        settings=s,
    )
    assert score == 0.5
    assert branch == "empty_peer_flow"
    assert alignment is None


def test_score_call_full_bullish_peers_strong() -> None:
    """call signal + all peers bullish → 1.0."""
    s = M25Settings()
    events = [
        _peer("bullish", "MSFT"),
        _peer("bullish", "GOOGL"),
        _peer("bullish", "META"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    assert score == 1.0
    assert branch == "strong"
    assert alignment == 1.0


def test_score_put_full_bearish_peers_strong() -> None:
    s = M25Settings()
    events = [
        _peer("bearish", "MSFT"),
        _peer("bearish", "GOOGL"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="put",
        settings=s,
    )
    assert score == 1.0
    assert branch == "strong"
    assert alignment == 1.0


def test_score_call_50_50_moderate() -> None:
    """Half bullish half bearish → 0.5 alignment → moderate band."""
    s = M25Settings()  # moderate threshold = 0.4
    events = [
        _peer("bullish", "A"),
        _peer("bullish", "B"),
        _peer("bearish", "C"),
        _peer("bearish", "D"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    # 2/4 = 0.5; default thresholds: strong>0.6, moderate>=0.4, weak>=0.2
    # 0.5 is in [moderate, strong) → moderate
    assert score == 0.7
    assert branch == "moderate"
    assert alignment == 0.5


def test_score_call_30pct_bullish_weak() -> None:
    """3/10 bullish = 0.3 alignment → weak band."""
    s = M25Settings()  # weak threshold = 0.2
    events = (
        [_peer("bullish", f"P{i}") for i in range(3)]
        + [_peer("bearish", f"P{i}") for i in range(3, 10)]
    )
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    assert alignment == 0.3
    assert score == 0.3
    assert branch == "weak"


def test_score_call_no_bullish_contrarian() -> None:
    """All peers opposite → 0.0 alignment → contrarian."""
    s = M25Settings()
    events = [
        _peer("bearish", "A"),
        _peer("bearish", "B"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    assert score == 0.0
    assert branch == "contrarian"
    assert alignment == 0.0


def test_score_all_neutral_returns_empty_peer_flow() -> None:
    """All peer events neutral → empty_peer_flow with branch label."""
    s = M25Settings()
    events = [
        _peer("neutral", "A"),
        _peer("neutral", "B"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    assert score == 0.5
    assert branch == "all_neutral"
    assert alignment is None


def test_score_mixed_neutral_excluded_from_denominator() -> None:
    """Neutral events are excluded; alignment based on non-neutral only.

    3 events: 2 bullish, 1 neutral. For 'call':
      - non_neutral = 2 (both bullish)
      - same_direction = 2
      - alignment = 2/2 = 1.0 → strong (NOT 2/3 = 0.67 → moderate)
    """
    s = M25Settings()
    events = [
        _peer("bullish", "A"),
        _peer("bullish", "B"),
        _peer("neutral", "C"),
    ]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=s,
    )
    assert alignment == 1.0
    assert score == 1.0
    assert branch == "strong"


def test_score_unknown_option_type_empty_peer_flow_distinct_branch() -> None:
    """Defensive: unknown option_type → empty_peer_flow + unknown_type branch."""
    s = M25Settings()
    events = [_peer("bullish", "A")]
    score, branch, alignment = _score_from_peer_flow(
        peer_events=events,
        option_type="banana",
        settings=s,
    )
    assert score == 0.5
    assert branch == "unknown_option_type"
    assert alignment is None


def test_score_profile_threshold_operator_override() -> None:
    """Operator can pin tighter strong threshold."""
    # 5 events, 3 bullish → alignment 0.6
    events = (
        [_peer("bullish", f"A{i}") for i in range(3)]
        + [_peer("bearish", f"B{i}") for i in range(2)]
    )
    # Default strong > 0.6 → at exactly 0.6 → moderate (0.6 not > 0.6)
    _score, branch_default, _ = _score_from_peer_flow(
        peer_events=events, option_type="call", settings=M25Settings(),
    )
    assert branch_default == "moderate"
    # Tighter operator strong > 0.5 → 0.6 > 0.5 → strong
    _score, branch_op, _ = _score_from_peer_flow(
        peer_events=events,
        option_type="call",
        settings=M25Settings(strong_alignment_threshold=0.5),
    )
    assert branch_op == "strong"


# ---------------------------------------------------------------------------
# Stage — async behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_on_preset_skips_providers() -> None:
    sector = _StubSectorProvider(peers=("MSFT",))
    peer_flow = _StubPeerFlowProvider(events=(_peer("bullish"),))
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event(sector_confirmation_score=0.9)
    ctx = PipelineContext(profile=load_default_profile())
    result = await stage.enrich(event, ctx)
    assert result.sector_confirmation_score == 0.9
    assert sector.peers_call_count == 0
    assert peer_flow.recent_flow_call_count == 0
    assert stage.last_execution_metadata == {"branch": "preset_skip"}


@pytest.mark.asyncio
async def test_sector_provider_timeout_emits_neutral() -> None:
    profile = load_default_profile()
    new_m25 = profile.scoring.modules.m25.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m25": new_m25})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    sector = _StubSectorProvider(peers=("MSFT",), delay_seconds=0.5)
    peer_flow = _StubPeerFlowProvider()
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.sector_confirmation_score == 0.5
    assert stage.last_execution_metadata == {
        "branch": "timeout",
        "timeout_source": "sector_map",
    }


@pytest.mark.asyncio
async def test_peer_flow_provider_timeout_emits_neutral() -> None:
    profile = load_default_profile()
    new_m25 = profile.scoring.modules.m25.model_copy(
        update={"provider_timeout_s": 0.05},
    )
    new_modules = profile.scoring.modules.model_copy(update={"m25": new_m25})
    new_scoring = profile.scoring.model_copy(update={"modules": new_modules})
    new_profile = profile.model_copy(update={"scoring": new_scoring})

    sector = _StubSectorProvider(peers=("MSFT", "GOOGL"))
    peer_flow = _StubPeerFlowProvider(
        events=(_peer("bullish"),),
        delay_seconds=0.5,
    )
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event()
    ctx = PipelineContext(profile=new_profile)
    await stage.enrich(event, ctx)
    assert event.sector_confirmation_score == 0.5
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "timeout"
    assert stage.last_execution_metadata["timeout_source"] == "peer_flow"


@pytest.mark.asyncio
async def test_no_peers_returns_no_sector_branch() -> None:
    """SectorMap returns empty (illiquid / ETF / missing data)."""
    sector = _StubSectorProvider(peers=())
    peer_flow = _StubPeerFlowProvider()
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.sector_confirmation_score == 0.5
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_sector"
    # Peer flow should NOT be called when no peers
    assert peer_flow.recent_flow_call_count == 0


@pytest.mark.asyncio
async def test_small_sector_uses_what_is_available() -> None:
    """Sector with 3 peers + peer_count=5 → query 3."""
    sector = _StubSectorProvider(peers=("A", "B", "C"))
    peer_flow = _StubPeerFlowProvider(
        events=(_peer("bullish", "A"), _peer("bullish", "B"),
                _peer("bullish", "C")),
    )
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # Only 3 peers queried
    assert peer_flow.last_tickers == ["A", "B", "C"]
    assert event.sector_confirmation_score == 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["peers_queried"] == "3"


@pytest.mark.asyncio
async def test_peers_truncated_to_peer_count() -> None:
    """Sector with 10 peers + peer_count=5 → query first 5."""
    peers = tuple(f"P{i}" for i in range(10))
    sector = _StubSectorProvider(peers=peers)
    peer_flow = _StubPeerFlowProvider(events=())  # empty
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # First 5 peers passed to peer_flow
    assert peer_flow.last_tickers == ["P0", "P1", "P2", "P3", "P4"]


@pytest.mark.asyncio
async def test_default_constructor_uses_noop_providers() -> None:
    stage = SectorPeerStage()
    event = _make_event()
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    # NoOp sector → no peers → no_sector branch
    assert event.sector_confirmation_score == 0.5
    assert isinstance(stage._sector_provider, NoOpSectorMapProvider)
    assert isinstance(
        stage._peer_flow_provider, NoOpPeerFlowProvider,
    )
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "no_sector"


@pytest.mark.asyncio
async def test_end_to_end_strong_branch() -> None:
    sector = _StubSectorProvider(peers=("MSFT", "GOOGL", "META"))
    peer_flow = _StubPeerFlowProvider(events=(
        _peer("bullish", "MSFT"),
        _peer("bullish", "GOOGL"),
        _peer("bullish", "META"),
    ))
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.sector_confirmation_score == 1.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "strong"
    assert stage.last_execution_metadata["alignment_pct"] == "1.0000"


@pytest.mark.asyncio
async def test_end_to_end_contrarian_branch() -> None:
    sector = _StubSectorProvider(peers=("MSFT", "GOOGL"))
    peer_flow = _StubPeerFlowProvider(events=(
        _peer("bearish", "MSFT"),
        _peer("bearish", "GOOGL"),
    ))
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    assert event.sector_confirmation_score == 0.0
    assert stage.last_execution_metadata is not None
    assert stage.last_execution_metadata["branch"] == "contrarian"


# ---------------------------------------------------------------------------
# Telemetry round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_round_trips_via_orchestrator() -> None:
    from uoa_detector.observability.decision_record import StageExecutionEntry
    sector = _StubSectorProvider(peers=("MSFT", "GOOGL"))
    peer_flow = _StubPeerFlowProvider(events=(
        _peer("bullish", "MSFT"),
        _peer("bullish", "GOOGL"),
    ))
    stage = SectorPeerStage(
        sector_provider=sector, peer_flow_provider=peer_flow,
    )
    event = _make_event(option_type="call")
    ctx = PipelineContext(profile=load_default_profile())
    await stage.enrich(event, ctx)
    entry = StageExecutionEntry(
        stage_name=stage.name,
        latency_ms=2.5,
        metadata=stage.last_execution_metadata,
    )
    assert entry.metadata is not None
    assert entry.metadata["branch"] == "strong"
    assert entry.metadata["peers_queried"] == "2"
    assert entry.metadata["peer_events_returned"] == "2"
