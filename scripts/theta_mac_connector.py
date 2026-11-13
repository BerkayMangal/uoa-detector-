#!/usr/bin/env python3
"""Run beside an authenticated Theta Terminal; Python 3.9+, standard library only.

A personalized download embeds a scoped connector token and its trusted UOA
origin, so no password prompt is needed. Unpersonalized copies retain Basic
authentication through environment variables or hidden prompts. Credentials are
never sent to Theta Terminal. The application calculates all results.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import signal
import socket
import ssl
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
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


def run_job(transport: Transport, job: dict[str, Any]) -> str:
    request_id, request_hash = job.get("request_id"), job.get("request_hash")
    if (not isinstance(request_id, str) or not _IDENTITY.fullmatch(request_id)
            or not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash)):
        raise ConnectorError("invalid_request")
    upload: dict[str, Any] = {
        "request_hash": request_hash, "status": "ok", "packets": [],
        "attempted_at": datetime.now(UTC).isoformat(),
    }
    try:
        contract = Contract.from_occ(job.get("option_symbol"))
        start = aware_time(job.get("planned_entry_at"))
        session_close = aware_time(job.get("session_close_at"))
        if session_close <= start or session_close.astimezone(ET).date() != start.astimezone(ET).date():
            raise ConnectorError("invalid_request")
        if start + timedelta(minutes=64) > datetime.now(UTC):
            raise ConnectorError("invalid_request")
        entry = transport.window(contract, start, "entry")
        upload["packets"].append(entry)
        selected = first_quote(entry, contract, session_close)
        if selected is not None:
            for minutes in (15, 60):
                target = selected + timedelta(minutes=minutes)
                if target >= session_close:
                    continue
                packet = transport.window(contract, target, "m" + str(minutes))
                first_quote(packet, contract, session_close)  # Reject an invalid window before uploading.
                upload["packets"].append(packet)
        encode_json(upload)  # The server also limits the entire upload to 8 MiB.
    except ConnectorError as error:
        upload.update(status=error.status, packets=[])
    transport.upload(request_id, upload)
    return str(upload["status"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="process at most four pending jobs (default)")
    modes.add_argument("--watch", action="store_true", help="keep checking while this window is open")
    modes.add_argument("--check", action="store_true", help="check UOA access and the local Terminal port only")
    parser.add_argument("--poll-seconds", type=int, default=60, help="watch interval, at least 60 seconds")
    args = parser.parse_args(argv)
    if args.poll_seconds < 60:
        parser.error("--poll-seconds must be at least 60")
    try:
        terminal = origin(os.environ.get("THETA_TERMINAL_URL", DEFAULT_TERMINAL), terminal=True)
        if EMBEDDED_TOKEN:
            base = origin(EMBEDDED_UOA)
            override = os.environ.get("UOA_BASE_URL")
            if override is not None and origin(override) != base:
                raise ConnectorError("invalid_request")
            transport = Transport(base, terminal, bearer_token=EMBEDDED_TOKEN)
        else:
            base = origin(os.environ.get("UOA_BASE_URL", DEFAULT_UOA))
            username = os.environ.get("UOA_USERNAME") or getpass.getpass("UOA username (hidden): ")
            password = os.environ.get("UOA_PASSWORD") or getpass.getpass("UOA password (hidden): ")
            transport = Transport(base, terminal, username, password)
        if args.check:
            transport.jobs()
            transport.check_terminal()
            print("UOA access OK. Theta Terminal port reachable; login and quote data still require a queued job.")
            return 0
        while True:
            jobs = transport.jobs()
            print(f"Pending jobs: {len(jobs)} (maximum four per pass).", flush=True)
            for index, job in enumerate(jobs, start=1):
                status = run_job(transport, job)
                print(f"Job {index} uploaded: {status}. View the result in UOA.", flush=True)
            if not args.watch:
                return 0
            time.sleep(args.poll_seconds)
    except (KeyboardInterrupt, EOFError):
        print("Connector stopped.")
        return 130
    except ConnectorError as error:
        if EMBEDDED_TOKEN and error.status == "authentication_failed":
            print("Bağlantı anahtarı geçersiz veya süresi dolmuş. UOA'dan yeni bağlantı dosyasını indirin.",
                  file=sys.stderr)
            return 1
        print(f"Connector stopped: {error.status}. Check UOA access and Theta Terminal, then run again.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
