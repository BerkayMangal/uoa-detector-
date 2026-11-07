"""One pinned runtime-cache bridge never rewrites or broadens historical evidence."""

from __future__ import annotations

import hashlib
import json

import pytest
from webapp import horizon_diagnostics
from webapp.history_capture import report
from webapp.history_capture.capture import digest
from webapp.horizon_research import report as horizons

DEPENDENCY = "src/uoa_detector/live_alpha/settings.py"
PROFILE = "profiles/live_alpha_v1.yaml"
ORIGINAL_REPORT = "b3b411c1092f00df8f39eadb03aca4050cb4425ff10befd88b2b2dfb080d17af"
ORIGINAL_AUDIT = "546c385c34262e7905e945ee676bfa6c6ee5d66d574630a189ea4cd8c2f6fd16"


@pytest.fixture(autouse=True)
def clear_report_cache():
    report.load.cache_clear()
    horizons.load.cache_clear()
    horizon_diagnostics.load.cache_clear()
    yield
    report.load.cache_clear()
    horizons.load.cache_clear()
    horizon_diagnostics.load.cache_clear()


def originals():
    original = json.loads((report.DIRECTORY / "report.json").read_text())
    audit = json.loads((report.DIRECTORY / "audit.json").read_text())
    return original, audit


def current():
    return {name: report.file_hash(name) for name in report.DEPENDENCIES}


def test_manifest_is_byte_pinned_and_matches_the_exact_runtime_profile_and_old_artifacts():
    raw = report._COMPATIBILITY_PATH.read_bytes()
    manifest = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == report._COMPATIBILITY_SHA256
    assert set(manifest) == report._COMPATIBILITY_FIELDS
    original, audit = originals()
    assert manifest["original_dependency_sha256"] == original["dependency_hashes"][DEPENDENCY]
    assert manifest["runtime_dependency_sha256"] == current()[DEPENDENCY]
    assert manifest["profile_sha256"] == current()[PROFILE]
    assert manifest["report_hash"] == original["report_hash"] == ORIGINAL_REPORT
    assert manifest["audit_hash"] == audit["audit_hash"] == ORIGINAL_AUDIT


def test_original_report_and_audit_return_with_every_original_value_and_hash_unchanged():
    original, audit = originals()
    before = {
        path: path.read_bytes()
        for path in (report.DIRECTORY / "report.json", report.DIRECTORY / "audit.json")
    }
    actual = report.load()
    assert actual == original | {"audit": audit}
    assert actual["winner"] is None and actual["validated_alpha"] is False
    assert all(path.read_bytes() == raw for path, raw in before.items())


def test_existing_horizon_report_and_posthoc_description_still_resolve_original_hashes():
    horizon = horizons.load()
    description = horizon_diagnostics.load()
    assert horizon["original_report_hash"] == ORIGINAL_REPORT
    assert (
        horizon["report_hash"] == "ed405e06de5069070f746a9675b015ae9f6dd2620ee0add3a65b1c8380e8182e"
    )
    assert (
        description["description_hash"]
        == "34f424bd527ca4b05943c0e58f0d9126f179d267e7797e4879128f48a1b7b170"
    )
    assert description["report_hash"] == horizon["report_hash"]


def test_exact_original_dependency_bytes_need_no_bridge(monkeypatch, tmp_path):
    original, audit = originals()
    monkeypatch.setattr(report, "_COMPATIBILITY_PATH", tmp_path / "absent.json")
    recorded = original["dependency_hashes"]
    assert report._compatible_dependencies(
        recorded, recorded, original["report_hash"], audit["audit_hash"]
    )


def test_equal_but_incomplete_dependency_maps_cannot_bypass_the_exact_keyset():
    assert not report._compatible_dependencies({}, {}, ORIGINAL_REPORT, ORIGINAL_AUDIT)


@pytest.mark.parametrize(
    "kind", ["runtime", "original", "profile_both", "report", "audit", "missing_key", "extra_key"]
)
def test_compatibility_does_not_accept_unregistered_hashes_or_dependency_keysets(kind):
    original, audit = originals()
    recorded, actual = dict(original["dependency_hashes"]), current()
    rh, ah = original["report_hash"], audit["audit_hash"]
    if kind == "runtime":
        actual[DEPENDENCY] = "1" * 64
    elif kind == "original":
        recorded[DEPENDENCY] = "2" * 64
    elif kind == "profile_both":
        recorded[PROFILE] = actual[PROFILE] = "3" * 64
    elif kind == "report":
        rh = "4" * 64
    elif kind == "audit":
        ah = "5" * 64
    elif kind == "missing_key":
        recorded.pop(PROFILE)
    else:
        recorded["new_dependency"] = "6" * 64
    assert not report._compatible_dependencies(recorded, actual, rh, ah)


@pytest.mark.parametrize(
    "name",
    [*report.DEPENDENCIES, "scripts/diagnose_vendor_history.py", "scripts/audit_vendor_history.py"],
)
def test_report_still_rejects_every_changed_dependency_and_both_original_scripts(monkeypatch, name):
    original = report.file_hash
    monkeypatch.setattr(
        report, "file_hash", lambda path: "f" * 64 if path == name else original(path)
    )
    with pytest.raises(ValueError, match="artifact/profile/implementation mismatch"):
        report.load()


@pytest.mark.parametrize(
    "damage", ["newline", "runtime_hash", "extra_key", "missing", "invalid_json"]
)
def test_manifest_changes_cannot_authorize_another_runtime(monkeypatch, tmp_path, damage):
    raw = report._COMPATIBILITY_PATH.read_bytes()
    path = tmp_path / "manifest.json"
    if damage == "newline":
        path.write_bytes(raw + b"\n")
    elif damage in {"runtime_hash", "extra_key"}:
        body = json.loads(raw)
        body["runtime_dependency_sha256" if damage == "runtime_hash" else "new_key"] = "a" * 64
        path.write_text(json.dumps(body))
    elif damage == "invalid_json":
        path.write_text("{")
    monkeypatch.setattr(report, "_COMPATIBILITY_PATH", path)
    with pytest.raises(ValueError, match="artifact/profile/implementation mismatch"):
        report.load()


@pytest.mark.parametrize("target", ["report", "audit"])
def test_even_resealed_alternative_report_or_audit_cannot_use_this_bridge(
    monkeypatch, tmp_path, target
):
    original, audit = originals()
    if target == "report":
        original["cache_compatibility_must_not_rewrite_results"] = True
        original.pop("report_hash")
        original["report_hash"] = digest(original)
        audit["original_report_hash"] = original["report_hash"]
    else:
        audit["cache_compatibility_must_not_replace_audit"] = True
    audit.pop("audit_hash")
    audit["audit_hash"] = digest(audit)
    (tmp_path / "report.json").write_text(json.dumps(original))
    (tmp_path / "audit.json").write_text(json.dumps(audit))
    monkeypatch.setattr(report, "DIRECTORY", tmp_path)
    with pytest.raises(ValueError, match="artifact/profile/implementation mismatch"):
        report.load()


def test_manifest_cannot_relabel_a_changed_profile_even_when_settings_runtime_is_registered(
    monkeypatch,
):
    original = report.profile_hash
    monkeypatch.setattr(
        report,
        "profile_hash",
        lambda name: "e" * 64 if name == "profiles/history_diagnostic_v1.yaml" else original(name),
    )
    with pytest.raises(ValueError, match="artifact/profile/implementation mismatch"):
        report.load()
