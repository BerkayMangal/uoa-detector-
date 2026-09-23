# options-alpha-v1 — durum

**Faz 5.24** · dal `p70-options-alpha-h03` · taban `main` 90b86e8
**Son güncelleme:** 2026-09-23 12:40Z

Ana ürün **opsiyon sinyal terminali**. Hisse tarafı yalnız karşılaştırma kolu.

---

## Kilometre taşları

| # | İş | Durum | Kanıt |
|---|---|---|---|
| **M0** | API yetenek matrisi | **DONE** (merge, canlı) | `capability_matrix.json` · 9/9 VERIFIED |
| **M1** | Uçtan uca: veri → aday → kontrat/spread → maliyet/risk → PAPER kart | **DONE** (merge, canlı) | `replay/2026-09-22/paper_card_{SPY,QQQ}.json` |
| **M2** | Dört temel yapı | **KISMEN** | `structures.py` dördünü de fiyatlıyor ve test ediyor; seçici long + dikey debit üretiyor |
| **M3** | Çıkış motoru + gerçek sonuç kaydı | **DONE** (merge, canlı) | `exits.py` · 9 elle-hesap test · `replay/2026-09-08/*_outcome.json` |
| **M4** | 12 hipotez ailesine veri fizibilitesi | **DONE** | `HYPOTHESES.md` — 8 tam, 3 kısıtlı, 0 erişilemez |
| **M5** | İlk aileyi koş ve hükme bağla | **DONE — REDDEDİLDİ** | `RESULT_H03.md` · `h03_result.json` |
| **M6** | Track B replikasyonu | **ENGEL ÖLÇÜLDÜ** | Pencere ~95 seans, şartı 4 çeyrek → kısmi test / yetersiz süre |
| **M7** | Ayrı opsiyon ekranı + tarayıcı doğrulaması | TODO | B1 yalnız *gözle doğrulamayı* tutar |
| **M8** | Mutasyon kanıtı tablosu | TODO | — |

---

## M5 — H03 koştu ve reddedildi

**Hipotez:** akışın kontratında ertesi yayımda açık pozisyon **artmışsa** (pozisyon
kapanmayıp açılmışsa), sonraki net opsiyon P&L'i teyitsiz eşleştirilmiş
kontratlardan iyi olur.

**Pencere:** 2026-05-14 … 2026-09-15, **85 seans**, 10 isim — ön-kayıttaki gibi.

### Eleme zinciri

1.544.478 kontrat-satırı tarandı → **58.787** uygun → **554** yapı kuruldu →
1.108 fiyatlandı (her aday long + spread) → **141** risk kapısını geçti.

### Kollar (birincil: `time_only`, 5 işlem günü, maliyet sonrası)

| Kol | n | Ortalama | Medyan | Kazanma |
|---|---|---|---|---|
| Teyitli (OI arttı) | **134** | **−75,31 $** | −46,90 $ | %7,5 |
| Kontrol (OI artmadı) | **7** | **−52,57 $** | −49,60 $ | %0 |

**Teyitli kol kontrolden kötü → REDDEDİLDİ.**

### Ek 3 koşumdan önce bunu söylemişti

Seçim filtresi (`volume(D) > OI(D)`) ile kolları ayıran değişken
(`OI(D+1) > OI(D)`) mekanik olarak korele. Açık pozisyon gün başı duruşu olduğu
için, hacmi onu aşan bir kontratta hacmin çoğu zaten pozisyon açıyor demektir.

Gerçekleşen: **134'e 7**, ve yedi kovanın altısı tek kollu kaldığı için düştü.

Dolayısıyla dürüst tam hüküm: **birincil ölçütte reddedildi, ve bu tasarım teyit
sinyalinin kendine özgü bilgisini ayrıştıramadı.** İkisi farklı cümle.

### Reddedilen cazibe

| Varyant | Teyitli | Kontrol |
|---|---|---|
| `time_only` **(birincil)** | −75,31 $ | −52,57 $ |
| `time_and_stop` | −34,23 $ | −46,71 $ |
| `time_target_stop` | −34,25 $ | −46,71 $ |

İkincil varyantlarda teyitli kol **daha iyi** görünüyor. Birincil ölçüt veriye
bakılmadan sabitlenmişti; kazanana geçmedim. Takip edilecekse yeni ön-kayıt ve
seçim maliyetinin tasarıma dahil edilmesi gerekir.

### Yapısal gerçek

554 yapının yalnız 141'i risk kapısını geçti; sebep tek — bir yapı 100 $ bütçeyi
aşıyor (örn. 221,60 $). Bu veri ya da hipotez sorunu değil, **sermaye ölçeği**.
Bütçe bu çalışma için büyütülmedi.

---

## Protokol disiplininin kaydı

H03 dört belgeyle donduruldu ve **dördü de koşumdan önce commit'lendi**:

| Belge | Ne dondurdu |
|---|---|
| `PREREG_H03.md` | Hipotez, bilgi zamanı, evren, pencere, birincil ölçüt, 3 deneme, hüküm merdiveni |
| ek 1 | "Olağandışı akış" = `volume(D) > OI(D)`; seans-isim başına tek aday; **yedek seçim yok** |
| ek 2 | Ek 1 kontrol kolunu yanlış tarif etmişti — sözleşme kazandı, kollar OI teyidine göre ayrılır |
| ek 3 | Seçim filtresi ile kol değişkeninin mekanik korelasyonu; protokol **değiştirilmedi** |

Ek 2 ve ek 3, uygulama sırasında kendi belgelerimde bulduğum kusurları
sonuçlardan **önce** kayda geçiriyor. Çelişkiyi görüp sessizce uygun olanı seçmek
bu projenin defalarca yakaladığı hata.

---

## M0–M3 özet

**Yetenek:** tarihli opsiyon zinciri gerçekten tarihsel (iki tarihte ortak 11.218
kontratın 10.927'sinin değeri farklı). Kontrat geçmişi günlük NBBO serisi veriyor.
Araştırma penceresi **2026-05-13**'te başlıyor. Kalite **B** — zaman alanı
`last_tape_time`, yani son işlem zamanı.

**İlk kartlar:** SPY 775/776 call debit (azami zarar 60,60 $), QQQ 760/761
(50,60 $). Dört isim risk kapısında doğru şekilde reddedildi.

**İlk gerçek sonuçlar:** 8 Eylül girişi, beş seans sonra SPY **−30,60 $**,
QQQ **−47,20 $**.

---

## Kaynak bütçesi

| Kaynak | Değer |
|---|---|
| UW günlük limit | 30.000 |
| Bugün harcanan | ~1.060 (hasat 845 + probe/kart ~215) |
| Hasat | 850 dosya, 85 seans, 384 MB — **gitignore'da**, komutla yeniden üretilir |
| Güvenli tavan | 5.000/gün |

---

## Henüz yapılmamış olanlar (açıkça)

- **Mutasyon kanıtı tablosu yok.** Kritik korumaların doğru sebepten kırıldığı
  gösterilmedi (§17).
- Opsiyon ekranı yok; açık PAPER pozisyonları canlı zamanlayıcıda izlenmiyor.
- Kalan 11 hipotez ailesi koşmadı.

Hiçbiri dış engel değil; yazılmamış kod. Engeller `BLOCKERS.md`'de.
