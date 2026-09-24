# STATUS — Live Alpha v1 (registry 5.25)

This is the §21 table of the master prompt. "Kod var" (code exists), "test var" (tests exist) and "canlı doğrulandı" (verified live) are different states.

| Teslim | Durum | Kanıt |
|---|---|---|
| Sayfa kurtarıldı | **Canlı doğrulandı**, 2026-09-24 11:20 UTC, `818f0cb`. `/alfa` 0.6–1.0 s (önceden 10–29 s). Yetkili Chrome oturumunda `/`, `/alfa`, `/opsiyon`, `/defter` ve `/gamma` 200 döndü. | **Arıza.** Railway HTTP logs on 2026-09-23/24 show authorised `GET /` taking 10–29 s. `board sources` adds up to ~18 s over 40+ queries at ~0.25 s each. At 09:24 UTC the owner gave up (499). **Düzeltme.** PR #85 prebuilds the view in the background and serves it from memory. 7 tests. |
| Güncel tarama | Kod ve test var. Canlı doğrulama merge'den sonra. | `webapp/live_alpha/job.py run_cycle`. Stored flow (`live-*` run), spot (`alfa_atm`), bars and expiries. The funnel and universe go in the snapshot. |
| Haber hattı | Kod ve test var. Gerçek erişim production'da doğrulanacak. | `GET /api/news/headlines?ticker=`, schema from the official docs (W1). The local key is revoked (B2). The production key works for other endpoints. |
| Karar motoru | Tamam (test). | Paths P1–P4, plus WATCH and AVOID. BUY/READY is reached by P1 and P2. CONDITIONAL comes from P3 and closed sessions. BEARISH comes from P4. `tests/unit/test_live_alpha_engine.py`. |
| Hisse planı | Tamam (test). | Entry zone, chase limit, 1.5 ATR stop, 2R policy target, 5 sessions, PAPER size. Checked by hand in `test_stock_plan_by_hand`. |
| Opsiyon planı | Tamam (test). Gerçek fiyatlı örnek merge'den sonra. | Four structures on the `options_alpha_v1` cost model. Spread arithmetic checked by hand. Multiplier carried through. QUOTE_PENDING, RISK_BLOCKED and INVALID paths covered. |
| Açık öneri kartı | Tamam (test ve önizleme). | `webapp/templates/live.html`, schema of master prompt §11. Preview rendered in Chrome from fixture data only, never shown as live. |
| Güncelleme | **Canlı doğrulandı.** İlk döngü 11:18:19 UTC: status=ok, PREMARKET, 10 kart, `/health` içinde `live_alpha`. | Daemon thread with its own loop. `alfa_live_scan` and `alfa_live_heartbeat` get one row per cycle. `/health` shows `live_alpha` once the job has cycled. Records are idempotent: a new rec only when the decision changes. |
| PAPER/izleme | Tamam (test). | Fill only on a price seen after publication. Exits: stop, target, time, thesis broken, daily bar (stop first). Exit signals are sticky. UNRESOLVED when there is no exit price. One PAPER per opportunity. |
| Kota ve sır güvenliği | Tamam (test). | Every call goes through `quota_ledger.reserve`. A refusal is a failure state. Basic auth on every route. POST routes check same origin. No key in code or fixtures. |
| Release | **Tamam.** #85 merge `6ca8d8b`, #86 merge `818f0cb`. Railway deploy SUCCESS. Health SHA eşleşiyor. | PR #85, then PR #86. Local gate is green. CI runs on each PR. |
