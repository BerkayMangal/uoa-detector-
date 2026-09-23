# S01 sonucu — MALIYET_BAGLI

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-23**
**Protokol:** `PREREG_S01.md` (PR #81), bu koşucu dosyası var olmadan önce donduruldu.
**Artefakt:** `artifacts/options-alpha-v1/s01_result.json`
**Veri kalitesi:** B · **0 UW isteği** · **Aile değil, deneme harcamadı**

---

## 1. Hüküm

**MALIYET_BAGLI.** §7'nin iki koşulu da sağlandı:

| Koşul | Eşik | Gerçekleşen |
|---|---|---|
| Maliyetin ortalama net kayba oranı | ≥ %75 | **%104** |
| Mid→mid piyasa ortalamasının %95 aralığı sıfırı içeriyor | evet | **+3,08 $ · [−2,39 , +8,23]** |

Ön-kayıtta yazılmış cümle:

> Bu kurguda (1 strike dikey, B-kalite giriş/çıkış, R=100 $) maliyet, sinyalin
> taşıyabileceği her şeyi yiyor. Yeni aile koşmak anlamsız; bir sonraki adım
> **yapı/maliyet revizyonu**dur ve Berkay'ın kararıdır, ayrı bir ön-kayıtla.

**Rastgele taban:** aile sinyalleri − rastgele giriş = **−1,59 $**, %95
**[−25,92 , +22,51]** → "Aile sinyalleri rastgele girişten ayırt edilemiyor."

## 2. Mutabakat kapısı

| Küme | İşlem | Uyuşmayan | Kapı |
|---|---|---|---|
| Sinyal (tekil) | 285 | **0** | geçti |
| Rastgele | 75 | **0** | geçti |

Her işlemin beş bileşeni motorun `time_only` P&L'ine kuruşu kuruşuna toplanıyor.
Yani bacaklar, giriş borcu, çıkış değeri ve komisyon bağımsız olarak yeniden
kuruldu ve motorla aynı çıktı.

## 3. Ayrıştırma (işlem başına ortalama, $)

| Bileşen | Sinyal (n=285) | Rastgele (n=75) |
|---|---|---|
| **Piyasa (mid → mid)** | **+3,08** | **+0,76** |
| Giriş makası | −17,16 | −15,34 |
| Giriş gecikme payı (%2) | −1,59 | −1,45 |
| **Çıkış makası** | **−51,51** | **−50,14** |
| Komisyon | −3,98 | −3,40 |
| **Net** | **−71,16** | **−69,57** |
| Maliyet toplamı | −74,24 | −70,33 |
| Maliyet / net kayıp | %104 | %101 |
| Gidiş-dönüş maliyet / giriş paket mid'i | **%134** | %130 |
| Kazanma oranı — net | %6,7 | %6,7 |
| Kazanma oranı — **mid** | **%45,3** | **%49,3** |

Okuma:

- **Piyasa tarafı nötr.** Mid'de işlemlerin yarıya yakını kazanıyor; ortalama
  hareket sıfırdan ayırt edilemiyor. Sinyal de rastgele de aynı.
- **Kaybın tamamı maliyet.** Ortalama paket mid'i ~55 $; gidiş-dönüş maliyet
  ~74 $. Pozisyon açıldığı anda, kapanışta ödenecek makasla birlikte, değerinin
  üstünde bir engelle başlıyor. %45'lik mid kazanma oranı maliyetten sonra %7'ye
  iniyor.
- **Kaybın ~%70'i çıkışta.** Çıkış makası giriş makasının **3 katı**.

Tekil küme 488 kayıttan 285 işlem; 92 işlem birden fazla ailede görünüyor (aile
başına: H01 158, H02 124, H03 100, H04 54, H10 52). Net ortalama (−71 $) aile
kollarının aralığı (−37…−84 $) içinde.

## 4. Kırılımlar (sinyal, tekil)

| Grup | n | Net | Piyasa | Maliyet | Mid kazanma | Maliyet / giriş mid'i |
|---|---|---|---|---|---|---|
| bear_put_debit | 152 | −81,07 | +0,44 | −81,51 | %46,1 | — |
| bull_call_debit | 133 | −59,83 | +6,09 | −65,92 | %44,4 | — |
| QQQ+SPY | 232 | −81,41 | −2,23 | −79,18 | %41,4 | %148 |
| diğer isimler | 53 | −26,28 | +26,32 | −52,60 | %62,3 | %80 |
| Giriş makası Ç1 (mid'in %5–16'sı) | 71 | −74,78 | +6,83 | −81,61 | %52,1 | %127 |
| Giriş makası Ç2 (%16–27) | 71 | −51,94 | +0,30 | −52,23 | %47,9 | %83 |
| Giriş makası Ç3 (%27–42) | 71 | −76,11 | −1,94 | −74,18 | %36,6 | %138 |
| Giriş makası Ç4 (%43 ve üstü) | 72 | −81,65 | +7,07 | −88,72 | %44,4 | %213 |

Okuma:

- **Her grupta maliyet kaybın tamamını ya da fazlasını açıklıyor.** Hiçbir
  kırılımda piyasa bileşeni maliyeti karşılayacak büyüklükte değil.
- **Giriş makası dar olan çeyrek de kurtulmuyor:** Ç1'de gidiş-dönüş maliyet
  giriş mid'inin %127'si, çünkü kaybın çoğu çıkışta (§5). Girişte dar makas
  seçmek sorunu çözmüyor.
- **QQQ+SPY en kötüsü** (maliyet mid'in %148'i). Pahalı dayanakta 1 strike
  genişlik paket değerini bacak fiyatlarına göre en çok küçülten durum.
  Diğer isimlerde piyasa bileşeni +26 $ görünüyor ama n=53 ve bu kırılım
  ön-kayıtta hüküm taşımıyor; bir bulgu olarak **sunulmuyor**.
- Ç4'ün üst sınırı mid'in %4150'si: paket mid'i sıfıra yakın birkaç işlem. Uç
  değer, ortalamayı değil oranı şişiriyor.

## 5. Teşhis — ön-kayıt DIŞI, hükme girmez

Aşağıdaki ölçüm sonuç görüldükten sonra, çıkış makasının neden bu kadar büyük
olduğunu açıklamak için yapıldı. Etiketi değiştirmez; bir revizyon önerisinin
**gerekçesi**dir, kanıtı değil.

Sinyal işlemlerinin bacak makası (bacağın kendi mid'ine oranla, %):

| Bacak | Giriş medyan | Çıkış medyan | Giriş p75 | Çıkış p75 |
|---|---|---|---|---|
| Uzun | 1,2 | 1,8 | 1,9 | 3,7 |
| Kısa | 1,3 | 2,2 | 2,2 | 4,5 |

İki mekanizma:

1. **Kaldıraç.** Bacak makası bacak fiyatının yalnız %1–2'si. Ama 1 strike
   dikeyin paket değeri bacak fiyatlarının kabaca onda biri; iki bacağın makası
   paket değerine göre ~10 kat büyüyor. Kurgu, makası **büyüten** bir yapı seçiyor.
2. **Seçim.** Uygunluk kapıları girişte dar makası seçiyor (ankraj ≤ %15, aynı
   seans); çıkışta kapı yok. Çıkış makasları medyanda ~1,5–1,7 kat, kuyrukta
   ~2 kat daha geniş. Girişte ödenen makas, kurgunun gerçek maliyetini
   **olduğundan az** gösteriyor.

## 6. Bu sonucun söylemedikleri

- **Sinyallerin değersiz olduğunu** söylemiyor. Söylediği: bu kurgu, sinyal
  içerse bile onu ölçemez; piyasa bileşeni hem sinyalde hem rastgelede sıfır
  civarında ve maliyet onu 20 kat aşıyor.
- Mid bir **muhasebe ekseni**, uygulanabilir fiyat değil (B kalite).
- Başka yapı **denenmedi.** Revizyon ayrı ön-kayıt ister.
- `no_exit_data` kayıtları dışarıda (`AUDIT_H10_TRADES.md` §6).
- Rastgele taban 590 çekilişten 75 fiyatlanmış işlem; aralığı geniş.

## 7. Sıradaki karar — Berkay'ın

Ön-kayıt §7 gereği öneri tek cümle: **yeni aile değil, yapı/maliyet revizyonu.**
Revizyonun kendisi bir strateji kararı; seçenekler, hiçbiri sınanmadan:

| Seçenek | Neyi hedefler | Bedeli |
|---|---|---|
| **Genişliği dolar/delta ile tanımlamak** (1 strike yerine) | Paket değerini büyütüp bacak makasının payını küçültmek | Maks. kayıp büyür → R=100 $'da adet 0'a düşebilir |
| **Çıplak long** (`min_debit_reduction_pct` kuralını kaldırmak) | Tek bacak = tek makas | Theta ve vega maruziyeti; R'ye sığmama |
| **R'yi büyütmek** | Adet kısıtını gevşetmek | Sermaye kararı |
| **Çıkışta makas kapısı** (çıkış makası eşiği aşarsa bir gün bekle) | Seçim asimetrisini kapatmak | Tutma süresi değişir, yeni deneme |

Her biri ayrı ön-kayıt, ayrı güç analizi ve 15'lik bütçeye eklenen yeni deneme
demektir.

## 8. Tekrar üretme

```bash
uv run python scripts/options_alpha_s01_study.py   # kota harcamaz, ~16 sn
```

Bootstrap ve rastgele taban tohumlu (20260923); birebir yeniden üretilir.
