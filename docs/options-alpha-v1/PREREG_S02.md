# Ön-kayıt — S02: vol primi, ölçülmüş maliyetle, para olarak

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-24**, tek bir kredi, borç
veya P&L hesaplanmadan. Koşucu yazılmadan sabitlenir ve sonuçlara göre
değiştirilmez.

---

## 1. Neden bu çalışma — ve neden şimdi en yüksek beklentili yol bu

Projenin bugüne kadarki tek istatistiksel olarak sağlam bulgusu **vol primi**:
örtük oynaklık gerçekleşenden sistematik olarak yüksek (taze pencerede
koşulsuz, vol puanı cinsinden Sharpe +1,44). Bu bulgu **bilgi** olarak sağlam,
ama `docs/edge_to_money.md` onu **para** olarak öldürdü. Ölüm sebebi
büyük ölçüde maliyet varsayımıydı:

> Opsiyon alış/satış makası %5 / %10 / %20 modellendi — "%10 temsilî, %5 cömert".

Bu bir **varsayımdı**, ölçüm değil. S01'in teşhisi (`RESULT_S01.md` §5) aynı
evrenin **gerçek** NBBO makaslarını ölçtü: bacak başına medyan **%1,2–2,2**
(bacağın kendi mid'ine oranla). %5'lik "cömert" varsayımda bile kısa straddle
t=2,14 çıkmıştı.

S01 ayrıca kayıpların neden ailelerde bu kadar büyük olduğunu gösterdi: 1 strike
dikey, küçük bacak makasını paket değerine göre ~10 kat büyütüyordu. Kısa ATM
iron butterfly bunun **tersi**: satılan ATM bacaklar pahalı, alınan kanatlar
ucuz; paket kredisi bacak makaslarına göre **büyük**. Yani aynı makas, bu yapıda
paketin küçük bir payıdır.

İki ayrı bulgu (sağlam vol primi + ölçülmüş düşük makas) aynı yeri gösteriyor.
Bu çalışma o kesişimi, **hiç kullanılmamış** bir pencerede sınar.

**Pencere neden meşru:** `docs/INDEX.md` §6 ve `preregister_D.md` §1,
**2026-05-01 sonrasını** Study D'nin "en saf canlı-OOS" alternatif penceresi
olarak ayırdı. Hasat (2026-05-14 … 2026-09-15) tam olarak o pencere. Options
Alpha aileleri bu hasadı yalnız **akış** hipotezleri için sorguladı; vol primi
bu pencerede **hiç** ölçülmedi.

## 2. Hipotez

Her seçilen giriş gününde, her isimde **koşulsuz** olarak kısa ATM iron butterfly
satmak (tanım §4), 5 işlem günü sonra kapatıldığında, **ölçülmüş** NBBO
maliyetleri ve komisyon düşüldükten sonra ortalama **pozitif** risk-başı getiri
üretir.

**Yön şimdi sabitlenir:** satıcı kazanır. Ortalama negatifse bu hipotezin
**reddidir**.

Koşul yok (gamma rejimi, IV-rank vb.). Gerekçe: bilgi olarak sağlam olan
**koşulsuz** vol primi; koşullama Study D'de ZAYIF/SINIRDA kaldı. Koşullu varyant
yalnız **ikincil** raporlanır (§7), hükme girmez.

## 3. Evren, pencere, veri

- **Evren:** profildeki on isim (SPY, QQQ, AAPL, NVDA, MSFT, AMZN, META, TSLA,
  AMD, GOOGL). Study D'nin 24 isimlik evreninden **farklı**; hasat bu on ismi
  kapsıyor. Bu farklılık açıkça kayıtlıdır: bu, D'nin replikasyonu değil,
  aynı ekonomik iddianın likit evrende para olarak sınanmasıdır.
- **Pencere:** hasadın 85 seansı. **Çakışmasız giriş takvimi:** seans indeksi
  0, 5, 10, … (çıkış = giriş + 5 seans hasat içinde kalacak şekilde) → **16
  giriş tarihi**.
- **Spot:** `data/study_f/bars.csv` günlük kapanış (commit'li).
- **0 UW isteği.**

## 4. Yapı (dondurulmuş)

Giriş günü D kapanışında, her isim için:

1. **Vade:** D'ye göre DTE'si **21–45** aralığında olan vadeler arasından DTE'si
   **30**'a en yakın olan (eşitlikte erken olan).
2. **ATM strike K:** o vadede hem call hem put satırı olan strike'lar arasından
   spot'a en yakın (eşitlikte düşük olan).
3. **Beklenen hareket:** `EM = spot × IV_atm × √(DTE/365)`,
   `IV_atm` = K'deki call ve put IV'lerinin ortalaması.
4. **Kanatlar:** call kanadı = `K + 1,5·EM`'e eşit ya da üstündeki **en küçük**
   listelenmiş call strike; put kanadı = `K − 1,5·EM`'e eşit ya da altındaki
   **en büyük** listelenmiş put strike. (1,5× `edge_to_money.md`'den miras.)
5. **Yapı:** K call **sat**, K put **sat**, call kanadı **al**, put kanadı **al**.
   Adet 1.

## 5. Fiyatlama (dondurulmuş, profilin maliyet modeli)

**Giriş (D kapanışı):**
```
KREDI_HAM = callK.bid + putK.bid − callKanat.ask − putKanat.ask
KREDI     = KREDI_HAM × (1 − 0,02)          # gecikme payı, bize karşı
```
`KREDI ≤ 0` ise yapı kurulmaz ve raporlanır.

**Çıkış (D + 5 seans kapanışı):**
```
BORC_HAM = callK.ask + putK.ask − callKanat.bid − putKanat.bid
BORC     = BORC_HAM × (1 + 0,02)
```
- Kısa bacaklardan birinin kotasyonu yoksa sonuç **BİLİNMİYOR**; P&L uydurulmaz,
  sayısı raporlanır.
- Kanat satırı yoksa kanat bid'i **0** alınır (hasat yalnız bid'i olan satırları
  tutar; satırın yokluğu satılacak bir bid olmadığı demektir). Bu bize **karşı**
  bir varsayımdır.

**Komisyon:** 4 bacak × 0,65 $ × 2 yön = **5,20 $**.

```
PNL         = (KREDI − BORC) × 100 − 5,20
MAKS_KAYIP  = (max(kanat mesafeleri) − KREDI) × 100 + 5,20
RoR         = PNL / MAKS_KAYIP                        # risk başı getiri
```

S01 ile aynı mantıkla **mid ayrıştırması**: `piyasa = (mid_giris − mid_cikis)×100`
(satıcı için), geri kalanı maliyet. Mutabakat: bileşenler PNL'e kuruşu kuruşuna
toplanmalı.

## 6. Birincil ölçüt, taban, hüküm merdiveni

**Birincil (tek):** tamamlanmış yapıların **ortalama RoR**'u.

**Taban:** en az **120** tamamlanmış yapı **ve** en az **12** giriş tarihi.
Altındaysa **INSUFFICIENT_DATA**.

`EXPLORATORY_PASS` **üç şartı birden** ister:

| # | Şart |
|---|---|
| 1 | Ortalama RoR **> 0** |
| 2 | **Giriş-tarihi blok** bootstrap %95 aralığının alt sınırı **> 0** (blok = giriş tarihi; aynı gün 10 isim aynı vol şokunu paylaşır) |
| 3 | Pencerenin **iki yarısında da** (ilk 8 / son 8 giriş tarihi) ortalama RoR **> 0** |

Biri düşerse **REJECTED**; düşen şartlar `verdict_failed_clauses`'a yazılır.
**FORWARD_PASS çıkamaz**: veri B kalite.

Bootstrap tohumu **20260923**, tekrar **10.000**.

**Deneme sayısı:** bu çalışma **1** deneme harcar. Kapsam toplamı 15 + 1 = **16**.

## 7. İkincil (hükme girmez, önceden ilan edilir)

1. **Tüm faz kaydırmaları:** giriş takvimini 0…4 başlangıç indeksiyle beşe
   kaydırıp ortalama RoR — takvim seçiminin şansa bağlı olup olmadığı.
2. **Koşullu alt küme:** nedensel IV-rank (ismin ATM IV'sinin, hasattaki **önceki**
   seanslardaki kendi ATM IV'lerine göre yüzdeliği) ≥ %75 olan girişler. Isınma
   için ilk 20 seans IV-rank'siz.
3. **Maliyet / kredi oranı** ve bunun `edge_to_money.md` varsayımlarıyla (%5 / %10
   / %20) karşılaştırması.
4. İsim başına ortalama RoR; yıllıklandırılmış Sharpe benzeri
   `ortalama/std × √(252/5)` (çakışmasız, tarih başına ortalama üzerinden).
5. **Kuyruk:** en kötü tek işlem, en kötü giriş tarihi (10 ismin toplamı) ve
   yapısal azami kayıp (kanatlar sayesinde sınırlı).

## 8. Bu çalışmanın veremeyeceği hüküm

- **Kuyruk örneklenmemiş olabilir:** 16 giriş tarihlik, ~4 aylık pencere bir vol
  çöküşü içermeyebilir. Kısa vol'un asıl riski o gündür. Yapı riski tanımlı
  (kanatlar), ama gerçekleşen Sharpe o günü **fiyatlamamıştır**.
- Gün içi fill yok (B kalite); tüm fiyatlar gün sonu NBBO.
- On likit isim; sonuç daha geniş makaslı isimlere genellenmez.
- 16 tarihlik blok bootstrap geniş aralık verir; geçiş bile kanıt değil,
  **ileriye dönük izlemeye** aday demektir.

## 9. Sayımlar (dondurmadan önce, P&L hesaplanmadan)

`scripts/options_alpha_s02_power.py`, yalnız sayım:

| | Değer |
|---|---|
| Çakışmasız giriş tarihi | **16** |
| Denenen (tarih × isim) | 160 |
| Kurulabilir + kısa bacakları çıkışta kotasyonlu | **160** |
| Tarih başına | 10 (hepsi) |
| Düşen | 0 |

Taban (120 yapı, 12 tarih) sayım olarak tutuyor.
