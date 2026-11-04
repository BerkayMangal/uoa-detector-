"""Research presentation distinguishes missing totals from known subtotals."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parents[2]


def render(value: dict[str, Any] | None, **extra: object) -> str:
    # The artifact template's JSON input is the intentionally untyped boundary.
    env = Environment(
        loader=FileSystemLoader(ROOT / "webapp/templates"),
        autoescape=select_autoescape(), undefined=StrictUndefined,
    )
    return env.get_template("_horizon_research.html").render(horizon_research=value, **extra)


def fixture() -> dict[str, Any]:
    return {
        "registered_cells": 1440, "assessed_cells": 1440,
        "complete_cells": 12, "partial_cells": 1428, "blocked_cells": 0,
        "repeated_control_cells": 12,
        "primary_family": {
            "complete_common_days": [], "administrative_days": ["2026-07-01"],
            "full_baseline_roster": 85, "administrative_roster": 40,
            "right_censored_days_excluded": ["2026-09-18"],
            "contrasts": [{
                "horizon": "m5", "complete_days": 0,
                "total_net_usd": None, "total_excess_usd": None,
                "mean_daily_difference_vs_m60_usd": None,
                "shared_max_deviation_descriptive_95pct_band": None,
            }],
        },
        "rows": [{
            "arm": "baseline_proxy", "delay_minutes": 1, "filter_minutes": 0,
            "horizon": "m5", "cost_bps_per_side": 5,
            "selected": 2, "resolved": 1, "missing": 1,
            "gate_true": 2, "gate_false": 0, "gate_unknown": 0,
            "status": "PARTIAL", "strict_net_usd": None, "strict_excess_usd": None,
            "known_net_usd": -12.5, "known_excess_usd": -2.5,
            "missing_reasons": {"missing_exit": 1},
        }],
    }


def test_unavailable_report_cannot_render_a_result_or_zero() -> None:
    page = render(None)
    assert "data-horizon-unavailable" in page
    assert "Getiri gösterilmiyor" in page and "eksik sonuç sıfır değildir" in page
    assert "data-primary-horizon" not in page and "+0.00" not in page


def test_missing_primary_and_strict_totals_remain_unknown_with_named_subtotal() -> None:
    page = render(fixture())
    assert 'data-primary-net>bilinmiyor' in page
    assert 'data-primary-excess>bilinmiyor' in page
    assert 'data-primary-difference>bilinmiyor' in page
    assert 'data-strict-net>bilinmiyor' in page
    assert 'data-strict-excess>bilinmiyor' in page
    assert 'data-known-subtotal>-12.50 / -2.50' in page
    assert "Bilinen alt toplam" in page and "+0.00" not in page


def test_known_zero_is_not_replaced_by_unknown_and_negative_result_is_preserved() -> None:
    report = fixture()
    row = report["primary_family"]["contrasts"][0]
    report["primary_family"]["complete_common_days"] = ["2026-07-01"]
    report["primary_family"]["bootstrap"] = {"status": "DESCRIPTIVE_ONLY", "draws": 1000, "block_sessions": 5}
    row.update(complete_days=1, total_net_usd=0.0, total_excess_usd=-7.25,
               mean_daily_difference_vs_m60_usd=0.0,
               shared_max_deviation_descriptive_95pct_band=(-1.0, 1.0))
    page = render(report)
    assert 'data-primary-net>+0.00' in page and 'data-primary-excess>-7.25' in page
    assert 'data-primary-difference>+0.00' in page and "[-1.00, +1.00]" in page


def test_four_clocks_live_boundaries_and_full_evidence_link_are_explicit() -> None:
    page = render(fixture())
    for text in ("300 saniye", "5 / 15 / 30", "+2 / +6 / +16", "ATR14", "beş seanslık",
                 "bilanço modeli kurmuyor", "olumlu ekonomik etki", "p-değeri", "1.440 hücrenin tamamı"):
        assert text in page
    assert 'href="/api/arastirma/ufuk"' in page
    assert "overflow-x-auto" in page and 'tabindex="0"' in page
    assert '<details open' in page and '<script' not in page


def test_unknown_candle_gate_is_visible_separately_from_false() -> None:
    report = fixture()
    report["rows"].append({
        **report["rows"][0], "horizon": "m60", "filter_minutes": 15,
        "gate_true": 2, "gate_false": 3, "gate_unknown": 4,
    })
    page = render(report)
    assert 'data-gate-minutes="15"' in page
    assert "2 / 3 / 4" in page and "Bilinmeyen filtre, yanlış filtre" in page


def test_censored_dates_quality_and_missing_reasons_are_readable() -> None:
    report = fixture()
    report["source_quality"] = {
        "minute_packets": 244, "flow_packets": 183, "rth_minutes": 95160,
        "negative_volume_rows": 16, "negative_volume_ticker_dates": 2,
        "negative_volume|NVDA|2026-07-01": 8,
    }
    report["rows"][0]["missing_reasons"] = {"exact_exit_minute_missing": 1}
    page = render(report)
    assert "Takvim nedeniyle dışarıda kalan günler · 1" in page and "2026-09-18" in page
    assert "Dakika paketi 244 · akış paketi 183" in page
    assert "Normal seans OHLC satırı 95160 · negatif hacim alanı 16" in page
    assert "Negatif hacim · NVDA · 2026-07-01: 8" in page
    assert "negative_volume|" not in page and "Çıkış dakikası eksik: 1" in page


def test_empty_common_panel_cannot_display_a_supplied_band_as_valid() -> None:
    report = fixture()
    report["primary_family"]["bootstrap"] = {"status": "DESCRIPTIVE_ONLY"}
    report["primary_family"]["contrasts"][0]["shared_max_deviation_descriptive_95pct_band"] = (-2.0, 3.0)
    page = render(report)
    assert "[-2.00, +3.00]" not in page


def test_no_resolved_unit_cannot_turn_empty_known_sum_into_an_observed_zero() -> None:
    report = fixture()
    report["rows"][0].update(resolved=0, known_net_usd=0.0, known_excess_usd=0.0)
    page = render(report)
    assert "Henüz fiyatlanmış birim yok" in page and "+0.00" not in page


def diagnostic_fixture() -> dict[str, Any]:
    return {"rows": [{
        "horizon": horizon, "cost_bps_per_side": cost, "reference_units": 3,
        "net_usd": None, "without_best_net_3_usd": None,
        "without_best_excess_3_usd": None, "gross_mae_observed": 0,
        "gross_mae_mean_pct": None, "worst_observed_gross_mae_pct": None,
    } for horizon in ("m5", "m15", "m30", "m60", "m120", "m180", "c0", "c1", "c3", "c5")
      for cost in (5, 10, 20, 50)]}


def test_posthoc_explanation_is_explicit_and_shows_all_ten_horizons() -> None:
    page = render(fixture(), horizon_diagnostics=diagnostic_fixture())
    assert "POSTHOC" in page and "sonuçları gördükten sonra" in page
    assert page.count("data-posthoc-horizon=") == 10
    for horizon in ("m5", "m15", "m30", "m60", "m120", "m180", "c0", "c1", "c3", "c5"):
        assert f'data-posthoc-horizon="{horizon}"' in page
    assert 'href="/api/arastirma/ufuk/dayaniklilik"' in page
    assert "çıkarılan kümeler farklı olabilir" in page and "Sermaye yeniden dağıtılmaz" in page
    assert "hesap düşüşü veya gerçekleşmiş stop zararı değildir" in page


def test_posthoc_missing_context_and_unknown_metrics_are_not_zero() -> None:
    missing = render(fixture())
    assert "data-horizon-diagnostics-unavailable" in missing
    assert "data-posthoc-horizon=" not in missing
    page = render(fixture(), horizon_diagnostics=diagnostic_fixture())
    assert "data-posthoc-net>bilinmiyor" in page
    assert "data-posthoc-without-best-net>bilinmiyor" in page
    assert "data-posthoc-without-best-excess>bilinmiyor" in page
    assert "data-posthoc-mean-mae>bilinmiyor" in page
    assert "data-posthoc-worst-mae>bilinmiyor" in page
    assert "+0.00" not in page


def test_posthoc_known_zero_and_negative_observed_values_remain_distinct() -> None:
    data = diagnostic_fixture()
    data["rows"][0].update(net_usd=0.0, without_best_net_3_usd=-12.5,
                           without_best_excess_3_usd=-3.0, gross_mae_observed=3,
                           gross_mae_mean_pct=0.0, worst_observed_gross_mae_pct=-4.25)
    page = render(fixture(), horizon_diagnostics=data)
    assert "data-posthoc-net>+0.00" in page
    assert "data-posthoc-without-best-net>-12.50" in page
    assert "data-posthoc-mean-mae>+0.00%" in page
    assert "data-posthoc-worst-mae>-4.25%" in page
