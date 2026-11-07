"""The new fusion section reports actual views without making unknowns profitable."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from webapp.board.db import make_engine
from webapp.fundamental_context import view as fundamental
from webapp.fundamental_context.model import Facts, Filing, Metric
from webapp.fusion_live import scoring
from webapp.fusion_live import view as fusion
from webapp.semantic_news import view as semantic

NOW = datetime(2026, 7, 1, 15, tzinfo=UTC)


def render(**changes):
    values = dict(
        fusion_engine=None,
        semantic_news=None,
        fundamental_context=None,
        fusion_research=None,
        fusion_capability=None,
        semantic_capability=None,
        fusion_timezone=ZoneInfo("Europe/London"),
    )
    values.update(changes)
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[2] / "webapp/templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
    )
    return env.get_template("_fusion_engine.html").render(**values)


@pytest.fixture
def blank(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fusion-template.db'}")
    value = fusion.build(engine, NOW)
    engine.dispose()
    return value


def test_all_unavailable_views_do_not_invent_records_returns_or_dom_controls():
    text = render()
    assert 'id="fusion-engine"' in text and "başarı olasılığı değil" in text
    assert "karar ve getiri bilinmiyor" in text and "$+0.00" not in text
    assert "Mevcut AL/SAT kuralları korunur" in text
    assert "<script" not in text and "<nav" not in text and "<form" not in text
    assert "<details open" not in text


def test_real_empty_view_keeps_unknown_candidate_count_and_research_scope(blank):
    text = render(fusion_engine=blank)
    assert "Yeni araştırma adayı" in text and "<b>bilinmiyor</b>" in text
    assert "0 / 80" in text and "getiri ölçülmedi" in text
    assert "$+0.00" not in text


def test_recorded_empty_saturday_is_zero_candidates_not_zero_profit(blank):
    value = replace(
        blank,
        status="NO_CANDIDATES",
        reason="Yeni aday bekleniyor.",
        candidate_count=0,
        activation_at=NOW,
        universe_verified=True,
    )
    text = render(fusion_engine=value)
    assert "<b>0</b>" in text and "Yeni aday bekleniyor" in text
    assert "01.07.2026 16:00:00 Londra" in text and "$+0.00" not in text


def test_partial_common_panel_never_displays_a_partial_sum(blank):
    arm = scoring.ArmMetric("base", "Akış + fiyat", 1, 1, 0, 0, True, 12345.67, "")
    horizon = scoring.Horizon("m15", "+15 dakika", 1, 1, True, False, (arm,))
    value = replace(blank, candidate_count=1, universe_verified=True, horizons=(horizon,))
    text = render(fusion_engine=value)
    assert "tamamlanmadı" in text and "ölçülmedi" in text and "12345.67" not in text


@pytest.mark.parametrize(
    "universe,common,arm_complete", [(False, True, True), (True, False, True), (True, True, False)]
)
def test_every_completeness_guard_is_required_before_a_total(blank, universe, common, arm_complete):
    arm = scoring.ArmMetric("base", "Akış + fiyat", 1, 1, 0, 0, arm_complete, 12345.67, "")
    horizon = scoring.Horizon("m60", "+60 dakika", 1, 1, True, common, (arm,))
    text = render(
        fusion_engine=replace(
            blank, candidate_count=1, universe_verified=universe, horizons=(horizon,)
        )
    )
    assert "12345.67" not in text


def test_complete_true_skip_keeps_legitimate_cash_zero_visible(blank):
    arms = tuple(
        scoring.ArmMetric(key, label, 1, 0, 1, 0, True, 0.0, "")
        for key, label in scoring.ARM_LABELS.items()
    )
    horizon = scoring.Horizon("m15", "+15 dakika", 1, 1, True, True, arms)
    value = replace(blank, candidate_count=1, universe_verified=True, horizons=(horizon,))
    text = render(fusion_engine=value)
    assert text.count("$+0.00") == 4 and "0 / 1 / 0" in text


def test_history_without_complete_sessions_never_prints_a_zero_net(blank):
    h = fusion.HistoryArm("base", "Akış + fiyat", 0, 0.0, 0.0, 0.0)
    value = replace(blank, progress=(fusion.Progress("m15", "+15 dakika", 0, 80, (h,)),))
    text = render(fusion_engine=value)
    assert "$+0.00" not in text


def test_mixed_news_grounded_quotes_and_metadata_are_escaped():
    quote = "Company raises revenue guidance but cuts margin <script>alert(1)</script>"
    item = semantic.ItemView(
        "story-one",
        "AAPL",
        "direct",
        "guidance",
        "mixed",
        "confirmed",
        "high",
        "Revenue and margin point in different directions.",
        (semantic.EvidenceView(quote, "direction"),),
        ("Only headline supplied.",),
    )
    row = semantic.Row(
        "key",
        "AAPL",
        status="completed",
        direction="mixed",
        supports=None,
        available_at=NOW,
        model="<img src=x onerror=alert(1)>",
        prompt_hash="a" * 64,
        schema_hash="b" * 64,
        source_hash="c" * 64,
        input_count=3,
        input_limited=True,
        items=(item,),
        analysis_finished_at=NOW,
        requested_at=NOW,
        received_at=NOW,
    )
    report = semantic.Report(NOW, NOW.date(), "AVAILABLE", "Güncel yorum kaydı.", rows=(row,))
    text = render(semantic_news=report)
    assert "Karma" in text and "Beklenti / rehberlik" in text and "3 başlık, sınırlı paket" in text
    assert "Makale tam metni yok" in text and "Giriş öncesi kullanım doğrulanmadı" in text
    assert "&lt;script&gt;" in text and "<script>" not in text and "<img src=x" not in text
    assert "01.07.2026 16:00:00 Londra" in text and "Prompt aaaaaaaaaaaa" in text
    assert "Maliyet bilinmiyor" in text


def test_fundamental_zero_is_distinct_from_unknown_surprise_and_currency():
    metrics = (
        Metric("revenue_yoy_pct", 0.0, "percent", "", ("source.revenue",)),
        Metric("eps_surprise_pct", None, "percent", "basis_period_currency_unverified"),
    )
    facts = Facts(
        "AAPL",
        "financials",
        NOW,
        metrics,
        (Filing(None, None, NOW.date(), "quarterly", "USD"),),
        ("missing_basis",),
    )
    row = fundamental.Row(
        "AAPL",
        "candidate",
        "RECORDED",
        (fundamental.Observation("financials", "recorded", NOW, facts=facts),),
        (facts,),
        ("missing_basis",),
    )
    report = fundamental.Report(NOW, NOW.date(), "RECORDED", "Bağlam kaydı.", rows=(row,))
    text = render(fundamental_context=report)
    assert "Ciro yıllık değişim</td><td>0.00" in text
    assert "Uyumlu kâr sürprizi (%)</td><td>bilinmiyor" in text
    assert "basis_period_currency_unverified" in text and "çeyreklik" in text and "USD" in text


def test_unavailable_research_never_presents_a_fake_5184_search():
    text = render(fusion_research={"status": "UNAVAILABLE", "configuration_trials": 0})
    assert "5184 sabit" not in text and "Tarihsel arama raporu henüz doğrulanamadı" in text


def test_actual_stored_research_summary_retains_fragility_and_global_fallback():
    from webapp.fusion_research import report

    body = report.load()
    assert body["status"] == "EXPLORATORY_COMPLETE"
    text = render(fusion_research=body)
    assert "5184 sabit yapılandırma" in text and "70 hisse" in text and "252 günlük seans" in text
    assert "yeni ve görülmemiş doğrulama" in text and "$-707.22" in text
    assert "$-70.12" in text and "$-1253.68" in text and "GLOBAL_FALLBACK" in text
    assert "Kısmi ortaklaştırma" not in text


def test_unknown_research_numbers_are_never_rendered_as_boolean_or_zero():
    body = {
        "status": "EXPLORATORY_COMPLETE",
        "configuration_trials": 5184,
        "source_tickers": [],
        "source_sessions": None,
        "reference_replay_evaluations": None,
        "exploratory_fit": {"final_diagnostic": {"net_usd": None}, "diagnostics": {}},
        "selected_controls": [
            {"variant": "cost_stress", "bps_per_side": 20, "metrics": {"net_usd": False}}
        ],
        "ticker_estimates": [],
    }
    text = render(fusion_research=body)
    assert "$+0.00" not in text and "$+1.00" not in text
    assert "ölçülmedi" in text


def test_readiness_review_state_does_not_keep_a_hardcoded_insufficient_message(blank):
    value = replace(blank, complete_common_sessions=80, readiness="READY_FOR_REVIEW")
    text = render(fusion_engine=value)
    assert "Ortak veri eşiği karşılandı" in text
    assert "henüz yok" not in text


def test_capability_probe_is_explicitly_not_a_candidate_and_preserves_unknown_surprise():
    body = {
        "status": "VERIFIED_CAPABILITY",
        "actual_requests": 3,
        "rows": [
            {
                "ticker": "AAPL",
                "endpoint": "earnings",
                "status": 200,
                "received_at": "2026-11-07T08:00:00+00:00",
                "extracted_at": "2026-11-07T08:01:00+00:00",
                "facts": {
                    "metrics": [
                        {
                            "name": "reported_eps",
                            "value": 2.05,
                            "unit": "unknown_currency/share",
                            "reason": "",
                        },
                        {
                            "name": "eps_surprise_pct",
                            "value": None,
                            "unit": "percent",
                            "reason": "basis_period_currency_unverified",
                        },
                    ],
                    "filings": [
                        {"period_end": "2026-09-30", "report_type": "quarterly", "currency": None}
                    ],
                },
            }
        ],
    }
    text = render(fusion_capability=body)
    assert "3 kaynak isteği" in text and "o hisse için yeni sinyal değildir" in text
    assert (
        "2.05" in text
        and "basis_period_currency_unverified" in text
        and "para birimi bilinmiyor" in text
    )
    assert "$+0.00" not in text


def test_actual_semantic_capability_keeps_three_synthetic_texts_one_real_charge_separate():
    import json

    body = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "artifacts/fusion-context-v1/semantic-capability.json"
        ).read_text()
    )
    assert body["status"] == "VERIFIED_INFERENCE"
    text = render(semantic_capability=body)
    assert "3 sentetik başlık" in text and "1 gerçek model isteği" in text
    assert "Sentetik metin · gerçek model çağrısı · piyasa haberi/işlem sonucu değil." in text
    assert "Olumlu" in text and "Olumsuz" in text and "Karma" in text
    assert "$0.013572" in text and "2247 giriş / 1801 çıkış tokenı" in text
    assert "Sayfadaki adaylar bu denemeden üretilmedi." in text


def test_semantic_capability_quote_injection_and_subcent_charge_are_not_misrepresented():
    body = {
        "status": "VERIFIED_INFERENCE",
        "model": "model",
        "actual_paid_attempts": 1,
        "usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.000321},
        "cases": [
            {
                "headline": "<img src=x onerror=alert(1)>",
                "direction": "unknown",
                "event": "other",
                "certainty": "unknown",
                "reason": "unknown",
                "evidence": [{"quote": "</td><script>alert(1)</script>"}],
                "limitations": ["Only headline."],
            }
        ],
    }
    text = render(semantic_capability=body)
    assert "$0.000321" in text and "$0.000000" not in text
    assert "&lt;img" in text and "&lt;script&gt;" in text
    assert "<img src=x" not in text and "<script>" not in text
