"""Financial periods/currencies/revisions never become invented causal features."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from webapp.fundamental_context.extract import extract

NOW = datetime(2026, 11, 7, 8, tzinfo=UTC)


def statements():
    def income(period, revenue):
        return dict(
            ticker="AAPL",
            fiscal_date_ending=period,
            report_type="quarterly",
            reported_currency="USD",
            total_revenue=str(revenue),
            gross_profit=str(revenue * 0.5),
            net_income=str(revenue * 0.2),
            operating_income=str(revenue * 0.3),
            inserted_at="2026-11-03T06:00:00Z",
            updated_at="2026-11-07T06:00:00Z",
        )

    return {
        "data": {
            "income_statements": [income("2026-09-30", 120), income("2025-09-30", 100)],
            "balance_sheets": [
                dict(
                    ticker="AAPL",
                    fiscal_date_ending="2026-09-30",
                    report_type="quarterly",
                    reported_currency="USD",
                    cash_and_cash_equivalents="60",
                    short_long_term_debt_total="30",
                )
            ],
            "cash_flows": [],
            "earnings": [],
        }
    }


def metrics(payload, endpoint="financials", **changes):
    f = extract(
        "AAPL",
        endpoint,
        payload,
        available_at=changes.get("available_at", NOW),
        asof=changes.get("asof", NOW),
    )
    return f, {m.name: m for m in f.metrics}


def test_coherent_period_currency_yoy_margins_and_balance_are_separate_facts():
    f, m = metrics(statements())
    assert m["revenue"].value == 120 and m["revenue_yoy_pct"].value == pytest.approx(20)
    assert m["net_margin_pct"].value == 20 and m["operating_margin_pct"].value == 30
    assert m["cash_to_debt"].value == 2 and m["debt"].value == 30
    assert f.available_at == NOW and f.filings[0].period_end.isoformat() == "2026-09-30"
    assert "filing_accession_unavailable" in f.missing and "filing_date_unavailable" in f.missing


@pytest.mark.parametrize(
    "damage", ["currency", "period", "report_type", "zero", "negative", "boolean"]
)
def test_incompatible_previous_observation_cannot_be_a_comparable(damage):
    p = statements()
    r = p["data"]["income_statements"][1]
    if damage == "currency":
        r["reported_currency"] = "EUR"
    elif damage == "period":
        r["fiscal_date_ending"] = "2025-06-30"
    elif damage == "report_type":
        r["report_type"] = "annual"
    elif damage == "zero":
        r["total_revenue"] = "0"
    elif damage == "negative":
        r["total_revenue"] = "-1"
    else:
        r["total_revenue"] = True
    _, m = metrics(p)
    assert m["revenue_yoy_pct"].value is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("updated_at", "2026-11-08T00:00:00Z"),
        ("inserted_at", "2026-11-07T07:00:00"),
        ("filing_date", "2026-11-08"),
        ("fiscal_date_ending", "2027-09-30"),
        ("ticker", "MSFT"),
    ],
)
def test_future_invalid_or_other_issuer_latest_row_is_not_used(field, value):
    p = statements()
    p["data"]["income_statements"][0][field] = value
    f, m = metrics(p)
    assert m["revenue"].value != 120 and f.missing


def test_breakdown_actual_mixed_duration_pattern_never_makes_a_false_margin():
    payload = {
        "data": {
            "annual_only": False,
            "general": [
                {
                    "ticker": "AAPL",
                    "formtype": "10-K",
                    "filing_date": "2026-11-03",
                    "report_period_end_date": "2026-09-26",
                    "accession_no": "example",
                    "revenue": "114477000000",
                    "gross_profit": "233602000000",
                    "net_income": "132175000000",
                    "net_income_loss": "30711000000",
                }
            ],
        }
    }
    f, m = metrics(payload, "fundamental-breakdown")
    assert all(v.value is None for v in m.values())
    assert "breakdown_mixed_period_semantics" in f.missing and f.filings[0].accession == "example"


def test_explicit_financial_scalar_contradiction_is_not_silently_divided():
    p = statements()
    p["data"]["income_statements"][0]["gross_profit"] = "500"
    f, m = metrics(p)
    assert m["revenue"].value is m["net_margin_pct"].value is None
    assert "statement_scalar_inconsistency" in f.missing


def test_liabilities_are_never_substituted_for_debt():
    p = statements()
    r = p["data"]["balance_sheets"][0]
    r.pop("short_long_term_debt_total")
    r["total_liabilities"] = "30"
    _, m = metrics(p)
    assert m["cash"].value == 60 and m["debt"].value is m["cash_to_debt"].value is None


def test_basis_unknown_native_earnings_does_not_trust_vendor_surprise():
    payload = {
        "data": [
            {
                "ticker": "AAPL",
                "fiscal_date_ending": "2026-09-30",
                "report_type": "quarterly",
                "report_date": "2026-11-02",
                "reported_eps": "2.05",
                "estimated_eps": "1.98",
                "surprise": "0.07",
                "surprise_percentage": "3.5354",
            }
        ]
    }
    _, m = metrics(payload, "earnings")
    assert m["reported_eps"].value == 2.05 and m["estimated_eps"].value == 1.98
    assert m["eps_surprise"].value is m["eps_surprise_pct"].value is None


@pytest.mark.parametrize("damage", ["none", "basis", "currency", "period", "small_consensus"])
def test_surprise_requires_explicit_compatible_basis_period_currency(damage):
    row = {
        "ticker": "AAPL",
        "fiscal_date_ending": "2026-09-30",
        "estimated_fiscal_date_ending": "2026-09-30",
        "report_type": "quarterly",
        "reported_currency": "USD",
        "estimated_currency": "USD",
        "reported_eps": "2",
        "estimated_eps": "1",
        "reported_basis": "GAAP",
        "estimated_basis": "GAAP",
    }
    if damage == "basis":
        row["estimated_basis"] = "adjusted"
    if damage == "currency":
        row["estimated_currency"] = "EUR"
    if damage == "period":
        row["estimated_fiscal_date_ending"] = "2025-09-30"
    if damage == "small_consensus":
        row["estimated_eps"] = "0.001"
    _, m = metrics({"data": [row]}, "earnings")
    if damage == "none":
        assert m["eps_surprise"].value == 1 and m["eps_surprise_pct"].value == 100
    elif damage == "small_consensus":
        assert m["eps_surprise_pct"].value is None
    else:
        assert m["eps_surprise"].value is m["eps_surprise_pct"].value is None


def test_physical_availability_does_not_move_to_filing_or_fiscal_date():
    f, _ = metrics(statements(), available_at=NOW - timedelta(seconds=1))
    assert f.available_at == NOW - timedelta(seconds=1)
    f, _ = metrics(statements(), available_at=NOW + timedelta(seconds=1))
    assert not f.metrics and f.missing == ("availability_after_asof",)
    with pytest.raises(ValueError, match="aware"):
        metrics(statements(), available_at=NOW.replace(tzinfo=None))


def test_oldest_first_series_is_sorted_and_revision_future_is_not_available():
    p = statements()
    p["data"]["income_statements"].reverse()
    _, m = metrics(p)
    assert m["revenue"].value == 120
    p["data"]["income_statements"] *= 130
    f, m = metrics(p)
    assert m["revenue"].value is None and "row_limit" in f.missing


def test_nonfinite_values_and_currency_missing_do_not_enter_numeric_feature():
    p = deepcopy(statements())
    p["data"]["income_statements"][0].pop("reported_currency")
    _, m = metrics(p)
    assert m["revenue"].value is None
    p = statements()
    p["data"]["income_statements"][0]["net_income"] = "NaN"
    _, m = metrics(p)
    assert m["net_margin_pct"].value is None
