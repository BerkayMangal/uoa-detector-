# Ön-kayıt — H04: akış fiyattan önce mi geliyor, sonra mı

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, tek bir sonuç
görülmeden. Koşucu yazılmadan sabitlenir ve sonuçlara göre değiştirilmez.

---

## 1. Neden bu aile, neden şimdi

`HYPOTHESES.md`'nin sıralaması **sonuçlara bakılmadan** yazıldı ve H03 → H01 →
**H04** diyor. H03 ve H01 reddedildikten sonra daha umutlu görünen bir aileye
atlamak, o dosyanın kendi deyimiyle bir **seçim maliyeti** olurdu ve tasarıma
dahil edilmesi gerekirdi. Sıra bozulmuyor: üçüncü aile H04.

## 2. Hipotez

Bir kontratta olağandışı hacim varken **dayanak henüz hareket etmemişse**, akış
fiyatı önceliyor olabilir. Dayanak zaten hareket etmişse, akış büyük olasılıkla
harekete **tepki**dir ve bilgi taşımaz.

**Yön şimdi sabitlenir:** A kolunun (fiyat henüz hareket etmemiş) sonraki net
opsiyon P&L'i, B kolundan (fiyat zaten hareket etmiş) **iyi** olacaktır.

Sonuç ters çıkarsa bu, hipotezin doğrulanması değil **reddi**dir. Ters yönü
"aslında şunu bulduk" diye sunmak yasaktır.

**Başarısızlık ihtimali dürüstçe:** hareket etmemiş dayanak, çoğu zaman haber
yokluğudur; olağandışı hacim de hedge ya da takvim kaynaklı olabilir. Ayrıca
"henüz hareket etmedi" ölçütü, düşük oynaklıklı rejimi seçiyor olabilir ve o
rejimde uzun prim zaten pahalıdır.

## 3. Bilgi zamanı

H01'deki kuralın aynısı, aynı sebeple: bir seansın hacmi ancak o seans
**kapandıktan sonra** tamamlanır.

| An | Ne biliniyor |
|---|---|
| Seans **D** kapanışı | D'nin tam hacmi ve D'nin getirisi henüz kullanılamaz |
| Seans **D+1** | D'nin hacmi ve D'nin kapanışı yayımlanmış |
| Seans **D+1** kapanışı | **GİRİŞ burada** |

Tutma: girişten itibaren **5 işlem günü**. Hem hacim hem getiri yalnız D ve
öncesinden okunur; D+1'in kendi getirisi kola karar vermede **kullanılmaz**.

## 4. Kolların tanımı (dondurulmuş)

Aday kontrat seçimi H01'in aynısıdır ve **değiştirilmez**: seans D'de, o hissenin
önceki **20 seansındaki** uygun kontrat hacimlerine göre yüzdeliği **≥ 90** olan
kontratlardan hacmi en yüksek olanı. Kolu belirleyen değişken bundan
**bağımsızdır** — H03'ün kusuru burada tekrarlanmıyor.

Dayanağın D günündeki hareketi, kendi geçmiş oynaklığına göre normalleştirilir:

```
z = |kapanış(D) / kapanış(D-1) - 1|  /  σ20
σ20 = D'den ÖNCEKİ 20 seansın günlük getirilerinin standart sapması
```

| Kol | Koşul |
|---|---|
| **A — fiyat hareket etmemiş** | `z ≤ 0,40` |
| **B — fiyat zaten hareket etmiş** | `z ≥ 0,90` |

0,40–0,90 arası bant **kasten kullanılmaz**; iki kolun ayrık olması karşılaştırmayı
keskinleştirir. Ölü bant genişliği **0,50 σ**.

Okunuşu: A kolu "günlük hareket yarım standart sapmanın bile altında", B kolu
"hareket yaklaşık bir tam standart sapma". Eşikler **şimdi** sabitlenmiştir ve
sonuçlara göre oynatılmayacaktır.

σ20 hesaplanamıyorsa (20 seans yoksa) o seans-isim üretmez. Bir kolda aday yoksa
o seans-isim **her iki koldan da** düşer.

## 4b. Eşikler nasıl seçildi — güç analizi, açık beyan

Bu ön-kaydın ilk taslağında eşikler `z ≤ 0,5` ve `z ≥ 1,5`'ti. Dondurmadan önce
bir **güç analizi** koşuldu ve o çift kol B'yi çökertiyordu.

Analiz **yalnız sayım** yaptı: hiçbir P&L, çıkış ya da sonuç hesaplanmadı.
Ölçülenler, ≥%90 yüzdelik aday seans-isimlerin z dağılımı ve bantlara düşen
sayıları. Aday toplamı **560**, z çeyrekleri %25=0,27 · %50=0,59 · %75=1,00 ·
%90=1,66.

Risk kapısının H01'de yapıların **%23**'ünü geçirdiği ölçümüyle çarpılan
projeksiyon:

| A / B eşiği | A aday | B aday | ~A kayıt | ~B kayıt | Taban 30 |
|---|---|---|---|---|---|
| 0,50 / 1,50 *(ilk taslak)* | 243 | 67 | 55,8 | **15,4** | **TUTMAZ** |
| 0,50 / 1,00 | 243 | 141 | 55,8 | 32,4 | tutar |
| 0,45 / 0,95 | 223 | 158 | 51,2 | 36,3 | tutar |
| **0,40 / 0,90 (seçilen)** | **195** | **171** | **44,8** | **39,3** | **tutar** |

**Kullanılan seçim kuralı (önceden ilan edilir):** ölü bandı en az 0,50 σ tutmak
şartıyla, **zayıf kolun projekte edilen örneklemini en büyük yapan** çift. Bu kural
aynı zamanda en dengeli kolları veriyor (195'e 171) — H03 kolları 134'e 7 kaldığı
için etkiyi ayrıştıramamıştı ve bu tasarım o hatayı tekrarlamamak için var.

**Meşruiyet sınırı, net olarak:** eşiği **örneklem büyüklüğü** gerekçesiyle seçmek
meşrudur ve burada beyan edilmiştir. Eşiği **sonucu iyileştirdiği için** seçmek
yasaktır ve yapılmadı — seçim anında hiçbir kolun P&L'i hesaplanmış değildi.
Koşumdan sonra eşik bir daha değişmeyecektir; değişirse bu yeni bir ön-kayıt ve
yeni bir deneme sayısı gerektirir.

Projeksiyonun kendisi bir tahmindir (risk kapısı oranı H01'den alındı). Gerçek
kayıt sayısı 30'un altına düşerse hüküm **INSUFFICIENT_DATA**'dır; taban aşağı
çekilmeyecektir.

**Yeniden üretme.** Bu tablo elle yazılmadı; betiğin çıktısıdır ve betik depoda:

```bash
uv run python scripts/options_alpha_h04_power.py
```

Betik yalnız sayım yapar ve denenen **sekiz** eşik çiftinin tamamını listeler —
yayımlanan tablo, daha geniş bir aramadan seçilmiş yüze gülen bir satır değil,
aramanın kendisidir. Kota harcamaz: yerel hasadı ve commit'li
`data/study_f/bars.csv`'yi okur.

## 5. Evren, pencere, veri

**Evren:** profildeki on isim. **Pencere:** giriş seansları
**2026-06-12 … 2026-09-15** — H01 ile aynı, 20 seanslık trailing pencerenin
hasadın başlangıcından sonra dolduğu ilk seans.

**Veri kaynağı ve dürüst operasyonelleştirme:** `HYPOTHESES.md` H04'ü
`flow-alerts + ohlc/1d` ile tarif ediyor. Elimdeki 850 dosyalık hasat **zincir**
verisidir, akış-uyarısı değil. Bu çalışma akışı **kontratın seans hacmi** üzerinden
operasyonelleştirir. Bu bir sadeleştirmedir ve sonucun kapsamını daraltır:
sweep/blok ayrımı, agresörlük tarafı ve gün içi zamanlama **yoktur**.

**Dayanak fiyatı: ek istek yok.** Günlük çubuklar depoda **commit'li** duruyor:
`data/study_f/bars.csv` (başlıklı `ticker,day,open,high,low,close,volume,...`,
70 isim, **2025-09-18 … 2026-09-18**). Profildeki on ismin hepsi var ve H04'ün
penceresinde SPY için 88 seans bulunuyor — σ20 için gereken 2026-05-14 öncesi
koşu payı dahil.

Dolayısıyla bu aile **0 UW isteği** harcar ve depodan birebir yeniden üretilir.
Yeni bir çekim yapılmaz; canlı uç noktadan okumak, commit'li dosyayla sonucun
ayrışmasına yol açar.

Çubuklar yalnız **D ve öncesi** için okunur (z hesabı). Tutma günlerinin
değerlemesi dayanaktan değil, zincir hasadından gelir; hasat 2026-09-15'te
bittiği için tutma penceresi zaten oradan sınırlanır.

**Kontrat seçimi:** motorun donmuş kapıları, değiştirilmeden. Yeni parametre yok.

## 6. Birincil ölçüt ve deneme bütçesi

**Birincil (tek):** yapı başına maliyet sonrası net opsiyon P&L, çıkış varyantı
**`time_only`**, **5 işlem günü**.

| # | Deneme |
|---|---|
| 1 | `time_only` (**birincil**) |
| 2 | `time_and_stop` (ikincil) |
| 3 | `time_target_stop` (ikincil) |

**Bu ailede 3.** Kapsam toplamı: H03'ün 3'ü + H01'in 3'ü + H04'ün 3'ü = **9**.
Sonradan kazanan ufka veya varyanta geçilmeyecektir.

## 7. Örneklem tabanı ve hüküm merdiveni

**Taban:** her kolda **en az 30** tamamlanmış yapı. Altındaysa
**INSUFFICIENT_DATA**.

`EXPLORATORY_PASS` **üç şartı birden** ister:

| # | Şart |
|---|---|
| 1 | A kolunun ortalama net P&L'i B kolunu **geçiyor** |
| 2 | A kolu maliyet sonrası **pozitif** |
| 3 | Seans-blok bootstrap %95 aralığı **sıfırı içermiyor** |

Üçünden biri düşerse hüküm **REJECTED**. Hangi şartın düştüğü artefakta
`verdict_failed_clauses` olarak yazılır.

> Bu üç şartlı biçim H01'in ekinden gelmektedir. Orada koşucu yalnız birinci
> şarta bakmış, merdivende olmayan bir etiket üretmiş ve §7'nin istediği
> bootstrap'i hiç hesaplamamıştı. Burada üçü de **koşucunun içinde** sınanır.

**FORWARD_PASS çıkamaz** — B kalite veri.

Karşılaştırma DTE ve delta kovalarında yapılır; tek kollu kova düşer ve düştüğü
raporlanır. Bootstrap birimi **kart değil seans**; aynı seansta girilen kartlar
bağımsız değildir. Tohum **20260923**, tekrar 10.000.

## 8. Bu çalışmanın veremeyeceği hüküm

- Gün içi sıra, fill ve "şu fiyattan dolardı" iddiası yok (B kalite).
- Akış = hacim sadeleştirmesi yüzünden agresörlük ve sweep/blok ayrımı yok.
- 58 giriş seansı; dört çeyreklik rejim taraması değil.
- Evren bugünden seçildi → survivorship; likidite kapıları ayrıca seçim yanlılığı.
- Aynı hasadın **üçüncü** kez sorgulanması. Toplam 9 denemelik bir aramada tek
  bir ailenin eşiği geçmesi tek başına kanıt değildir.

## 9. Önceden yazılmış olumsuz sonuç cümlesi

Sonuç ne çıkarsa çıksın rapor şu iki cümleyi **ayrı** kuracaktır:

1. **İşlenebilirlik:** kol maliyet sonrası pozitif mi.
2. **Göreli etki:** fark sıfırdan ayırt edilebiliyor mu.

Biri diğerini ima etmez. "Kontrolü geçti" tek başına geçiş değildir.
