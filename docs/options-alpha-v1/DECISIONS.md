# options-alpha-v1 — kararlar

Bu dosya, bu kapsamda alınan ve geri alınabilir olması gereken kararları tutar.
Her karar: ne, neden, nasıl geri alınır.

---

## D1. Faz numarası: 5.24, ve 5.10–5.19 bandına girilmedi

**Karar.** Bu kapsam `docs/INDEX.md` §7'ye **5.24 — Options Alpha v1** olarak
kaydedilir.

**Neden.** §7'deki tahsis durumu ölçüldü: 5.0–5.9 tek tek alınmış, **5.10–5.19
adlandırılmış adaylarla dolu bir bant** (flow_poll düzeltmesi, gamma_live birim
sorunu, dedicated Postgres…), 5.20/5.21/5.22 Study E/F/G. Master prompt bu banda
sessizce yerleşmeyi açıkça yasaklıyor ve haklı: o numaralar başka işlerin.
5.23 ise "sıradaki çalışma harfi (H)" için ayrılmış. Bu kapsam bir çalışma harfi
değil, ürün + araştırma alanı — o yüzden 5.23'ü de tüketmiyor, 5.24'e oturuyor.

**Geri alma.** INDEX satırını sil; bu dizindeki hiçbir şey başka bir faza atıfta
bulunmuyor.

---

## D2. Ölçmeden tasarlamak yok: yetenek matrisi önce

**Karar.** Hiçbir opsiyon yapılandırma kodu, `scripts/probe_uw_option_capability.py`
gerçek isteklerle koşup matrisi üretmeden yazılmaz.

**Neden.** Master prompt §5'in kuralı. Ama somut sebep şu: ilk elle geçişte
**iki kez** yanlış sonuca varıldı ve ikisi de tasarımı yanlış yöne çevirecekti.

**Geri alma.** Yok — bu bir çalışma kuralı, kod değil.

---

## D3. `/historic` boş değil; zarf `chains`

**Karar.** Tüm `/api/option-contract/{id}/historic` okumaları satırları önce
`chains`, sonra `data` anahtarından alır.

**Neden.** İlk probe bu uç noktayı vadesi dolmuş **ve** canlı likit kontratlarda
0 satır döndürdü ve neredeyse "sağlayıcı engeli" diye kaydedildi. Gerçek sebep
probe'un yalnız `data`'ya bakmasıydı; üretimdeki `webapp/board/outcome_job.py`
zaten `chains`'i okuyor. Doğru okunduğunda SPY261016C00785000 için
**92 günlük seri** geliyor (2026-05-12 .. 2026-09-22), içinde `nbbo_bid`,
`nbbo_ask`, `implied_volatility`, `open_interest`, taraf bazlı hacimler.

Kayda değer olan: kendi ayrıştırma hatamı dış engel diye raporlamak, görevin
açıkça yasakladığı şeydi. Matris üreticisi artık üretimle **aynı** zarf kuralını
kullanıyor ki bu hata tekrarlanamasın.

**Geri alma.** Yok — bu bir hata düzeltmesi.

---

## D4. Zincir NBBO'su **B kalite**, A değil

**Karar.** `option-chains` ve `/historic` üzerinden gelen NBBO, master prompt
§7'deki **B seviyesi** (gün sonu / seyrek zamanlı gerçek snapshot) sayılır.
Gün içi fill sırası veya stop tetiklenme sırası iddiası bu veriyle yapılamaz.

**Neden.** Satırların zaman alanı `last_tape_time` ve bu **son işlem** zamanı,
kotasyon zaman damgası değil. Kotasyonun hangi an geçerli olduğu bilinmiyor.
Ayrıca `delta` bazı satırlarda `null`, `implied_volatility` screener çıktısında
boş gelebiliyor.

**Sonucu.** Bu veriyle yapılabilecek olan: gün sonu fiyatına dayalı, muhafazakâr
varsayımlı, giriş/çıkış gecikmesi açıkça modellenmiş araştırma. Yapılamayacak
olan: gün içi gerçekleşme, stop/hedef sıralaması, "şu fiyattan dolardı" iddiası.

**Geri alma.** Gün içi kotasyon serisi olan bir kaynak doğrulanırsa kalite
seviyesi yükseltilir; o zamana kadar her sonuç B etiketi taşır.

---

## D5. Aday üreteci `screener/option-contracts`, elle zincir taraması değil

**Karar.** Aday kontratlar `/api/screener/option-contracts` ile üretilir.

**Neden.** `date=` gerçekten filtreliyor — kanıt: 2026-09-22 ve 2026-06-26 için
**tamamen farklı** sembol listeleri döndü. Uç nokta ~48 alan (delta, gamma,
theta, vega, `premium`, `days_of_vol_greater_than_oi`, `is_new`, `vol_pctile_15d`,
`prev_oi`, `next_earnings_date`, `sector`) ve ~90 filtre parametresi taşıyor;
`min_dte`/`max_dte`, `min_delta`/`max_delta`, `vol_greater_oi`, `min_premium`
doğrudan seçim kurallarına karşılık geliyor.

Alternatif — her gün 12–14 bin satırlık tam zinciri çekip elde filtrelemek —
aynı sonucu çok daha fazla kota ile üretirdi.

**Dikkat.** `unusual=true` preset'i UW'nin **kendi** olağandışılık tanımını
uygular (volume>OI, OTM, DTE≤60, ask-side≥%50, prim≥10k$). Bu bir seçim
yanlılığıdır ve kullanıldığı her sonuçta belirtilir; evren tarafsız değildir.

**Geri alma.** Aday üreteci arayüzünün arkasında; tam zincir taramasına dönmek
tek modül değişikliği.

---

## D6. Araştırma penceresi 2026-05-13'te başlar

**Karar.** Opsiyon araştırmasının en eski seansı **2026-05-13**.

**Neden.** Ölçüldü: 2026-05-13 → 200 ve doğru tarihli 13.604 satır;
2026-05-12 → **403**. Kontrat geçmişi de aynı çapaya dayanıyor (92 günlük
serinin ilk günü 2026-05-12).

**Not.** Bu, hisse tarafındaki tabandan (2026-05-12, spot-exposures) bir seans
yukarıda. İki uç nokta aynı çapayı bir gün farkla gösteriyor.

**Geri alma.** Yok — ölçüm. Pencere büyürse yeniden ölçülür.
