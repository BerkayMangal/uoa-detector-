# Phase 5.71.3 — P2 PAPER baseline

The scan observer saves the first eligible stock plan and later records
the paired stock/SPY entry and 60-minute prices in the same transaction
as the existing case and audit event. The reference stays active when
all stop arms finish earlier. It uses the original scan receipt clocks
and stored quote inputs. Local observation needs no HTTP polling.

The PAPER screen displays the counts at each stage and the recorded
quote clocks. Closed session comparisons show P2, cash, an expected
equal-count random allocation from the same roster, and SPY at the
P2 quote clocks. All comparisons use whole shares and show the three
fixed cost assumptions. A missing control price leaves the entire
session outside the dollar comparison.

Older records can still be opened. They have no fixed-hold reference.
The observer records unavailable status for a missed enrollment, a
missing pair, or a target outside the original regular session. A
recoverable later scan keeps its original clocks and recovery flag.

The synthetic application check exercised the actual scan observer,
transactional store, private HTML and per-case JSON. It checked the
60-minute reference after the stop arms closed, a duplicate scan, a
missing control, a half-day, and a recovered enrollment. The screen
was inspected at 1440, 390, and 320 pixels. The synthetic fixture
provides no market result. The draft PR is the delivery boundary.
