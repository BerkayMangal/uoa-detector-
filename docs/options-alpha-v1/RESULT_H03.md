# H03 sonucu — REDDEDİLDİ

> **Düzeltme 2026-09-23 (`AUDIT_H10_TRADES.md`).** Bu belgedeki sayılar v1 motorundan. Motor, ufkun son günü fiyatlanamadığında önceki bir günü `time` çıkışı olarak yazıyordu (hasat DTE<10 satırları atıyor). Düzeltilmiş motorla sürümlü yeniden koşum: REJECTED kalıyor (v2: teyitli 96, kontrol 4 tamamlanmış yapı). v1 artefaktı korunuyor; v2 ayrı dosyada.

**Koşum tarihi:** 2026-09-23 · **Protokol:** `PREREG_H03.md` + ek 1, 2, 3
(hepsi koşumdan önce commit'lendi) · **Kalite seviyesi:** B
**Artifact:** `artifacts/options-alpha-v1/h03_result.json`

```
uv run python scripts/options_alpha_h03_study.py
```

---

## 1. Hüküm

**REDDEDİLDİ.** Teyitli kolun maliyet sonrası ortalama net opsiyon P&L'i kontrol
kolunu **geçmedi** — daha kötü çıktı. Donmuş hüküm merdiveni (sözleşme §9) bu
durumda REJECTED der.

---

## 2. Sayılar

**Pencere:** 2026-05-14 … 2026-09-15, **85 seans**, 10 isim. Ön-kayıtta yazdığı
gibi; genişletilmedi.

### Eleme zinciri

| Aşama | Adet |
|---|---|
| Taranan kontrat-satırı | **1.544.478** |
| Uygunluk kapılarını geçen | 58.787 |
| Yapı kurulan | 554 |
| Fiyatlanan | 1.108 *(554 aday × long + spread)* |
| **Risk kapısını geçen** | **141** |

En çok eleyen: açık pozisyon yetersiz (587.159), DTE çok kısa (285.567),
bid çok düşük (201.320), makas çok geniş (136.465).

Aday çıkmayan isim-seans: **84**. Yedek seçim yok, o yüzden bunlar hiçbir şey
üretmedi.

### Kollar — birincil ölçüt (`time_only`, 5 işlem günü, maliyet sonrası)

| Kol | n | Ortalama | Medyan | Kazanma oranı |
|---|---|---|---|---|
| **Teyitli** (OI arttı) | 134 | **−75,31 $** | −46,90 $ | %7,5 |
| **Kontrol** (OI artmadı) | 7 | **−52,57 $** | −49,60 $ | %0 |

Tek karşılaştırılabilir kova (DTE 14–30 / delta 0,25–0,40):
kontrol n=7 ort −52,57 $ · teyitli n=74 ort −82,81 $.

**Yedi kovanın altısı tek kollu olduğu için düştü.**

---

## 3. Ek 3'ün koşumdan önce söylediği aynen gerçekleşti

Koşumdan **önce** yazılıp commit'lenen ek 3, seçim filtresinin (`volume(D) >
open_interest(D)`) kolları ayıran değişkenle (`open_interest(D+1) >
open_interest(D)`) mekanik olarak korele olduğunu ve kontrol kolunun inşa gereği
küçük kalacağını söylemişti.

Gerçekleşen: **134'e 7**. Yani:

> Kontrol kolu zayıf çıkarsa bu, hipotezin yanlışlandığı değil, **bu tasarımın
> onu ayrıştıramadığı** anlamına gelir.

Bu cümle sonuçtan önce yazıldı ve şimdi geçerli. Hükmün dürüst tam hâli:
**birincil ölçütte REDDEDİLDİ, ve tasarım teyit sinyalinin kendine özgü
bilgisini ayrıştıramadı.**

---

## 4. Reddedilen cazibe: ikincil varyantlar daha iyi görünüyor

| Varyant | Teyitli | Kontrol |
|---|---|---|
| `time_only` **(birincil)** | −75,31 $ | −52,57 $ |
| `time_and_stop` | **−34,23 $** | −46,71 $ |
| `time_target_stop` | **−34,25 $** | −46,71 $ |

İkincil varyantlarda teyitli kol kontrolden **daha iyi** çıkıyor. Birincil
ölçüt veriye bakılmadan `time_only` olarak sabitlenmişti; şimdi kazanan varyanta
geçmek, sözleşme §10'un açıkça yasakladığı şeydir.

**Geçilmedi.** Bu satırlar bulgu olarak değil, **ilan edilmiş üç denemenin
ikincil teşhisi** olarak duruyor. Biri bunu takip etmek isterse yeni bir
ön-kayıt, yeni bir deneme bütçesi ve seçim maliyetinin istatistiksel tasarıma
dahil edilmesi gerekir.

---

## 5. Bu sonucun söylemedikleri

- **"Opsiyon alıp satmak zarar ettirir" demiyor.** Ölçülen şey tek bir tetik,
  tek bir tutma ufku, tek bir evren ve 85 seans.
- **"T+1 OI teyidi işe yaramaz" demiyor.** Bu tasarım onu ayrıştıramadı (§3).
- **Gün içi hiçbir iddia taşımıyor.** B kalite veri: zincirin zaman alanı
  `last_tape_time`, yani son *işlem* zamanı. Fill sırası, stop tetiklenme sırası
  veya "şu fiyattan dolardı" iddiası yok.
- **`FORWARD_PASS` bu veriden çıkamaz** — sözleşme §10'da böyle yazıyor.
- Akış popülasyonu UW'nin kendi tanımı değil, benim `volume > OI` işletmem; ama
  evren ve likidite kapıları yine seçim yanlılığı taşıyor.

---

## 6. Yapısal gerçek: 100 $ R bağlayıcı kısıt

554 yapının yalnız **141'i** risk kapısını geçti. Geçemeyenlerin sebebi tek:
tek bir yapı 100 $'lık bütçeyi aşıyor (örnek: 221,60 $).

Bu, veri ya da hipotez sorunu değil — sermaye ölçeği. Aynı protokol daha büyük
bir R ile çok daha fazla yapı üretirdi. **Bütçe bu çalışma için büyütülmedi**
ve büyütülmesi ayrı bir karardır.

---

## 7. Tekrar üretme

```bash
uv run python scripts/options_alpha_harvest_chains.py --start 2026-05-14 --end 2026-09-15
uv run python scripts/options_alpha_h03_study.py
```

Hasat gitignore'da (384 MB); komut onu yeniden üretir. Sonuç artifact'ı
profil hash'ini, kalite seviyesini ve 141 kaydın tamamını taşır.
