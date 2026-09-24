# BLOCKERS — Live Alpha v1

Only real external blockers. Each one names the evidence and what clears it.

| # | Blocker | Evidence | Clears when |
|---|---|---|---|
| B1 | Merge to `main` needs the owner. | The agent's `gh pr merge` was refused by the session's permission classifier ("Merge Without Review"), 2026-09-24 10:1x UTC. | Berkay runs `gh pr merge <n> --merge` after reading the PR. Railway then deploys `main`. |
| B2 | The local `.env` Unusual Whales key is revoked. | `GET /api/news/headlines` and `/api/stock/NVDA/info` both returned 401 `unrecognized_token`, 2026-09-24 10:0x UTC. Production uses its own Railway variable, which works (the refresher logs live UW activity). | Berkay pastes the current key into `.env`. Only local probes and key-gated integration tests need it. The product does not. |
| B3 | The agent cannot read Railway variables. | `railway variables` was refused by the permission classifier ("Production Reads"). | Not needed. The authorised browser check runs through Berkay's logged-in Chrome session. |
