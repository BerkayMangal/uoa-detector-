"""Phase 3.6.3 — fusion wiring for the self-derived GEX axis.

The fusion cells' dealer-gamma stage (M21) must be fed by the
ThetaData-derived provider when chain snapshots are supplied, with every
other axis left on its NoOp default.
"""

from __future__ import annotations

from pathlib import Path

from uoa_detector.backtest.cell_runner import (
    _replay_stages,
    fusion_stages_with_thetadata,
)
from uoa_detector.pipeline.stages import (
    DealerGammaStage,
    IVExhaustionStage,
    OpeningClosingStage,
    PriceConfirmationStage,
)
from uoa_detector.sources.thetadata_derived.dealer_gamma import (
    ThetaDataDealerPositioningProvider,
)
from uoa_detector.sources.thetadata_derived.iv_oi import (
    ThetaDataIVHistoryProvider,
    ThetaDataOpenInterestProvider,
)
from uoa_detector.sources.thetadata_derived.price_action import (
    ThetaDataPriceActionProvider,
)


def _m21(stages: list[object]) -> DealerGammaStage:
    return next(s for s in stages if isinstance(s, DealerGammaStage))


def test_fusion_stages_with_thetadata_wires_m21(tmp_path: Path) -> None:
    stages = fusion_stages_with_thetadata(tmp_path)
    m21 = _m21(list(stages))
    assert isinstance(m21._provider, ThetaDataDealerPositioningProvider)


def test_fusion_stages_with_thetadata_wires_iv_and_oi(tmp_path: Path) -> None:
    stages = list(fusion_stages_with_thetadata(tmp_path))
    m24 = next(s for s in stages if isinstance(s, IVExhaustionStage))
    m27 = next(s for s in stages if isinstance(s, OpeningClosingStage))
    assert isinstance(m24._iv_provider, ThetaDataIVHistoryProvider)
    assert isinstance(m27._provider, ThetaDataOpenInterestProvider)


def test_price_axis_wired_only_with_spot_series(tmp_path: Path) -> None:
    # Without spot_series_dir, M23 stays on its NoOp default.
    without = list(fusion_stages_with_thetadata(tmp_path))
    m23 = next(s for s in without if isinstance(s, PriceConfirmationStage))
    assert not isinstance(m23._provider, ThetaDataPriceActionProvider)
    # With spot_series_dir, M23 is fed by the self-derived provider.
    with_spot = list(fusion_stages_with_thetadata(tmp_path, tmp_path))
    m23b = next(s for s in with_spot if isinstance(s, PriceConfirmationStage))
    assert isinstance(m23b._provider, ThetaDataPriceActionProvider)


def test_replay_stages_fusion_routes_to_gex_when_snapshots_given(
    tmp_path: Path,
) -> None:
    stages = _replay_stages("fusion", chain_snapshots_dir=tmp_path)
    m21 = _m21(list(stages))
    assert isinstance(m21._provider, ThetaDataDealerPositioningProvider)


def test_replay_stages_fusion_noop_without_snapshots_or_uw() -> None:
    # No snapshots, no UW → default NoOp M21 (not the GEX provider).
    stages = _replay_stages("fusion")
    m21 = _m21(list(stages))
    assert not isinstance(m21._provider, ThetaDataDealerPositioningProvider)


def test_replay_stages_single_has_no_dealer_gamma_stage() -> None:
    # Single (core-flow) cells don't run the dealer-gamma axis at all.
    stages = _replay_stages("single", chain_snapshots_dir=Path("/unused"))
    assert not any(isinstance(s, DealerGammaStage) for s in stages)
