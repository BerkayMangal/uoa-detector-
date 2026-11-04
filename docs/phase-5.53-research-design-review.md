# Research-design review and registered corrections

Date: 2026-11-04. The owner-provided `research-design-brief.md` is a proposal,
not experimental evidence. Its anti-fabrication objective is retained; the
following claims and methods are corrected before new observed outcomes.

1. **Coverage and power.** Three selected names and 41 evaluation sessions do not
   establish broad profitability, but power is not literally zero. A target of
   300–500 names and multiple years is an acquisition ambition, not a derived
   minimum. Minimum detectable effects require independent-day variance, costs,
   dependence and a prespecified economically meaningful effect. No numerical
   power estimate is reported without those inputs. Existing 80-session support
   gates remain unchanged; passing a sample gate alone does not validate alpha.
2. **Five policy gates.** A=base, B=base+RS, C=base+news, D=base+RS+news,
   E=base+(RS OR news). A/B/C/D form a 2x2 policy-gate ablation; E is the distinct
   current OR comparator. This is not a randomized causal intervention on news.
   The primary incremental contrast is E minus B on common complete calendar
   days, with four fixed preassigned reference-unit slots and no reallocation.
   The news-only additions have N=true, R=false. Unknown is not false. Different
   selected per-trade means cannot be presented as a paired treatment effect.
3. **What a news flag means.** It establishes an exact-ticker headline in a
   successful, physically captured response, within the fixed publication window.
   It does not establish positive sentiment, an earnings surprise, exhaustive
   coverage or original first publication. An empty page is response-level
   absence, not proof there was no relevant article anywhere. No historical news
   is fabricated from today's payload. Raw versions, receipt hashes, publication,
   application check and post-commit availability remain distinct clocks.
4. **Actual existing limits.** Local inventory has 614 historical market packets
   over 61 source sessions, but zero news packets. The historical twelve controls
   remain negative exploratory proxies, not the current full live strategy's
   backtest. The UW endpoint is documented as latest headlines; current access
   does not prove multi-year first-version history. Old application-clock rows
   cannot acquire retroactive physical receipts. Reviewed PAPER/forward history
   is not permanently untouched OOS for a hypothesis designed after review.
5. **Failure of H1.** A pooled null does not logically eliminate a preregistered
   conditional effect. A CI crossing zero is inconclusive, not proof of zero
   alpha. Futility requires a justified upper bound below the prespecified
   economic effect; support requires adequate fresh evidence, not a favorable
   p-value selected after repeated peeking. No new stop-at-positive search.
6. **News controls.** Unrestricted cross-ticker/date shuffling destroys news
   intensity, earnings, intraday and regime structure. A future placebo needs
   defensible block/stratum exchangeability. Window sensitivity is additional
   tested specifications, not a free test; monotonicity is not an economic law.
   Direction ordering requires a stated hypothesis; modern model weights can
   leak historical outcomes even with date/price removed from the prompt.
   Prospective, version-pinned labels are preferable. No external LLM is invoked.
7. **Direction and opening trades.** Bid/ask aggressor, sweeps, multileg flags
   and volume versus OI are distinct. Volume greater than yesterday's OI cannot
   identify a specific trade as opening. Ambiguous directions remain excluded or
   unknown under the existing source rules; no convenient long/short reversal.
8. **Execution and metrics.** Fixed one-minute-delay/60-minute cached-price
   references are not intraday executions, stop/target replays or five-day policy
   outcomes. Whole shares, explicit per-side costs and exact-time SPY matching
   are retained. Intraday option NBBO/exit data are required for an option study;
   do not subtract generic option friction from stock returns and call it option
   P&L. Matched-SPY excess is not beta-neutral alpha. An independent-unit ledger
   is not a cash portfolio, so trade-count annualized Sharpe, cash-account
   drawdown/Calmar and capacity are not asserted. DSR/SPA remain unestimated when
   full trial histories and defensible sampling assumptions are absent.
9. **Holdouts.** Study G stays sealed throughout this delivery. No file is opened
   merely because the attachment mentions a final-stage unsealing. A genuinely
   future cohort starts after this registered implementation; all prior reviewed
   windows retain their exploratory status. The existing 438-candidate protocol
   is not expanded or quietly relaxed.

## Execution ledger for this delivery

The new work implements five fixed arms, four explicit cost schedules (one base,
three diagnostic stresses), and a read-only source inventory/export/evaluator.
These are registered configurations, not a claim of executed market trials.
Synthetic software tests validate arithmetic and invariants only. No new fitted
winner, formal inference, historical news return or validated alpha is reported.
Actual local/native source counts and deployment evidence belong in the closeout.

Next studies require separately recorded proposals: normalized same-time flow,
news-direction/novelty taxonomy, point-in-time earnings strata, execution-window
comparisons and original-call replay parity. Existing live rules and original
records stay the reference, with no automatic promotion from this research.

## Final as-of hardening

Exports refuse a first-day cutoff strictly inside the post-open assessment
interval; a subset cannot silently replace the original first daily opportunity.
Price labels use only new, profile-matching machine-context scans, with knowledge
time at scan completion after its price reads. Legacy alpha-lab hook-start clocks
cannot supply strict labels. Cache/source timestamps are still not exchange
execution timestamps. Exact-time SPY matching and the reference-unit limitation
remain; no stock or option fill is claimed.
