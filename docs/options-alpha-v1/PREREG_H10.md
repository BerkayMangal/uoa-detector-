# Ön-kayıt — H10: uzun prim ucuzken mi alınır

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, tek bir P&L
görülmeden. Koşucu yazılmadan sabitlenir ve sonuçlara göre değiştirilmez.

---

## 1. Bu aile neyi diriltmiyor

Bu proje **vol primini maliyet sonrası işlenemez** ilan etti
(`docs/edge_to_money.md`) ve koşullama replikasyonunu WEAK buldu
(`docs/study_D_result.md`). Bunlar vol **satma** tarafına ilişkin hükümlerdir.

H10 karşı taraftır: **uzun prim satın almak**. İkisi birbirinin tersi değil —
vol satmanın maliyet sonrası çalışmaması, vol almanın çalıştığını ima etmez;
makas ve komisyon **her iki** yönde de aleyhtedir. Bu aile reddedilmiş bir kuralın
yeni adla geri getirilmesi **değildir**. Hüküm ne çıkarsa çıksın
`edge_to_money.md`'nin hükmü **değişmez**; o ayrı bir sorudur.

Sıralama: `HYPOTHESES.md` H03 → H01 → H04 → H06 → **H10** diyor. İlk üçü koştu ve
reddedildi; H06 ölçüldü ve dondurulmadı (`INFEASIBLE_H06.md`). Sıra korunuyor.

## 2. Hipotez

Bir kontratın örtük oynaklığı, dayanağın **kendi gerçekleşen** oynaklığına göre
ucuzsa, onu satın almanın sonraki net opsiyon P&L'i, örtük oynaklığın pahalı
olduğu durumdan **iyi** olur.

```
ucuzluk = IV(kontrat, D) / gerceklesen_vol20(dayanak, D)
gerceklesen_vol20 = D'den ONCEKI 20 gunluk getirinin std'si × sqrt(252)
```

**Yön şimdi sabitlenir:** ucuz kol (A) > pahalı kol (B). Ters çıkarsa bu hipotezin
**reddidir**; ters yön "aslında şunu bulduk" diye sunulamaz.

**Başarısızlık ihtimali dürüstçe:** IV genelde gerçekleşenin üstündedir (prim
budur), dolayısıyla "ucuz" dilim düşük IV rejimini seçiyor olabilir. Ayrıca ucuzluk
oranı, dayanağın yakın geçmişte sakin olmasını seçebilir — H04'te tam bu tür bir
seçimin işe yaramadığı görüldü. Ve §4b'de ölçülen makas farkı kol A'nın aleyhinedir.

## 3. Bilgi zamanı

| An | Ne biliniyor |
|---|---|
| Seans **D** kapanışı | D'nin zinciri ve IV'si henüz yayımlanmamış sayılır |
| Seans **D+1** | D'nin zinciri yayımlanmış |
| Seans **D+1** kapanışı | **GİRİŞ burada** |

Tutma **5 işlem günü**. `gerceklesen_vol20` yalnız **D'den önceki** getirilerden
hesaplanır; D'nin kendi getirisi dahil **edilmez** — kendi ölçeklediği hareketi
içeren bir pencere, hareket büyüdükçe küçülür ve hareketin düştüğü kolu kayırır.

## 4. Kolların tanımı (dondurulmuş)

Ucuzluk **seans düzeyinde** atanır. Gerekçesi §4b'de ölçülmüştür: oranın paydası
(dayanağın gerçekleşen vol'ü) bir seans-ismin **tüm** kontratlarında aynıdır, skew
ve term oranı yalnız biraz oynatır, seanslar arası değişim ise büyüktür.

Her (isim, seans) için **temsilci**: o seansın uygunluk kapılarını geçen
kontratları arasında **hacmi en yüksek** olanı. Çalışmanın zaten işleme sokacağı
aday budur.

| Kol | Koşul |
|---|---|
| **A — ucuz** | `ucuzluk ≤ 0,90` |
| **B — pahalı** | `ucuzluk ≥ 1,10` |

0,90–1,10 arası bant **kasten kullanılmaz**; genişliği **0,20** orandır. Eşikler
**şimdi** sabitlenmiştir.

`gerceklesen_vol20` hesaplanamıyorsa, IV yoksa, delta yoksa ya da makas
hesaplanamıyorsa o seans-isim üretmez.

## 4b. Eşikler ve yapı nasıl seçildi — güç analizi, açık beyan

`scripts/options_alpha_h10_power.py`, **yalnız sayım**: hiçbir P&L, çıkış ya da
sonuç hesaplanmadı.

**Birinci yapı (kontrat düzeyi, aynı-seans eşlemeli) çöktü.** 58.787 uygun
kontratta 22.401 ucuz / 13.584 pahalı olmasına rağmen, **aynı** seans-isimde
ikisini birden bulmak nadir: en iyi çiftte **68** eşli seans-isim → ~15,6 kayıt,
taban 30. Sebep yapısal — payda seans içinde sabit.

Bu, taslağımdaki bir iddiayı çürüttü: "ucuzluk kontrat düzeyinde bir değişkendir"
**yanlıştı**. Pratikte seans düzeyinde bir değişken, tıpkı H04'ün z'si gibi.

**İkinci yapı (seans düzeyi) tabanı tutuyor.** 790 temsilci seans-isim:

| A ≤ / B ≥ | A seans | B seans | ~A kayıt | ~B kayıt | makas A−B | Taban 30 |
|---|---|---|---|---|---|---|
| 0,90 / 1,20 | 338 | 131 | 77,7 | 30,1 | +0,8 | tutar |
| 0,95 / 1,15 | 395 | 172 | 90,8 | 39,5 | +0,8 | tutar |
| **0,90 / 1,10 (seçilen)** | **338** | **223** | **77,7** | **51,2** | **+0,7** | **tutar** |
| 1,00 / 1,30 | 461 | 81 | 105,9 | 18,6 | +0,8 | tutmaz |
| 0,85 / 1,25 | 275 | 109 | 63,2 | 25,0 | +0,9 | tutmaz |
| 1,00 / 1,20 | 461 | 131 | 105,9 | 30,1 | +0,8 | tutar |
| 0,95 / 1,25 | 395 | 109 | 90,8 | 25,0 | +0,8 | tutmaz |
| 0,80 / 1,20 | 231 | 131 | 53,1 | 30,1 | +1,0 | tutar |

**Kullanılan seçim kuralı (H04'te ilan edilenin aynısı):** ölü bandı en az 0,20
oran tutmak şartıyla, **zayıf kolun projekte edilen örneklemini en büyük yapan**
çift. Bu 0,90/1,10'u veriyor (zayıf kol ~51,2) ve aynı zamanda en dengeli kolları.

**Meşruiyet sınırı:** eşik ve yapı **örneklem büyüklüğü** gerekçesiyle seçildi ve
burada beyan edildi; **sonucu iyileştirdiği için** değil — seçim anında hiçbir kolun
P&L'i hesaplanmamıştı. İki yapısal varyantta duruldu; denenen sekiz çiftin tamamı
yukarıda, aramanın kendisi olarak.

**Bedeli adıyla:** aynı-gün kontrolü kayboldu. Kollar artık farklı piyasa
günlerinden gelebiliyor ve piyasa-günü etkisi farkın içine karışabilir. Bootstrap
birimi bu yüzden seans kalıyor (§7).

### 4b.1 Makas farkı — yönü ve sonuca etkisi, önceden bağlanır

Bileşim teşhisi (§7'nin şartı) seçilen çiftte şunu veriyor:

| Kol | n | IV | makas (mid %) | DTE | delta |
|---|---|---|---|---|---|
| A (ucuz) | 338 | 0,35 | **%3,5** | 28 | 0,41 |
| B (pahalı) | 223 | 0,32 | **%2,8** | 30 | 0,38 |

Ucuz kol sistematik olarak **daha geniş** makasa düşüyor (sekiz çiftin hepsinde
+0,7 ila +1,0 puan). DTE ve delta ise neredeyse eşleşmiş.

H06 bu noktada dondurulmamıştı; aradaki fark **yön**. Takvim confound'u her iki
tarafı da imal edebiliyordu. Burada confound tek taraflı ve **kol A'nın
aleyhine**: maliyet modeli makası giriş/çıkış fiyatlarının içinde tahsil ettiği
için ucuz kol handikaplı başlıyor.

Bu yüzden yorum kuralı **şimdi** bağlanır:

- **A kolu kazanırsa:** bulgu **muhafazakârdır** — daha geniş makasa rağmen kazandı.
- **A kolu kaybederse:** kaybın bir kısmı maliyettir ve rapor bunu "ucuzluk işe
  yaramıyor" diye **değil**, "maliyet dezavantajı ayrıştırılamadı" diye yazacaktır.
- Her iki hâlde kolların ortalama makas, IV, DTE ve delta değerleri **raporlanır**.

## 5. Evren, pencere, veri

**Evren:** profildeki on isim. **Pencere:** giriş seansları
**2026-06-12 … 2026-09-15** (H01/H04 ile aynı).

**Veri:** yerel hasat (`implied_volatility` alanı zaten saklanıyor) + commit'li
`data/study_f/bars.csv`. **0 UW isteği.**

**Kalite şerhi:** IV, sağlayıcının kendi modeline göre hesaplanmış bir alandır ve
`last_tape_time` sorunu onu da kapsar. Bu aile "IV yanlış fiyatlanmıştı" demez;
"bu alanın verdiği ucuzluk sıralaması sonraki P&L'i ayırıyor mu" diye sorar.

**Aday seçimi:** motorun donmuş kapıları, değiştirilmeden. H01/H04'ün hacim
yüzdeliği filtresi **kullanılmaz** — bu aile akış hakkında değil, primin fiyatı
hakkında. Bu, diğer ailelerle birebir karşılaştırmayı zorlaştırır ve rapor bunu
belirtir.

## 6. Birincil ölçüt ve deneme bütçesi

**Birincil (tek):** yapı başına maliyet sonrası net opsiyon P&L, çıkış
**`time_only`**, 5 işlem günü.

| # | Deneme |
|---|---|
| 1 | `time_only` (**birincil**) |
| 2 | `time_and_stop` (ikincil) |
| 3 | `time_target_stop` (ikincil) |

**Bu ailede 3.** Kapsam toplamı 9 + 3 = **12**. H06 koşulmadığı için deneme
tüketmedi.

## 7. Örneklem tabanı ve hüküm merdiveni

**Taban:** her kolda en az **30** tamamlanmış yapı. Altındaysa
**INSUFFICIENT_DATA**.

`EXPLORATORY_PASS` **üç şartı birden** ister:

| # | Şart |
|---|---|
| 1 | A kolunun ortalama net P&L'i B kolunu **geçiyor** |
| 2 | A kolu maliyet sonrası **pozitif** |
| 3 | Seans-blok bootstrap %95 aralığı **sıfırı içermiyor** |

Biri düşerse **REJECTED**; düşen şartlar artefakta `verdict_failed_clauses` olarak
yazılır. **FORWARD_PASS çıkamaz** — B kalite veri.

Bootstrap birimi **kart değil seans**, tohum **20260923**, tekrar 10.000.
Karşılaştırma DTE ve delta kovalarında; tek kollu kova düşer ve raporlanır.

**Zorunlu ek teşhis:** kolların ortalama IV, makas yüzdesi, DTE ve delta değerleri
raporlanır (§4b.1'in yorum kuralı bunlara dayanır).

## 8. Bu çalışmanın veremeyeceği hüküm

- Gün içi sıra, fill, "şu fiyattan dolardı" yok (B kalite).
- `edge_to_money.md`'nin vol-satma hükmü hakkında hiçbir şey söylemez.
- Aynı-gün kontrolü yok (§4b); piyasa-günü etkisi farkın içinde olabilir.
- Aynı hasadın dördüncü kez sorgulanması; 12 denemelik aramada tek aile tek başına
  kanıt değildir.
- Evren bugünden seçildi → survivorship; likidite kapıları ayrıca seçim yanlılığı.

## 9. Önceden yazılmış sonuç cümlesi

İki cümle **ayrı** kurulacak: **işlenebilirlik** (kol maliyet sonrası pozitif mi) ve
**göreli etki** (fark sıfırdan ayırt edilebiliyor mu). Biri diğerini ima etmez.
"Kontrolü geçti" tek başına geçiş değildir.
