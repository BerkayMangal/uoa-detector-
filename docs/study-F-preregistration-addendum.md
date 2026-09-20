# Study F — pre-registration addendum: purge at the walk-forward boundary

**Written before any estimator was fitted.** The frozen document
`docs/study-F-preregistration.md` is not edited (D1); this addendum supersedes its
§6 on one point and nothing else. Study D set the precedent with
`docs/preregister_D_addendum.md`.

## What was found

The eight-dimension audit of 2026-09-19 examined `scripts/study_e_signal.py`, the
harness Study F's §6 copied its walk-forward design from, and found that **it has
no purge at the fold boundary**:

> `folds` splits on day index with no purge or embargo, while the label at day t
> spans t+1..t+horizon. At horizon 5, fold 0's last training day is 2026-05-18 and
> the test fold opens 2026-05-19; the training label for 2026-05-18 is
> log(close 2026-05-26) − log(close 2026-05-18), and all five sessions it spans are
> test-fold sessions.

Per fold, the final `horizon − 1` training days carry label windows that overlap
the test window, and the last training day's label is built **entirely** from test
prices. That directly contradicts Study F's own §6 ("Folds never overlap") and
Study E's frozen pre-registration ("No row from a test fold ever informs its own
training set").

## Why it matters more here than it did there

The bias is one-directional and lands where it does the most damage. The null
permutes labels **within each day**, which destroys the cross-sectional content of
the leaked forward returns — so the leak can inflate the **real** arm's IC while
leaving the null distribution it is measured against untouched. That asymmetry
makes the null too lenient, which is the one failure mode a shuffled-label control
exists to rule out.

Study E survived it because its verdict was a rejection, and a rejection that
holds despite an inflated real arm is still a rejection. Study F cannot rely on
that: it was designed with more breadth precisely so that a pass becomes possible,
and a horizon-5 pass on an unpurged harness would be indistinguishable from a
boundary artifact.

## The amendment

§6's walk-forward is amended as follows, for **both** arms identically:

- The training window ends `horizon − 1` sessions before the test window opens.
  Concretely, where the training days were `days[:start]`, they become
  `days[: start − (horizon − 1)]`.
- This is a no-op at horizon 1 and drops the last 4 training days at horizon 5.
- The same purge applies to any inner split used for early stopping or model
  selection inside the training window.
- Nothing else changes: the fold length, the step, the rolling window, the ten
  configurations, the 200 null passes and all four thresholds stand as frozen.

## What this does not do

It does not re-open any other part of the protocol. The thresholds are not
touched, the feature list is not touched, and no result has been computed — the
feature library exists and passes its tests, but no model has been fitted on this
panel and no IC has been observed. If the amendment had been written after seeing
a number, it would be worthless, which is why it is being written now and
committed before the runner exists.

## The obligation this creates

Study F's implementation must carry a test that **fails when the purge is
removed** — asserting that no training label's window intersects its test fold.
The pre-registration's §8 already requires a failing test for boundary defects
(item 4); this addendum promotes that item from "should" to the specific,
checkable form above. A purge that cannot be shown to be doing anything is the
same species of claim as a guard that cannot fail, and this repository has shipped
four of those (P39, P40, P44, P45).

## Study E

Study E's own result is not revised by this document. Its harness has the same
defect, recorded by the audit as a separate finding, and correcting it there is a
separate piece of work with its own verdict: the published rejection stands
because the leak inflates the arm that still failed.
