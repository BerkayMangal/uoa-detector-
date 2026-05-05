"""Phase 3.3.1.2 tests for ``redact_secrets`` structlog processor.

Pins:
  - keys ending in _key / _token / _secret / _password / _credential
    redact their values
  - bare 'key', 'token', 'secret', 'password', 'credential' redact
  - case-insensitive matching
  - non-credential keys pass through unchanged
  - nested dicts recurse one level
  - lists not recursed (deliberate cost/value tradeoff)
  - integration: structlog pipeline with redact_secrets shows
    no secret in rendered output
"""

from __future__ import annotations

import io
import logging

import pytest
import structlog

from uoa_detector.observability.redact import _should_redact, redact_secrets

# ---------------------------------------------------------------------------
# Key-name matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "api_key",
    "thetadata_api_key",
    "key",
    "access_token",
    "token",
    "password",
    "user_password",
    "secret",
    "client_secret",
    "credential",
    "user_credential",
    # Case-insensitive
    "API_KEY",
    "Api_Key",
    "ACCESS_TOKEN",
])
def test_should_redact_matches_credential_keys(key: str) -> None:
    assert _should_redact(key) is True


@pytest.mark.parametrize("key", [
    "ticker",
    "event",
    "label",
    "max_r",
    "key_metric",  # 'key' as prefix, not suffix
    "tokenizer",   # 'token' as prefix, not suffix
    "eventer",
    "secretly_named",  # underscore not at the right boundary
])
def test_should_not_redact_non_credential_keys(key: str) -> None:
    assert _should_redact(key) is False


# ---------------------------------------------------------------------------
# Processor: top-level redaction
# ---------------------------------------------------------------------------


def test_processor_redacts_top_level_credential_keys() -> None:
    event = {
        "event": "request_sent",
        "api_key": "td_secret_xyz",
        "ticker": "AAPL",
    }
    out = redact_secrets(None, "info", event)
    assert out["api_key"] == "***REDACTED***"
    assert out["event"] == "request_sent"
    assert out["ticker"] == "AAPL"


def test_processor_does_not_mutate_input() -> None:
    """The input event_dict is not modified in place; a new dict is returned.

    This matters because structlog may pass the same event_dict to
    multiple processors; mutating it would cause cross-talk.
    """
    event = {"api_key": "td_secret_xyz", "ticker": "AAPL"}
    redact_secrets(None, "info", event)
    # Input still has the original value.
    assert event["api_key"] == "td_secret_xyz"


def test_processor_redacts_multiple_credential_keys() -> None:
    event = {
        "thetadata_api_key": "td_xyz",
        "uw_token": "uw_abc",
        "user_password": "p4ssw0rd",
        "ticker": "MSFT",
    }
    out = redact_secrets(None, "info", event)
    assert out["thetadata_api_key"] == "***REDACTED***"
    assert out["uw_token"] == "***REDACTED***"
    assert out["user_password"] == "***REDACTED***"
    assert out["ticker"] == "MSFT"


def test_processor_passes_through_non_credential_keys() -> None:
    event = {
        "event": "decision_emitted",
        "ticker": "AAPL",
        "combined_score": 0.85,
        "label": "tier1_uoa",
    }
    out = redact_secrets(None, "info", event)
    assert out == event


# ---------------------------------------------------------------------------
# Nested dict recursion (one level)
# ---------------------------------------------------------------------------


def test_processor_recurses_into_nested_dict() -> None:
    """Logged request payloads with embedded credentials get redacted."""
    event = {
        "event": "outbound_http",
        "method": "GET",
        "headers": {
            "Authorization": "Bearer xyz",  # 'authorization' doesn't match
            "X-API-Key": "td_secret_xyz",   # 'x-api-key' doesn't match either
            "api_key": "td_secret_xyz",      # this one DOES match
        },
        "ticker": "AAPL",
    }
    out = redact_secrets(None, "info", event)
    # 'api_key' inside 'headers' is redacted
    assert out["headers"]["api_key"] == "***REDACTED***"
    # Other headers pass through (we don't entropy-scan; we name-match)
    assert out["headers"]["Authorization"] == "Bearer xyz"
    assert out["headers"]["X-API-Key"] == "td_secret_xyz"


def test_processor_does_not_recurse_beyond_one_level() -> None:
    """Two-level nesting: inner credentials are NOT redacted by design."""
    event = {
        "outer": {
            "middle": {
                "api_key": "td_secret",
            },
        },
    }
    out = redact_secrets(None, "info", event)
    # Inner dict's api_key NOT redacted (depth=2 from top is too deep).
    # The top-level 'outer' key itself is non-credential, so passes through.
    assert out["outer"]["middle"]["api_key"] == "td_secret"


def test_processor_does_not_scan_lists() -> None:
    """List of dicts is NOT scanned for credentials (decision: cost/value)."""
    event = {
        "items": [{"api_key": "td_secret"}, {"foo": "bar"}],
    }
    out = redact_secrets(None, "info", event)
    # api_key inside the list is NOT redacted.
    assert out["items"][0]["api_key"] == "td_secret"


# ---------------------------------------------------------------------------
# End-to-end: structlog pipeline with redact_secrets
# ---------------------------------------------------------------------------


def test_structlog_pipeline_with_redact_secrets_does_not_emit_value() -> None:
    """Configure structlog with redact_secrets; emit a log line with a
    credential key; assert the rendered output never contains the
    secret value."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger("test_redact")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        structlog.configure(
            processors=[
                redact_secrets,
                structlog.processors.add_log_level,
                structlog.processors.KeyValueRenderer(drop_missing=True),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=False,
        )
        logger = structlog.get_logger("test_redact")
        logger.info(
            "outbound_http_request",
            ticker="AAPL",
            api_key="td_secret_xyz_should_never_show",
            uw_token="uw_token_should_never_show",
            url="https://api.example.com/quote",
        )
    finally:
        root.removeHandler(handler)

    rendered = buf.getvalue()
    assert "td_secret_xyz_should_never_show" not in rendered
    assert "uw_token_should_never_show" not in rendered
    # And the redaction marker IS present
    assert "***REDACTED***" in rendered
    # Non-credential keys passed through
    assert "AAPL" in rendered
    assert "outbound_http_request" in rendered


def test_structlog_pipeline_redacts_nested_payload() -> None:
    """A logged 'payload' dict containing api_key is redacted in the
    rendered line."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger("test_redact_nested")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        structlog.configure(
            processors=[
                redact_secrets,
                structlog.processors.KeyValueRenderer(drop_missing=True),
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=False,
        )
        logger = structlog.get_logger("test_redact_nested")
        logger.info(
            "outbound_http",
            payload={"api_key": "td_xyz_should_never_show", "method": "GET"},
        )
    finally:
        root.removeHandler(handler)

    rendered = buf.getvalue()
    assert "td_xyz_should_never_show" not in rendered
    assert "***REDACTED***" in rendered
    assert "method" in rendered  # non-credential key passes through
