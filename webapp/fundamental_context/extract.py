"""Bounded financial JSON adapter; revised statements become known only now.

Any is restricted to the vendor JSON boundary. Missing accounting/currency/period
identity never becomes zero, and vendor surprise fields are not trusted labels.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from webapp.fundamental_context.model import Endpoint, Facts, Filing, Metric, aware
from webapp.fundamental_context.settings import settings


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _time(value: object) -> datetime | None:
    try:
        return aware(datetime.fromisoformat(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _period(row: dict[str, Any]) -> date | None:
    return _day(row.get("fiscal_date_ending") or row.get("report_period_end_date"))


def _kind(row: dict[str, Any]) -> str | None:
    raw = _text(row.get("report_type"))
    if raw and raw.casefold() in {"quarterly", "annual"}:
        return raw.casefold()
    return {"10-K": "annual", "10-Q": "quarterly"}.get(str(row.get("formtype", "")).upper())


def _currency(row: dict[str, Any]) -> str | None:
    raw = _text(row.get("reported_currency") or row.get("currency"))
    return raw.upper() if raw and len(raw) == 3 and raw.isalpha() else None


def _rows(raw: object, ticker: str, asof: datetime, missing: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        missing.add("rows_unavailable")
        return []
    if len(raw) > settings().source_row_limit:
        missing.add("row_limit")
        # Vendor lists need not be newest first; never select from a silently
        # truncated oldest-first financial series.
        return []
    out = []
    for row in raw:
        if not isinstance(row, dict) or row.get("ticker") != ticker:
            missing.add("issuer_mismatch_or_invalid_row")
            continue
        period = _period(row)
        if period is None or period > asof.date():
            missing.add("period_missing_or_future")
            continue
        rejected = False
        for field in ("filing_date", "report_date"):
            if row.get(field) is not None and (
                (day := _day(row[field])) is None or day > asof.date()
            ):
                rejected = True
                missing.add("filing_or_report_date_invalid_or_future")
        for field in ("inserted_at", "updated_at"):
            if row.get(field) is not None:
                at = _time(row[field])
                if at is None or at > asof:
                    rejected = True
                    missing.add("vendor_clock_invalid_or_future")
        if not rejected:
            out.append(row)
    return sorted(
        out,
        key=lambda r: (
            _period(r) or date.min,
            _kind(r) == "quarterly",
            _time(r.get("updated_at")) or datetime.min.replace(tzinfo=asof.tzinfo),
        ),
        reverse=True,
    )


def _filing(row: dict[str, Any]) -> Filing:
    return Filing(
        _text(row.get("accession_no")),
        _day(row.get("filing_date")),
        _period(row),
        _kind(row),
        _currency(row),
        _time(row.get("inserted_at")),
        _time(row.get("updated_at")),
        _day(row.get("report_date")),
        _text(row.get("report_time")),
        _text(row.get("accounting_basis")),
    )


def extract(
    ticker: str, endpoint: Endpoint, payload: object, *, available_at: datetime, asof: datetime
) -> Facts:
    available, now = aware(available_at), aware(asof)
    missing: set[str] = set()
    metrics: list[Metric] = []
    filings: list[Filing] = []
    if available > now:
        return Facts(ticker, endpoint, available, (), (), ("availability_after_asof",))
    data = payload.get("data") if isinstance(payload, dict) else None

    def metric(
        name: str, value: float | None, unit: str, reason: str, paths: tuple[str, ...] = ()
    ) -> None:
        if value is not None and not math.isfinite(value):
            value, reason = None, "nonfinite_derived_value"
        metrics.append(Metric(name, value, unit, "" if value is not None else reason, paths))
        if value is None:
            missing.add(name + ":" + reason)

    if endpoint == "fundamental-breakdown":
        rows = _rows(data.get("general") if isinstance(data, dict) else None, ticker, now, missing)
        if rows:
            filings.append(_filing(rows[0]))
        # Actual native general rows mix annual and quarterly field durations.
        # Form type / period-end alone does not certify a coherent numerator and
        # denominator; preserve raw packet privately, no financial ratio here.
        for name in (
            "revenue",
            "net_income",
            "operating_income",
            "revenue_yoy_pct",
            "operating_margin_pct",
            "net_margin_pct",
            "cash",
            "debt",
            "cash_to_debt",
        ):
            metric(name, None, "unknown", "field_duration_currency_unverified")
        missing.add("breakdown_mixed_period_semantics")
    elif endpoint == "earnings":
        rows = _rows(data, ticker, now, missing)
        row = rows[0] if rows else {}
        if row:
            filings.append(_filing(row))
        actual, estimated = _number(row.get("reported_eps")), _number(row.get("estimated_eps"))
        unit = (_currency(row) or "unknown_currency") + "/share"
        metric("reported_eps", actual, unit, "reported_eps_unavailable", ("data[].reported_eps",))
        metric(
            "estimated_eps", estimated, unit, "estimated_eps_unavailable", ("data[].estimated_eps",)
        )
        reported_basis = _text(row.get("reported_basis") or row.get("accounting_basis"))
        estimated_basis = _text(row.get("estimated_basis") or row.get("accounting_basis"))
        known_basis = {"gaap", "ifrs", "adjusted", "non-gaap"}
        basis_ok = (
            reported_basis is not None
            and estimated_basis is not None
            and reported_basis.casefold() in known_basis
            and reported_basis.casefold() == estimated_basis.casefold()
        )
        period_ok = (
            _period(row) is not None
            and _kind(row) is not None
            and _day(row.get("estimated_fiscal_date_ending")) == _period(row)
        )
        currency_ok = _currency(row) is not None and _text(
            row.get("estimated_currency")
        ) == _currency(row)
        compatible = basis_ok and period_ok and currency_ok
        delta = (
            actual - estimated
            if compatible and actual is not None and estimated is not None
            else None
        )
        metric(
            "eps_surprise",
            delta,
            unit,
            "basis_period_currency_unverified",
            (
                "data[].reported_eps",
                "data[].estimated_eps",
                "data[].reported_basis",
                "data[].estimated_basis",
                "data[].reported_currency",
                "data[].estimated_currency",
                "data[].fiscal_date_ending",
                "data[].estimated_fiscal_date_ending",
            ),
        )
        pct = (
            delta / abs(estimated) * 100
            if delta is not None
            and estimated is not None
            and abs(estimated) >= settings().eps_denominator_floor
            else None
        )
        metric(
            "eps_surprise_pct",
            pct,
            "percent",
            "basis_period_currency_unverified_or_small_consensus",
            (
                "data[].reported_eps",
                "data[].estimated_eps",
                "data[].reported_basis",
                "data[].estimated_basis",
                "data[].reported_currency",
                "data[].estimated_currency",
                "data[].fiscal_date_ending",
                "data[].estimated_fiscal_date_ending",
            ),
        )
    elif endpoint == "financials":
        income = _rows(
            data.get("income_statements") if isinstance(data, dict) else None, ticker, now, missing
        )
        balances = _rows(
            data.get("balance_sheets") if isinstance(data, dict) else None, ticker, now, missing
        )
        row = income[0] if income else {}
        if row:
            filings.append(_filing(row))
        currency, kind, period = _currency(row), _kind(row), _period(row)
        identity_ok = currency is not None and kind is not None and period is not None
        revenue = _number(row.get("total_revenue")) if identity_ok else None
        net = _number(row.get("net_income")) if identity_ok else None
        operating = _number(row.get("operating_income")) if identity_ok else None
        # Field contradictions are not repaired by substituting another period.
        gross = _number(row.get("gross_profit"))
        if revenue is not None and (revenue < 0 or (gross is not None and gross > revenue)):
            revenue = net = operating = None
            missing.add("statement_scalar_inconsistency")
        unit = currency or "unknown_currency"
        for name, value, field in [
            ("revenue", revenue, "total_revenue"),
            ("net_income", net, "net_income"),
            ("operating_income", operating, "operating_income"),
        ]:
            metric(
                name,
                value,
                unit,
                "period_currency_or_value_unverified",
                ("data.income_statements[]." + field,),
            )
        previous = next(
            (
                r
                for r in income[1:]
                if period is not None
                and (p := _period(r)) is not None
                and p.year == period.year - 1
                and p.month == period.month
                and _currency(r) == currency
                and _kind(r) == kind
            ),
            None,
        )
        old = _number(previous.get("total_revenue")) if previous else None
        growth = (
            (revenue / old - 1) * 100
            if revenue is not None and old is not None and old > 0
            else None
        )
        metric(
            "revenue_yoy_pct",
            growth,
            "percent",
            "comparable_previous_period_unavailable",
            (
                "data.income_statements[].total_revenue",
                "data.income_statements[].fiscal_date_ending",
                "data.income_statements[].report_type",
                "data.income_statements[].reported_currency",
            ),
        )
        for name, value in [("operating_margin_pct", operating), ("net_margin_pct", net)]:
            metric(
                name,
                value / revenue * 100
                if value is not None and revenue is not None and revenue > 0
                else None,
                "percent",
                "matched_positive_revenue_unavailable",
                (
                    "data.income_statements[].total_revenue",
                    "data.income_statements[]."
                    + ("operating_income" if name == "operating_margin_pct" else "net_income"),
                    "data.income_statements[].fiscal_date_ending",
                    "data.income_statements[].reported_currency",
                ),
            )
        balance = (
            next(
                (
                    r
                    for r in balances
                    if _period(r) == period and _kind(r) == kind and _currency(r) == currency
                ),
                {},
            )
            if identity_ok
            else {}
        )
        if balance:
            filings.append(_filing(balance))
        cash = _number(balance.get("cash_and_cash_equivalents"))
        debt = _number(balance.get("short_long_term_debt_total"))
        if cash is not None and cash < 0:
            cash = None
        if debt is not None and debt < 0:
            debt = None
        metric(
            "cash",
            cash,
            unit,
            "matched_balance_cash_unavailable",
            ("data.balance_sheets[].cash_and_cash_equivalents",),
        )
        metric(
            "debt",
            debt,
            unit,
            "matched_total_debt_unavailable",
            ("data.balance_sheets[].short_long_term_debt_total",),
        )
        metric(
            "cash_to_debt",
            cash / debt if cash is not None and debt is not None and debt > 0 else None,
            "ratio",
            "matched_positive_debt_unavailable",
            (
                "data.balance_sheets[].cash_and_cash_equivalents",
                "data.balance_sheets[].short_long_term_debt_total",
                "data.balance_sheets[].fiscal_date_ending",
                "data.balance_sheets[].reported_currency",
            ),
        )
        if any(f.accession is None for f in filings):
            missing.add("filing_accession_unavailable")
        if any(f.filing_date is None for f in filings):
            missing.add("filing_date_unavailable")
    else:
        raise ValueError("unsupported_fundamental_endpoint")
    return Facts(
        ticker, endpoint, available, tuple(metrics), tuple(filings), tuple(sorted(missing))
    )
