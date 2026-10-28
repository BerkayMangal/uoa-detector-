# Phase 5.46 operational delivery retry

2026-10-28 Istanbul. Validated candidate 7ecf4fe9731c0e605a5b46dc0cef9cdebae675ed
passed main CI 40667297247, but Railway reported `Deployment failed` for deployment
887c89ad-47ca-4995-a421-d136f2361e26. The public runtime still served the previous
cdc7add with startup storage OperationalError. No new release/recovery was claimed.
The provider status gives no build/runtime cause. The native Railway deployment
log has been requested; this workspace cannot read the Railway control plane.

Independent production verification:

- Fresh separate Python 3.12 environment, `uv sync --locked --no-dev`.
- Build/install a non-editable project wheel with no development dependency.
- Start the actual uvicorn/FastAPI app with lifespan/profile validation enabled.
  Live tasks explicitly disabled locally; no vendor call or real DB used.
- Actual localhost /health 200 with candidate SHA, /hakkinda 200, generated
  opening-measurements CSS present, /acilis 401 without auth.
- This checks packaging/startup locally; it does not establish why Railway's
  attempt failed or prove production data access. No platform cause is inferred.

One normal deployment retry is requested via this documentation-only commit.
Application source, dependency lock, start/deploy configuration, profiles,
policies and all recorded evidence remain identical to the validated candidate.
No connection retry/cadence, service target or infrastructure resource changes.
Existing owner authorization covers push/merge/deploy. A second provider failure
will remain explicitly blocked pending the actual provider log, rather than
causing a loop of arbitrary code/config changes or blind redeploys.

Full gate and green PR/main CI remain required for this operational commit.
Only an actual matching deployed SHA establishes delivery; PostgreSQL recovery
still separately requires successful real scan/desk/research/opening cycles.
