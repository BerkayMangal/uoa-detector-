# Phase 5.47.2 — compatible Docker selection and owner-requested release

Frozen before the deployment-config edit, 2026-10-29 Istanbul. The owner
explicitly requests that Codex handle publication without another panel task.
Main 1490313 has green CI but public health/CSS still identify cdc7add/5.45.1.
The latest GitHub provider status reports deployment failure. The owner's manual
redeploy did not establish publication of current main; its native build log is
not accessible here. No previous failure cause is inferred.

Use Railway's documented Dockerfile-path selection without the optional builder
selector: retain build.dockerfilePath=Dockefile (spelled Dockerfile in JSON) and
the existing root Dockerfile, remove only build.builder. The official
config-as-code reference accepts DOCKERFILE, while the public CLI GraphQL Builder
enum differs. This is a supported configuration simplification, not evidence
that the previous value was invalid. Railway documents that a root Dockerfile
is always used when present. Preserve start/health/restart settings exactly.

No runtime, dependency, strategy, profile, evidence, data, credential, database
target or infrastructure resource changes. The previously verified Docker image
and all features remain the candidate. Verify JSON/path, unchanged deploy
settings and Docker inputs, then full hermetic pytest/mypy/ruff/lock/diff.
Use a green PR and owner-authorized merge to trigger one new current-main
deployment through the existing authenticated GitHub connection. Observe its
new provider status and actual HTTP SHA/assets. No repeating commits/redeploys
after another provider failure without new evidence; no invented success.

References: official railwayapp/docs content/docs/builds/dockerfiles.md and
content/docs/config-as-code/reference.md. Actual publication requires matching
new main SHA and current assets, separately from data freshness/real cycle health.
