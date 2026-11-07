"""Operational diagnostics cannot silently display modified evidence as verified."""

import json

import pytest
from webapp import fusion_capability as capability


def test_both_shipped_summaries_have_valid_seals_and_no_performance_inference():
    for name in ("capability", "semantic-capability"):
        result = capability.load(name)
        assert result["inference"] == "NOT_PERFORMED"
        assert len(result["artifact_hash"]) == 64
    model = capability.load("semantic-capability")
    assert model["synthetic_inputs"] is True
    assert model["market_calls_created"] == 0


@pytest.mark.parametrize("change", ["value", "seal", "inference", "nonfinite", "oversize"])
def test_modified_or_unbounded_diagnostic_is_rejected(tmp_path, monkeypatch, change):
    monkeypatch.setattr(capability, "ROOT", tmp_path)
    body = capability.seal({"inference": "NOT_PERFORMED", "value": 1})
    if change == "value":
        body["value"] = 2
    elif change == "seal":
        body["artifact_hash"] = "0" * 64
    elif change == "inference":
        body = capability.seal({"inference": "PROVEN_PROFIT", "value": 1})
    raw = json.dumps(body)
    if change == "nonfinite":
        raw = '{"value": NaN}'
    elif change == "oversize":
        raw = " " * (capability.MAX_BYTES + 1)
    (tmp_path / "capability.json").write_text(raw)
    with pytest.raises(ValueError):
        capability.load("capability")
