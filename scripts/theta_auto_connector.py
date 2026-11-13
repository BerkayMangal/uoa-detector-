#!/usr/bin/env python3
"""Continuous device connector; Python 3.9+, standard library only.

Native quote/transport helpers below preserve Phase 5.68 behavior and bounds.
A separately enrolled, revocable device handles current and future validation
jobs only. Install once on the Mac; the application calculates all results.
No credential prompts, remote Terminal exposure, or order capability.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import plistlib
import random
import re
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

UTC = timezone.utc  # noqa: UP017 -- standalone connector supports Python 3.9.
ET = ZoneInfo("America/New_York")
DEFAULT_UOA = "https://uoa-detector-production.up.railway.app"
DEFAULT_TERMINAL = "http://127.0.0.1:25503"
# The authenticated download route replaces each exact marker once.
EMBEDDED_TOKEN = ""
EMBEDDED_UOA = ""
MAX_BYTES = 8 * 1024 * 1024
MAX_JOBS = 4
MAX_ROWS = 50_000
TIMEOUT_SECONDS = 20
WINDOW_SECONDS = 120
_OCC = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})")
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ConnectorError(Exception):
    """A fixed safe code; never expose a URL, HTTP body, or credentials."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectorError("request_failed")


def terminal_clock(moment: datetime, *, round_up: bool) -> str:
    """Theta Terminal v3 accepts at most milliseconds (six digits give HTTP 400; Phase 5.70.2).

    Quote timestamps are millisecond-resolution, so rounding the start up and the end down keeps exactly the
    quotes inside the original window and never admits one earlier than the start.
    """
    local = moment.astimezone(ET)
    extra = local.microsecond % 1000
    if extra:
        local += timedelta(microseconds=1000 - extra if round_up else -extra)
    return local.time().isoformat(timespec="milliseconds")


@contextmanager
def request_timeout(status: str):
    """Mac/POSIX main-thread deadline includes connect, headers, and body reads."""
    def expire(_signum, _frame):
        raise ConnectorError(status)

    previous_handler = signal.signal(signal.SIGALRM, expire)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def origin(value: str, *, terminal: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConnectorError("invalid_request") from None
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path not in ("", "/")
        or parsed.scheme not in ("http", "https")
        or (parsed.scheme == "http" and not local)
        or (terminal and not local) or (port is not None and port < 1)
    ):
        raise ConnectorError("invalid_request")
    return value.rstrip("/")


def aware_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ConnectorError("invalid_request")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(UTC)
    except ValueError:
        raise ConnectorError("invalid_request") from None


def native_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ConnectorError("unsupported_payload")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            first = stamp.replace(tzinfo=ET, fold=0)
            second = stamp.replace(tzinfo=ET, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError
            stamp = first
        return stamp.astimezone(UTC)
    except ValueError:
        raise ConnectorError("unsupported_payload") from None


def number(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ConnectorError("unsupported_payload")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConnectorError("unsupported_payload") from None
    if not result.is_finite():
        raise ConnectorError("unsupported_payload")
    return result


@dataclass(frozen=True)
class Contract:
    symbol: str
    expiration: date
    strike: Decimal
    right: str

    @classmethod
    def from_occ(cls, symbol: object) -> Contract:
        match = _OCC.fullmatch(symbol) if isinstance(symbol, str) else None
        if match is None:
            raise ConnectorError("invalid_request")
        try:
            expiry = datetime.strptime(match.group(2), "%y%m%d").date()
        except ValueError:
            raise ConnectorError("invalid_request") from None
        strike = Decimal(match.group(4)) / 1000
        if strike <= 0:
            raise ConnectorError("invalid_request")
        return cls(match.group(1), expiry, strike, "call" if match.group(3) == "C" else "put")

    def matches(self, identity: object) -> bool:
        if not isinstance(identity, dict):
            return False
        expiry = str(identity.get("expiration", ""))
        if len(expiry) == 8 and expiry.isdigit():
            expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        right = str(identity.get("right", "")).lower()
        right = {"c": "call", "p": "put"}.get(right, right)
        return (
            identity.get("symbol") == self.symbol and expiry == self.expiration.isoformat()
            and number(identity.get("strike")) == self.strike and right == self.right
        )


def first_quote(
    packet: dict[str, Any], contract: Contract, session_close: datetime,
) -> datetime | None:
    """Select only from a completely valid native packet; no synthetic timestamps."""
    if packet["http_status"] == 472:
        return None
    if packet["http_status"] != 200:
        raise ConnectorError("unsupported_payload")
    start, end, received = (aware_time(packet[key]) for key in
                            ("window_start", "window_end", "received_at"))
    payload = packet["payload"]
    pending = [payload]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if any(node.get(key) not in (None, "", False) for key in
                   ("next_page", "next_page_url", "next_cursor", "has_more")):
                raise ConnectorError("unsupported_payload")
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    if isinstance(payload, dict):
        groups = [payload] if "contract" in payload else payload.get("response", payload.get("data"))
    else:
        groups = payload
    if not isinstance(groups, list):
        raise ConnectorError("unsupported_payload")
    seen: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    count = 0
    for group in groups:
        if not isinstance(group, dict):
            raise ConnectorError("unsupported_payload")
        group_identity = group.get("contract", group)
        if not contract.matches(group_identity):
            raise ConnectorError("unsupported_payload")
        data = group.get("data") if "contract" in group else [group]
        if not isinstance(data, list):
            raise ConnectorError("unsupported_payload")
        for row in data:
            count += 1
            if count > MAX_ROWS:
                raise ConnectorError("response_too_large")
            if not isinstance(row, dict):
                raise ConnectorError("unsupported_payload")
            identity = dict(group_identity)
            identity.update({key: row[key] for key in ("symbol", "expiration", "strike", "right")
                             if key in row})
            if not contract.matches(identity):
                raise ConnectorError("unsupported_payload")
            stamp = native_time(row.get("quote_timestamp", row.get("timestamp")))
            if ("quote_timestamp" in row and "timestamp" in row
                    and native_time(row["quote_timestamp"]) != native_time(row["timestamp"])):
                raise ConnectorError("unsupported_payload")
            if not start <= stamp <= end or stamp > received:
                raise ConnectorError("unsupported_payload")
            values = tuple(number(row.get(key)) for key in ("bid", "ask", "bid_size", "ask_size"))
            bid, ask, bid_size, ask_size = values
            if bid < 0 or ask < bid or any(size < 0 or size != size.to_integral_value()
                                         for size in (bid_size, ask_size)):
                raise ConnectorError("unsupported_payload")
            if stamp in seen and seen[stamp] != values:
                raise ConnectorError("unsupported_payload")
            seen[stamp] = values
    for stamp, values in sorted(seen.items()):
        bid, ask, bid_size, ask_size = values
        # The job supplies the server-validated official early-close boundary.
        local = stamp.astimezone(ET)
        clock = local.hour * 60 + local.minute
        if (local.weekday() < 5 and 570 <= clock < 960 and stamp < session_close
                and bid > 0 and ask > 0 and min(bid_size, ask_size) >= 1):
            return stamp
    return None


def encode_json(value: object) -> bytes:
    try:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise ConnectorError("unsupported_payload") from None
    if len(body) > MAX_BYTES:
        raise ConnectorError("response_too_large")
    return body


def bad_constant(value: str) -> None:
    raise ConnectorError("unsupported_payload")


class Transport:
    def __init__(
        self, base_url: str, terminal_url: str, username: str = "", password: str = "",
        *, bearer_token: str | None = None,
    ) -> None:
        self.base = origin(base_url)
        self.terminal = origin(terminal_url, terminal=True)
        if bearer_token is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", bearer_token):
                raise ConnectorError("invalid_request")
            self._authorization = "Bearer " + bearer_token
        else:
            if not username or not password or ":" in username:
                raise ConnectorError("invalid_request")
            self._authorization = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        self._remote = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        self._local = build_opener(ProxyHandler({}), NoRedirect(),
                                   HTTPSHandler(context=ssl.create_default_context()))

    def _request(self, url: str, *, terminal: bool = False, data: bytes | None = None) -> tuple[int, object]:
        destination = urlsplit(url)
        expected = self.terminal if terminal else self.base
        if origin(destination.scheme + "://" + destination.netloc, terminal=terminal) != expected:
            raise ConnectorError("invalid_request")
        headers = {"Accept": "application/json"}
        if not terminal:
            headers["Authorization"] = self._authorization
        if data is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.base})
        request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        opener = self._local if terminal else self._remote
        try:
            deadline = time.monotonic() + TIMEOUT_SECONDS
            with request_timeout("terminal_unreachable" if terminal else "request_failed"), opener.open(
                request, timeout=TIMEOUT_SECONDS,
            ) as response:
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                    raise ConnectorError("response_too_large")
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ConnectorError("terminal_unreachable" if terminal else "request_failed")
                    chunk = response.read(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ConnectorError("response_too_large")
                    chunks.append(chunk)
                body = b"".join(chunks)
                code = response.status
            try:
                return code, json.loads(body, parse_constant=bad_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise ConnectorError("unsupported_payload") from None
        except HTTPError as error:
            code = error.code
            error.close()
            if terminal and code == 472:
                return 472, None
            if code in (401, 403):
                raise ConnectorError("authentication_failed") from None
            raise ConnectorError("request_failed") from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise ConnectorError("terminal_unreachable" if terminal else "request_failed") from None

    def jobs(self) -> list[dict[str, Any]]:
        _, payload = self._request(self.base + "/api/theta/jobs?limit=4")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ConnectorError("unsupported_payload")
        if len(payload["jobs"]) > MAX_JOBS or any(not isinstance(job, dict) for job in payload["jobs"]):
            raise ConnectorError("unsupported_payload")
        return payload["jobs"]

    def window(self, contract: Contract, start: datetime, label: str) -> dict[str, Any]:
        end = start + timedelta(seconds=WINDOW_SECONDS)
        local = start.astimezone(ET)
        if (end > datetime.now(UTC) or local.date() != end.astimezone(ET).date()
                or contract.expiration < local.date()):
            raise ConnectorError("invalid_request")
        params = {
            "symbol": contract.symbol, "expiration": contract.expiration.strftime("%Y%m%d"),
            "strike": format(contract.strike, "f"), "right": contract.right,
            "date": local.strftime("%Y%m%d"), "interval": "tick", "format": "json",
            "start_time": terminal_clock(start, round_up=True),
            "end_time": terminal_clock(end, round_up=False),
        }
        code, payload = self._request(self.terminal + "/v3/option/history/quote?" + urlencode(params), terminal=True)
        return {
            "label": label, "window_start": start.isoformat(), "window_end": end.isoformat(),
            "received_at": datetime.now(UTC).isoformat(), "payload": payload,
            "http_status": code, "requested_interval": "tick",
        }

    def upload(self, request_id: str, payload: dict[str, Any]) -> object:
        _, result = self._request(self.base + "/api/theta/results/" + quote(request_id, safe=""),
                                  data=encode_json(payload))
        return result

    def check_terminal(self) -> None:
        parsed = urlsplit(self.terminal)
        try:
            with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=3):
                pass
        except OSError:
            raise ConnectorError("terminal_unreachable") from None


VERSION = "5.70"
AGENT_LABEL = "com.uoa.theta-connector"
MAX_OUTBOX_BYTES = 64 * 1024 * 1024
MAX_OUTBOX_FILES = 8
_JOB_ID = re.compile(r"theta_[0-9a-f]{32}")
_SPOOL_NAME = re.compile(r"(theta_[0-9a-f]{32})_([0-9a-f]{64})\.json")
_TRANSIENT_TERMINAL = {"terminal_unreachable", "theta_authentication_failed"}
_DEFINITE_UPLOAD_REJECTIONS = {
    "completed_result_is_immutable", "attempt_limit_reached", "request_hash_mismatch",
    "request_not_found", "invalid_theta_upload", "failure_upload_must_not_contain_packets",
    "unique_entry_packet_required", "packet_query_window_mismatch", "packet_contract_or_request_invalid",
    "both_exit_packets_required_after_valid_entry", "exit_packets_without_valid_entry",
    "report_terminal_health_without_consuming_job",
}
_QUOTE_VALIDATION_REJECTIONS = {
    "invalid_theta_upload", "failure_upload_must_not_contain_packets", "unique_entry_packet_required",
    "packet_query_window_mismatch", "packet_contract_or_request_invalid",
    "both_exit_packets_required_after_valid_entry", "exit_packets_without_valid_entry",
}


class UploadRejectedError(ConnectorError):
    def __init__(self, reason: str, http_status: int) -> None:
        super().__init__("upload_failed")
        self.reason = reason
        self.http_status = http_status


class UploadResponseGuard:
    """Only bounded, allowlisted server refusals are definite acknowledgments."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def open(self, request: Request, timeout: int):
        try:
            return self.delegate.open(request, timeout=timeout)
        except HTTPError as error:
            if error.code not in (404, 409, 422):
                raise
            try:
                raw = error.read(4097)
                body = json.loads(raw) if len(raw) <= 4096 else None
                reason = body.get("detail") if isinstance(body, dict) else None
            except (OSError, ValueError, UnicodeError, RecursionError):
                reason = None
            finally:
                error.close()
            if isinstance(reason, str) and reason in _DEFINITE_UPLOAD_REJECTIONS:
                raise UploadRejectedError(reason, error.code) from None
            raise ConnectorError("request_failed") from None


class AutoTransport(Transport):
    """The device credential is confined to exactly the pinned UOA origin."""

    def _request(self, url: str, *, terminal: bool = False, data: bytes | None = None) -> tuple[int, object]:
        try:
            return super()._request(url, terminal=terminal, data=data)
        except ConnectorError as error:
            if error.status == "authentication_failed":
                raise ConnectorError("theta_authentication_failed" if terminal else "uoa_authentication_failed") from None
            if terminal and error.status == "request_failed":
                raise ConnectorError("terminal_unreachable") from None
            raise

    def jobs(self) -> list[dict[str, Any]]:
        _, payload = self._request(self.base + "/api/theta/device/jobs?limit=4")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ConnectorError("unsupported_payload")
        if len(payload["jobs"]) > MAX_JOBS or any(not isinstance(job, dict) for job in payload["jobs"]):
            raise ConnectorError("unsupported_payload")
        return payload["jobs"]

    def heartbeat(self, terminal_state: str, last_error: str | None, outbox_count: int) -> None:
        self._request(self.base + "/api/theta/device/heartbeat", data=encode_json({
            "connector_version": VERSION, "terminal_state": terminal_state,
            "last_error": last_error, "outbox_count": outbox_count,
        }))

    def upload_raw(self, request_id: str, body: bytes) -> None:
        if not _JOB_ID.fullmatch(request_id) or len(body) > MAX_BYTES:
            raise ConnectorError("invalid_request")
        remote = self._remote
        self._remote = UploadResponseGuard(remote)
        try:
            _, result = self._request(self.base + "/api/theta/device/results/" + request_id, data=body)
        finally:
            self._remote = remote
        if (not isinstance(result, dict) or result.get("request_id") != request_id
                or not isinstance(result.get("receipt_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["receipt_id"])):
            raise ConnectorError("upload_acknowledgment_invalid")


def app_directory() -> Path:
    return Path.home() / "Library" / "Application Support" / "UOATheta"


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ConnectorError("unsafe_local_path")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ConnectorError("unsafe_local_path")
    path.chmod(0o700)


def private_write(path: Path, body: bytes) -> None:
    """Create mode0600 before writing; atomically replace only an owned path."""
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid())):
        raise ConnectorError("unsafe_local_path")
    fd, temporary = tempfile.mkstemp(prefix=".uoa-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_read(path: Path, maximum: int) -> bytes:
    if path.is_symlink():
        raise ConnectorError("unsafe_local_path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > maximum):
                raise ConnectorError("unsafe_local_file")
            body = stream.read(maximum + 1)
        if len(body) > maximum:
            raise ConnectorError("unsafe_local_file")
        return body
    except FileNotFoundError:
        raise ConnectorError("device_not_enrolled") from None


class Outbox:
    """Immutable upload bytes survive ambiguous network acknowledgments."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        private_directory(directory)

    def files(self) -> list[Path]:
        files = sorted(self.directory.glob("*.json"))
        if len(files) > MAX_OUTBOX_FILES or any(not _SPOOL_NAME.fullmatch(path.name) for path in files):
            raise ConnectorError("outbox_invalid")
        return files

    def save(self, request_id: str, body: bytes) -> Path:
        if not _JOB_ID.fullmatch(request_id) or len(body) > MAX_BYTES:
            raise ConnectorError("invalid_request")
        digest = hashlib.sha256(body).hexdigest()
        path = self.directory / (request_id + "_" + digest + ".json")
        files = self.files()
        if path in files:
            if private_read(path, MAX_BYTES) != body:
                raise ConnectorError("outbox_invalid")
            return path
        rejected = self.rejected_files()
        if (len(files) >= MAX_OUTBOX_FILES
                or sum(item.stat().st_size for item in files + rejected) + len(body) > MAX_OUTBOX_BYTES):
            raise ConnectorError("outbox_full")
        private_write(path, body)
        return path

    def rejected_files(self) -> list[Path]:
        directory = self.directory / "rejected"
        if directory.is_symlink():
            raise ConnectorError("unsafe_local_path")
        files = sorted(directory.glob("*.json"))
        if len(files) > 32 or any(not _SPOOL_NAME.fullmatch(path.name) for path in files):
            raise ConnectorError("outbox_invalid")
        return files

    def retain_rejection(self, path: Path, body: bytes, reason: str) -> None:
        directory = self.directory / "rejected"
        private_directory(directory)
        target = directory / path.name
        files = self.rejected_files()
        if target not in files and len(files) >= 32:
            raise ConnectorError("outbox_full")
        if target in files and private_read(target, MAX_BYTES) != body:
            raise ConnectorError("outbox_invalid")
        private_write(target, body)
        private_write(target.with_suffix(".reason"), reason.encode())
        path.unlink()
        report("Kaynak yanıtı inceleme için korundu: " + reason, error=True)

    def flush(self, transport: AutoTransport, *, terminal_state: str | None = None) -> int:
        rejected = 0
        for path in self.files():
            match = _SPOOL_NAME.fullmatch(path.name)
            if match is None:
                raise ConnectorError("outbox_invalid")
            body = private_read(path, MAX_BYTES)
            if hashlib.sha256(body).hexdigest() != match.group(2):
                raise ConnectorError("outbox_invalid")
            if terminal_state is not None:
                transport.heartbeat(terminal_state, "upload_failed" if self.rejected_files() else None, len(self.files()))
            try:
                transport.upload_raw(match.group(1), body)
            except UploadRejectedError as error:
                self.retain_rejection(path, body, error.reason)
                rejected += 1
                if error.http_status == 422 and error.reason in _QUOTE_VALIDATION_REJECTIONS:
                    try:
                        original = json.loads(body)
                        # A failed quote-validation attempt is truthful UNKNOWN;
                        # rejected failure receipts never recursively create more.
                        if isinstance(original, dict) and original.get("status") == "ok":
                            failure = {"request_hash": original["request_hash"],
                                       "attempted_at": original.get("attempted_at"),
                                       "status": "unsupported_payload", "packets": []}
                            self.save(match.group(1), encode_json(failure))
                    except (ValueError, KeyError, TypeError, RecursionError):
                        raise ConnectorError("outbox_invalid") from None
                continue
            path.unlink()  # Only a validated acknowledgment permits removal.
        return rejected


def collect_job(transport: AutoTransport, job: dict[str, Any]) -> tuple[str, bytes]:
    request_id, request_hash = job.get("request_id"), job.get("request_hash")
    if (not isinstance(request_id, str) or not _JOB_ID.fullmatch(request_id)
            or not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
        raise ConnectorError("invalid_request")
    upload: dict[str, Any] = {
        "request_hash": request_hash, "status": "ok", "packets": [],
        "attempted_at": datetime.now(UTC).isoformat(),
    }
    try:
        contract = Contract.from_occ(job.get("option_symbol"))
        start = aware_time(job.get("planned_entry_at"))
        closing = aware_time(job.get("session_close_at"))
        if (closing <= start or closing.astimezone(ET).date() != start.astimezone(ET).date()
                or start + timedelta(minutes=64) > datetime.now(UTC)):
            raise ConnectorError("invalid_request")
        entry = transport.window(contract, start, "entry")
        upload["packets"].append(entry)
        selected = first_quote(entry, contract, closing)
        if selected is not None:
            for minutes in (15, 60):
                target = selected + timedelta(minutes=minutes)
                if target >= closing:
                    continue
                packet = transport.window(contract, target, "m" + str(minutes))
                first_quote(packet, contract, closing)
                upload["packets"].append(packet)
        encode_json(upload)
    except ConnectorError as error:
        if error.status in _TRANSIENT_TERMINAL:
            raise  # A transport outage is health, never a consumed data attempt.
        upload.update(status=error.status, packets=[])
    return request_id, encode_json(upload)


def cycle(transport: AutoTransport, outbox: Outbox) -> bool:
    rejected = outbox.flush(transport, terminal_state="unknown")  # Replay while Terminal is down.
    try:
        transport.check_terminal()
    except ConnectorError as error:
        transport.heartbeat("unreachable", error.status, len(outbox.files()))
        return False
    transport.heartbeat("reachable", "upload_failed" if rejected or outbox.rejected_files() else None, len(outbox.files()))
    for job in transport.jobs():
        try:
            request_id, body = collect_job(transport, job)
        except ConnectorError as error:
            if error.status not in _TRANSIENT_TERMINAL:
                raise
            state = "authentication_failed" if error.status == "theta_authentication_failed" else "unreachable"
            health_error = "authentication_failed" if state == "authentication_failed" else error.status
            transport.heartbeat(state, health_error, len(outbox.files()))
            return False
        outbox.save(request_id, body)
        outbox.flush(transport, terminal_state="reachable")
        transport.heartbeat("reachable", "upload_failed" if outbox.rejected_files() else None, len(outbox.files()))
    return True


def reconnect_delay(failures: int) -> float:
    return min(300.0, 60.0 * 2 ** min(max(failures - 1, 0), 3) + random.uniform(0, 5))


def report(message: str, *, error: bool = False) -> None:
    """Only fixed messages/codes reach this bounded private log."""
    print(message, file=sys.stderr if error else sys.stdout, flush=True)
    directory = app_directory()
    if not directory.is_dir() or directory.is_symlink():
        return
    path = directory / "connector.log"
    previous = directory / "connector.log.1"
    try:
        if path.is_symlink() or previous.is_symlink():
            return
        if path.exists() and path.stat().st_size >= 256 * 1024:
            os.replace(path, previous)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            if os.fstat(stream.fileno()).st_uid != os.getuid():
                return
            os.fchmod(stream.fileno(), 0o600)
            stream.write(datetime.now(UTC).isoformat() + " " + message + "\n")
    except OSError:
        pass  # Logging cannot interrupt safe reconnect or disclose private errors.


def run_loop(transport: AutoTransport, outbox: Outbox, *, once: bool = False) -> int:
    failures = 0
    while True:
        terminal_wait = False
        try:
            succeeded = cycle(transport, outbox)
            terminal_wait = not succeeded
            failures = 0 if succeeded else failures + 1
            report("UOA bağlantısı açık; veri ve cihaz durumunu UOA'dan izleyebilirsin."
                   if succeeded else "Theta Terminal bekleniyor; bağlantı otomatik yeniden denenecek.")
        except ConnectorError as error:
            if error.status == "uoa_authentication_failed":
                report("UOA cihaz yetkisi geçersiz veya kaldırılmış. UOA'dan yeni cihaz bağlantısı hazırla.", error=True)
                return 0  # A supervisor must not spin on a revoked credential.
            failures += 1
            report("Bağlantı yeniden denenecek: " + error.status, error=True)
            if error.status == "outbox_full":
                # This bounded retention limit needs owner review, never deletion.
                with suppress(ConnectorError):
                    transport.heartbeat("unknown", "upload_failed", len(outbox.files()))
        if once:
            return 1 if failures else 0
        time.sleep(reconnect_delay(failures) if failures and not terminal_wait else 60)


def configuration(directory: Path) -> tuple[str, str, str]:
    if EMBEDDED_TOKEN:
        token, base = EMBEDDED_TOKEN, origin(EMBEDDED_UOA)
        terminal = origin(os.environ.get("THETA_TERMINAL_URL", DEFAULT_TERMINAL), terminal=True)
    else:
        try:
            saved = json.loads(private_read(directory / "device.json", 4096))
            token, base, terminal = saved["token"], origin(saved["base_url"]), origin(saved["terminal_url"], terminal=True)
            if base != origin(EMBEDDED_UOA):
                raise ConnectorError("invalid_request")
        except (ValueError, KeyError, TypeError):
            raise ConnectorError("device_not_enrolled") from None
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ConnectorError("device_not_enrolled")
    override = os.environ.get("UOA_BASE_URL")
    if override is not None and origin(override) != base:
        raise ConnectorError("invalid_request")
    return base, terminal, token


def launchctl(*arguments: str, required: bool = True) -> None:
    completed = subprocess.run(["/bin/launchctl", *arguments], capture_output=True, timeout=20, check=False)
    if required and completed.returncode:
        raise ConnectorError("mac_service_install_failed")


def install(directory: Path, base: str, terminal: str, token: str) -> None:
    if sys.platform != "darwin":
        raise ConnectorError("mac_required_for_install")
    private_directory(directory)
    agents = Path.home() / "Library" / "LaunchAgents"
    if agents.is_symlink():
        raise ConnectorError("unsafe_local_path")
    agents.mkdir(parents=True, exist_ok=True)
    target = directory / "connector.py"
    source = Path(__file__).read_text()
    lines = source.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith("EMBEDDED_TOKEN = ")]
    if len(matches) != 1:
        raise ConnectorError("invalid_installer_source")
    lines[matches[0]] = 'EMBEDDED_TOKEN = ""\n'
    launchctl("bootout", "gui/" + str(os.getuid()) + "/" + AGENT_LABEL, required=False)
    private_write(directory / "device.json", encode_json({"token": token, "base_url": base, "terminal_url": terminal}))
    private_write(target, "".join(lines).encode())
    # The downloaded personalized source also contains its restricted credential.
    if not Path(__file__).is_symlink():
        Path(__file__).chmod(0o600)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(target), "--watch"],
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 60,
        "WorkingDirectory": str(directory),
        # Safe fixed-code output is retained in a bounded log by the process.
    }
    agent = agents / (AGENT_LABEL + ".plist")
    private_write(agent, plistlib.dumps(plist))
    launchctl("bootstrap", "gui/" + str(os.getuid()), str(agent))
    print("Kurulum tamamlandı. Mac oturumun açıldığında bağlantı otomatik başlar. ThetaData Terminal açık ve oturum açılmış olmalı.")


def uninstall(directory: Path) -> None:
    if sys.platform != "darwin":
        raise ConnectorError("mac_required_for_install")
    launchctl("bootout", "gui/" + str(os.getuid()) + "/" + AGENT_LABEL, required=False)
    agent = Path.home() / "Library" / "LaunchAgents" / (AGENT_LABEL + ".plist")
    if agent.exists():
        body = plistlib.loads(private_read(agent, 16384))
        if body.get("Label") != AGENT_LABEL:
            raise ConnectorError("unsafe_local_file")
        agent.unlink()
    if directory.is_symlink():
        raise ConnectorError("unsafe_local_path")
    for name in ("device.json", "connector.py", "connector.lock", "connector.log", "connector.log.1"):
        path = directory / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                raise ConnectorError("unsafe_local_path")
            path.unlink()
    spool = directory / "outbox"
    if spool.exists():
        if spool.is_symlink():
            raise ConnectorError("unsafe_local_path")
        for path in Outbox(spool).files():
            private_read(path, MAX_BYTES)
            path.unlink()
        if not any(spool.iterdir()):
            spool.rmdir()
    if directory.exists() and not any(directory.iterdir()):
        directory.rmdir()
    print("Mac bağlantısı kaldırıldı. UOA'daki cihaz yetkisini de 'Bağlantıyı kaldır' ile iptal edebilirsin.")


def auto_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--install", action="store_true", help="install the current user's automatic Mac connection")
    modes.add_argument("--uninstall", action="store_true", help="remove this app's local connection files")
    modes.add_argument("--once", action="store_true", help="process one bounded pass")
    modes.add_argument("--watch", action="store_true", help="reconnect automatically (default)")
    modes.add_argument("--check", action="store_true", help="report UOA access and Terminal TCP health")
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 9):  # noqa: UP036 -- downloaded script supports Python 3.9.
            raise ConnectorError("python39_required")
        directory = app_directory()
        if args.uninstall:
            uninstall(directory)
            return 0
        # Browser download permissions are outside this program's control until
        # first execution. Every personalized mode makes the owned source private.
        if EMBEDDED_TOKEN:
            source_path = Path(__file__)
            if source_path.is_symlink() or source_path.stat().st_uid != os.getuid():
                raise ConnectorError("unsafe_local_path")
            source_path.chmod(0o600)
        base, terminal, token = configuration(directory)
        transport = AutoTransport(base, terminal, bearer_token=token)
        if args.install:
            transport.jobs()  # Verify the device before replacing local state; zero jobs is valid.
            install(directory, base, terminal, token)
            return 0
        private_directory(directory)
        lock_path = directory / "connector.lock"
        if lock_path.is_symlink():
            raise ConnectorError("unsafe_local_path")
        with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("UOA bağlantısı zaten çalışıyor.")
                return 0
            outbox = Outbox(directory / "outbox")
            if args.check:
                transport.jobs()
                try:
                    transport.check_terminal()
                except ConnectorError as error:
                    transport.heartbeat("unreachable", error.status, len(outbox.files()))
                    raise
                transport.heartbeat("reachable", None, len(outbox.files()))
                print("UOA erişimi ve Terminal portu açık. Giriş yetkisi ve gerçek kotasyon verisi henüz doğrulanmış değildir.")
                return 0
            return run_loop(transport, outbox, once=args.once)
    except (KeyboardInterrupt, EOFError):
        print("Bağlantı durduruldu.")
        return 130
    except ConnectorError as error:
        print("Bağlantı durumu: " + error.status, file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Bağlantı durumu: local_setup_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(auto_main())
