# Phase 5.69 — US intraday research: archival acceptance record

Prepared 12 November 2026 under the owner's explicit authorization to pursue
autonomous US research using the paid data sources and existing UOA work.
This phase preserves a reviewable record of the initial 150-idea classification
and the bounded US pilot. It does not change live signals, application behavior,
strategy thresholds, protected study labels, or historical PAPER records, and
does not authorize broker execution or claim a profitable strategy.

## 1. Timing and authority

This repository document is an **archival registration**, assembled after the
original work-directory protocol and implementation seals were written. It must
not be represented as a repository preregistration that existed before that
work. The original protocol, registry, clarifications and their seals below
retain authority; the summary here does not retrospectively alter them.

The original v2 freeze records `2026-11-12T16:22:54.207223+00:00`, explicitly
stating “frozen before this pilot outcome evaluation; no claimed prior-history
blindness.” It binds the protocol and registry. The reviewed engine record is
dated `2026-11-12T16:41:05.565604+00:00`; it binds those documents, both
clarifications and seven implementation/test files, records 20 synthetic tests,
and states that real market labels had not been opened for this pilot. These
are preserved local records, not independently notarized chronology.

The original hypothesis source digest is
`5948db58084f89c465a760dd6fbad884d304dde5d2dfac7366b0aff94ebbd7a4`.
The inventory embeds a creation timestamp of
`2026-11-12T16:20:46.257841+00:00` but its observed filesystem modification time
is later. Its current bytes are archived faithfully; its digest was not listed
in the v2 freeze or engine document seals. No claim of earlier cryptographic
commitment to that final inventory digest follows from its creation field.

The following originals are copied unchanged under
[`artifacts/research150-us/`](../artifacts/research150-us/README.md). The recorded
times are observed source filesystem modification times in UTC, not independent
proof of creation or preregistration. Full precision and byte sizes appear in
[`archive-manifest.json`](../artifacts/research150-us/archive-manifest.json).

| Original file | Observed source modification, 2026-11-12 UTC | SHA256 |
|---|---|---|
| `pilot-protocol-v2.md` | 16:22:54.206962 | `456dfa9b66a3de453c1d3af46722c590c81adf30fc35738cfbcd992e3ca2ac29` |
| `pilot-registry-v2.json` | 16:22:54.211348 | `cb04d0c52d8a941ab622e69b2d21281fe72afbc6418dd0bf82f461435b8bcaa9` |
| `pilot-freeze-v2.json` | 16:22:54.211776 | `693cb156b36b5b76485389f641522236f4bf274c2fdb1ebb0324f2217f882571` |
| `pilot-v2-implementation-clarification.md` | 16:25:35.370224 | `8821e884953b191c6846c464b003292d853138b45cdd912865c2a331289a2d99` |
| `pilot-v2-inference-gates.md` | 16:27:18.537012 | `dba8fa02830e9c8112da47c90cc1b55e2a4e22259ced86b7d24263838a02e23e` |
| `engine-code-hashes.json` | 16:41:05.566459 | `65c7cb765fcedf4f60e7a4f017b8e79f193c185fbbaefb2eae04f6dd376769dc` |
| `inventory.json` | 16:28:15.036473 | `81cfa00a63daf9b26436faceec5f6064a949b2d686a3cb7341d8443fd59f62d4` |

The initial `pilot-protocol.md` proposal was superseded without being executed;
it remains in the research workspace and does not govern this run. Changes or
corrections require a separate dated record; frozen originals are not rewritten.

## 2. What the 150 ideas establish

The source list concerns BIST/VIOP and frequently requires ITCH, market depth,
individual trades, or other unavailable data. US adaptations are new hypotheses.
The inventory classifies all 150 source ideas as follows:

| Data feasibility | Ideas | Interpretation |
|---|---:|---|
| `TESTABLE_US_WITH_AVAILABLE_OHLCV` | 13 | Potentially measurable from available US OHLCV, subject to actual coverage and a precise registered implementation. |
| `PROXY_ADAPTATION_NEW_HYPOTHESIS` | 58 | Needs an explicitly different US proxy or specification; the original claim is not thereby tested. |
| `UNAVAILABLE_EXACT_DATA` | 79 | Exact required data is unavailable; cannot substitute invented depth, trades, classifications, or timestamps. |

These are **feasibility counts, not 150 tested strategies**. All original-market
hypotheses remain `NOT_TESTED`. Being feasible does not imply that an idea is
profitable or included in the first pilot.

## 3. Frozen first pilot

The authoritative [v2 protocol](../artifacts/research150-us/pilot-protocol-v2.md)
and [registry](../artifacts/research150-us/pilot-registry-v2.json) specify three
US adaptations: H138 squeeze plus breakout, H142 return outlier reversal, and
H145 price/volume momentum. Their matched baselines remove the designated
condition or use the specified simple direction rule.

- Nine fixed symbols: SPY, QQQ, AAPL, NFLX, NVDA, TSLA, MSFT, META, AMZN.
- Three completed signal-bar widths (1/5/15 minutes), three independent holding
  periods (1/5/15 minutes), and separate long/short arms give 54 candidates and
  54 paired baselines: **108 policies**. Fixed round-trip stock costs of 5 bps
  primary and 10/20 bps stresses give **324 policy-cost views**, not independent
  replications. There are 54 primary candidate-minus-baseline contrasts.
- Jan 2–Sep 30, 2026 is the development/reference period, with expanding April
  through September evaluation blocks. Oct 1–Nov 11 is the frozen chronological
  evaluation period. Actual covered sessions determine evaluability.
- Features use completed native-minute-derived bars, reset each regular US
  session, and require the fixed nine preceding bars plus the current bar.
  Exact calendar, latency, entry/exit, conflict and coverage rules remain as
  written in the protocol and implementation clarification.
- Historical entry/exit opens with one-minute latency are hypothetical price
  proxies. Equal 1/9 symbol sleeves retain inactive capital as cash; unavailable
  returns remain unknown. Short-direction results do not verify borrow/locates.

The inventory and pilot do not expand the live strategy or silently test the
other 147 source ideas, other horizons, option contracts, or news filters.

## 4. Interpretation and inference boundaries

Previously examined history remains **burned**. The June–September panel
includes 61 previously inspected sessions. November 9–11 outcomes and other late
2026 research have also been observed. The chronological final period is partly
previously observed, **not an untouched holdout**. Existing protected/reserved
study labels remain excluded. Prior UOA research includes at least 5,184
exploratory configurations; the total selection history is incompletely known.

Inference uses paired daily candidate-minus-baseline returns, shared contiguous
session blocks across all registered contrasts, and the frozen multiplicity
correction. The two archived clarifications require at least 60 complete paired
sessions; at least 20 nonzero daily differences and positive finite HAC variance
for an eligible contrast; and at least 20 eligible contiguous block starts with
every analyzed session represented. Five-session primary blocks, ten-session
sensitivity, 10,000 draws, seed 150, and simultaneous one-sided 99% bounds remain
fixed. No denominator floor or artificial fill repairs a failed gate.

The October–November interval has fewer than 60 sessions and can only receive
descriptive summaries under this protocol. A relative improvement can be a
less-negative strategy beating a losing baseline; it does not establish
absolute profitability. DSR remains insufficient evidence without defensible
trial history, dependence, moments and sample size. Reused-history statistics
cannot establish fresh confirmation, regardless of a favorable number.

News is **context only**: it does not enter this pilot's signals or create a
historical confluence claim. Historical event timestamps do not prove the
application physically possessed the inputs at decision time. ThetaData is
**awaiting a real Terminal response**; code availability, subscription payment
and stock OHLCV outcomes do not establish live Theta access or option returns.
Option economics require the separate exact-contract quote validation in
[Phase 5.68](phase-5.68-theta-option-validation-acceptance.md).

## 5. Review package and completion

This change contains documentation, compact governance artifacts and a frozen
research source snapshot; it changes no application runtime. The original seven
governance/inventory files total 313,966 bytes. A deterministic 62,799-byte
[`engine-source.zip`](../artifacts/research150-us/engine-source.zip) contains
exactly the seven Python files named in the original engine hash record, with
each original byte sequence preserved. Its SHA256 is
`748bd66901de840506e57517a775fbb8adaf315ee7288af84c1b2aa045d0e57b`.
The artifact README describes safe verification, extraction under `engine/`,
synthetic self-tests, and metadata sealing before a private market replay.

The public review package must remain below 2 MB. Do not include raw prices,
provider/native packets, news content, credentials, or owner-private exports.
Existing local path strings inside the frozen originals are preserved only for
byte identity; new documentation uses relative artifact references. Market
reproduction still requires the private dataset plan, coverage manifest and
hash-matched canonical files; these are not supplied by the source archive.

[The result record](phase-5.69-results.md) stays pending until completed outputs,
coverage exclusions, all registered views, run seals and independent review are
available. Completion requires honest accounting of missing data and every
registered arm, verification that archived bytes match their seals, clear
stock/option/source distinctions, and a bounded durable result package. It does
not require finding a winner. No signal promotion, new forward trial or runtime
change follows automatically from this research phase.
