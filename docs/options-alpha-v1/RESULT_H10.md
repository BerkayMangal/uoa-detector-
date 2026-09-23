# H10 sonucu — INSUFFICIENT_DATA (taban iki kayıt eksik)

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-23**
**Protokol:** `PREREG_H10.md` — commit `456dbcc`, bu koşucu dosyası var olmadan
önce donduruldu. Ek yazılmadı.
**Artefakt:** `artifacts/options-alpha-v1/h10_result.json`
**Veri kalitesi:** B · **0 UW isteği** (yerel hasat + commit'li `data/study_f/bars.csv`)

**Hipotez:** bir kontratın örtük oynaklığı dayanağın kendi gerçekleşen oynaklığına
göre ucuzsa, onu satın almanın sonraki net P&L'i pahalı olduğu durumdan iyi olur.
Ön-kayıtta yön sabitlendi: **A (ucuz) > B (pahalı)**.

---

## 1. Hüküm

**INSUFFICIENT_DATA.** `PREREG_H10.md` §7 her kolda en az **30** tamamlanmış yapı
istiyordu. Gerçekleşen:

| Kol | n | Taban 30 |
|---|---|---|
| A (ucuz) | **45** | tuttu |
| B (pahalı) | **28** | **TUTMADI** — iki eksik |

Taban kapısı üç şartlı merdivenden **önce** gelir, dolayısıyla üç şart hiç
değerlendirilmedi.

### Reddedilen üç hamle

1. **Tabanı 28'e çekmek.** Sonucu gördükten sonra eşik oynatmak bu projede yasak.
   Taban koşumdan önce 30'du ve 30 kalıyor.
2. **Bandı genişletmek** (örn. B ≥ 1,05) ve B'ye kayıt toplamak. Aynı yasak, daha
   ince biçimi.
3. **Ortalamaları geçerli bir karşılaştırma gibi sunmak.** Taban tutmadığı için
   kollar arası kıyas hükme bağlanamaz.

## 2. Zaten sunulacak bir şey yok

Taban bir yana, veri etkinin **izini** de göstermiyor:

| | A (ucuz) | B (pahalı) |
|---|---|---|
| n | 45 | 28 |
| Ortalama | **−49,33 $** | **−49,78 $** |
| Medyan | −51,60 $ | −40,20 $ |
| Kazanma | %2,2 (45'te 1) | %17,9 (28'de 5) |
| Ortalama ucuzluk | 0,746 | 1,316 |
| Ortalama IV | 0,231 | 0,235 |
| Ortalama makas | %2,10 | %1,93 |
| Ortalama DTE | 24,4 | 25,2 |
| Ortalama delta | 0,360 | 0,331 |

Fark **+0,45 $**. Seans-blok bootstrap: 40 blok, %95 aralık **[−30,88 , +37,56]**,
farkın ≤ 0 çıkma oranı **0,501** — yazı tura. İki kol da yapı başına yaklaşık
**−49 $**.

Yani bu çalışma yalnızca **güçsüz** değil; taban tutsaydı bile birincil ölçütte
gösterilecek bir etki yoktu. Taban tutmuş olsaydı 1. şart (A > B) 0,45 $ ile teknik
olarak sağlanır, 2. şart (maliyet sonrası pozitif) ve 3. şart (aralık sıfırı
içermiyor) **düşerdi** — yani hüküm yine REJECTED olurdu. Bu bir geçiş senaryosu
değil.

Kovalar da aynı yöne bakıyor: en kalabalık kovada (DTE 14-30 / delta 0,25-0,40,
n=28'e 18) A **−48,71**, B **−26,78** — yani B daha iyi.

## 3. Güç analizimdeki modelleme boşluğu

Güç analizi (`scripts/options_alpha_h10_power.py`) kol başına **77,7** ve **51,2**
kayıt öngörmüştü. Gerçekleşen **45** ve **28**.

Risk kapısı varsayımı **doğruydu**: 309 yapının 73'ü geçti, %23,6 — betiğin
kullandığı %23'e çok yakın. Hata orada değil.

Boşluk şurada: betik, kol atanmış seans-isim sayısını doğrudan risk kapısı oranıyla
çarpıyordu ve arada duran **ankraj → kayıt elemesini** hiç modellemiyordu. Girişte
(D+1) ankrajın hâlâ zincirde olması, delta taşıması ve **hem** DTE 14–60 **hem**
|delta| 0,25–0,70 kovasına düşmesi gerekiyor. Uçtan uca oran:

```
73 kayıt / 561 kol atanmış seans-isim ≈ %13     (betiğin varsaydığı: %23)
```

Bu, dondurmadan önce yakalanabilecek bir kusurdu ve **yakalanamadı**; koşum
yakaladı. H04 ve H06'da güç analizi tasarımı kurtarmıştı, burada eksik kaldı.
Betiğin hesabı **değiştirilmedi** — §4b'nin tablosu ona atıf yapıyor ve yeniden
üretilebilir kalmalı; boşluk docstring'e not edildi. Sıradaki ailenin güç analizi
kova elemesini de modelleyecektir.

## 4. Beklentinin tersine çıkan şey: makas dezavantajı büyük ölçüde kayboldu

Dondurma öncesi ölçüm, ucuz kolun **+0,7 puan** daha geniş makasa düştüğünü
söylüyordu ve §4b.1 bunun üzerine bir yorum kuralı bağlamıştı. Gerçekleşen
örneklemde fark **+0,17 puan** (%2,10'a %1,93).

Sebebi: kayıtlar, kol atanmış seans-isimlerin kova-filtreli bir altkümesi, ve
filtre bileşimi değiştirdi. Koşucu §4b.1'in "muhafazakâr" dalını teknik olarak
tetikledi (A, daha geniş makasa rağmen B'yi geçti), ama fark 0,45 $ olduğu için bu
şerhin taşıdığı ağırlık **yoktur** ve şişirilmiyor. Dürüst okuma: bu koşumda
maliyet dezavantajı da, etki de yok.

## 5. Sayılar

**Pencere:** hasat 85 seans, 40 giriş seansı bloğu. **Giriş D+1 kapanışı.**

### Eleme zinciri

1.161.371 kontrat-satırı tarandı → **42.739** uygun → **309** yapı kuruldu →
618 fiyatlandı → **73** risk kapısını geçti. Kayıt tam **73** (45 + 28).

Tarama sayısı H01/H04'ten düşük çünkü bu koşucu uygunluğu yalnız **giriş
penceresindeki** akış seansları için hesaplıyor, 85 seansın tamamı için değil —
aile akış dağılımı kurmadığından trailing pencereye ihtiyacı yok.

Ölü bantta (0,90–1,10) **184** seans-isim düştü. Gerçekleşen vol hesaplanamayan
**0** — commit'li çubuklar pencereyi tam kapsıyor.

**Düşen kova:** `46-60 / 0,25-0,40`, yalnız B kolu taşıdığı için.

### İkincil varyantlar

| Varyant | A (ucuz) | B (pahalı) |
|---|---|---|
| `time_only` **(birincil)** | −49,33 $ | −49,78 $ |
| `time_and_stop` | −33,57 $ | −38,96 $ |
| `time_target_stop` | −33,57 $ | −38,96 $ |

İkincilde A daha iyi görünüyor (5,39 $ fark) ama **terfi ettirilmedi**; birincil
ölçüt veriye bakılmadan sabitlenmişti. Ayrıca taban yine tutmuyor.

## 6. Bu sonucun söylemedikleri

- **Hipotez çürütülmedi ve doğrulanmadı.** Taban tutmadı; birincil ölçütte de etki
  görünmedi. İkisi farklı cümle ve ikisi de burada geçerli.
- **Vol SATMA hakkında hiçbir şey söylemez.** `docs/edge_to_money.md`'nin hükmü
  değişmedi; bu aile primin alım tarafıydı.
- **Aynı-gün kontrolü yoktu** (§4b): kollar farklı piyasa günlerinden gelebiliyor.
- **Gün içi iddia yok** (B kalite): fill, stop/hedef sıralaması, gerçekleşme yok.
- **100 $'lık R bağlayıcı:** 309 yapının 236'sı bütçeye sığmadı. Sermaye ölçeği.
- **Çoklu deneme:** bu dördüncü **koşan** aile, toplam ilan edilmiş deneme **12**
  (H03 3 + H01 3 + H04 3 + H10 3). H06 koşulmadı, deneme tüketmedi.

## 7. Tekrar üretme

```bash
uv run python scripts/options_alpha_h10_power.py    # sayimlar, kota harcamaz
uv run python scripts/options_alpha_h10_study.py    # calisma, kota harcamaz
```

Bootstrap tohumlanmış (20260923); aralık birebir yeniden üretilir.
