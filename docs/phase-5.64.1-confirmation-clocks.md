# Phase5.64.1 — physical confirmation clocks

Engineering decision registered before strengthening the new storage adapter.
Use two receipt stages in the same dedicated append-only receipt table: first
clock after event/packet commit, second clock after the first receipt commits.
Record.confirmed_at is the second clock, never the pre-commit stamp. Receipt
checksums bind stage/parent/checksum/time and the second stage to the first.

Candidate entry delay starts after this confirmed availability. A packet whose
confirmed availability misses its target deadline cannot supply an observed
price, even when HTTP completed earlier. Entry-based exit horizons start at the
entry outcome's confirmed availability. Delayed commits remain visible missing
evidence; do not backdate a request or repair a historical observation.

Only activation can finish an absent confirmation stage after restart, stamped
at the actual new time. Candidate/packet/outcome receipts are never recovered
for an already existing unconfirmed event. This changes no sampling predicate,
provider, strategy threshold, cap or live decision. Verify late commits and
stage integrity before publication; old methods and their receipts remain intact.
