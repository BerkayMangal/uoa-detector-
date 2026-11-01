# Phase 5.47.3 — remove unsupported Railway build secret mount

2026-11-01. Existing owner authorization covers fixing and deploying the service.
Native build logs for deployment 6c9d6d79-c7e6-4402-8b85-89e6210dce66 now establish
an exact cause: "dockerfile invalid: flag '--mount=type=secret,id=build_ca' is
missing a type=cache argument (other mount types are not supported) at Line 13".
No runtime started. The earlier portable-cache fix left this separate mount.

Remove the optional build-only CA secret mount and its shell wrapper. Use the
same locked uv installation with standard verified TLS and the existing pinned
images. Do not add certificates, disable verification, change dependency locks,
modify trading/research policy or delete database evidence. Add a regression
check that RUN mounts use only Railway's supported cache mount type. Verify
full repository gate/CI and actual native build, health SHA and private pages.
Release is a concrete supported fix, not a blind retry-only commit. Preserve
new history-research work separately while delivering this operational repair.
