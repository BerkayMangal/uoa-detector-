"""Phase 3.4.8.3 tests for scripts/run_m28_overnight.py CLI.

Pins:
  Argument parser:
    - run-selector mutually exclusive (--run-id, --since-yesterday,
      --since)
    - storage mutually exclusive (--store-url, --in-memory)
    - all flags parse correctly

  _select_runs:
    - --run-id → returns [that id]
    - --since-yesterday → filters by finished_at within 24h
    - --since ISO → filters by cutoff
    - --max-runs N → caps the list

  _build_provider:
    - --dry-run → NoOpOpenInterestProvider, client=None
    - no UW key + non-dry-run → RuntimeError

  _format_stats / _stats_as_dict:
    - human format includes all branch counts
    - JSON format is dict-shaped

  _main_async (in-memory + dry-run):
    - empty store + --since-yesterday → exit 3 (no runs)
    - populated store + --run-id existing → exit 0
    - populated + --dry-run → no provider calls

  Integration with M28Validator (in-memory path):
    - end-to-end dry-run skips validation entirely
"""

from __future__ import annotations

import argparse

# CLI under test — import via importlib to avoid pkg-init side effects
import importlib.util
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "run_m28_overnight.py"
)


@pytest.fixture(scope="module")
def cli_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "run_m28_overnight", _SCRIPT_PATH,
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def test_parser_requires_run_selector(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])  # no selector


def test_parser_requires_storage(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["--run-id", "r1"])  # missing store


def test_parser_run_id_with_in_memory(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    args = p.parse_args(["--run-id", "r1", "--in-memory"])
    assert args.run_id == "r1"
    assert args.in_memory is True


def test_parser_selectors_mutually_exclusive(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args([
            "--run-id", "r1", "--since-yesterday", "--in-memory",
        ])


def test_parser_storage_mutually_exclusive(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    with pytest.raises(SystemExit):
        p.parse_args([
            "--run-id", "r1",
            "--store-url", "sqlite:///x.db",
            "--in-memory",
        ])


def test_parser_dry_run_flag(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    args = p.parse_args([
        "--run-id", "r1", "--in-memory", "--dry-run",
    ])
    assert args.dry_run is True


def test_parser_json_output_flag(cli_module) -> None:  # type: ignore[no-untyped-def]
    p = cli_module._build_arg_parser()
    args = p.parse_args([
        "--run-id", "r1", "--in-memory", "--json-output",
    ])
    assert args.json_output is True


# ---------------------------------------------------------------------------
# _build_provider
# ---------------------------------------------------------------------------


def test_build_provider_dry_run_returns_noop(cli_module) -> None:  # type: ignore[no-untyped-def]
    from uoa_detector.providers.open_interest import NoOpOpenInterestProvider
    provider, client = cli_module._build_provider(dry_run=True)
    assert isinstance(provider, NoOpOpenInterestProvider)
    assert client is None


def test_build_provider_no_key_no_dry_run_raises(  # type: ignore[no-untyped-def]
    cli_module, monkeypatch,
) -> None:
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="UNUSUAL_WHALES_API_KEY"):
        cli_module._build_provider(dry_run=False)


# ---------------------------------------------------------------------------
# _format_stats + _stats_as_dict
# ---------------------------------------------------------------------------


def test_format_stats_includes_all_branches(cli_module) -> None:  # type: ignore[no-untyped-def]
    from uoa_detector.pipeline.validators import ValidationStats
    s = ValidationStats(
        run_id="r1", total_signals=10,
        confirmed=5, ambiguous=2, closing=1,
        skipped_below_threshold=1, skipped_no_m27_score=1,
        errors=0, pending_no_data=0,
    )
    text = cli_module._format_stats(s)
    assert "run_id=r1" in text
    assert "total=10" in text
    assert "validated=8" in text
    assert "confirmed=5" in text


def test_stats_as_dict_round_trip(cli_module) -> None:  # type: ignore[no-untyped-def]
    from uoa_detector.pipeline.validators import ValidationStats
    s = ValidationStats(run_id="r1", total_signals=3)
    d = cli_module._stats_as_dict(s)
    assert d["run_id"] == "r1"
    assert d["total_signals"] == 3


# ---------------------------------------------------------------------------
# _select_runs
# ---------------------------------------------------------------------------


def test_select_runs_explicit_run_id(cli_module) -> None:  # type: ignore[no-untyped-def]
    from uoa_detector.backtest.store import BacktestStore
    args = argparse.Namespace(
        run_id="abc",
        since_yesterday=False,
        since=None,
        max_runs=None,
    )
    store = BacktestStore()
    selected = cli_module._select_runs(store, args)
    assert selected == ["abc"]
    store.close()


def test_select_runs_since_yesterday_filters_old_runs(  # type: ignore[no-untyped-def]
    cli_module,
) -> None:
    from uoa_detector.backtest.store import BacktestStore
    profile_module = "uoa_detector.calibration"
    import importlib
    profile_mod = importlib.import_module(profile_module)
    profile = profile_mod.load_default_profile()
    store = BacktestStore()
    rid_old = store.start_run(profile=profile)
    store.finish_run()
    # Manually backdate the run's finished_at to 2 days ago
    old_meta = store._runs[rid_old]
    store._runs[rid_old] = old_meta.model_copy(
        update={"finished_at": datetime.now(UTC) - timedelta(days=2)},
    )
    rid_new = store.start_run(profile=profile)
    store.finish_run()
    args = argparse.Namespace(
        run_id=None,
        since_yesterday=True,
        since=None,
        max_runs=None,
    )
    selected = cli_module._select_runs(store, args)
    assert rid_new in selected
    assert rid_old not in selected
    store.close()


def test_select_runs_max_runs_caps(cli_module) -> None:  # type: ignore[no-untyped-def]
    import importlib

    from uoa_detector.backtest.store import BacktestStore
    profile_mod = importlib.import_module("uoa_detector.calibration")
    profile = profile_mod.load_default_profile()
    store = BacktestStore()
    for _ in range(3):
        store.start_run(profile=profile)
        store.finish_run()
    args = argparse.Namespace(
        run_id=None,
        since_yesterday=True,
        since=None,
        max_runs=2,
    )
    selected = cli_module._select_runs(store, args)
    assert len(selected) == 2
    store.close()


# ---------------------------------------------------------------------------
# _main_async (end-to-end via main entry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_empty_store_returns_3(cli_module) -> None:  # type: ignore[no-untyped-def]
    args = cli_module._build_arg_parser().parse_args([
        "--since-yesterday", "--in-memory", "--dry-run",
    ])
    rc = await cli_module._main_async(args)
    assert rc == 3


@pytest.mark.asyncio
async def test_main_populated_store_dry_run_returns_0(  # type: ignore[no-untyped-def]
    cli_module, capsys,
) -> None:
    """Populate a real SQLite store, then run dry-run mode."""
    from uoa_detector.backtest.sqlite_store import SqliteBacktestStore
    from uoa_detector.calibration import load_default_profile
    from uoa_detector.domain.agreement import SourceAgreement
    from uoa_detector.domain.events import EnrichedEvent, OptionsPrint
    from uoa_detector.domain.labels import LabelDecision, SignalLabel
    from uoa_detector.domain.risk import PositionSize, RiskBucket

    profile = load_default_profile()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        url = f"sqlite:///{db_path}"
        store = SqliteBacktestStore(database_url=url)
        rid = store.start_run(profile=profile)
        op = OptionsPrint(
            event_id="e1",
            timestamp=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
            ticker="AAPL", option_type="call",
            strike=Decimal("150.00"), expiry=date(2024, 2, 16),
            dte=32, spot_price=Decimal("150.00"),
            premium_paid=Decimal("100000"),
            option_price=Decimal("1.50"),
            implied_volatility=0.25,
            bid=Decimal("1.45"), ask=Decimal("1.55"),
            fill_side="at_ask", exchange="CBOE",
            is_iso=False, open_interest=1000,
            source_agreement=SourceAgreement(
                sources_seen=("synthetic",),
                premium_disagreement=Decimal("0"),
                timestamp_skew_ms=0,
                classification_disagreement=False,
                confidence_tier="single",
            ),
        )
        event = EnrichedEvent(print=op)
        event.opening_closing_score = 0.7
        decision = LabelDecision(
            label=SignalLabel.STANDARD_UOA, reason="t",
        )
        size = PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5)
        store.add(event, decision, size)
        store.finish_run()
        store.close()

        args = cli_module._build_arg_parser().parse_args([
            "--run-id", rid, "--store-url", url, "--dry-run",
        ])
        rc = await cli_module._main_async(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert rid in captured.out


@pytest.mark.asyncio
async def test_main_no_uw_key_non_dry_run_returns_2(  # type: ignore[no-untyped-def]
    cli_module, monkeypatch,
) -> None:
    """No UW key + actual validation requested → exit 2."""
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    args = cli_module._build_arg_parser().parse_args([
        "--run-id", "r1", "--in-memory",
    ])
    rc = await cli_module._main_async(args)
    assert rc == 2


def test_format_stats_zero_signals_clean_output(cli_module) -> None:  # type: ignore[no-untyped-def]
    """Edge case: empty run formats without crashing."""
    from uoa_detector.pipeline.validators import ValidationStats
    s = ValidationStats(run_id="r1")
    text = cli_module._format_stats(s)
    assert "run_id=r1" in text
    assert "total=0" in text


def test_main_returns_int_exit_code(cli_module) -> None:  # type: ignore[no-untyped-def]
    """main() returns an int (used as sys.exit code)."""
    rc = cli_module.main([
        "--since-yesterday", "--in-memory", "--dry-run",
    ])
    assert isinstance(rc, int)


# Suppress unused imports
_ = os
