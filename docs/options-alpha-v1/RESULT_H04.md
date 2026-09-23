# H04 sonucu — REDDEDİLDİ (yön hipotezin tersine çıktı)

**Faz 5.24 · options_alpha_v1** · koşum **2026-09-23**
**Protokol:** `PREREG_H04.md` — commit `17677a5`, merge `faf6982`, **bu koşucu
dosyası var olmadan önce**. Ek yazılmadı.
**Artefakt:** `artifacts/options-alpha-v1/h04_result.json`
**Veri kalitesi:** B — zaman alanı `last_tape_time`, yani son **işlem** zamanı

**Hipotez:** bir kontratta olağandışı hacim varken **dayanak henüz hareket
etmemişse** akış fiyatı önceliyor olabilir; dayanak zaten hareket etmişse akış
harekete **tepki**dir. Ön-kayıtta yön sabitlendi: **A kolu B kolunu geçecek.**

---

## 1. Hüküm

**REDDEDİLDİ**, ve reddin biçimi önemli: **yön hipotezin tersine çıktı.**

| Kol | n | Ortalama | Medyan | Kazanma | Ortalama z |
|---|---|---|---|---|---|
| **A — hareket etmemiş** (z ≤ 0,40) | **42** | **−60,47 $** | −40,90 $ | %11,9 | 0,196 |
| **B — zaten hareket etmiş** (z ≥ 0,90) | **33** | **−51,93 $** | −46,60 $ | %15,2 | 1,616 |

Tahmin edilen A > B idi; gerçekleşen **A < B**. Ön-kayıt §2 bu durumu önceden
bağlamıştı:

> Sonuç ters çıkarsa bu, hipotezin doğrulanması değil **reddi**dir. Ters yönü
> "aslında şunu bulduk" diye sunmak yasaktır.

O yüzden burada "akış fiyatı takip ediyor, demek ki tepki akışı daha iyi" gibi bir
cümle kurulmuyor. Ters yön, yeni bir bulgu değil, bu hipotezin reddidir. Takip
edilecekse **yeni** bir ön-kayıt ve yeni bir deneme sayısı gerekir.

### §7'nin üç şartı

| # | Şart | Durum |
|---|---|---|
| 1 | A kolu B kolunu geçiyor | **DÜŞTÜ** (−60,47 < −51,93) |
| 2 | A kolu maliyet sonrası pozitif | **DÜŞTÜ** (−60,47 $) |
| 3 | Bootstrap aralığı sıfırı içermiyor | **DÜŞTÜ** ([−44,42, +25,70]) |

Üçü birden düştü. `EXPLORATORY_PASS` üçünü birlikte istiyordu.

---

## 2. Örneklem tabanı tuttu — ve bunu güç analizine borçlu

| | A | B |
|---|---|---|
| Güç analizinin projeksiyonu | ~44,8 | ~39,3 |
| Gerçekleşen | **42** | **33** |
| Taban (30) | tuttu | tuttu |

Taslaktaki bantlar (`z ≤ 0,5` / `z ≥ 1,5`) dondurulmuş olsaydı B kolu ~15 kayıtta
kalacak ve hüküm **INSUFFICIENT_DATA** olacaktı — yani hipotez sınanamadan
kapanacaktı. Eşikler dondurmadan **önce**, yalnız **sayım** üzerinden değiştirildi
(`PREREG_H04.md` §4b, betik `scripts/options_alpha_h04_power.py`).

Bu, eşik oynatmanın masum olduğu anlamına gelmez; masum olan **ne zaman ve neye
bakarak** oynatıldığıdır. Seçim anında hiçbir kolun P&L'i hesaplanmamıştı ve
seçim kuralı ("ölü bant ≥ 0,50 σ şartıyla zayıf kolun projeksiyonunu en büyük
yapan çift") koşumdan önce ilan edilmişti.

**Çapraz doğrulama:** güç betiği ölü banda 194 aday düştüğünü söylemişti; koşucu
bağımsız olarak **194** buldu. İki kod yolu aynı sayıyı veriyor.

---

## 3. Sayılar

**Pencere:** hasat 85 seans; 44 giriş seansı bloğu. **Giriş D+1 kapanışı.**

### Eleme zinciri

1.655.820 kontrat-satırı tarandı → **62.615** uygun → **272** yapı kuruldu →
544 fiyatlandı (her aday long + spread) → **75** risk kapısını geçti.

Kayıt sayısı **tam 75** (42 + 33). Ölü bantta **194** seans-isim düştü, σ20
hesaplanamayan **0** — commit'li `data/study_f/bars.csv` pencereyi tam kapsıyor.

### Kovalar

| DTE | delta | A (hareket etmemiş) | B (zaten hareket etmiş) |
|---|---|---|---|
| 14-30 | 0,25-0,40 | n=24 **−56,63** | n=25 **−38,45** |
| 14-30 | 0,40-0,55 | n=7 −60,20 | n=6 −84,13 |
| 31-45 | 0,25-0,40 | n=7 −86,77 | n=1 −114,60 |
| 31-45 | 0,40-0,55 | n=1 −28,60 | n=1 −133,20 |

**Simpson tersine dönmesi yok.** Her iki kolda da en kalabalık kova (n=24'e 25)
toplamla **aynı** yönü gösteriyor: B daha iyi. H01'de manşeti tersine çeviren kova
sorunu burada yaşanmadı; yön tutarlı ve bu yüzden red daha net.

**Düşen kovalar:** `46-60 / 0,25-0,40` ve `46-60 / 0,40-0,55` — ikisi de yalnız A
kolu taşıdığı için düştü.

### Seans-blok bootstrap

| | |
|---|---|
| Blok | 44 giriş seansı |
| Tekrar | 10.000, tohum 20260923 |
| Gözlenen fark (A − B) | **−8,53 $** |
| %95 aralık | **[−44,42 , +25,70]** |
| Farkın ≤ 0 çıkma oranı | 0,683 |

Aralık sıfırı içeriyor. Yani **A'nın daha kötü olduğu da** istatistiksel olarak
kurulamıyor: veri "A kötüdür" demeye de yetmiyor. Tek söylenebilen, A'nın B'yi
geçtiğine dair hiçbir kanıt olmadığı.

### İkincil varyantlar

| Varyant | A | B |
|---|---|---|
| `time_only` **(birincil)** | −60,47 $ | −51,93 $ |
| `time_and_stop` | −33,99 $ | −33,51 $ |
| `time_target_stop` | −33,99 $ | −33,51 $ |

İkincilde zarar küçülüyor ama kollar neredeyse **çakışıyor** (fark 0,48 $). Stop
uygulanınca iki kol ayırt edilemez hale geliyor — ayrışmanın tamamı tutma
süresinin sonundaki kuyrukta. Birincil ölçüt veriye bakılmadan sabitlenmişti;
kazanana geçilmedi.

---

## 4. Belgede bulunan kusur (kayda geçer)

`PREREG_H04.md` §4'ün son cümlesi H01'den devralınmış:

> Bir kolda aday yoksa o seans-isim **her iki koldan da** düşer.

Bu cümle H04'te **uygulanamaz**. Kolu belirleyen z, kontratın değil **(isim,
seans)** çiftinin özelliğidir; dolayısıyla bir seans-isim en fazla **bir** kola
girer ve ikisini birden besleyemez. Cümle hiçbir alternatif davranışa izin
vermiyor ve hiçbir sayıyı değiştirmiyor, ama sessizce geçmek yerine buraya
yazılıyor. Protokol değiştirilmedi.

---

## 5. Bu sonucun söylemedikleri

- **Akış-öncüllüğü fikri çürütülmedi.** Çürütülen, bu kapılar + bu yapılar + bu
  çıkışlar + bu maliyet modeliyle, hacmi akış vekili sayan bu
  operasyonelleştirmede, bu pencerede işlenebilirliği.
- **Akış = hacim sadeleştirmesi.** `flow-alerts` kullanılmadı; sweep/blok ayrımı,
  agresörlük tarafı ve gün içi zamanlama yok. Bu, ön-kayıt §5'te koşumdan önce
  yazılmıştı.
- **Gün içi hiçbir iddia yok** (B kalite): stop/hedef sıralaması, fill, gün içi
  gerçekleşme yok.
- **44 giriş seansı bloğu** — dört çeyreklik rejim taraması değil.
- **100 $'lık R bağlayıcı.** 272 yapının 197'si bütçeye sığmadığı için düştü.
  Sermaye ölçeği sorunu; bütçe bu çalışma için büyütülmedi.
- **Çoklu deneme:** bu üçüncü aile, toplam ilan edilmiş deneme **9**
  (H03 3 + H01 3 + H04 3). Üç ailenin üçü de reddedildi.

---

## 6. Tekrar üretme

```bash
# Hasat yoksa once (kota harcar, ~845 istek):
uv run python scripts/options_alpha_harvest_chains.py

# Guc analizi (kota harcamaz):
uv run python scripts/options_alpha_h04_power.py

# Calisma (kota harcamaz):
uv run python scripts/options_alpha_h04_study.py
```

Bootstrap tohumlanmış; aralık birebir yeniden üretilir. Dayanak çubukları
commit'li (`data/study_f/bars.csv`), dolayısıyla bu aile **0 UW isteği** harcadı.
