# ThetaData capability probe (Phase 5.2.K2)

Type: RECORD (the 2026-09-15 probe) and PLAN (the re-run procedure). This is not
an acceptance contract. Append each future re-run as a dated section at the end.

Owner decision K2 (`docs/phase-5.2-alfa-board-acceptance.md:72-73`, §8): ThetaData
is not wired to the Alfa Board. Probe the subscription and record what it
permits. The companion decision is in `docs/thetadata-decision.md`.

**Result, 2026-09-15: the probe is blocked.** Theta Terminal rejected the login
with `Invalid credentials`. Port 25503 never opened and 0 HTTP requests were
sent. The subscription tier and what the account can fetch are **unknown**.

---

## 1. What was attempted

Run at about 11:34 ET on Tuesday 2026-09-15 (market open) by an agent session on
the owner's laptop. The request budget was 6 HTTP requests, snapshot endpoints
only: no history and no bulk.

| # | Action | Result |
|---|---|---|
| 1 | `java -version` | OpenJDK 25.0.2 (Homebrew build) |
| 2 | Checked that credential files exist in `/Users/berkay/Documents/uoa-detector-/`, the jar directory (metadata only) | `creds.txt` exists: 34 bytes, 2 non-empty lines, line 1 contains `@`. `config.toml` exists and sets no credential or API-key field. Contents were not printed. |
| 3 | Read the repo's v3 notes (`docs/thetadata-v3-migration.md`, `sources/thetadata/client.py`, `mapping.py`, `providers/price_action.py`) | Base `http://127.0.0.1:25503`; params `symbol`, `expiration`, `strike` as a dollar string, `right`; `format=json` (the default is CSV); HTTP 472 means "no data", not an auth error |
| 4 | Pre-checks | No Theta Terminal process running; ports 25503, 25520 and 25510 closed. Planned expiry: 20260916. |
| 5 | Launched the Terminal from the jar directory: `(cd /Users/berkay/Documents/uoa-detector- && exec nohup java -jar ThetaTerminalv3.jar > …/alfa_probe/theta_terminal.log 2>&1) &` | PID 15153. Log: Bootstrap `20250709:85346bb`, Terminal `20260819:a4abac9`, config file `/Users/berkay/Documents/uoa-detector-/config.toml`, log directory `/tmp`, environment `PROD` |
| 6 | Waited for port 25503 with `nc -z` every 2 s (max 120 s), watching the process and the log. A TCP check was used so that polling did not spend the request budget. | Stopped after about 10 s when the log printed `[09-15-2026 16:35:28.914] ERROR: Invalid credentials. Please check your credentials file, API key, or environment variable, and try again.` (local BST, which is 11:35:28 ET). The port never opened. |
| 7 | Sent the snapshot requests | **Not sent** (0 of 6): nothing was listening |
| 8 | Searched for tier lines (OPTION / STOCK / INDEX with FREE / VALUE / STANDARD / PRO) in the run log, `/private/tmp/terminal-latest.log` and `/private/tmp/terminal-debug.log` | None found. The Terminal stopped at authentication, before it would print tiers. |
| 9 | Listed the names (not values) of ThetaData variables in the launching shell | `THETADATA_API_KEY` and `THETADATA_USERNAME` are set |
| 10 | Grep of the Terminal debug log for auth lines, with secrets masked, to see which credential source was used | **Denied** by the session's permission classifier. Not retried. |
| 11 | Stopped the Terminal | SIGTERM ended PID 15153 within 5 s; no ThetaTerminal process left; ports 25503 and 25520 closed |

Planned requests, none sent:

| Path | Planned params | HTTP status |
|---|---|---|
| `/v3/stock/snapshot/quote` | `symbol=SPY&format=json` | NOT SENT |
| `/v3/option/snapshot/quote` | `symbol=SPY&expiration=20260916&strike=<ATM>&right=call&format=json` | NOT SENT |
| `/v3/option/snapshot/greeks…` | same as above | NOT SENT |

Artifacts are outside git and were session-scoped, so they may already be gone:
the session scratchpad `alfa_probe/theta_terminal.log` (6 lines, no secrets),
`/private/tmp/terminal-latest.log` and `/private/tmp/terminal-debug.log`.

## 2. What the result does NOT tell us

1. **The tier.** The OPTION, STOCK and INDEX tiers are not known, and neither is
   whether any subscription is active or billed.
2. **What is permitted.** Nothing says whether snapshot quotes or greeks work on
   this account. The probe is not evidence that they are refused.
3. **Which credential was rejected.** Three sources were present at launch:
   - (a) `creds.txt` next to the jar;
   - (b) `THETADATA_API_KEY` in the shell environment. ThetaData's v3
     getting-started page names this variable as a Terminal credential source.
   - (c) a `.env` file in the jar directory (135 bytes, dated 2026-06-22,
     contents not read). The same page says the Terminal reads
     `THETADATA_API_KEY` from a `.env` next to the jar.

   The pages read do not document which source wins.
4. **Why it was rejected.** The probe cannot tell a lapsed account from a
   changed password or a wrong value in the variable. One specific hypothesis,
   unverified:
   - The project's own template stores the ThetaData *password* under that name:
     `THETADATA_API_KEY=<thetadata password>` (`docs/phase-3.5-acceptance.md:91`).
   - `docs/DATA_INTEGRATION.md:43-45` describes a separate dashboard API key.
   - If the Terminal preferred (b) or (c) over (a), a password presented as an
     API key would fail with exactly this message.
5. **Network or vendor health.** The message reads like an auth-server reply,
   which would mean the network path worked. This was not verified.
6. **Rate or concurrency limits.** The last recorded values are
   `Max concurrent requests: 8` and a sustained ~25 req/s
   (`profiles/v5_default.yaml:290-291`, commit `42f7672`, 2026-05-16).

**Known history (not current state):**
- 2026-05-12: `OPTION.STANDARD` + `STOCK.FREE` active, `STOCK.VALUE` not active
  (`docs/phase-3.3.8-acceptance.md:28-30`,
  `docs/phase-3.5.1-validation-record.md:12`).
- 2026-05-16: upgraded to PRO. The Terminal reported `Options: PROFESSIONAL` and
  `Max concurrent requests: 8` (commit `42f7672`).
- The newest local ThetaData datasets were last written on 2026-06-24
  (`bulk_2024`, `spot_series_2024` and `chain_snapshots_2024` directory
  timestamps). No later ThetaData use is recorded.

## 3. Re-run procedure

Berkay runs this on the laptop. Every command prints names, counts or HTTP
codes only; none prints a credential.

**Preconditions**
- A US trading day, 09:45–15:45 ET, so the snapshots reflect a live session.
- No Terminal running: `pgrep -fl ThetaTerminal` prints nothing, and
  `nc -z 127.0.0.1 25503` fails.
- Never add `expiration=*` or any `/history/` path to this probe. The owner's
  spec forbids starting the 50–200 GB historical download.

**Step 0: account page (browser, 2 minutes).**
1. Log in at thetadata.net with the email and password that `creds.txt` should
   hold.
2. Write down the plan names, billing state, renewal date and whether an API key
   exists.
3. If the browser login fails, the password has changed: reset it and update
   `creds.txt`.
4. If no subscription is active, stop here. The tier question is moot; see
   `docs/thetadata-decision.md`.

**Step 1: Run A, `creds.txt` only.** Launch from a clean directory with an empty
environment. That way no `.env` sits next to the jar and no `THETADATA_*`
variable (whatever its exact name) reaches Java.

```bash
mkdir -p ~/theta-probe
cp /Users/berkay/Documents/uoa-detector-/ThetaTerminalv3.jar /Users/berkay/Documents/uoa-detector-/config.toml ~/theta-probe/
cd ~/theta-probe
env -i HOME="$HOME" PATH="$PATH" java -jar ThetaTerminalv3.jar \
  --creds-file /Users/berkay/Documents/uoa-detector-/creds.txt 2>&1 | tee ~/theta-probe/run-a.log
```

- `--creds-file` comes from ThetaData's v3 getting-started page; this repo has
  not used it before. If the Terminal rejects the flag, drop it and link the file
  instead of copying the secret:
  `ln -s /Users/berkay/Documents/uoa-detector-/creds.txt ~/theta-probe/creds.txt`.
- The first launch in a new directory may download the Terminal's `lib/` again
  (176 MB in the jar directory).
- Copying `config.toml` keeps port 25503 and the `/tmp` log directory.

In a second tab, wait for the port (up to 2 minutes):

```bash
for i in $(seq 1 60); do nc -z 127.0.0.1 25503 && echo UP && break; sleep 2; done
grep -c 'Invalid credentials' ~/theta-probe/run-a.log
```

**How to read Run A**

| Outcome | Meaning | Next |
|---|---|---|
| `UP` and a count of 0 | `creds.txt` is valid. The 2026-09-15 failure came from the environment variable or the jar-directory `.env`. | Step 2 (optional), then §4 and §5 |
| Count ≥ 1, port never opens | The account rejects the email and password in `creds.txt` | Use the Step 0 result: browser login fails → reset the password; browser works but no subscription → lapsed; browser works and subscription active → contact ThetaData support, quoting Terminal build `20260819:a4abac9` and the log line |

**Step 2 (optional): confirm the bad source.** Stop Run A first.

Run B uses the same clean directory with the normal shell environment and no
creds file: remove the `creds.txt` link if you made one, and launch without
`--creds-file`.

```bash
cd ~/theta-probe && rm -f creds.txt
java -jar ThetaTerminalv3.jar 2>&1 | tee ~/theta-probe/run-b.log
```

If Run B shows `Invalid credentials` while Run A passed, the environment
variable holds a value the Terminal does not accept. To check which variables
are present without printing values:

```bash
env | cut -d= -f1 | grep -i theta
grep -c '^THETADATA_API_KEY=' /Users/berkay/Documents/uoa-detector-/.env
```

**Permanent fix.** Launch the Terminal with
`env -u THETADATA_API_KEY -u THETADATA_USERNAME`, and remove that key from the
jar-directory `.env` or put the real dashboard API key there.

Do not rename the variable in the repo's `.env`. The project's ThetaData smoke
test is gated on it (`tests/integration/test_thetadata_smoke.py:66-69`).

**Step 3: send the §4 requests** while Run A is up.

**Step 4: stop and clean up.**

```bash
pkill -f ThetaTerminalv3.jar; sleep 3; nc -z 127.0.0.1 25503 || echo CLOSED
rm -f ~/theta-probe/creds.txt
```

Keep `run-a.log`, `run-b.log` and the response files out of git; the logs may
contain the account email.

**Step 5: record.** Append a `## Re-run YYYY-MM-DD` section to this file with:
- the Step 0 plan and billing facts;
- the Run A and Run B outcomes;
- the tier lines (§5);
- the HTTP status and row count for each §4 request.

## 4. Requests to send once login works

Rules for every request:
- **Base.** `http://127.0.0.1:25503`.
- **`format=json`.** Always set it; v3 defaults to CSV
  (`docs/thetadata-v3-migration.md` §3, B5).
- **Params.** Repo-verified v3 conventions (`docs/thetadata-v3-migration.md`
  §3): `symbol`; `expiration` as `YYYYMMDD`; `strike` as a dollar string with
  two decimals; `right` = `call`/`put`.
- **Paths and tier labels.** From ThetaData's public v3 endpoint pages, read
  2026-09-15. They are not yet confirmed against this account.

The brief names `/v3/option/snapshot/greeks`. The docs show a family of greeks
endpoints: `/greeks/all`, `/greeks/first_order`, `/greeks/second_order`,
`/greeks/third_order` and `/greeks/implied_volatility`. A bare `/greeks` path
does not appear. Requests 3 and 4 use two members of that family because their
documented tiers differ, which is what separates Standard from Pro.

```bash
cd ~/theta-probe
B=http://127.0.0.1:25503

# 1. Stock snapshot quote: spot for the ATM strike, and the STOCK tier
curl -s -o r1.json -w 'r1 %{http_code}\n' "$B/v3/stock/snapshot/quote?symbol=SPY&format=json"

# Set these by hand from r1.json: next SPY expiration, and ATM strike = mid of bid/ask rounded to $1
EXP=YYYYMMDD
K=000.00

# 2. Option snapshot quote (docs: Value, Standard, Pro)
curl -s -o r2.json -w 'r2 %{http_code}\n' "$B/v3/option/snapshot/quote?symbol=SPY&expiration=$EXP&strike=$K&right=call&format=json"

# 3. First-order greeks snapshot (docs: Standard, Pro)
curl -s -o r3.json -w 'r3 %{http_code}\n' "$B/v3/option/snapshot/greeks/first_order?symbol=SPY&expiration=$EXP&strike=$K&right=call&format=json"

# 4. All greeks snapshot (docs: Pro)
curl -s -o r4.json -w 'r4 %{http_code}\n' "$B/v3/option/snapshot/greeks/all?symbol=SPY&expiration=$EXP&strike=$K&right=call&format=json"
```

**What each request shows** (response fields per the docs):
- **r1.** `timestamp`, `bid`, `ask`, sizes. The docs list `venue` values `nqb`
  (default) and `utp_cta`: Standard and Pro give a real-time Nasdaq Basic BBO,
  and Value gives a **15-minute delayed** UTP/CTA NBBO.
  - If `timestamp` is about 15 minutes behind the wall clock (ET), the stock tier
    is Value.
  - If r1 returns a 4xx, retry once with `&venue=utp_cta`. That is request 5, the
    only allowed extra.
- **r2.** `timestamp`, `bid_size`, `bid`, `ask_size`, `ask` with exchange and
  condition codes.
- **r3.** `delta`, `theta`, `vega`, `rho`, `implied_vol`, `iv_error`,
  `underlying_price`.
- **r4.** Everything in r3, plus `gamma`, `vanna`, `charm`, `vomma` and third-order
  greeks.

**Status codes**, as the repo client treats them
(`src/uoa_detector/sources/thetadata/client.py:256-276`):

| Status | Meaning | Action |
|---|---|---|
| 200 with rows | permitted | record the row count and `timestamp` |
| 472 | "no data found" (not an auth error) | the query was accepted, but the strike or expiration is wrong or there is no quote yet. Fix `EXP`/`K` and retry once. |
| other 4xx | the client raises `ThetaDataAuthError`; for a logged-in Terminal this is the permission path | record the code and the first 200 characters of the body. The repo does not record ThetaData's exact permission code or message. |
| 5xx | transient | retry once |
| connection refused | Terminal not up | back to §3 |

**OPTION tier from r2–r4**

| r2 | r3 | r4 | OPTION tier |
|---|---|---|---|
| 200 | 4xx | 4xx | VALUE |
| 200 | 200 | 4xx | STANDARD |
| 200 | 200 | 200 | PRO |
| 4xx | – | – | no tier with snapshot access (FREE, or none) |

The whole probe is 4 requests (5 with the venue retry), a few KB each.

## 5. Reading the subscription tier lines

**Where.** Terminal stdout (the `tee` log in §3) and the `log_directory` in
`config.toml`. On 2026-09-15 that was `/tmp`, holding `terminal-latest.log`
and `terminal-debug.log`.

**When.** After a successful login. The 2026-09-15 run never reached this point,
which is why no tier line appeared.

**Format on record.**
- Commit `42f7672` (2026-05-16) quotes the Terminal: `Options: PROFESSIONAL` and
  `Max concurrent requests: 8`.
- On 2026-05-12, the account's tiers were recorded with the names
  `OPTION.STANDARD`, `STOCK.FREE` and `STOCK.VALUE`
  (`docs/phase-3.3.8-acceptance.md:28-30`).
- The exact layout printed by build `20260819:a4abac9` is not recorded, so
  search by word:

```bash
grep -i -E '(option|stock|indic|index)[a-z]*[.: ]+(free|value|standard|pro)|max concurrent' ~/theta-probe/run-a.log /tmp/terminal-latest.log
```

**How to read them.** Each asset class (options, stocks, indices) has one tier,
ordered FREE < VALUE < STANDARD < PRO(FESSIONAL). What each tier means for this
project, per the public endpoint pages and repo history:

| Line | Snapshot access | Project relevance |
|---|---|---|
| Options FREE, or no options line | none of the §4 option requests | no option data at all |
| Options VALUE | `option/snapshot/quote` | quotes only; no greeks snapshots |
| Options STANDARD | adds `greeks/first_order` and `greeks/implied_volatility` | the 2026-05-12 state |
| Options PRO / PROFESSIONAL | adds `greeks/all` (gamma and higher orders) | the 2026-05-16 state. It paired with `Max concurrent requests: 8`, the value behind `historical_concurrency: 8` (`profiles/v5_default.yaml:291`). |
| Stocks FREE | no stock snapshot quote is listed for this tier | M23 moved to UW for this reason (`docs/phase-3.3.8-acceptance.md:28-40`) |
| Stocks VALUE | 15-minute delayed stock NBBO; `/v3/stock/history/ohlc` | the add-on declined in Phase 3.3.8 |
| Stocks STANDARD / PRO | real-time stock BBO | – |
| `Max concurrent requests: N` | parallel request cap | below 8 means a lower tier than in May. Record it; do not edit the profile. |

**Cross-check.** The tier lines and the §4 status codes must agree. If they do
not, the status codes win, because they show what the account can actually
fetch. Record both.
