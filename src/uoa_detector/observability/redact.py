"""Log redaction processor — Phase 3.3.1.2.

structlog processor that scans the event_dict for keys matching
common credential-name patterns and replaces their values with
``'***REDACTED***'`` before any renderer sees them.

decision (key-name redaction, not value pattern matching):
  We redact based on the *key name* (e.g., ``api_key``,
  ``password``, ``token``) rather than trying to detect
  high-entropy strings in values. Three reasons:
  1. Speed — a pure dict-key check is O(1) per key; entropy
     scoring on every value is expensive at log volume.
  2. False-negative cost is asymmetric. If the key is named
     ``api_key`` and the value somehow isn't entropy-like (test
     fixture, placeholder), redacting is still correct. The
     opposite — high-entropy ticker symbol or correlation ID
     accidentally redacted — is annoying.
  3. Combined with SecretStr: SecretStr already redacts values
     in repr; this processor catches the case where someone
     log-passes a raw string by mistake. Defense in depth.

decision (suffix matching, not whole-word):
  Keys like ``thetadata_api_key``, ``user_password``,
  ``gh_access_token`` all match. The pattern is ``*_<suffix>``
  with case-insensitive comparison. Suffixes: ``key``, ``token``,
  ``secret``, ``password``, ``credential``.

decision (value type preservation):
  Redacted values are always replaced with the literal string
  ``'***REDACTED***'``. We do NOT preserve the original type
  (e.g., ``SecretStr``) because the renderer downstream may do its
  own escaping and the goal is unambiguous human-readable
  redaction in the log line.

decision (recursive into dicts):
  If a logged kwarg is a dict (e.g., a request body for HTTP
  debug), the processor recurses one level into nested dicts to
  catch ``payload={'api_key': 'xyz'}`` patterns. Lists are NOT
  scanned — that adds traversal cost without a clear win, and
  list-of-creds is an unusual shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger


_REDACTED = "***REDACTED***"

# Suffix match (case-insensitive). Matches keys like 'api_key',
# 'thetadata_api_key', 'access_token', 'user_password', etc.
_REDACT_SUFFIXES: tuple[str, ...] = (
    "key",
    "token",
    "secret",
    "password",
    "credential",
)


def _should_redact(key: str) -> bool:
    """Return True if ``key`` matches a credential-name pattern.

    Bare 'key' or 'token' or 'secret' / 'password' / 'credential'
    or any suffix-style name like 'api_key', 'access_token',
    'user_password' matches. The check is case-insensitive.
    """
    lowered = key.lower()
    return any(
        lowered == suffix or lowered.endswith("_" + suffix)
        for suffix in _REDACT_SUFFIXES
    )


def redact_secrets(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """structlog processor: redact credential-named keys in ``event_dict``.

    Recurses one level into nested dicts so logged request payloads
    don't leak. Lists are NOT recursed.
    """
    return _redact_dict(event_dict, depth=0)


def _redact_dict(d: EventDict, *, depth: int) -> EventDict:
    """Walk a dict and redact credential keys; recurse one level."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if _should_redact(k):
            out[k] = _REDACTED
        elif isinstance(v, dict) and depth < 1:
            out[k] = _redact_dict(v, depth=depth + 1)
        else:
            out[k] = v
    return out
