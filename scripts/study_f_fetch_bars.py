"""Fetch daily regular-session bars for the Study F research universe.

    uv run python scripts/study_f_fetch_bars.py <universe.txt> <out.csv> [since]

``since`` is an optional inclusive ISO lower bound on the session date:

    ... data/study_f/universe.txt data/study_f/bars.csv                 # every session
    ... data/study_f/universe.txt data/study_g/bars.csv 2026-09-19      # Study G's window

One Unusual Whales request per ticker (``/api/stock/{t}/ohlc/1d``), parsed by the
same ``webapp.ohlc.regular_session_bars`` the live board uses, so the research
panel and the board cannot disagree about what a session's OHLC is.

Why this exists rather than the daily_close job: that job writes ``alfa_daily_bar``,
which the live spot frame reads, and it is scoped to the ten board tickers. Writing
sixty more names into it would pollute a production table with symbols the board
does not track. The research panel is a separate file.

Resumable and append-only: tickers already present in the output are skipped, so a
killed run is restarted by running it again. Every ticker's session count is
recorded in the sidecar ``*.coverage.csv`` — a name with short history must be
excluded by a *stated* rule in the pre-registration, never dropped silently here.
"""
from __future__ import annotations

import asyncio
import csv
import pathlib
import sys
from datetime import date

# pyproject packages only src/uoa_detector, so `webapp` is never installed and a
# script run as `python scripts/x.py` gets scripts/ on sys.path instead of the repo
# root. Same bootstrap as scripts/audit_board_html.py, for the same reason.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from webapp.board.refresher import _LIVE_CALIBRATION_PROFILE

# The private helper, on purpose. It is the single place that filters to
# market_time == "r", drops unparseable dates and orders oldest-first; the board's
# two public readers are both built on it. Research needs the same session filter
# but MORE columns than RegularBar carries (volume), and widening RegularBar would
# ripple into alfa_daily_bar and the live spot frame for no board benefit.
from webapp.ohlc import _regular_session_rows, _to_float

from uoa_detector.calibration import load_profile
from uoa_detector.config.credentials import Credentials

# All three live in the client module; webapp.board.uw_errors re-exports only
# NotFound. Note the hierarchy: UnusualWhalesNotFoundError SUBCLASSES
# UnusualWhalesAuthError, so the not-found except clause below must stay first —
# otherwise a ticker with no data would read as a key failure and abort the run.
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesClient,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
)

_FIELDS = (
    "ticker", "day", "open", "high", "low", "close", "volume", "total_volume",
)


def _already_fetched(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        return {row["ticker"] for row in csv.DictReader(handle) if row.get("ticker")}


def _row_values(ticker: str, day: object, row: dict[str, object]) -> dict[str, object]:
    return {
        "ticker": ticker,
        "day": day.isoformat(),  # type: ignore[attr-defined]
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "close": _to_float(row.get("close")),
        "volume": _to_float(row.get("volume")),
        "total_volume": _to_float(row.get("total_volume")),
    }


async def _fetch_one(client: object, ticker: str) -> list[dict[str, object]]:
    """One ticker's regular sessions, oldest first, with volume kept.

    A date carrying two regular rows that disagree is DROPPED, the same rule
    ``daily_close._unique_bars`` applies in production: a session whose own source
    contradicts itself is not a measurement. Identical duplicates collapse to one.
    """
    payload = await client.request_json(f"/api/stock/{ticker}/ohlc/1d")  # type: ignore[attr-defined]
    seen: dict[object, dict[str, object]] = {}
    conflicting: set[object] = set()
    for day, row in _regular_session_rows(payload.get("data")):
        values = _row_values(ticker, day, row)
        previous = seen.get(day)
        if previous is not None and previous != values:
            conflicting.add(day)
            continue
        seen[day] = values
    return [values for day, values in sorted(seen.items()) if day not in conflicting]


def keep_since(
    rows: list[dict[str, object]], since: str | None,
) -> list[dict[str, object]]:
    """Rows whose session date is on or after ``since``. Inclusive; None keeps all.

    A named function rather than a comprehension inside ``main`` so a test can
    exercise the real boundary instead of restating the comparison — a test that
    rewrites the rule it is checking passes whatever this file does, which is the
    shape of guard this repository has shipped five times.
    """
    if since is None:
        return rows
    return [row for row in rows if str(row["day"]) >= since]


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    universe_path, out_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    # Optional inclusive lower bound on the session date, as an ISO day. Study G is
    # scored on sessions that did not exist when its hypothesis was frozen
    # (docs/study-G-preregistration.md §5), and the cleanest place to enforce that
    # boundary is here — at panel construction — rather than in the search code,
    # which is the committed producer of Study F's published result and must keep
    # behaving exactly as it did. ISO dates compare correctly as strings.
    since = sys.argv[3] if len(sys.argv) > 3 else None
    if since is not None:
        date.fromisoformat(since)  # fail now on a malformed bound, not after 71 requests
        print(f"keeping sessions on or after {since}", flush=True)
    tickers = [
        line.strip().upper()
        for line in universe_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    done = _already_fetched(out_path)
    todo = [t for t in tickers if t not in done]
    print(f"universe {len(tickers)}, already stored {len(done)}, fetching {len(todo)}", flush=True)

    profile = load_profile(_LIVE_CALIBRATION_PROFILE)
    client = UnusualWhalesClient(
        api_key=Credentials().require_unusual_whales_api_key(),
        settings=profile.data_sources.unusual_whales,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not out_path.exists()
    coverage: list[tuple[str, int, str, str]] = []
    requests = 0
    with out_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        if fresh:
            writer.writeheader()
        for ticker in todo:
            try:
                rows = keep_since(await _fetch_one(client, ticker), since)
                requests += 1
            except UnusualWhalesNotFoundError:
                print(f"  {ticker}: no data (not found)", flush=True)
                coverage.append((ticker, 0, "", ""))
                continue
            except (UnusualWhalesAuthError, UnusualWhalesDailyLimitError):
                print(f"  {ticker}: key or budget failure; stopping so the rest stay unfetched")
                raise
            if not rows:
                print(f"  {ticker}: empty payload", flush=True)
                coverage.append((ticker, 0, "", ""))
                continue
            writer.writerows(rows)
            handle.flush()
            coverage.append((ticker, len(rows), str(rows[0]["day"]), str(rows[-1]["day"])))
            print(f"  {ticker}: {len(rows)} sessions {rows[0]['day']} .. {rows[-1]['day']}", flush=True)

    cov_path = out_path.with_suffix(".coverage.csv")
    write_header = not cov_path.exists()
    with cov_path.open("a", encoding="utf-8", newline="") as handle:
        cov = csv.writer(handle)
        if write_header:
            cov.writerow(["ticker", "sessions", "first_day", "last_day"])
        cov.writerows(coverage)

    close = getattr(client, "aclose", None)
    if close is not None:
        await close()
    print(f"done: {requests} requests, coverage -> {cov_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
