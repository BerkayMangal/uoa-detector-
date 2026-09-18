#!/usr/bin/env bash
# Verify the live Alfa Board after a deploy, in one command.
#
#   bash scripts/verify_live_board.sh [expected_sha7]
#
# Checks, in order:
#   1. /health returns 200 (and the deployed SHA when the endpoint reports one).
#   2. The auth wall: 401 without credentials, 200 with them.
#   3. The board renders rows, and the honesty audit passes on the LIVE html:
#      no forbidden word, an "AMA" clause on every row, no 0-1 score outside the
#      audit block, every rendered quote carries an age, and the audit is not
#      vacuous (it asserts a row count > 0).
#   4. Row counts of the alfa_* tables.
#
# Inputs, each resolved from the environment first and from the Railway CLI
# second, so the rail also runs where there is no Railway CLI (a cloud session,
# CI, a fresh machine):
#
#   BOARD_URL           the board to check   (default: the production URL)
#   WEB_AUTH_USER       basic-auth user      (checks 2b and 3)
#   WEB_AUTH_PASSWORD   basic-auth password  (checks 2b and 3)
#   DATABASE_URL        Postgres connection  (check 4)
#   RAILWAY_DIR         a linked Railway project directory, for the fallback
#
# No credential is ever printed: the password goes straight into a curl config
# on stdin, and psql output is scrubbed of connection strings.
#
# Exit codes: 0 = every check ran and passed (VERIFY: PASS). 1 = a check failed
# (VERIFY: FAIL). 2 = nothing failed but a check could not run for lack of an
# input (VERIFY: PARTIAL) — an incomplete run must never read as a pass.
#
# Check 4 needs a direct Postgres connection. A runner without one (a sandbox
# whose egress is HTTPS-only) should leave DATABASE_URL unset, so the check
# reports SKIP instead of failing on a connection it was never going to make.
set -u
URL="${BOARD_URL:-https://uoa-detector-production.up.railway.app}"
EXPECT_SHA="${1:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -f "$TMP"/page.html "$TMP"/health.json; rmdir "$TMP" 2>/dev/null' EXIT
fail=0
skipped=0
note() { printf '%-28s %s\n' "$1" "$2"; }
bad() { note "$1" "FAIL: $2"; fail=1; }
skip() { note "$1" "SKIP: $2"; skipped=1; }

RAILWAY_DIR="${RAILWAY_DIR:-$REPO/../railway_probe}"
railway_json() {  # the linked project's variables as JSON, or nothing at all
  command -v railway >/dev/null 2>&1 || return 0
  (cd "$RAILWAY_DIR" 2>/dev/null && railway variables --json 2>/dev/null)
}
railway_var() {  # $1 = variable name; prints its value, or nothing
  railway_json | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1]) or "")
except Exception:
    pass
' "$1"
}

auth_curl_cfg() {  # emits a curl config on stdout; the password never reaches the log
  if [ -n "${WEB_AUTH_USER:-}" ] && [ -n "${WEB_AUTH_PASSWORD:-}" ]; then
    printf 'user = "%s:%s"\n' "$WEB_AUTH_USER" "$WEB_AUTH_PASSWORD"
    return 0
  fi
  railway_json | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# The service names them WEB_AUTH_*; the alternatives are tried so the script
# keeps working if the variable is ever renamed. Values are written straight
# into a curl config on stdout and never printed.
for user_key, pass_key in (
    ("WEB_AUTH_USER", "WEB_AUTH_PASSWORD"),
    ("WEB_AUTH_USERNAME", "WEB_AUTH_PASSWORD"),
    ("BASIC_AUTH_USER", "BASIC_AUTH_PASSWORD"),
    ("BASIC_AUTH_USERNAME", "BASIC_AUTH_PASSWORD"),
):
    u, p = d.get(user_key), d.get(pass_key)
    if u and p:
        print("user = \"%s:%s\"" % (u, p))
        break
'
}

# Resolved once: the Railway CLI, when it is the source, is slow enough that
# three calls were noticeable. The value stays in memory and is never printed.
AUTH_CFG="$(auth_curl_cfg)"

# 1. health
code=$(curl -s -o "$TMP/health.json" -w '%{http_code}' "$URL/health")
[ "$code" = 200 ] && note "/health" "200" || bad "/health" "$code"
sha=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("sha") or json.load(open(sys.argv[1])).get("commit") or "")
except Exception: print("")' "$TMP/health.json" 2>/dev/null)
if [ -n "$sha" ]; then
  note "deployed sha" "$sha"
  [ -n "$EXPECT_SHA" ] && case "$sha" in "$EXPECT_SHA"*) ;; *) bad "deployed sha" "expected $EXPECT_SHA";; esac
fi

# 2. auth wall
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/")
[ "$code" = 401 ] && note "/ without credentials" "401" || bad "/ without credentials" "$code"
if [ -n "$AUTH_CFG" ]; then
  code=$(printf '%s\n' "$AUTH_CFG" | curl -s -K - -o "$TMP/page.html" -w '%{http_code}' "$URL/")
  [ "$code" = 200 ] && note "/ with credentials" "200" || bad "/ with credentials" "$code"
  # Contract §4.1 asks for a p95 under 1.5 s. Unit tests cannot measure that (the
  # runner's speed is not the product's), so the real number is taken here.
  render=$(printf '%s\n' "$AUTH_CFG" | curl -s -K - -o /dev/null -w '%{time_total}' "$URL/")
  note "live render (s)" "$render"
else
  skip "/ with credentials" "no WEB_AUTH_USER / WEB_AUTH_PASSWORD, no Railway CLI"
  skip "live render (s)" "needs credentials"
fi

# 3. honesty audit on the live html
if [ ! -s "$TMP/page.html" ]; then
  if [ -n "$AUTH_CFG" ]; then
    bad "board html" "empty response"
  else
    skip "board html audit" "needs credentials"
  fi
else
  (cd "$REPO" && uv run python scripts/audit_board_html.py "$TMP/page.html") || fail=1
fi

# 4. alfa_ table row counts
DBURL="${DATABASE_URL:-$(railway_var DATABASE_URL)}"
if [ -z "$DBURL" ]; then
  skip "alfa tables" "no DATABASE_URL, no Railway CLI"
elif ! command -v psql >/dev/null 2>&1; then
  skip "alfa tables" "psql not installed"
else
  PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}" psql "$DBURL" -X -A -t -F ' | ' -v ON_ERROR_STOP=1 -c "
    SELECT table_name,
           (xpath('/row/c/text()', query_to_xml('select count(*) as c from ' || quote_ident(table_name), false, true, '')))[1]::text
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name LIKE 'alfa\\_%'
    ORDER BY table_name;" 2>&1 | sed -E 's#postgres(ql)?://[^ ]+#<redacted>#g' | sed 's/^/  alfa table  /'
  # A database that refuses the connection used to read as a pass, because the
  # pipeline reported sed's status. It is a failure, and it is named as one.
  # PIPESTATUS is taken on the very next line: the test that reads it would
  # otherwise overwrite it with its own status.
  psql_status=${PIPESTATUS[0]}
  [ "$psql_status" = 0 ] || bad "alfa tables" "psql exit $psql_status"
fi

if [ "$fail" != 0 ]; then
  echo "VERIFY: FAIL"
  exit 1
elif [ "$skipped" != 0 ]; then
  echo "VERIFY: PARTIAL (a check above could not run; see its SKIP line)"
  exit 2
fi
echo "VERIFY: PASS"
exit 0
