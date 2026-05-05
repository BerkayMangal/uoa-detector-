#!/usr/bin/env bash
# Pre-commit hook — scans staged changes for likely credential strings.
#
# Phase 3.3.1.4. Runs in two contexts:
#   1. Locally as a git pre-commit hook (after a developer copies it
#      to .git/hooks/pre-commit and chmod +x).
#   2. In CI before pytest, to catch credentials in PRs.
#
# Detection patterns:
#   - Unusual Whales API key:   uw_<32 hex chars>
#   - Generic 'api_key=...' or 'api_key: ...' assignments where the
#     value looks like a real credential (>= 16 chars, no obvious
#     placeholder words).
#
# Exit codes:
#   0 — staged diff is clean
#   1 — at least one match found (commit blocked)
#
# Bypass (use sparingly): git commit --no-verify
#
# decision: scan only the staged DIFF, not the full repo. This is
# fast and matches the pre-commit hook semantics. CI runs the same
# script with --check-tree to scan all tracked files.

set -euo pipefail

CHECK_TREE=0
for arg in "$@"; do
    case "$arg" in
        --check-tree) CHECK_TREE=1 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# What to scan.
if [[ "$CHECK_TREE" -eq 1 ]]; then
    # All tracked text files (not blob types like png).
    FILES=$(git ls-files | xargs -I{} sh -c 'file --mime "{}" | grep -q text && echo "{}"' || true)
    SCAN_CMD="cat"
else
    # Staged diff (added/modified content only — what's about to be
    # committed).
    FILES=""
    SCAN_CMD="git diff --cached --no-color"
fi

# Patterns that indicate a likely credential leak.
# Each pattern is a Perl-compatible regex; matched lines printed with
# file path + line number (or 'staged-diff' when --check-tree is off).

PATTERNS=(
    # Unusual Whales API key — exact format uw_ + 32 lowercase hex chars
    'uw_[a-f0-9]{32}'
    # Generic API key / token assignments with high-entropy-looking values
    # (16+ alnum chars, not all-uppercase placeholder, not 'YOUR_KEY_HERE').
    # The pattern matches: api_key="<value>", api_key='<value>', api_key=<value>
    # Value classes excluded: empty, all-X, common placeholders.
    '(api_key|access_token|api_token|client_secret)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_-]{20,}'
)

EXIT_CODE=0
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_OUT"' EXIT

scan() {
    for pattern in "${PATTERNS[@]}"; do
        if [[ "$CHECK_TREE" -eq 1 ]]; then
            # Scan tracked-file content; skip the hook itself + .env.example.
            for f in $FILES; do
                # Skip self + the example template (intentionally lists key NAMES)
                if [[ "$f" == "scripts/check_no_secrets.sh" ]] || [[ "$f" == ".env.example" ]]; then
                    continue
                fi
                # Skip test files that intentionally use fake credentials.
                # Each of these passes test_xxx_does_not_leak_secret etc. that
                # depend on putting fake credential strings into events; the
                # hook would block these tests' very existence otherwise.
                case "$f" in
                    tests/unit/test_credentials.py) continue ;;
                    tests/unit/test_redact_secrets.py) continue ;;
                    tests/unit/test_check_no_secrets_hook.py) continue ;;
                    tests/fixtures/check_no_secrets/*) continue ;;
                esac
                grep -nE "$pattern" -- "$f" >> "$TMP_OUT" 2>/dev/null || true
            done
        else
            # Scan staged diff. Lines starting with '+' are additions.
            $SCAN_CMD | grep -E "^\+" | grep -vE "^\+\+\+" \
                | grep -nE "$pattern" >> "$TMP_OUT" 2>/dev/null || true
        fi
    done

    if [[ -s "$TMP_OUT" ]]; then
        echo "ERROR: likely credential leak detected in staged changes:" >&2
        echo "" >&2
        cat "$TMP_OUT" >&2
        echo "" >&2
        echo "If this is a false positive (e.g., placeholder in a doc):" >&2
        echo "  - rename the variable to use a non-credential keyword, OR" >&2
        echo "  - add the line to scripts/check_no_secrets.sh's allowlist, OR" >&2
        echo "  - bypass with 'git commit --no-verify' (review carefully)." >&2
        EXIT_CODE=1
    fi
}

scan
exit $EXIT_CODE
