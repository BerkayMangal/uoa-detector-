# H02 sonucu — REDDEDİLDİ (iki şart düştü)

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-23**
**Protokol:** `PREREG_H02.md` — commit `84192d1`, bu koşucu dosyası var olmadan
önce donduruldu. Ek yazılmadı.
**Artefakt:** `artifacts/options-alpha-v1/h02_result.json`
**Veri kalitesi:** B · **0 UW isteği** (yalnız yerel hasat)
**Motor:** H10 işlem denetiminin düzelttiği `exits.py` ile koşuldu
(`AUDIT_H10_TRADES.md`): ufkun son günü fiyatlanamayan pozisyon artık önceki bir
günün değeriyle "time" çıkışı sayılmıyor, `no_exit_data` oluyor. Bu koşumda
40 kayıt (A 21, B 19) bu yoldan geçti. Düzeltilmiş motorla yeniden üretim birebir.

**Hipotez:** bir kontratta olağandışı hacim tek seferlik değil **tekrarlıyorsa**
(aynı kontrat D-1..D-5 içinde en az iki seans daha ≥%90 yüzdelikteyse), sonraki
net opsiyon P&L'i tek seferlik olağandışı hacimden iyi olur. Ön-kayıtta yön
sabitlendi: **A (tekrarlayan) > B (tek seferlik)**.

---

## 1. Hüküm

**REJECTED.** Taban tuttu, üç şartlı merdiven değerlendirildi, iki şart düştü:

| # | Şart | Sonuç |
|---|---|---|
| 1 | A ortalaması B'yi geçiyor | **tuttu** (−53,47 $ > −58,47 $) |
| 2 | A maliyet sonrası pozitif | **DÜŞTÜ** (−53,47 $) |
| 3 | Seans-blok bootstrap %95 aralığı sıfırı içermiyor | **DÜŞTÜ** ([−20,31 , +27,28]) |

§9'un iki ayrı cümlesi:

- **İşlenebilirlik:** yok. Tekrarlayan kol maliyet sonrası yapı başına yaklaşık
  **−53 $** kaybediyor; 67 tamamlanmış yapının 8'i kazandı.
- **Göreli etki:** sıfırdan ayırt edilemiyor. Fark **+5,00 $**, 48 blokluk
  bootstrap'ta farkın ≤ 0 çıkma oranı **0,326**.

1. şartın tutması **tek başına geçiş değildir** (§9) ve bir bulgu olarak
sunulmuyor.

## 2. Kollar (birincil: `time_only`, 5 işlem günü, maliyet sonrası)

| | A (tekrarlayan) | B (tek seferlik) |
|---|---|---|
| Kayıt | 88 | 76 |
| Tamamlanmış (P&L'li) | **67** | **57** |
| Ortalama | **−53,47 $** | **−58,47 $** |
| Medyan | −44,60 $ | −50,60 $ |
| Kazanma | %11,9 | %3,5 |
| Ortalama tekrar | 3,26 | 0 |
| Ortalama hacim | 13.414 | 14.243 |
| Ortalama açık pozisyon | **34.467** | **9.112** |
| Ortalama makas | %1,77 | %1,79 |
| Ortalama IV | 0,237 | 0,212 |
| Ortalama DTE | 24,6 | 26,6 |
| Ortalama delta | 0,364 | 0,364 |

Taban (her kolda ≥ 30 tamamlanmış yapı) **tuttu**: 67 ve 57. Güç analizinin
projeksiyonu kol başına ~45'ti; gerçekleşen daha yüksek. H10'daki modelleme
boşluğu (kova elemesinin varsayılması) burada kapatılmıştı ve projeksiyon bu kez
**muhafazakâr** tarafta kaldı.

Her iki kolda da kayıtların ~%23'ü `no_exit_data` ile kapandı (A 21, B 19) —
tutma günlerinde paket fiyatlanamadı; bunlar P&L'e **başarı sayılmadı**, dışarıda
bırakıldı (M8'in 7. koruması).

## 3. Zorunlu teşhis (§7)

### 3.1 Likidite şerhi — tetiklenmedi, ama tablo değişti

| Kol | İsim dağılımı | En büyük pay |
|---|---|---|
| A | QQQ 32 · SPY 31 · NVDA 7 · TSLA 7 · AMZN 5 · AAPL 4 · MSFT 1 · GOOGL 1 | **%36** |
| B | QQQ 34 · SPY 31 · TSLA 3 · AMZN 3 · NVDA 3 · MSFT 1 · AAPL 1 | %45 |

§7'nin eşiği %50'ydi; A'nın en büyük payı %36 → şerh **tetiklenmedi**, sonuç
"likidite seçimi ayrıştırılamadı" diye yazılmıyor.

Ama dürüstçe: güç analizi (§4b.1) en büyük payı **%13** ölçmüştü. O ölçüm kova
elemesinden **sonra**, risk kapısından **önce** idi. Risk kapısı (100 $'lık R)
bileşimi ETF'lere yığdı: kayıtlarda QQQ+SPY payı A'da **%72**, B'de **%86**.
Yani bu çalışma pratikte iki ETF üzerine koştu. Bu, iki kolu **birbirine**
göre bozmuyor (ikisi de aynı yöne yığıldı), ama sonucun genellenebilirliğini
daraltıyor.

### 3.2 Açık pozisyon farkı — beklenen bir yan etki

Tekrarlayan kontratların açık pozisyonu tek seferliklerin **~3,8 katı**. Bu
tasarımın doğal sonucu: birkaç gün üst üste olağandışı hacim gören kontrat
pozisyon biriktirir. Kollar yani "tekrar" ile birlikte "yerleşik pozisyon"u da
ayırıyor; iki değişken burada ayrıştırılamaz. Etki olmadığı için bu şerh sonucu
değiştirmiyor, ama gelecekte OI'ye dayalı bir aile (H03 benzeri) bu örtüşmeyi
hesaba katmalı.

## 4. Kovalar

| Kova | A | B |
|---|---|---|
| DTE 14-30 / delta 0,25-0,40 | n=54, **−50,41 $** | n=37, −55,47 $ |
| DTE 14-30 / delta 0,40-0,55 | n=15, −51,93 $ | n=16, −51,28 $ |
| DTE 14-30 / delta 0,55-0,70 | n=1, P&L yok | n=1, −34,60 $ |
| DTE 31-45 / delta 0,25-0,40 | n=10, −44,90 $ | n=10, −84,30 $ |
| DTE 31-45 / delta 0,40-0,55 | n=6, −98,40 $ | n=4, −38,75 $ |
| DTE 46-60 / delta 0,40-0,55 | n=2, −31,90 $ | n=4, −35,00 $ |

**Düşen kova:** `46-60 / 0,25-0,40`, yalnız B kolu taşıdığı için.

En kalabalık kovada A 5 $ önde; ikinci kovada eşit; küçük kovalar iki yöne de
savruluyor. Kovalar tutarlı bir yön göstermiyor.

## 5. Sayılar

**Pencere:** hasat 85 seans (2026-05-14 … 2026-09-15), 48 giriş seansı bloğu.
**Giriş D+1 kapanışı.** İlk akış günü 26. seans (20 seans yüzdelik penceresi +
5 seans geriye bakış).

### Eleme zinciri

1.655.820 kontrat-satırı tarandı → **62.615** uygun → **319** eşli seans-isim
(güç analiziyle birebir) → **487** yapı kuruldu → 974 fiyatlandı → **164** risk
kapısını geçti. Kayıt tam **164** (88 + 76).

Ölü bantta (tekrar = 1) **883** aday düştü. Yalnız bir kolda aday taşıyan
**178** seans-isim her iki koldan düştü. İki kolu da **skorlanan** çift yalnız
**46** — eşleme aday aşamasında yapıldı (ön-kayıt §4, H01 ile aynı), kayıt
aşamasında değil; bootstrap bu yüzden eşli fark üzerinden değil, seans-blok
üzerinden kol ortalaması farkı alıyor.

### İkincil varyantlar

| Varyant | A (tekrarlayan) | B (tek seferlik) |
|---|---|---|
| `time_only` **(birincil)** | −53,47 $ | −58,47 $ |
| `time_and_stop` | −37,35 $ | −37,44 $ |
| `time_target_stop` | −37,35 $ | −37,44 $ |

İkincilde fark **0,09 $**'a iniyor. Stop, iki kolun kaybını da ~16–21 $ kesiyor;
hedef hiç tetiklenmediği için iki ikincil varyant birebir aynı. Terfi ettirilecek
bir şey yok.

## 6. Bu sonucun söylemedikleri

- **"Tekrarlayan akış kötü" demiyor.** Söylediği: bu pencerede, bu evrende, bu
  yapılarla tekrarlayan kol maliyet sonrası para kaybediyor ve tek seferlikten
  ayırt edilemiyor.
- **Kurumsal sweep tekrarı hakkında değil** (§3): günlük hacim; agresör tarafı,
  sweep/blok ayrımı ve gün içi zamanlama yok.
- **Gün içi iddia yok** (B kalite): fill, stop/hedef sıralaması, gerçekleşme yok.
- **Pratikte iki ETF:** kayıtların %72–86'sı QQQ+SPY (§3.1).
- **100 $'lık R bağlayıcı:** 487 yapının 164'ü risk kapısını geçti.
- **Çoklu deneme:** bu beşinci **koşan** aile, toplam ilan edilmiş deneme **15**
  (H03 3 + H01 3 + H04 3 + H10 3 + H02 3). H06 koşulmadı, deneme tüketmedi.

## 7. Kapsamın durumu

Düzeltilmiş motorla (v2, `AUDIT_H10_TRADES.md`) beş aile koştu: **H03, H01, H02
reddedildi; H04 ve H10 INSUFFICIENT_DATA.** (H04 v1'de REJECTED'tı; `no_exit_data`
düzeltmesi bir kolu tabanın altına indirdi.) Hiçbir kol maliyet sonrası pozitif
değil: tamamlanmış yapıların kol ortalamaları **−46 $ ile −84 $** arasında
(H03 kontrol kolu n=4 hariç). Bu, tek tek hipotezlerden çok **yapı + maliyet +
5 günlük tutma** kurgusunun kendisi hakkında bir işaret; bir sonraki adım altıncı
bir aile değil, bu ortak paydanın sorgulanması olmalı.

## 8. Tekrar üretme

```bash
uv run python scripts/options_alpha_h02_power.py    # sayimlar, kota harcamaz
uv run python scripts/options_alpha_h02_study.py    # calisma, kota harcamaz
```

Bootstrap tohumlanmış (20260923); aralık birebir yeniden üretilir.
