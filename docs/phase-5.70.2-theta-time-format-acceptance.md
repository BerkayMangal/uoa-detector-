# Phase 5.70.2 — Send Theta Terminal window times in milliseconds

Frozen before this repair, 13 November 2026. Found while handing over the
automatic Theta measurement (Phase 5.70), whose eight queued jobs still show
zero attempts.

## Finding

The installed Mac connector (5.70) and the bounded pairing connector build the
`/v3/option/history/quote` query with `start_time`/`end_time` formatted with
`isoformat(timespec="microseconds")`, for example `10:00:00.000000`. Theta
Terminal v3 rejects six fractional digits. Reproduced on 13 November 2026
against the running Terminal on 127.0.0.1:25503, same contract and window:

| start_time / end_time            | HTTP |
|----------------------------------|------|
| `10:00:00.000000` / `10:02:00.000000` | 400 |
| `10:00:00.000` / `10:02:00.000`       | 200 |
| `10:00:00` / `10:02:00`               | 200 |

The base transport maps that 400 to `request_failed`; `AutoTransport` then maps
every terminal `request_failed` to `terminal_unreachable`. `cycle` treats that
as a transient outage: it reports the device as unreachable, consumes no
attempt, and retries the same job a minute later. The Mac log therefore repeats
"Theta Terminal bekleniyor" while the Terminal is in fact listening. The same
format is used by `ThetaQuoteClient` in `src/uoa_detector/options_alpha/theta_quotes.py`.

## Repair

- Format query times with three fractional digits. The window start is rounded
  up and the window end rounded down to the millisecond. Theta quote timestamps
  are millisecond-resolution, so the set of quotes inside the window is exactly
  the set the microsecond window describes; no quote earlier than the target can
  enter.
- Apply it in `scripts/theta_auto_connector.py`, `scripts/theta_mac_connector.py`
  and `ThetaQuoteClient`.
- Packet fields (`window_start`, `window_end`, `received_at`), server-side
  window validation, cost model, attempt limits and the transient-outage rule
  are unchanged.

## Out of scope (separate decision)

Mapping every terminal HTTP error (400/404/429/5xx) to `terminal_unreachable`
hides deterministic request errors as outages. Changing that classification
changes when attempts are consumed, a Phase 5.70 contract decision. It is left
for the owner.

## Tests

Existing tests that pinned `10:00:00.000000` encoded the rejected format and are
updated to `10:00:00.000`, with this document as the reason (D10). New tests:
sub-millisecond start rounds up, sub-millisecond end rounds down, whole-second
times keep `.000`, for all three call sites. Full repository gate before commit.
