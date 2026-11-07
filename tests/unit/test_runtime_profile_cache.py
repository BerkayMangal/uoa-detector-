"""Exact-byte profile caching preserves validation, clocks and immutable values."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import event
from webapp.board.db import make_engine
from webapp.board.quota_ledger import ensure_quota_tables
from webapp.live_alpha import store
from webapp.news_research import storage as news_storage
from webapp.shadow_capture import bindings, collector, config, storage, view

from tests.unit.test_news_adapter import NOW, scan, snapshot
from tests.unit.test_shadow_collector import Client, Clock
from uoa_detector.live_alpha import settings as live


@pytest.fixture(autouse=True)
def empty_cache():
    live._parse_live_alpha_settings.cache_clear()
    yield
    live._parse_live_alpha_settings.cache_clear()


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_bytes(live.DEFAULT_PROFILE.read_bytes())
    return path


def count_parses(monkeypatch):
    original = live.YAML
    calls = []

    def factory(*args, **kwargs):
        parser = original(*args, **kwargs)
        load = parser.load

        def counted(stream):
            calls.append(True)
            return load(stream)

        parser.load = counted
        return parser

    monkeypatch.setattr(live, "YAML", factory)
    return calls


@pytest.mark.parametrize("suffix", [b"", b"\n# Equivalent presentation bytes\n"])
def test_all_parsed_fields_match_the_original_uncached_construction(profile, suffix):
    raw = profile.read_bytes() + suffix
    profile.write_bytes(raw)
    old = live.LiveAlphaSettings(
        **YAML(typ="safe").load(io.BytesIO(raw)),
        profile_sha256=hashlib.sha256(raw).hexdigest(),
    )
    actual = live.load_live_alpha_settings(profile)
    assert actual.model_dump(mode="json") == old.model_dump(mode="json")
    assert actual.model_json_schema() == old.model_json_schema()


def test_every_public_call_reads_and_hashes_current_bytes_while_parsing_once(profile, monkeypatch):
    calls = count_parses(monkeypatch)
    original = Path.read_bytes
    reads = []

    def read(path):
        if path == profile:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    first = live.load_live_alpha_settings(profile)
    second = live.load_live_alpha_settings(str(profile))
    assert first is second
    assert len(reads) == 2 and len(calls) == 1
    assert first.profile_sha256 == hashlib.sha256(original(profile)).hexdigest()


def test_equivalent_yaml_bytes_have_different_raw_identity_without_changed_values(profile):
    raw = profile.read_bytes()
    original = live.load_live_alpha_settings(profile)
    profile.write_bytes(raw + b"\n# New exact bytes\n")
    changed = live.load_live_alpha_settings(profile)
    assert changed is not original and changed.profile_sha256 != original.profile_sha256
    assert changed.model_dump(exclude={"profile_sha256"}) == original.model_dump(
        exclude={"profile_sha256"}
    )


def test_changed_valid_file_is_seen_immediately_without_changing_frozen_source_profile(profile):
    raw = profile.read_bytes()
    original = live.load_live_alpha_settings(profile)
    profile.write_bytes(raw.replace(b"spot_max_age_seconds: 900", b"spot_max_age_seconds: 901"))
    changed = live.load_live_alpha_settings(profile)
    assert original.price.spot_max_age_seconds == 900
    assert changed.price.spot_max_age_seconds == 901
    assert original.profile_sha256 != changed.profile_sha256
    profile.write_bytes(raw)
    assert live.load_live_alpha_settings(profile) is original


@pytest.mark.parametrize(
    ("replacement", "error"),
    [(b"- not a mapping\n", ValueError), (b"key: [\n", YAMLError), (b"", ValueError)],
)
def test_invalid_new_bytes_never_fall_back_to_an_earlier_valid_cache(profile, replacement, error):
    live.load_live_alpha_settings(profile)
    profile.write_bytes(replacement)
    with pytest.raises(error):
        live.load_live_alpha_settings(profile)


def test_unknown_keys_and_existing_range_validator_still_reject_changes(profile):
    raw = profile.read_bytes()
    live.load_live_alpha_settings(profile)
    profile.write_bytes(raw + b"\nunknown_profile_field: true\n")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        live.load_live_alpha_settings(profile)
    profile.write_bytes(raw.replace(b"min_dte: 14", b"min_dte: 46"))
    with pytest.raises(
        ValidationError, match=r"option_plan.min_dte cannot exceed option_plan.max_dte"
    ):
        live.load_live_alpha_settings(profile)


def test_a_removed_file_does_not_reuse_its_previously_cached_contents(profile):
    live.load_live_alpha_settings(profile)
    profile.unlink()
    with pytest.raises(FileNotFoundError):
        live.load_live_alpha_settings(profile)


def test_cached_nested_settings_and_calendar_values_remain_immutable(profile):
    settings = live.load_live_alpha_settings(profile)
    with pytest.raises(ValidationError, match="frozen_instance"):
        settings.stock_plan.r_usd = 1
    with pytest.raises(ValidationError, match="frozen_instance"):
        settings.profile_sha256 = "altered"
    with pytest.raises(TypeError):
        settings.calendar.holidays[0] = settings.calendar.holidays[0]
    copied = settings.model_copy(
        update={"stock_plan": settings.stock_plan.model_copy(update={"r_usd": 1})}
    )
    assert copied.stock_plan.r_usd == 1
    assert live.load_live_alpha_settings(profile).stock_plan.r_usd == 100


def test_cold_concurrent_requests_coalesce_one_parse_without_sharing_yaml_state(
    profile, monkeypatch
):
    calls = count_parses(monkeypatch)
    start = threading.Barrier(8)

    def load(_):
        start.wait(timeout=10)
        return live.load_live_alpha_settings(profile)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(load, range(8)))
    assert all(row is results[0] for row in results)
    assert len(calls) == 1
    assert live._parse_live_alpha_settings.cache_info().currsize == 1


def test_cache_is_bounded_to_eight_exact_byte_path_entries(profile, tmp_path):
    raw = profile.read_bytes()
    paths = []
    for number in range(12):
        path = tmp_path / f"copy-{number}.yaml"
        path.write_bytes(raw)
        live.load_live_alpha_settings(path)
        paths.append(path)
    before = live._parse_live_alpha_settings.cache_info()
    assert before.maxsize == before.currsize == 8
    live.load_live_alpha_settings(paths[0])
    after = live._parse_live_alpha_settings.cache_info()
    assert after.currsize == 8 and after.misses == before.misses + 1


def test_invalid_mapping_message_keeps_the_requested_file_path(profile):
    profile.write_text("- list\n")
    with pytest.raises(ValueError, match=f"{profile}: profile is not a mapping"):
        live.load_live_alpha_settings(profile)


def test_four_completed_source_candidates_have_identical_cached_and_uncached_reports(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SHADOW_CAPTURE_ENABLED", "true")
    engine = make_engine(f"sqlite:///{tmp_path / 'four-source-candidates.db'}")
    try:
        store.ensure_live_tables(engine)
        news_storage.ensure(engine)
        storage.ensure(engine)
        ensure_quota_tables(engine)
        storage.activate(engine, lambda: NOW - timedelta(hours=1))
        data = snapshot()
        tickers = ("NVDA", "AMD", "MSFT", "AAPL")
        for ticker in tickers:
            data["flow_observations"][ticker] = copy.deepcopy(data["flow_observations"]["NVDA"])
            data["news_observations"][ticker] = copy.deepcopy(data["news_observations"]["NVDA"])
            data["news_research_context"]["prices"][ticker] = copy.deepcopy(
                data["news_research_context"]["prices"]["NVDA"]
            )
            news_storage.append(
                engine,
                ticker,
                {
                    "data": [
                        {
                            "headline": "Synthetic company wins contract",
                            "source": "FixtureWire",
                            "tickers": [ticker],
                            "created_at": (NOW - timedelta(hours=1)).isoformat(),
                        }
                    ]
                },
                NOW - timedelta(seconds=30),
                NOW - timedelta(seconds=20),
                clock=lambda: NOW - timedelta(seconds=10),
                application_checked_at=NOW - timedelta(seconds=30),
            )
        scan(engine, data=data)
        clock = Clock(NOW + timedelta(seconds=2))
        assert bindings.discover(engine, clock(), clock=clock)["admitted"] == 4
        client = Client(clock)
        client.prices = {ticker: 100.0 for ticker in tickers} | {"SPY": 500.0}
        for second in (64, 966, 3666):
            clock.at = NOW + timedelta(seconds=second)
            asyncio.run(collector.tick(engine, client, clock(), clock=clock))
        assert len(client.calls) == 24
        identity = config.identity()
        writes = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _c, _cu, sql, *_a: (
                writes.append(sql)
                if sql.lstrip().upper().startswith(("CREATE", "INSERT", "UPDATE", "DELETE"))
                else None
            ),
        )
        calls = count_parses(monkeypatch)
        live._parse_live_alpha_settings.cache_clear()
        cached = view.build(engine, clock())
        assert len(calls) == 1
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(
                live, "_parse_live_alpha_settings", live._parse_live_alpha_settings.__wrapped__
            )
            uncached = view.build(engine, clock())
        assert len(calls) >= 600
        assert asdict(cached) == asdict(uncached)
        assert cached.candidate_count == 4 and all(c.observed == 4 for c in cached.coverage)
        assert config.identity() == identity and not writes and len(client.calls) == 24
    finally:
        collector._HALTS.clear()
        collector._COOLDOWNS.clear()
        engine.dispose()
