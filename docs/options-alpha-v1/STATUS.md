# options-alpha-v1 — durum

**Faz 5.24** · dal `p68-options-alpha-v1-capability` · taban `main` 5b7917c
**Son güncelleme:** 2026-09-23 09:30Z

Ana ürün **opsiyon sinyal terminali**. Hisse tarafı yalnız karşılaştırma kolu.

---

## Kilometre taşları

| # | İş | Durum | Kanıt |
|---|---|---|---|
| **M0** | API yetenek matrisi — ölçülmüş, uydurulmamış | **DONE** | `artifacts/options-alpha-v1/capability_matrix.json` · 9/9 VERIFIED · 19 istek |
| **M1** | Uçtan uca: gerçek veri → aday → kontrat seçimi → maliyet/risk → PAPER kart → izleme → sonuç | **TODO** | — |
| **M2** | Dört temel yapı (long call/put, bull call debit, bear put debit) | TODO | — |
| **M3** | Çıkış motoru: `take_profit_or_stop` gerçek kodla + izleme | TODO | — |
| **M4** | 12 hipotez ailesine veri fizibilitesi | TODO | — |
| **M5** | Desteklenen ailelerin koşan uygulaması ve gerçek sonuçları | TODO | — |
| **M6** | Track B replikasyonu (değişmeden) veya kesin örneklem engeli | TODO | — |
| **M7** | Ayrı opsiyon ekranı + tarayıcı doğrulaması | TODO | B1 engeli yalnız *gözle doğrulamayı* tutar, kodu değil |

---

## M0 — ne ölçüldü

Dokuz satırın hepsi gerçek isteklerle **VERIFIED**. Tasarımı belirleyen dört bulgu:

**1. Tarihli opsiyon zinciri gerçekten tarihsel.** `option-chains?date=&greeks=true`
kontrat başına `nbbo_bid`/`nbbo_ask`, IV, delta/gamma/theta/vega/rho, OI, hacim
veriyor. Kanıt satır sayısı değil — iki tarihte de bulunan 11.218 kontratın
**10.927'sinin değerleri farklı**. Satır sayısına bakmak yeterli olmazdı; vade
geçişleri zaten sayıyı değiştirir.

**2. Kontrat geçmişi var ve çıkış fiyatlamasının omurgası.**
`/api/option-contract/{id}/historic` günlük `nbbo_bid`/`nbbo_ask` serisi veriyor —
SPY261016C00785000 için **92 gün** (2026-05-12 .. 2026-09-22), 30 alan.

**3. Aday üreteci hazır.** `screener/option-contracts` nokta-zaman çalışıyor
(`date=` farklı tarihlerde farklı sembol listesi döndürüyor), ~48 alan ve ~90
filtre parametresi taşıyor.

**4. Çok bacaklı işlemler gerçek.** `/api/option-trades/multi-leg` tanınmış
paketleri `strategy`, `net_bid`/`net_ask`, `breakevens`, `max_loss`/`max_profit`
ve net greeks ile döndürüyor. **24 saatlik sorgu penceresi** sınırı var.

**Araştırma penceresi: 2026-05-13 → bugün.** 05-13 doğru tarihli 13.604 satır
veriyor, 05-12 → 403.

**Kalite seviyesi B, A değil.** Zaman alanı `last_tape_time` yani **son işlem**
zamanı; kotasyonun hangi an geçerli olduğu bilinmiyor. Gün içi fill sırası veya
stop tetiklenme sırası iddiası bu veriyle yapılamaz (`DECISIONS.md` D4).

---

## Kaynak bütçesi (ölçüldü, varsayılmadı)

| Kaynak | Değer | Kaynak |
|---|---|---|
| UW günlük limit | **30.000** | `x-uw-token-req-limit` başlığı, 2026-09-23 |
| Bugün harcanan | **193** | `x-uw-daily-req-count`, matris koşusundan sonra |
| M0'ın maliyeti | 19 istek | probe çıktısı |
| Tahtanın günlük payı | ~4.400 türetilmiş | devir raporu §3.2 — **ölçülmemiş**, üst sınır varsayımı |
| Bu iş için güvenli tavan | **5.000/gün** | üretim sorgularına öncelik; araştırmaya kalan |
| Paralellik | en fazla 3 alt ajan | master prompt §3 |
| Disk | artifact'lar <100 KB | ölçüldü |

Bütçe bol. Kısıt kota değil, **veri kalitesi** (B seviyesi).

---

## Sırada ne var

**M1**, çünkü görev onu birinci sıraya koydu ve altyapıya gömülüp kullanılabilir
akış üretmemek açıkça yasak.

Piyasa kapalıyken (şu an ET 05:30) uçtan uca akış **tamamlanmış bir seans**
üzerinden, tarihli uç noktalarla koşulacak. Bu canlı fırsat değil, hattın
çalıştığının kanıtı — ve kartta öyle etiketlenecek.

---

## Henüz yapılmamış olanlar (açıkça)

- Opsiyon yapılandırma motoru yok — spread mantığı depoda **hiç** yok
- Açık pozisyon izleme yok
- `take_profit_or_stop` hâlâ `NotImplementedError`
- Opsiyon ekranı yok
- Hiçbir hipotez koşmadı

Bunların hiçbiri dış engel değil; yazılmamış kod. Engeller `BLOCKERS.md`'de.
