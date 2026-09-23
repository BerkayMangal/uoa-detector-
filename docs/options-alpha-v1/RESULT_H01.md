# H01 sonucu — REDDEDİLDİ

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-23**
**Protokol:** `PREREG_H01.md` + `PREREG_H01_ADDENDUM.md`
**Artefakt:** `artifacts/options-alpha-v1/h01_result.json`
**Profil:** `profiles/options_alpha_v1.yaml` (sha256 artefaktta)
**Veri kalitesi:** B — zaman alanı `last_tape_time`, yani son **işlem** zamanı

**Hipotez:** bir kontratın seans hacmi **kendi hissesinin** son 20 seanslık
dağılımına göre olağandışı yüksekse (≥ %90'lık), sonraki net opsiyon P&L'i normal
seviyedeki (%40–60) kontratlardan iyi olur.

---

## 1. Hüküm

**REDDEDİLDİ**, ve iddia iki ayrı cümleye bölünür:

1. **İşlenebilirlik: REDDEDİLDİ.** İki kol da maliyet sonrası ağır negatif.
   Yüksek kol yapı başına **−54,52 $**, 111 gözlemde **12** kazanan.
2. **Göreli etki: KURULAMADI.** Yüksek kol kontrolü geçiyor, ama fark sıfırdan
   ayırt edilemiyor: seans-blok aralığı **[−3,28, +37,58]** sıfırı içeriyor.

`PREREG_H01.md` §7'de `EXPLORATORY_PASS` üç şartı **birden** istiyordu. İkisi
düştü:

| §7 şartı | Durum |
|---|---|
| Yüksek kol kontrolü geçiyor | **sağlandı** (−54,52 $ > −70,75 $) |
| Maliyet sonrası **pozitif** | **DÜŞTÜ** (−54,52 $) |
| Blok bootstrap aralığı sıfırı içermiyor | **DÜŞTÜ** ([−3,28, +37,58]) |

Merdiven "kontrolü geçiyor ama para kaybediyor" hâline ad vermemişti; boşluk ve
çözümü `PREREG_H01_ADDENDUM.md`'de, sonuçlardan sonra yazıldığı açıkça
belirtilerek kayıtlı.

---

## 2. Sayılar

**Pencere:** giriş seansları **2026-06-12 … 2026-09-15**, 58 giriş seansı,
10 isim. Hasat 85 seans. **Giriş D+1 kapanışı** — bir seansın hacmi ancak o seans
kapandıktan sonra tamamlanır.

### Eleme zinciri

1.655.820 kontrat-satırı tarandı → **62.615** uygun → **879** yapı kuruldu →
1.758 fiyatlandı (her aday long + spread) → **202** risk kapısını geçti.

Kayıt sayısı **tam 202**: risk kapısını geçen her yapı bir kayıt üretti. Kalan
677 yapı 100 $'lık R'ye sığmadığı için sıfır adede indi.

**En büyük elemeler:** açık pozisyon yetersiz 631.270 · DTE çok kısa 305.953 ·
bid çok düşük 217.147 · makas çok geniş 144.137 · hacim yetersiz 135.650.

**Tek kollu düşen:** 30 seans-isim. Bir kolda aday yoksa o seans-isim
**her iki koldan da** düşüyor — ön-kayıt §5.

### Kollar — birincil ölçüt (`time_only`, 5 işlem günü, maliyet sonrası)

| Kol | n | Ortalama | Medyan | %5 kırpılmış | Kazanan | Toplam | En kötü |
|---|---|---|---|---|---|---|---|
| **Yüksek** (≥%90) | **111** | **−54,52 $** | −45,20 $ | −47,53 $ | 12 (%10,8) | −6.051,40 $ | −403,80 $ |
| **Kontrol** (%40–60) | **91** | **−70,75 $** | −46,80 $ | −55,79 $ | 4 (%4,4) | −6.438,20 $ | −699,20 $ |

---

## 3. Manşet neden "yüksek kol kazandı" değil

Üç teşhis, ham +16,23 $'lık farkın nereden geldiğini gösteriyor.

**a) Fark merkezde değil, kuyrukta.** Ortalama farkı +16,23 $, **medyan farkı
+1,60 $**. Kırpılmış ortalamalarda fark +8,26 $'a iniyor. Dağılımın ortası iki
kolu neredeyse ayırt etmiyor.

**b) Farkın yarısına yakını tek işlemde.** Kontrol kolundaki tek bir SPY işlemi
(2026-07-22, −699,20 $) çıkarılınca fark **+16,23 → +9,25 $**.

**c) En kalabalık kova işareti tersine çeviriyor.**

| DTE | delta | Yüksek | Kontrol | Fark | ağırlık |
|---|---|---|---|---|---|
| 14-30 | 0,25-0,40 | n=69 **−49,13** | n=30 **−45,89** | **−3,24** | 30 |
| 14-30 | 0,40-0,55 | n=19 −63,63 | n=28 −74,81 | +11,18 | 19 |
| 31-45 | 0,25-0,40 | n=12 −69,83 | n=9 −79,31 | +9,48 | 9 |
| 31-45 | 0,40-0,55 | n=7 −66,86 | n=10 −77,16 | +10,30 | 7 |
| 46-60 | 0,25-0,40 | n=1 −43,80 | n=5 −54,84 | +11,04 | 1 |
| 46-60 | 0,40-0,55 | n=3 −34,13 | n=4 **−205,80** | +171,67 | 3 |

Her iki kolda da **en kalabalık** olan kovada yüksek kol **daha kötü**. Toplamdaki
üstünlük küçük kovalardan, özellikle n=3'e n=4'lük son satırdan geliyor.

Bileşim etkisi ayıklandığında — her kovanın farkı, o kovada **küçük** kolun
gözlem sayısıyla ağırlıklandırılarak — fark **+11,57 $** ve yüksek kol 6 kovanın
5'inde iyi. Yani etki tamamen bir Simpson tersine dönmesi **değil**; ama
ağırlığın en büyük olduğu tek kovada işaret ters.

**Düşen kovalar:** `14-30 / 0,55-0,70` ve `31-45 / 0,55-0,70` — ikisi de yalnız
kontrol kolu taşıdığı için düştü (ön-kayıt §7).

---

## 4. Seans-blok bootstrap

Aynı seansta girilen kartlar bağımsız değil: tek bir piyasa hareketi hepsini
birlikte sürüklüyor. Bu yüzden yeniden örnekleme birimi **kart değil seans**.

| | |
|---|---|
| Blok | 58 giriş seansı |
| Tekrar | 10.000, tohum **20260923** |
| Gözlenen fark | **+16,23 $** |
| %95 aralık | **[−3,28 , +37,58]** |
| Farkın ≤ 0 çıkma oranı | **0,056** |

Aralık sıfırı içeriyor. §7'nin üçüncü şartı bu yüzden düşüyor — ve bu şart,
maliyet-pozitiflik şartından **bağımsız** olarak düşüyor. Hüküm iki ayrı sebeple
REJECTED.

---

## 5. İkincil varyantlar

| Varyant | Yüksek | Kontrol |
|---|---|---|
| `time_only` **(birincil)** | −54,52 $ | −70,75 $ |
| `time_and_stop` | −33,68 $ | −36,90 $ |
| `time_target_stop` | −33,68 $ | −36,90 $ |

İkincil varyantlarda zarar küçülüyor ama **hâlâ negatif**, ve kollar arası fark
3,22 $'a iniyor. H03'te olduğu gibi birincil ölçüt veriye bakılmadan
sabitlenmişti; kazanana geçilmedi.

İki ikincil varyantın **birebir aynı** çıkması tesadüf değil: hedef, yapının
brüt tavanına göre kapanıyor ve bu örneklemde hiçbir pozisyon hedefe ulaşmadı,
dolayısıyla `time_target_stop` fiilen `time_and_stop`'a indi.

---

## 6. Bu sonucun söylemedikleri

- **Hacim sinyalinin kendisi çürütülmedi.** Çürütülen şey, bu kapılar + bu
  yapılar + bu çıkışlar + bu maliyet modeli ile bu pencerede işlenebilirliği.
- **Gün içi hiçbir iddia yok.** B kalite veri: stop/hedef sıralaması, fill,
  gün içi gerçekleşme yok. Günlük kapanış değerleri üzerinden değerlendirildi.
- **Pencere 58 giriş seansı** — dört çeyreklik bir rejim taraması değil.
- **Evren bugünden seçildi** → survivorship. Likidite kapıları ayrıca seçim
  yanlılığı taşıyor.
- **100 $'lık R bağlayıcı kısıt.** 879 yapının 677'si bütçeye sığmadığı için
  düştü. Bu veri ya da hipotez sorunu değil, **sermaye ölçeği**; bütçe bu
  çalışma için büyütülmedi.
- **Çoklu deneme:** options_alpha_v1 kapsamında bu ikinci aile, toplam ilan
  edilmiş deneme **6**. Tek bir ailenin eşiği geçmesi altı denemelik bir aramada
  tek başına kanıt olmazdı — burada zaten geçmedi.

---

## 7. Koşucuda bulunan kusur

İlk hâli §7'yi yanlış uyguluyordu: yalnız birinci şarta bakıyor, merdivende
**var olmayan** bir etiket (`EXPLORATORY_PASS_CANDIDATE`) üretiyor ve §7'nin
açıkça istediği **bootstrap'i hiç hesaplamıyordu**. Düzeltilmeseydi bu aile
yanlış etiketle yayımlanmış olacaktı. Ayrıntı: `PREREG_H01_ADDENDUM.md` §4.

---

## 8. Tekrar üretme

```bash
# Hasat yoksa once (kota harcar, ~845 istek):
uv run python scripts/options_alpha_harvest_chains.py

# Calisma (yerel hasattan okur, kota harcamaz):
uv run python scripts/options_alpha_h01_study.py
```

Bootstrap tohumlanmış olduğu için aralık birebir yeniden üretilir.
