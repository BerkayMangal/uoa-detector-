#!/usr/bin/env python3
"""Plan or run ten bounded, read-only Theta Terminal capability probes.

Dry-run is the default. --execute is required for network calls and should only
be used after the owner has authorized this Terminal location. Provider login
belongs to Theta Terminal; this script never reads or sends provider secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 10


def build_plan(symbol: str, expiry: str, strike: str, right: str,
               session: str, start_time: str, window_seconds: int) -> list[dict]:
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
        raise ValueError("symbol must identify one underlying; no wildcards")
    expiry_day, session_day = date.fromisoformat(expiry), date.fromisoformat(session)
    if session_day > expiry_day:
        raise ValueError("quote date is after the selected contract expiration")
    try:
        strike_value = Decimal(strike)
    except InvalidOperation as exc:
        raise ValueError("invalid strike") from exc
    if not strike_value.is_finite() or strike_value <= 0:
        raise ValueError("strike must be a positive finite dollar amount")
    if right not in {"call", "put"}:
        raise ValueError("select exactly call or put")
    if not 1 <= window_seconds <= 30:
        raise ValueError("probe window must be 1 through 30 seconds")
    start = datetime.fromisoformat(session + "T" + start_time)
    if start.tzinfo is not None:
        raise ValueError("start-time is local America/New_York, without offset")
    end = start + timedelta(seconds=window_seconds)
    if not (start.date() == end.date() and start.strftime("%H:%M:%S") >= "09:30:00"
            and end.strftime("%H:%M:%S") <= "16:00:00"):
        raise ValueError("probe must lie inside 09:30 through 16:00 New York time")
    contract = {"symbol": symbol, "expiration": expiry,
                "strike": format(strike_value, "f"), "right": right}
    daily = {**contract, "date": session}
    window = {**daily, "start_time": start.strftime("%H:%M:%S.%f"),
              "end_time": end.strftime("%H:%M:%S.%f")}

    def item(name: str, path: str, params: dict, category: str) -> dict:
        return {"name": name, "path": "/v3" + path,
                "params": {**params, "format": "json"}, "category": category}

    return [
        item("expirations", "/option/list/expirations", {"symbol": symbol}, "metadata"),
        item("strikes", "/option/list/strikes", {"symbol": symbol, "expiration": expiry}, "metadata"),
        item("available_quote_dates", "/option/list/dates/quote", contract, "metadata"),
        item("nbbo_ticks", "/option/history/quote", {**window, "interval": "tick"}, "quote"),
        item("trades", "/option/history/trade", window, "trade"),
        item("trade_quote", "/option/history/trade_quote", {**window, "exclusive": "true"}, "trade_quote"),
        item("open_interest", "/option/history/open_interest", daily, "open_interest"),
        item("all_greeks", "/option/history/greeks/all", {**window, "interval": "1s"}, "greeks"),
        item("bid_mid_ask_iv", "/option/history/greeks/implied_volatility", {**window, "interval": "1s"}, "iv"),
        item("end_of_day", "/option/history/eod", {**contract, "start_date": session, "end_date": session}, "eod"),
    ]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify(status: int, payload: object, raw: bytes) -> str:
    if status == 401:
        return "AUTH_REQUIRED"
    if status == 403:
        return "ACCESS_DENIED"  # Do not assume subscription, network, or credentials.
    if status == 472:
        return "NO_DATA"  # Existing repository documents this Theta status.
    if status == 429:
        return "RATE_LIMITED"
    if status in {400, 404, 405, 422}:
        return "REQUEST_OR_ENDPOINT_UNSUPPORTED"
    if 300 <= status < 400:
        return "UNEXPECTED_REDIRECT"
    if status >= 500:
        return "SERVICE_ERROR"
    if status != 200:
        return "HTTP_ERROR"
    if payload is None:
        return "NON_JSON_RESPONSE" if raw.strip() else "EMPTY_BODY"
    if payload == []:
        return "EMPTY_DATA"
    if isinstance(payload, list):
        return "DATA_RETURNED_UNVALIDATED"
    if isinstance(payload, dict):
        for key in ("response", "data"):
            if isinstance(payload.get(key), list):
                entries = payload[key]
                if not entries:
                    return "EMPTY_DATA"
                if all(isinstance(x, dict) and isinstance(x.get("data"), list) for x in entries):
                    return "DATA_RETURNED_UNVALIDATED" if any(x["data"] for x in entries) else "EMPTY_DATA"
                return "DATA_RETURNED_UNVALIDATED"
        return "UNEXPECTED_JSON_SHAPE"
    return "UNEXPECTED_JSON_SHAPE"


def execute(base_url: str, plan: list[dict], output: Path) -> dict:
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("base URL must be an HTTP(S) Terminal origin without credentials/query/fragment")
    if parsed.path.rstrip("/") not in {"", "/v3"}:
        raise ValueError("base URL path must be empty or /v3")
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if len(plan) > MAX_REQUESTS:
        raise ValueError("request budget exceeded")
    output.mkdir(parents=True, exist_ok=False)
    opener = urllib.request.build_opener(NoRedirect())  # Uses inherited proxy/CA configuration.
    records, stop_reason = [], None
    for index, request in enumerate(plan):
        rec = {**request, "requested_at": datetime.now(UTC).isoformat()}
        if stop_reason:
            rec["status"] = "SKIPPED_" + stop_reason
            records.append(rec)
            continue
        raw, code = b"", 0
        url = origin + request["path"] + "?" + urllib.parse.urlencode(request["params"])
        try:
            try:
                response = opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=15)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                code = response.code
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                rec["content_type"] = response.headers.get("Content-Type")
                rec["retry_after"] = response.headers.get("Retry-After")
            if len(raw) > MAX_RESPONSE_BYTES:
                rec["status"] = "RESPONSE_TOO_LARGE"
                rec["raw_truncated"] = True
                raw = raw[:MAX_RESPONSE_BYTES]
            else:
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    payload = None
                rec["status"] = classify(code, payload, raw)
                rec["raw_truncated"] = False
                if isinstance(payload, dict):
                    rec["top_level_keys"] = sorted(payload)
                elif isinstance(payload, list):
                    rec["top_level_rows"] = len(payload)
            rec["http_status"] = code
            rec["bytes_saved"] = len(raw)
            rec["sha256"] = hashlib.sha256(raw).hexdigest()
            filename = f"{index + 1:02d}-{request['name']}.response"
            (output / filename).write_bytes(raw)
            rec["raw_file"] = filename
            if code in {401, 429}:
                stop_reason = rec["status"]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            rec["status"] = "TRANSPORT_ERROR"
            rec["error_type"] = type(exc).__name__  # Avoid echoing URLs or environment values.
            stop_reason = "TRANSPORT_ERROR"
        rec["completed_at"] = datetime.now(UTC).isoformat()
        records.append(rec)
        (output / "manifest.json").write_text(json.dumps({"requests": records}, indent=2))
        print(request["name"], rec["status"], flush=True)
        if index + 1 < len(plan):
            time.sleep(0.75)
    report = {"generated_at": datetime.now(UTC).isoformat(), "request_budget": MAX_REQUESTS,
              "source": "Theta Terminal; actual returned data remains unvalidated until contract/time checks",
              "credentials": "Handled by Terminal; no provider credentials read or transmitted by this script",
              "requests": records}
    (output / "manifest.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", required=True)
    parser.add_argument("--strike", required=True)
    parser.add_argument("--right", choices=["call", "put"], required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--start-time", default="10:00:00", help="America/New_York local time")
    parser.add_argument("--window-seconds", type=int, default=10)
    parser.add_argument("--base-url", default=os.environ.get("THETADATA_BASE_URL"))
    parser.add_argument("--out", type=Path, default=Path("work/theta-capability-probe"))
    parser.add_argument("--execute", action="store_true", help="Actually call the authorized Terminal; default only prints plan")
    args = parser.parse_args()
    try:
        plan = build_plan(args.symbol.upper(), args.expiry, args.strike, args.right,
                          args.date, args.start_time, args.window_seconds)
        if not args.execute:
            print(json.dumps({"mode": "DRY_RUN_NO_NETWORK", "requests": plan}, indent=2))
            return 0
        if not args.base_url:
            parser.error("--execute requires --base-url or THETADATA_BASE_URL identifying the authorized Terminal")
        execute(args.base_url, plan, args.out)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
