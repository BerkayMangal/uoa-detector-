"""Stored summaries stay SELECT-only, version separated and honest about missingness."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import event
from webapp import news_pilot_view as view
from webapp.board.db import make_engine
from webapp.news_research import config

NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)


def arm(key: str) -> dict[str, object]:
    return dict(
        arm=key,
        selected=4,
        ineligible=0,
        unknown=0,
        priced=0,
        complete_days=0,
        incomplete_days=1,
        net_complete_days=None,
        excess_complete_days=None,
    )


def envelope(day: str = "2026-11-05", state: str = "completed") -> dict[str, object]:
    summary = dict(
        day=day,
        source_identity="a" * 64,
        profile_hash=config.profile_hash(),
        implementation_hash=config.implementation_hash(),
        source_scans=143,
        source_characters=21_000_000,
        source_character_budget=22_020_096,
        new_context_scans=143,
        legacy_scans=0,
        opportunity_rows=1825,
        base_true=7,
        base_unknown=3,
        roster_slots=4,
        price_rows=4000,
        stored_prices=4000,
        complete_common_sessions=0,
        eb_paired_days=0,
        unit_missing={"entry_missing_observed_price": 1, "exit_missing_observed_price": 3},
        audit_missing={"price_context_unavailable": 5},
        arms=[arm(k) for k in "ABCDE"],
        stresses=[
            dict(
                slippage_bps_per_side=bp, complete_common_sessions=0, arms=[arm(k) for k in "ABCDE"]
            )
            for bp in (10, 20, 50)
        ],
        inference="NOT_PERFORMED",
        winner=None,
    )
    return dict(
        day=day,
        status=state,
        recorded_at=NOW.isoformat(),
        summary=summary if state == "completed" else None,
        reason=None,
        key=hashlib.sha256(day.encode()).hexdigest(),
        profile_hash=config.profile_hash(),
        implementation_hash=config.implementation_hash(),
    )


def project(monkeypatch, records):
    seen = []

    def read(_engine, now, limit):
        assert now == NOW and limit == view.DISPLAY_LIMIT + 1
        seen.append(True)
        return records

    monkeypatch.setattr(view, "_read", read)
    result = view.build(None, NOW)
    assert seen == [True]
    return result


def render(result):
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "webapp/templates"),
        autoescape=select_autoescape(("html",)),
    )
    return env.get_template("_news_pilot.html").render(news_pilot=result)


def test_real_absent_storage_is_unavailable_and_never_initialized_on_read(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'absent.db'}")
    writes = []

    def sql(_c, _cursor, statement, *_args):
        if (
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))
        ):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", sql)
    result = view.build(engine, NOW)
    assert result.status == "STORAGE_UNAVAILABLE" and not result.rows
    assert result.source_scans is None and result.complete_common_sessions is None
    assert writes == [] and "sıfır getiri" in render(result)
    engine.dispose()


def test_empty_current_storage_has_no_invented_return_or_source_counts(monkeypatch):
    result = project(monkeypatch, [])
    assert result.status == "EMPTY" and result.completed_days == 0
    assert result.source_scans is None and result.roster_slots is None
    assert result.inference == "NOT_PERFORMED" and result.winner is None
    assert "getiri bilinmiyor" in render(result)


def test_two_days_keep_all_arms_costs_missing_values_and_newest_order(monkeypatch):
    result = project(monkeypatch, [envelope("2026-11-04"), envelope()])
    assert result.completed_days == 2 and result.source_scans == 286
    assert result.opportunity_rows == 3650 and result.roster_slots == 8
    assert result.complete_common_sessions == 0 and result.eb_paired_days == 0
    assert [r.day for r in result.rows] == [date(2026, 11, 5), date(2026, 11, 4)]
    assert all([a.key for a in r.arms] == list("ABCDE") for r in result.rows)
    assert all(a.net is None and a.excess is None for r in result.rows for a in r.arms)
    assert len(result.rows[0].stresses) == 3
    html = render(result)
    assert "286" in html and "Karşılaştırmalı getiri henüz ölçülemedi" in html
    assert (
        "Giriş fiyatı zamanında gözlenmedi" in html and "Çıkış fiyatı zamanında gözlenmedi" in html
    )
    assert all(f"{k} · {label}" in html for k, label in view.ARM_LABELS.items())
    assert all(f"{bp} bp / yön" in html for bp in (10, 20, 50))
    assert "ölçülmedi" in html and "+0.00" not in html


def test_failed_and_pending_do_not_become_completed_zero_days(monkeypatch):
    pending, failed = envelope(state="pending"), envelope("2026-11-04", "failed")
    failed["reason"] = "source_limit_exceeded"
    result = project(monkeypatch, [pending, failed])
    assert result.completed_days == 0 and result.source_scans is None
    assert result.rows[0].status == "pending" and result.rows[1].status == "failed"
    assert result.rows[0].complete_common_sessions is None and not result.rows[1].arms
    html = render(result)
    assert "Hesap bekliyor" in html and "Hesap tamamlanamadı" in html
    assert "Kaynak sınırı aşıldı" in html and "Net $" not in html


def test_other_versions_are_excluded_without_pooling_sources(monkeypatch):
    old, other = envelope("2026-11-04"), envelope("2026-11-03")
    old["profile_hash"] = "old"
    other["implementation_hash"] = "old"
    result = project(monkeypatch, [old, envelope(), other])
    assert result.ignored_versions == 2 and result.completed_days == 1
    assert result.source_scans == 143 and result.opportunity_rows == 1825
    assert "2 farklı profil veya kod sürümü" in render(result)


def test_read_limit_is_disclosed_and_totals_cover_only_displayed_records(monkeypatch):
    records = [envelope((date(2026, 11, 5) - timedelta(days=i)).isoformat()) for i in range(11)]
    result = project(monkeypatch, records)
    assert result.limited and len(result.rows) == 10
    assert result.source_scans == 1430 and result.completed_days == 10
    assert "tüm arşivin toplamı değildir" in render(result)


def test_latest_record_for_same_day_does_not_duplicate_counts(monkeypatch):
    old, new = envelope(state="pending"), envelope()
    old["recorded_at"] = (NOW - timedelta(seconds=1)).isoformat()
    result = project(monkeypatch, [old, new])
    assert result.completed_days == 1 and result.source_scans == 143 and len(result.rows) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "winner",
        "inference",
        "count",
        "bool",
        "missing_arm",
        "bad_source",
        "future",
        "naive",
        "summary_profile",
        "secret_key",
    ],
)
def test_unverified_records_never_supply_metrics(monkeypatch, damage):
    item = envelope()
    summary = item["summary"]
    if damage == "winner":
        summary["winner"] = "C"
    elif damage == "inference":
        summary["inference"] = "VALIDATED"
    elif damage == "count":
        summary["source_scans"] = -1
    elif damage == "bool":
        summary["roster_slots"] = True
    elif damage == "missing_arm":
        summary["arms"].pop()
    elif damage == "bad_source":
        summary["source_identity"] = "bad"
    elif damage == "future":
        item["recorded_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif damage == "naive":
        item["recorded_at"] = "2026-11-06T10:00:00"
    elif damage == "summary_profile":
        summary["profile_hash"] = "other"
    else:
        item["key"] = "PRIVATE_SQL_SECRET"
    result = project(monkeypatch, [item])
    assert result.invalid_records == 1 and result.source_scans is None and not result.rows
    assert "PRIVATE_SQL_SECRET" not in render(result)


def test_eb_pair_completeness_and_known_cash_zero_are_not_all_arm_completeness(monkeypatch):
    item = envelope()
    item["summary"]["eb_paired_days"] = 1
    for a in item["summary"]["arms"]:
        if a["arm"] in ("B", "E"):
            a.update(
                selected=0,
                ineligible=4,
                complete_days=1,
                incomplete_days=0,
                net_complete_days=0.0,
                excess_complete_days=0.0,
            )
    result = project(monkeypatch, [item])
    assert result.complete_common_sessions == 0 and result.eb_paired_days == 1
    assert result.rows[0].arms[1].net == 0 and result.rows[0].arms[0].net is None
    assert "ölçülmedi" in render(result) and "+0.00" in render(result)


def test_storage_failure_hides_exception_details_and_requires_aware_clock(monkeypatch):
    def fail(*_a, **_k):
        raise RuntimeError("PASSWORD PRIVATE SQL")

    monkeypatch.setattr(view, "_read", fail)
    result = view.build(None, NOW)
    assert result.status == "STORAGE_UNAVAILABLE" and "PASSWORD" not in render(result)
    with pytest.raises(ValueError, match="aware"):
        view.build(None, NOW.replace(tzinfo=None))


def test_record_time_display_uses_london_and_is_not_source_publication_clock(monkeypatch):
    item = envelope()
    item["recorded_at"] = "2026-06-10T12:00:00+00:00"
    result = project(monkeypatch, [item])
    assert result.rows[0].recorded_at.hour == 13
    html = render(result)
    assert "13:00 Londra" in html and "haberin ilk yayın saati değildir" in html


def test_actual_registered_seed_reads_and_renders_without_get_writes(tmp_path):
    from webapp.news_pilot import service, storage

    engine = make_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    storage.ensure(engine)
    assert service.import_seed(engine, NOW) == 2
    writes = []

    def sql(_c, _cursor, statement, *_args):
        if (
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"))
        ):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", sql)
    result = view.build(engine, NOW)
    assert result.completed_days == 2 and result.source_scans == 286
    assert result.opportunity_rows == 3651 and result.roster_slots == 8
    assert result.complete_common_sessions == 0 and result.eb_paired_days == 0
    assert (
        sum(
            m.count
            for row in result.rows
            for m in row.unit_missing
            if m.code == "entry_missing_observed_price"
        )
        == 3
    )
    assert (
        sum(
            m.count
            for row in result.rows
            for m in row.unit_missing
            if m.code == "exit_missing_observed_price"
        )
        == 5
    )
    assert not writes and "3651" in render(result)
    assert all(a.priced == 0 for row in result.rows for a in row.arms)
    engine.dispose()


def test_real_initialized_empty_namespace_is_empty_not_unavailable(tmp_path):
    from webapp.news_pilot import storage

    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    storage.ensure(engine)
    result = view.build(engine, NOW)
    assert result.status == "EMPTY" and result.completed_days == 0
    assert result.source_scans is None and result.complete_common_sessions is None
    engine.dispose()
