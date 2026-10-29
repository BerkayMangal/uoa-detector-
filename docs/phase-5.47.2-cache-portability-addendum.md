# Phase 5.47.2 addendum — portable dependency-install layer

Frozen before this Dockerfile edit, 2026-10-29 Istanbul. While executing the
owner-authorized release, the official Railway Dockerfile guide documents cache
mount IDs in the form s/<service id>-<target path>. The existing anonymous
/root/.cache/uv cache mount does not follow that provider-specific format.
This is concrete compatibility evidence, not the missing native build log or
proof of the earlier deployment failure cause.

Supersede only the preceding Docker-input-preservation clause in
phase-5.47.2-docker-selection-acceptance.md: remove that cache mount and install
with uv sync --no-cache, preserving the locked/main-only/non-editable dependency
selection, verified image digests, optional trusted-CA secret, TLS verification,
runtime sources, command and deploy settings. Do not hardcode a provider service
ID. No dependency/strategy/profile/database/credential/resource changes.

Actually rebuild the modified Docker image and verify imports, lifespan boot,
health/current identity, public page, authentication and all frozen report hash
checks, without production credentials, network access at runtime or vendor work.
Run the complete final-state hermetic gate before the single release commit.
The earlier Docker proof remains historical; produce new build/runtime evidence.
No claim of actual publication without the provider and HTTP evidence.

Source: railwayapp/docs content/docs/builds/dockerfiles.md, Cache mounts.
