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
# Credentials and DATABASE_URL are read from Railway inside this process and are
# never printed. Exit code 0 means every check passed.
set -u
URL="${BOARD_URL:-https://uoa-detector-production.up.railway.app}"
EXPECT_SHA="${1:-}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -f "$TMP"/page.html "$TMP"/health.json; rmdir "$TMP" 2>/dev/null' EXIT
fail=0
note() { printf '%-28s %s\n' "$1" "$2"; }
bad() { note "$1" "FAIL: $2"; fail=1; }

RAILWAY_DIR="${RAILWAY_DIR:-$REPO/../railway_probe}"
auth_curl_cfg() {  # emits a curl config on stdout; the password never reaches the log
  (cd "$RAILWAY_DIR" 2>/dev/null && railway variables --json 2>/dev/null) | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
u, p = d.get("BASIC_AUTH_USERNAME"), d.get("BASIC_AUTH_PASSWORD")
if u and p:
    print("user = %s:%s" % (u, p))
'
}

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
code=$(auth_curl_cfg | curl -s -K - -o "$TMP/page.html" -w '%{http_code}' "$URL/")
[ "$code" = 200 ] && note "/ with credentials" "200" || bad "/ with credentials" "$code"

# 3. honesty audit on the live html
if [ -s "$TMP/page.html" ]; then
  (cd "$REPO" && uv run python scripts/audit_board_html.py "$TMP/page.html") || fail=1
else
  bad "board html" "empty response"
fi

# 4. alfa_ table row counts
DBURL=$( (cd "$RAILWAY_DIR" 2>/dev/null && railway variables --json 2>/dev/null) \
  | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("DATABASE_URL",""))
except Exception: print("")' )
if [ -n "$DBURL" ] && command -v psql >/dev/null; then
  psql "$DBURL" -X -A -t -F ' | ' -c "
    SELECT table_name,
           (xpath('/row/c/text()', query_to_xml('select count(*) as c from ' || quote_ident(table_name), false, true, '')))[1]::text
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name LIKE 'alfa\\_%'
    ORDER BY table_name;" 2>&1 | sed -E 's#postgres(ql)?://[^ ]+#<redacted>#g' | sed 's/^/  alfa table  /'
else
  note "alfa tables" "skipped (no psql or no Railway context)"
fi

[ "$fail" = 0 ] && echo "VERIFY: PASS" || echo "VERIFY: FAIL"
exit "$fail"
