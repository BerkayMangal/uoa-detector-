# Phase 5.47.4 — expand PORT in the Docker startup override

2026-11-01. Native build 0c7fbdec-5c8b-453c-b0dd-bd7f7a1ab8c1 now passes, but
runtime logs repeatedly report: "Invalid value for '--port': '$PORT' is not a
valid integer." Railway passes this Docker startup override as exec arguments;
its literal variable is not expanded. The image's own CMD already used sh -c,
but the configured override replaced it.

Wrap the existing override in sh -c with exec and ${PORT:-8000}, preserving the
same module, bind address, Docker image and health settings. Test the actual
exec-style invocation with both configured and absent PORT and verify process
replacement, not just a string match. No policy, dependency, database or research
change. Full gate/CI and native runtime/SHA verification remain required.

For immediate availability, the operator used the supported rollback-to-deployment
operation for the previously working cdc7add image, ID
5155055c-ca28-4125-98d0-6c609bb0fa38 (canRollback=true), while this tested repair
is prepared. That is a temporary availability action, not completion of the new
release. No database rollback or deletion. Owner explicitly authorizes operational
fixes and publishing; no repeat permission/token request is necessary.
