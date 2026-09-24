# PRODUCT_OVERRIDE — Live Alpha v1 (registry 5.25)

Date: 2026-09-24. Owner instruction: `UOA_Canli_Alfa_Urununu_Bitir_Master_Prompt.md`
(24 Sep 2026), given in chat with the explicit order to build, merge and deploy.

## What changes

1. **The live recommendation screen gives a view.** The new screen at `/`
   ("Bugünün Fırsatları") issues reasoned ALIM (BUY), KOŞULLU ALIM
   (CONDITIONAL_BUY), İZLE (WATCH), KAÇIN / AŞAĞI YÖNLÜ KURULUM (AVOID /
   BEARISH_SETUP) and ÇIKIŞ İNCELEMESİ (EXIT_REVIEW, only for a tracked
   position). The old "this page never says buy/sell" rule stays in force for
   the screens it was written for: the Alfa Board (now `/alfa`), `/gamma`,
   `/opsiyon`, `/defter`. Their honesty guard (`webapp/board/honesty.py`) and
   its render test are unchanged, except that the path `/` moves out of that
   test's list because `/` is now the new screen. `/alfa` stays in the list.
2. **The stock is an execution path, not only a comparison arm.** Option
   flow is one of the information sources. The stock and the option structure
   are two real ways to act on the same opportunity, and the screen says which
   one it prefers and why.
3. **Research results are not a publishing precondition.** H-family verdicts,
   four quarters of data or a positive p-value are not required before a
   clearly labelled experimental policy may publish a view.

## What does not change

- No broker connection, no automatic order, live or paper. `BROKER_EXECUTION`
  is closed. PAPER is a simulation ledger in our own database.
- Past verdicts stand. Directional UOA confluence stays REJECTED
  (phase-3.6-closeout). H01, H02, H03 and H04 stay REJECTED, and H10 stays
  INSUFFICIENT_DATA. S01 found the loss is all cost. S02 found the vol premium
  untradeable. The new policy records `derived_from` and these findings on
  every recommendation. It never shows them as supporting evidence.
- Frozen profiles (`profiles/v5_*.yaml`, `board_v1.yaml`,
  `options_alpha_v1.yaml`) are not edited. The new policy has its own profile,
  `profiles/live_alpha_v1.yaml`.
- Frozen acceptance docs are not edited. This override and
  `docs/phase-5.25-live-alpha-acceptance.md` are new documents.
- `alfa_*` evidence tables get no DROP, DELETE or ALTER. The new records live in
  new `alfa_live_*` tables, created with `create(checkfirst=True)`.
- No edge claim. Every recommendation carries
  `evidence_status = EXPERIMENTAL_RULES`, and the screen shows it.

## Three things kept apart

- **Güncel işlem önerisi.** A view derived from today's reachable data, with
  sources, conditions and risk. This is what v1 produces.
- **Ölçülmüş işlem avantajı.** A cost-adjusted performance that a separate
  evaluation has measured. None exists. The forward PAPER record is what will
  one day measure it.
- **Emir icrası.** Not in scope.
