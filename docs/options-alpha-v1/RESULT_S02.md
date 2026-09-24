# S02 sonucu — REDDEDİLDİ (vol primi bu pencerede para değil)

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-24**
**Protokol:** `PREREG_S02.md` — commit `06bcdcf`, bu koşucu dosyası var olmadan
önce donduruldu ve push edildi. Ek yazılmadı.
**Artefakt:** `artifacts/options-alpha-v1/s02_result.json`
**Veri kalitesi:** B · **0 UW isteği** · **1 deneme** (kapsam toplamı 16)

**Hipotez:** her giriş gününde, her isimde koşulsuz kısa ATM iron butterfly
satmak (21–45 DTE, 30'a en yakın; kanatlar 1,5× beklenen hareket), 5 işlem günü
sonra kapatınca, ölçülmüş makas + %2 gecikme + komisyon sonrası ortalama pozitif
risk-başı getiri (RoR) üretir. Yön sabitti: **satıcı kazanır.**

---

## 1. Hüküm

**REJECTED.** Taban tuttu (160 yapı, 16 tarih), üç şartın üçü de düştü:

| # | Şart | Sonuç |
|---|---|---|
| 1 | Ortalama RoR > 0 | **DÜŞTÜ** — **−%9,7** |
| 2 | Tarih-blok bootstrap %95 alt sınırı > 0 | **DÜŞTÜ** — **[−%15,1 , −%5,3]**, aralığın tamamı negatif |
| 3 | İki yarıda da pozitif | **DÜŞTÜ** — ilk yarı −%9,4, ikinci yarı −%10,0 |

Bu bir "sınırda kaldı" sonucu değil: 10.000 bootstrap çekilişinin **tamamı** sıfırın
altında. Beş farklı giriş takviminin (faz kaydırması) beşi de −%9,7 ile −%11,3
arasında. Mutabakat: 0 uyuşmazlık, 0 bilinmeyen çıkış.

## 2. Sayılar (yapı başına ortalama)

| | Değer |
|---|---|
| Kredi (gecikme sonrası) | 3.004 $ |
| Paket mid'i (giriş) | 3.179 $ |
| Azami kayıp | 3.917 $ |
| **Net P&L** | **−386,79 $** |
| **RoR** | **−%9,7** (medyan −%5,3) |
| Kazanma oranı | %30,6 |
| En kötü işlem | −3.999,62 $ |
| En kötü giriş tarihi (10 isim toplamı) | −14.292,80 $ |
| Yıllıklandırılmış Sharpe (tarih ortalamaları) | −6,5 |

### Ayrıştırma — kayıp nereden geliyor

| Bileşen | $ / yapı |
|---|---|
| **Satıcının mid'de kazandığı prim** | **−16,05** |
| Giriş makası | −113,49 |
| Giriş gecikme payı (%2) | −61,30 |
| Çıkış makası | −124,36 |
| Çıkış gecikme payı (%2) | −66,38 |
| Komisyon | −5,20 |
| **Toplam maliyet** | **−370,73** (mid kredinin **%11,7**'si) |

Okuma: **satılacak prim yoktu** (mid'de −16 $, sıfır civarı) ve maliyet her
işlemde ~371 $ aldı. Kayıp maliyetten, ama maliyet olmasa bile kazanç yoktu.

## 3. Hükümden bağımsız teşhis (ön-kayıt DIŞI)

Sonuç görüldükten sonra, "neden prim yoktu?" sorusu için:

1. **Pencerede vol primi neredeyse sıfırdı.** Gerçekleşen 5 günlük |hareket|,
   örtük hareketin ortalama **0,77** katı. Normal dağılımda beklenen |hareket|
   zaten **0,80σ**; yani gerçekleşen/örtük ≈ **0,97**. Study D'nin taze penceresinde
   (2024–25) prim büyüktü (Sharpe +1,44); **2026 Mayıs–Eylül'de yoktu.**
2. **IV düşüşü yardım etmedi:** ATM IV giriş→çıkış ortalama −0,4 puan (satıcı
   lehine), ama yetmedi. Pencerede IV %29–42 arasında dolaştı; Temmuz tepesi
   (~%42) ve Ağustos düşüşü (~%31).
3. **Hüküm maliyet varsayımına duyarlı değil:**

| Senaryo | Ortalama $ | RoR | Kazanma |
|---|---|---|---|
| Ön-kayıt (makas + %2 gecikme + komisyon) | −386,79 | −%9,7 | %31 |
| %2 gecikme payı **olmadan** | −259,11 | −%6,4 | %41 |
| **Sıfır maliyet** (yalnız mid) | −16,05 | −%0,5 | %66 |

   Sıfır maliyette bile negatif. Satıcı mid'de %66 kazanıyor ama kayıpları daha
   büyük; tanımlı riskli yapının klasik profili.

4. **Makas varsayımı meselesi kapandı, ama ters yönde.** `edge_to_money`'nin %10
   varsayımı bu evren için gerçekten kötümserdi: ölçülen giriş makası mid
   kredinin **%3,6**'sı. Ama bu yeterli değil; çünkü **tek başına vol primi 5 günlük
   tutmada, bu pencerede, sıfır.** Maliyet düşük olsa da taşıyacak edge yok.

## 4. İkincil sonuçlar (önceden ilan edildi, hükme girmez)

- **Koşullu (nedensel IV-rank ≥ %75):** 42 yapı, RoR **−%12,0**, kazanma %14.
  Yüksek IV'de satmak burada **daha kötü**.
- **İsim başına:** onunun onu da negatif; en iyisi SPY (−%0,8, kazanma %75), en
  kötüsü MSFT (−%18,5).
- **Faz kaydırmaları:** 0–4 arası beşi de −%9,7 … −%11,3.

## 5. Bu sonucun söylemedikleri

- **"Vol primi yoktur" demiyor.** 2024–25'te büyüktü; 4 aylık bir pencerede yoktu.
  Rejim bağımlı bir prim, kısa pencerede sıfır ya da negatif olabilir.
- **Vadeye kadar tutmayı sınamadı.** Tutma 5 gündü (profil). `edge_to_money` ~1 ay
  tutuyordu. Ama 85 seanslık pencerede çakışmasız 20 günlük tutma ~40 yapı verir;
  taban altı kalır ve aynı pencereyi altıncı kez sorgulamak olur. **Koşulmadı.**
- **Kuyruk yine örneklenmedi:** en kötü tarih −14.293 $ (10 fly), ama gerçek bir
  vol çöküşü penceresi değildi.

## 6. Tekrar üretme

```bash
uv run python scripts/options_alpha_s02_power.py   # sayim, P&L yok
uv run python scripts/options_alpha_s02_study.py   # calisma, ~1 dk, kota harcamaz
```

Bootstrap tohumlu (20260923); birebir yeniden üretilir.
