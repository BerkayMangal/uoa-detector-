# Research150: remaining OHLCV supplement S1

Frozen at **2026-11-12T16:51:26.074469+00:00**, before this supplement is run. V2 first-label execution had already started around 16:47 UTC. The author has not read pilot outcomes. This is exploratory prespecification, not a globally outcome-untouched or confirmatory experiment.

The source is the exact 150-item BIST/VIOP text; every original remains NOT_TESTED. This supplement registers only US adaptations of **79, 80, 139, 140, 144**. All five keep widths **1/5/15 minutes**, independent holds **1/5/15 minutes**, separate directions, the same nine-symbol universe, exact one-minute latency, costs **5/10/20 bps round trip**, non-overlap state, coverage rules, cash sleeves and inference framework. V2 artifacts are unchanged.

## Exact common rules

At signal bar k, r[k]=ln(C[k]/O[k]). Python slices exclude their right endpoint; sign(0)=0. All history is completed, prior-only where stated, and resets each RTH session. The default nine-prior-bar eligibility remains; H139 needs sixteen prior bars, and H144 additionally needs sixty native one-minute bars before the current signal bar starts. Candidate and baselines use identical family-valid input rosters. Undefined statistics are UNKNOWN/ineligible, not a zero signal.

Enter at the native one-minute open one full minute after signal-bar close; exit at the native one-minute open 1/5/15 minutes after entry, strictly before session close. Each policy has independent non-overlap state. Use unchanged v2 quality, accounting and cost conventions. Actual full information intervals govern purge, including longer supplement histories.

## Five executable definitions

### H079_US_S1 — original 79: Ay Sonu Kurumsal Rebalancing

> Her ayın son 2 işlem gününde portföy dengeleme akışlarının yarattığı yönlü momentum.

**Feature:**
- M_s(k) = [s*ln(C[k]/C[k-3]) > 0] AND [s*r[k] > 0]. E(d)=1 iff d is one of the last two official US regular trading dates in the FULL calendar month containing d.

**Candidate:** E(d) AND M_s(k)

**Fixed baselines:**
- momentum_no_calendar: M_s(k)

**Eligibility and limits:**
- Missing OHLCV or default warm-up makes every arm ineligible. Calendar classification cannot use the last two dates in the downloaded subset.
- Measures a last-two-session momentum filter; does not identify fund rebalancing orders or establish that rebalancing caused returns.
- Baseline comparison changes exposure and turnover; improvement may reflect cash or fewer costs. It cannot alone identify a calendar-specific edge.

### H080_US_S1 — original 80: Yeni Ay İlk 2 Gün Akışı

> Ayın ilk 2 işlem gününde yeni fon girişleriyle oluşan pozitif eğilim.

**Feature:**
- B(d)=1 iff d is one of the first two official US regular trading dates in the FULL calendar month containing d.

**Candidate:** B(d); enter the separately registered side s at each eligible non-overlapping decision.

**Fixed baselines:**
- always_same_side: TRUE at every data-eligible decision; same side s.

**Eligibility and limits:**
- Default complete nine-prior-bar warm-up is retained for both arms. No current-bar price-direction condition is introduced.
- Fund inflows are unobserved.
- Filter-versus-always-active comparison changes exposure/turnover; lower friction alone is not proof of a month-start effect.
- Long is the stated positive-drift claim. Short is a separately registered falsification diagnostic and cannot be promoted as support for it.

### H139_US_S1 — original 139: Hurst Üstel Katsayısı Rejim Filtresi

> Kayan pencerede Hurst üssü > 0.55 iken trend takip, < 0.45 iken ortalamaya dönüş algoritmasını aktif etme.

**Feature:**
- x = r[k-16:k], exactly 16 prior completed signal-bar within-bar log returns, oldest first; exclude current r[k].
- For each m in {4,8,16}, partition x into all consecutive non-overlapping blocks of m, beginning at x[0]; block counts are 4,2,1.
- For a block b: mean_b=mean(b), Z[0]=0, Z[j]=sum_{i=1..j}(b[i]-mean_b), R_b=max(Z)-min(Z), S_b=sample_sd(b,ddof=1).
- Q_m = arithmetic mean of R_b/S_b across EVERY block at that scale. Every block must have finite S_b>0 and R_b>0. Any invalid block makes candidate AND both baselines ineligible; do not omit blocks.
- For a_m=ln(m), y_m=ln(Q_m), H_RS = sum((a_m-mean(a))*(y_m-mean(y)))/sum((a_m-mean(a))^2), the OLS slope with intercept across the three scales. No clipping or fitted correction.
- d(k)=sign(r[k]) if H_RS>0.55; d(k)=-sign(r[k]) if H_RS<0.45; otherwise d(k)=0. Equality at either threshold means cash; r[k]=0 means cash.

**Candidate:** d(k)==s

**Fixed baselines:**
- always_momentum: sign(r[k])==s
- always_contrarian: -sign(r[k])==s

**Eligibility and limits:**
- At least sixteen prior complete bars plus current bar in the same RTH session. Both fixed baselines use exactly the same estimator-valid roster.
- Original thresholds 0.55 and 0.45 are retained, but the 16-return, three-scale R/S estimator and base direction rule are newly declared conventions.
- This tiny-window R/S proxy is biased and uncertain; it does not establish a true Hurst exponent, persistence, long memory, or a market regime. No threshold calibration is performed.
- Both baseline contrasts must be shown; never choose the better-looking baseline after results.
- First eligible standard-session decisions: 09:47/10:55/13:45 ET for 1m/5m/15m. Feature start is start[k-16].

### H140_US_S1 — original 140: Getiri Otokorelasyon İşaret Değişimi

> 15 dakikalık getirilerin 1. gecikmeli otokorelasyonu pozitifken momentum, negatifken contrarian çalışma.

**Feature:**
- rho(k)=Pearson(r[k-9:k-1], r[k-8:k]), exactly eight overlapping lag-1 pairs from the nine PRIOR completed signal-bar returns. d(k)=sign(rho(k))*sign(r[k]).

**Candidate:** d(k)==s

**Fixed baselines:**
- always_momentum: sign(r[k])==s
- always_contrarian: -sign(r[k])==s

**Eligibility and limits:**
- Either lag array with zero/nonfinite variance makes every arm ineligible. Exactly zero rho or current return gives candidate cash, not a missing observation. All arms share the same valid prior-feature roster.
- The source explicitly specifies 15-minute returns. The 1m and 5m versions are separately registered timeframe adaptations, not replications of that wording.
- Rolling window length, within-bar return convention and current-return direction rule were absent from the original and are fixed here without outcome access.
- Both baseline contrasts must be reported.

### H144_US_S1 — original 144: Kuyruk Riski (Tail Imbalance)

> 1 dakikalık getirilerin pozitif kuyruk (en iyi %5) ve negatif kuyruk (en kötü %5) asimetrisinin yön belirlemesi.

**Feature:**
- Let a=start_time[k] of the CURRENT signal bar, not its end. Take exactly the 60 contiguous native 1m bars with start_time in [a-60 minutes,a), within the current RTH session. The whole current signal bar is excluded.
- For each native minute j, u[j]=ln(close[j]/open[j]). Sort all 60 u ascending, retaining ties. L=mean(sorted_u[0:3]); U=mean(sorted_u[57:60]), exactly the bottom and top 5%.
- If U>L, T=(U+L)/(U-L). Candidate direction is sign(T). No clipping; T may lie outside [-1,1] when both ranked tails have the same sign.

**Candidate:** s*T>0; exactly T=0 means cash.

**Fixed baselines:**
- always_same_side: TRUE on the same valid-tail feature roster; same side s.

**Eligibility and limits:**
- U<=L or any missing/nonfinite required minute makes every arm ineligible. Preserve the v2 minimum nine prior signal bars in addition to the sixty prior native minutes.
- Call this signed top/bottom-3-of-60 tail balance, not a conditional positive-versus-negative tail estimator; the worst three observations can still be positive.
- Every cadence uses native 1m tails, preserving the source return granularity. 5m and 15m change only decision cadence, not tail sampling.
- Window length, normalization and sign mapping are operational choices absent from the source.
- First eligible standard-session decisions: 10:31/10:35/12:00 ET for 1m/5m/15m. The current signal bar contributes no tail observations.

## Calendar and inference safeguards

Month-start/end flags use the full official calendar month, even beyond captured data. November 11 is not November month-end. The JSON stores the fixed first/last two calendar dates for every relevant month. No future prices enter the flag.

Calendar-filter comparisons change time in market, turnover and costs. An improvement versus an always-active baseline may come from cash or reduced friction; it does not identify a calendar-specific return effect. Report distinct active event days; repeated intraday trades and cash-only days do not manufacture independent calendar replications. Sparse calendar arms remain descriptive.

The registry has **90 candidate/diagnostic policies** (81 source-directed, nine H80 short falsification controls), **126 baseline entries**, **126 primary contrasts**, and **648 policy-cost views**. Two fixed baselines are mandatory for H139/H140; never select one after seeing results. Shared or similar baselines are not independent trials.

Append the 126 contrasts to v2's 54, for 180 current registered primary contrasts when reporting together. Preserve the full larger search history, including 5,184 known earlier exploratory configurations and incompletely known additional searches. Joint date-block max-statistic inference must cover the combined selection family. DSR cannot claim a complete search history or confirmation from these counts.

Retain v2's minimum 60 complete day clusters, paired date-level accounting, expanding chronological development folds, exact interval purge, 5-session blocks / 10-session sensitivity, 10,000 resamples, seed150 and one-sided simultaneous99% bounds. October–November remains partly observed chronology with too few sessions for a confirmation pass. No automatic promotion to UOA production.

## Remaining five ideas are not silently dropped

### Original 71: Saat Başı Mikroyapı Anomalisi

> Her tam saatin ilk 2 dakikasında kurumsal TWAP/VWAP algoritmalarının devreye girmesiyle oluşan periyodik hacim sıçramaları.

**Disposition:** DESCRIPTIVE_ONLY_NO_ORIGINAL_TRADING_DIRECTION

For each local full hour H=10:00,...,15:00 with complete native minutes, D_H=ln(1+V[H]+V[H+1m])-ln(1+V[H-2m]+V[H-1m]). Aggregate within ticker/day and then across the fixed nine sleeves; compare paired two-minute volume windows descriptively.

The original predicts volume periodicity, not a long/short return. OHLCV cannot identify institutional TWAP/VWAP actors. Assigning a direction, 1/5/15m holding policy, or using 5m/15m bars to recover the exact first two minutes would introduce a new hypothesis. This descriptive formula is not registered for execution in S1.

### Original 75: Gün İçi vs Gece Getiri Ayrışması

> Gece negatif getiri üreten günlerde gün içi pozitif tepki, gece pozitif günlerde gün içi realizasyon eğilimi.

**Disposition:** BLOCKED_CORPORATE_ACTION_ADJUSTMENT

With verified split factor S_d (new shares per previous share), cash entitlement D_d per previous share, prior scheduled-session close C_prev and current open O_d, economic overnight gap g=ln((S_d*O_d+D_d)/C_prev). A new fixed-hold contrarian US rule could set direction=-sign(g), with a same-clock unconditional baseline.

Vendor adjustment semantics and point-in-time split/dividend entitlements are unverified. Do not assume S=1,D=0 or compare stale last-available closes across missing sessions. Source describes whole-session overnight/intraday decomposition; 1/5/15m fixed holds would additionally be an explicit new horizon adaptation.

### Original 87: Tatil Sonrası İlk İşlem Günü

> Uzun tatil dönüşü ilk 1 saatte biriken emirlerin yarattığı aşırı oynaklık ve ortalamaya dönüş.

**Disposition:** UNDERPOWERED_AND_FIRST_HOUR_WARMUP_CONFLICT

Long holiday reopening means the previous official regular session is at least four calendar days earlier, with at least one scheduled weekday exchange holiday in between.

Compare first-hour realized variance from 60 completed native 1m within-bar returns against ordinary-session first-hour variance; mean-reversion would require a separately frozen reference-price and entry rule.

Only seven such reopening dates fall in this captured window. Source confines its event to the first hour. The unchanged v2 15m warm-up is not complete until 12:00, outside that window. Do not weaken warm-up or extend the event to fabricate 15m eligibility.

### Original 143: Volatilite Kümelenmesi (GARCH Etkisi)

> Yüksek volatiliteli bir saati takip eden 2 saatte pozisyon büyüklüğünü yarıya indirerek Sharpe oranını artırma.

**Disposition:** DEFERRED_OVERLAY_SPECIFICATION

Required before a separately frozen overlay:
- A frozen base strategy selected without its returns.
- Exact high-volatility hour estimator/threshold and history.
- Two-hour half-size activation/expiry and overlapping-trigger rules.
- Capital, cash, transaction-cost scaling, and risk-adjusted paired baseline.

Position halving is a risk overlay, not a direction. The source does not supply the base policy or high-volatility definition. No overlay formula or engine run is authorized in S1.

### Original 146: Shannon Entropisi ile Piyasa Verimliliği

> Fiyat adımlarının entropisi maksimuma ulaştığında (tam rastgelelik) işlemi tamamen durdurup nakitte kalma.

**Disposition:** DEFERRED_OVERLAY_AND_INVALID_RANDOMNESS_INFERENCE

Required before a separately frozen overlay:
- Frozen base strategy, price-step granularity and bins.
- Entropy sample length, definition of maximum/near-maximum, and zero-step handling.
- Cash-filter timing and paired ungated baseline.

Marginal Shannon entropy does not prove temporal independence, full randomness, or market efficiency. Price steps and bins are unspecified, so a new named filter must be defined independently. No overlay formula or engine run is authorized in S1.

## Review and provenance

Independent formula review by /root/independent_review was completed before this supplement run. Its requirements concerning full calendars, every-block Hurst validity, prior-only autocorrelation, unbounded ranked-tail balance and exposure attribution are incorporated. The JSON retains exact original source text/hash and full structured definitions; supplement-registry-s1.json lists every policy and contrast. supplement-freeze-s1.json hashes the frozen artifacts.

No outcomes were opened, no fitted thresholds were chosen, and no raw, canonical, collector, v2 or live-policy files were changed by this specification work.
