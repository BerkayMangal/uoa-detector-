# options-alpha-v1 — 12 hipotez ailesinin veri fizibilitesi

**Tarih:** 2026-09-23 · **Dayanak:** `artifacts/options-alpha-v1/capability_matrix.json`
(9/9 VERIFIED, gerçek probe'larla) · **Kalite seviyesi:** B

Bu dosya **fizibilite** belirler, sonuç değil. Hiçbir aile burada koşulmadı ve
hiçbirinin edge'i olduğu iddia edilmiyor. Sıralama ekonomik gerekçe ve uygulama
maliyetine göredir — sonuçlara bakılarak değil.

---

## Ölçülmüş veri zemini

| Yetenek | Durum | Sınır |
|---|---|---|
| `option-chains?date=&greeks=true` | VERIFIED | Taban **2026-05-13**, ~95 seans. Kontrat başına NBBO, IV, delta/gamma/theta/vega/rho, OI, hacim |
| `option-contract/{id}/historic` | VERIFIED | Günlük NBBO + IV + OI + taraf bazlı hacim serisi (örnekte 92 gün) |
| `screener/option-contracts` | VERIFIED | Nokta-zaman, ~90 filtre (dte, delta, premium, vol/OI, `is_new`, `days_of_vol_greater_than_oi`) |
| `option-trades/flow-alerts` | VERIFIED | `newer_than`/`older_than` ile seans penceresi |
| `option-trades/multi-leg` | VERIFIED | **24 saatlik sorgu penceresi** — tarihsel hasat gün gün |
| `option-contract/{id}/intraday` | VERIFIED | İşlem OHLC + taraf bazlı hacim. **Kotasyon serisi değil** (C seviyesi) |
| `volume-profile` | VERIFIED | Fiyat bazında taraf hacmi, tarihli |
| `expiry-breakdown?date=` | VERIFIED | Vade başına OI/hacim/zincir sayısı |
| `spot-exposures`, `greek-exposure/strike` | VERIFIED | Tarihli, taban 2026-05-12 |
| `stock/{t}/ohlc/1d` | VERIFIED | 252 seans |

**Nokta-zaman tehlikeleri (ölçülmüş, depoda kayıtlı):**
- Sektör üyeliği, kazanç tahminleri ve ekonomik takvim **bugünkü bilgiyi** yansıtır;
  replay-güvenli değil (`phase-3.9-closeout` §7/7).
- Günlük OI **gün başı** semantiğinde: D satırı D-1'de oluşan OI'yi taşır.
- IV-rank'in 2026-05-04 öncesi geçmişi yok.
- Zincir NBBO'sunun zaman alanı `last_tape_time` = **son işlem** zamanı.

---

## Fizibilite tablosu

| # | Aile | Gerekli veri | Erişim | Zaman güvenliği | Hüküm |
|---|---|---|---|---|---|
| **H01** | Göreli agresif akış | flow-alerts + kontrat likiditesi + aynı-saat geçmiş dağılım | VAR | Akış olay zamanlı; dağılım yalnız **geçmişten** kurulmalı | **DESTEKLENİR** |
| **H02** | Tekrarlayan akış | flow-alerts (çoklu gün) + kontrat kimliği | VAR | Olay zamanlı | **DESTEKLENİR** — ama bölünmüş sweep'leri bağımsız sayma riski tasarımda çözülmeli |
| **H03** | T+1 OI uyumu | flow-alerts + `/historic` OI serisi | VAR | **Giriş ancak T+1 yayımlandıktan sonra**; OI gün başı semantiği zorunlu | **DESTEKLENİR** |
| **H04** | Akış/fiyat gecikmesi | flow-alerts + `ohlc/1d` (+ `intraday` bağlam) | VAR | Gün içi sıra iddiası yok (B seviyesi) | **DESTEKLENİR** |
| **H05** | Sektör-göreli akış | flow-alerts + sektör üyeliği | **KISMİ** | Sektör üyeliği **replay-güvenli değil** | **KISITLI** — üyelik bugünden alınırsa survivorship; ileriye dönük yakalama gerekir |
| **H06** | Vade/strike yoğunlaşması ve göçü | `expiry-breakdown?date=` + zincir OI | VAR | Tarihli, nokta-zaman | **DESTEKLENİR** |
| **H07** | Çok bacaklı işlem bağlamı | `multi-leg` + `/legs` | VAR | **24 saatlik pencere** → gün gün hasat | **DESTEKLENİR** (hasat maliyeti yüksek) |
| **H08** | Olay sonrası devam | gerçek açıklanma zamanı + akış + fiyat | **KISMİ** | Kazanç takvimi revize ediliyor, replay-güvenli değil | **KISITLI** — yalnız açıklanma zamanı bağımsız doğrulanabilen olaylar |
| **H09** | Olay öncesi prim davranışı | IV/vade yapısı + takvim | **KISMİ** | Aynı takvim sorunu + IV-rank geçmişi 2026-05-04'ten | **KISITLI** |
| **H10** | Uzun prim / konveksite | zincir NBBO + IV + gerçekleşen vol | VAR | B seviyesi maliyet varsayımıyla | **DESTEKLENİR** — v1'in dört yapısı zaten bunu fiyatlıyor |
| **H11** | Skew / vade yapısı | zincir (çoklu vade, delta bazında) | VAR | Tarihli | **DESTEKLENİR** — ham IV'leri vadeler arası karşılaştırmama şartıyla |
| **H12** | Rejime bağlı akış (ablation) | H01–H04'ten biri + `spot-exposures` | VAR | Tarihli | **DESTEKLENİR** — ama daha önce **reddedilmiş** gamma-yön kuralını diriltmemek şartıyla |

**Özet:** 12 aileden **8'i tam desteklenir**, 3'ü nokta-zaman kirlenmesi yüzünden
**kısıtlı**, 0'ı erişilemez. §9'un "en az altı aileyi gerçek koşucuya bağla"
hedefi veri açısından karşılanabilir.

---

## Track B replikasyonu (ayrı tutulur)

Özgün profil ve kabul şartları **değiştirilmeden** koşulur. Ölçülmüş engel:
pencere **~95 seans**, Track B'nin kendi şartı **4 çeyrekte 3'ü pozitif** ve
yılda ≥30 işlem. 95 seans ≈ 4,5 ay → dört çeyreği kapsamıyor.

**Hüküm: KISMİ TEST / YETERSİZ SÜRE.** Yeni hipotez başarıları Track B başarısı
diye yazılamaz.

---

## Sıralama (ekonomik gerekçe + uygulama maliyeti, sonuçlara bakmadan)

1. **H03** — T+1 OI uyumu. Teyit erişilebilir olduktan sonra giriş kuralı en net
   tanımlanabilen aile; veri tek uç noktadan geliyor.
2. **H01** — göreli agresif akış. Mutlak prim yerine kendi geçmişine göre
   normalleştirme, en savunulabilir ekonomik gerekçe.
3. **H04** — akış/fiyat gecikmesi. Beklenen ilişki yönü **sonucu görmeden**
   sabitlenmeli.
4. **H06** — vade/strike yoğunlaşması. Tek uç nokta, düşük hasat maliyeti.
5. **H10** — uzun prim/konveksite. Yapı motoru zaten mevcut.
6. **H02** — tekrarlayan akış. Bölünmüş sweep birleştirmesi tasarım işi gerektirir.

H05/H08/H09 kısıtlı; H07 pahalı; H11/H12 ilk dalgaya girmez.

---

## Bu dosyanın taahhüt ETMEDİĞİ

- Hiçbir ailenin edge'i olduğu.
- Fizibilitenin uygulama demek olduğu — desteklenen aile için özellik üretimi,
  deterministik kural, profil, test, gerçek koşu ve sonuç kaydı gerekir.
- Sıralamanın sonuçlara göre değişebileceği. Değişirse bu bir **seçim maliyetidir**
  ve istatistiksel tasarıma dahil edilir.
