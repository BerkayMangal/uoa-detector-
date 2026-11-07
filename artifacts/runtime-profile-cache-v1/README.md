# Exact runtime profile cache compatibility

This fixed manifest permits the original vendor-history report to remain
readable with one byte-identified runtime change: bounded parsing of the exact
same raw profile bytes into immutable settings. The report loader pins this
manifest's byte hash. It accepts only the listed original/runtime settings
hashes, unchanged profile and original report/audit hashes. Every other source,
profile, script, report and audit check remains exact.

The original historical report and audit payloads and their hashes are unchanged.
This is an operational compatibility statement, not a new historical audit or
evidence of strategy performance. Later source edits require another explicit
reviewed compatibility decision; this manifest is not a wildcard allowlist.

The original `scripts/audit_vendor_history.py` deliberately continues to require
its original dependency bytes. Re-running that historical audit uses a compatible
pre-cache checkout, such as commit
`ecc9f745507d3bdd4d784c14d0373022e4390841`, with its original private source
archive. Do not run it on modified runtime bytes and call the old audit rerun.
The web loader's exact bridge preserves already recorded audit provenance.

Protocol: `docs/phase-5.66.1-runtime-profile-cache.md`. The runtime cache reads and
hashes the actual file on every public call, keys by raw bytes/path/hash, retains
at most eight entries and coalesces concurrent cold parses. Changed, missing,
invalid or unknown-key files never fall back to a previous cached profile.
