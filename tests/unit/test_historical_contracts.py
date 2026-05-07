"""Phase 3.3.4.5 tests for ``historical.contracts``.

Pins:
  - list_contracts: GET /v2/list/contracts/option/quote with root param
  - dict-row response shape parsed
  - list-row response shape (4-element arrays) parsed
  - rights filter: defaults to {C, P}; subset honoured
  - DTE filter: min_dte / max_dte applied client-side
  - max_contracts cap: deterministic sort then truncate
  - malformed row dropped, others kept
  - empty data → []
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from uoa_detector.historical.contracts import (
    ContractListFilter,
    ThetaDataContractLister,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, dict[str, Any]] = {}

    def stub(self, path: str, response: dict[str, Any]) -> None:
        self.responses[path] = response

    async def request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        return self.responses.get(path, {"data": []})


# ---------------------------------------------------------------------------
# Endpoint + params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_contracts_calls_correct_endpoint() -> None:
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        {"data": []},
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    await lister.list_contracts("aapl")
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == "/v2/list/contracts/option/quote"
    assert params == {"root": "AAPL"}  # uppercased


# ---------------------------------------------------------------------------
# Response shape decoding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_contracts_parses_dict_rows() -> None:
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        {"data": [
            {"expiration": 20240216, "strike": 1500000, "right": "C"},
            {"expiration": "20240216", "strike": "1550000", "right": "P"},
        ]},
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(as_of_date=date(2024, 1, 1)),
    )
    assert len(contracts) == 2
    assert contracts[0].expiry == date(2024, 2, 16)
    assert contracts[0].strike_dollars == Decimal("150")
    assert contracts[0].right == "C"
    assert contracts[1].right == "P"


@pytest.mark.asyncio
async def test_list_contracts_parses_array_rows() -> None:
    """Some ThetaData shapes return rows as [exp, strike, right, ...]."""
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        {"data": [
            [20240216, 1500000, "C", "extra"],
            [20240216, 1550000, "P"],
        ]},
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(as_of_date=date(2024, 1, 1)),
    )
    assert len(contracts) == 2


@pytest.mark.asyncio
async def test_list_contracts_empty_data_returns_empty() -> None:
    client = _FakeClient()
    client.stub("/v2/list/contracts/option/quote", {"data": []})
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    assert await lister.list_contracts("AAPL") == []


@pytest.mark.asyncio
async def test_list_contracts_malformed_row_dropped() -> None:
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        {"data": [
            {"expiration": "garbage"},  # bad
            {"expiration": 20240216, "strike": 150000, "right": "C"},
            {"expiration": 20240216, "strike": 155000, "right": "X"},  # bad right
        ]},
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL", filters=ContractListFilter(as_of_date=date(2024, 1, 1)),
    )
    assert len(contracts) == 1
    assert contracts[0].right == "C"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _make_response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": rows}


@pytest.mark.asyncio
async def test_rights_filter_default_includes_both() -> None:
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        _make_response([
            {"expiration": 20240216, "strike": 150000, "right": "C"},
            {"expiration": 20240216, "strike": 150000, "right": "P"},
        ]),
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL", filters=ContractListFilter(as_of_date=date(2024, 1, 1)),
    )
    assert len(contracts) == 2


@pytest.mark.asyncio
async def test_rights_filter_calls_only() -> None:
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        _make_response([
            {"expiration": 20240216, "strike": 150000, "right": "C"},
            {"expiration": 20240216, "strike": 150000, "right": "P"},
        ]),
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(
            rights=frozenset({"C"}), as_of_date=date(2024, 1, 1),
        ),
    )
    assert len(contracts) == 1
    assert contracts[0].right == "C"


@pytest.mark.asyncio
async def test_dte_filter_min_dte() -> None:
    """min_dte=14 drops contracts expiring within 14 days of as_of."""
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        _make_response([
            {"expiration": 20240105, "strike": 150000, "right": "C"},  # 4 dte
            {"expiration": 20240216, "strike": 150000, "right": "C"},  # 46 dte
        ]),
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(
            min_dte=14, as_of_date=date(2024, 1, 1),
        ),
    )
    assert len(contracts) == 1
    assert contracts[0].expiry == date(2024, 2, 16)


@pytest.mark.asyncio
async def test_dte_filter_max_dte() -> None:
    """max_dte=30 drops contracts expiring more than 30 days out."""
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        _make_response([
            {"expiration": 20240115, "strike": 150000, "right": "C"},  # 14 dte
            {"expiration": 20240216, "strike": 150000, "right": "C"},  # 46 dte
        ]),
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(
            max_dte=30, as_of_date=date(2024, 1, 1),
        ),
    )
    assert len(contracts) == 1
    assert contracts[0].expiry == date(2024, 1, 15)


@pytest.mark.asyncio
async def test_max_contracts_cap_applied() -> None:
    """max_contracts=2 truncates after deterministic sort."""
    client = _FakeClient()
    client.stub(
        "/v2/list/contracts/option/quote",
        _make_response([
            {"expiration": 20240216, "strike": 160000, "right": "C"},
            {"expiration": 20240216, "strike": 150000, "right": "C"},
            {"expiration": 20240216, "strike": 155000, "right": "C"},
            {"expiration": 20240216, "strike": 145000, "right": "C"},
        ]),
    )
    lister = ThetaDataContractLister(client=client)  # type: ignore[arg-type]
    contracts = await lister.list_contracts(
        "AAPL",
        filters=ContractListFilter(
            max_contracts=2, as_of_date=date(2024, 1, 1),
        ),
    )
    assert len(contracts) == 2
    # Sort is by (expiry, strike, right) so the lowest strikes come first
    strikes = [c.strike_dollars for c in contracts]
    assert strikes == sorted(strikes)
