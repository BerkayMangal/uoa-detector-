# RESUME — Live Alpha v1

Start here after a context handover.

- Contract: `docs/phase-5.25-live-alpha-acceptance.md`.
- Product decision: `docs/live-alpha-v1/PRODUCT_OVERRIDE.md`.
- Evidence table: `docs/live-alpha-v1/STATUS.md`.
- Engine (pure): `src/uoa_detector/live_alpha/`.
- I/O, job, page: `webapp/live_alpha/`.
- Screen: `webapp/templates/live.html` at `/`.
- Policy profile: `profiles/live_alpha_v1.yaml`. Changing a threshold means a new `policy_version`, never an edit after results (D4).

## Branches and PRs

- `p82-board-prerender-hotfix` → PR #85. P0: the board page answered in 10-29 s. It must merge first.
- `p83-live-alpha-v1` → PR for 5.25. It is stacked on #85.

## Gate

```
export UV_NO_SYNC=1 PYTHONPATH=src MYPYPATH=src
env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME uv run pytest -q && uv run mypy --strict src/ webapp/ && uv run ruff check . && uv lock --check
```

`PYTHONPATH=src` is needed on this laptop because macOS keeps setting the
`hidden` flag on `.venv/.../*.pth`. Python 3.14 then skips the editable install.
CI is not affected.

## Live verification

- The page is at `https://uoa-detector-production.up.railway.app/`, behind Basic auth.
- The Live Alpha thread logs `live alpha cycle: status=...` once per cycle. It is visible in the Railway deploy log.
- The newest `alfa_live_scan` row is what the page shows.
- `alfa_live_heartbeat` has one row per cycle.
