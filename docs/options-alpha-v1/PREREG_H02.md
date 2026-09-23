# Ön-kayıt — H02: tekrarlayan akış

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, tek bir P&L
görülmeden. Koşucu yazılmadan sabitlenir ve sonuçlara göre değiştirilmez.

---

## 1. Neden bu aile

`HYPOTHESES.md` sıralaması: H03 → H01 → H04 → H06 → H10 → **H02**. İlk üçü koştu ve
reddedildi, H06 ölçülüp dondurulmadı (`INFEASIBLE_H06.md`), H10 koştu ve
INSUFFICIENT_DATA verdi (`RESULT_H10.md`). Sıra korunuyor.

## 2. Hipotez

Bir kontratta olağandışı hacim **tek seferlik** değil, **tekrarlıyorsa** — aynı
kontrat önceki seanslarda da olağandışı hacim göstermişse — sonraki net opsiyon
P&L'i tek seferlik olağandışı hacimden **iyi** olur.

**Yön şimdi sabitlenir:** tekrarlayan (kol A) > tek seferlik (kol B). Ters çıkarsa bu
hipotezin **reddidir**; ters yön "aslında şunu bulduk" diye sunulmaz.

**Ekonomik gerekçe:** tek günlük sıçrama gürültü, hedge ya da takvim olabilir. Aynı
kontrata birden fazla seans boyunca **dönen** ilgi, pozisyon kuruluyor olmasına daha
yakındır.

**Başarısızlık ihtimali dürüstçe:** tekrar, likiditenin kendisini seçebilir — en likit
kontrat her gün en yüksek hacimlidir ve "tekrar" sadece "bu QQQ kontratı hep işlem
görüyor" demek olabilir. §4b bunu **ölçtü** ve yığılma bulmadı, ama §7 teşhisi
koşumda da raporlanacak.

## 3. Bölünmüş sweep sorunu — ve neden bu tasarımda yok

`HYPOTHESES.md` H02 için şunu uyarmıştı: *"bölünmüş sweep'leri bağımsız sayma riski
tasarımda çözülmeli."* Tek bir büyük emir onlarca print'e bölünürse `flow-alerts`
üzerinden bakan bir tasarım bunu "tekrarlayan ilgi" sanır.

Bu çalışma `flow-alerts` **kullanmıyor**: akışı **günlük zincir hacmi** üzerinden
okuyor ve günlük hacim gün içi bölünmeleri zaten toplamış durumda. Sorun tasarımda
çözülmüyor, **ortaya çıkmıyor**.

Bedeli açıkça: agresörlük tarafı, sweep/blok ayrımı ve gün içi zamanlama **yok**.
Dolayısıyla bu aile "kurumsal sweep tekrarı" hakkında değil, "aynı kontrata birden
fazla gün olağandışı hacim dönmesi" hakkındadır. İkisi farklı iddia.

## 4. Kolların tanımı (dondurulmuş)

Olağandışılık ölçüsü H01'in aynısı, değiştirilmeden: kontratın seans hacminin, o
hissenin önceki **20 seansındaki** uygun kontrat hacimleri dağılımındaki yüzdeliği.
Seans D'de yüzdeliği **≥ 90** olan kontratlar aday havuzudur.

Kolu belirleyen değişken, aynı kontratın **geçmiş tekrarı**:

```
tekrar(kontrat, D) = D-1 .. D-5 arasinda yuzdeligi >= 90 olan seans sayisi
```

| Kol | Koşul |
|---|---|
| **A — tekrarlayan** | `tekrar ≥ 2` |
| **B — tek seferlik** | `tekrar = 0` |

Geriye bakış **k = 5**. `tekrar = 1` **kasten kullanılmaz** — ölü bant {1}. Eşikler
**şimdi** sabitlenmiştir.

**Yapı: aynı-seans EŞLEMELİ.** Bir seans-isim ancak **her iki** kolda da aday
taşıyorsa üretir; karşılaştırma böylece aynı isim ve aynı gün içinde yapılır ve
piyasa-günü etkisi farkın içinden çıkar. Bir kolda aday yoksa o seans-isim **her iki
koldan da** düşer.

Bu cümle burada **anlamlıdır** ve §4b'de ölçülmüştür: tekrar sayısı kontratın kendi
geçmişinden gelir, yani gerçekten **kontrat düzeyinde** bir değişkendir. H06'da vade
payları, H10'da ucuzluk oranı seans düzeyine çökmüştü (paydaları seans içinde
sabitti) ve eşleme örneklemi açlıktan öldürmüştü; burada 197 eşli seans-isim var.

**Aday seçimi:** seans-isim-kol başına tek aday — o kolun koşulunu sağlayanlar
arasından **hacmi en yüksek** kontrat. Yedek seçim yok.

## 4b. Eşikler ve yapı nasıl seçildi — güç analizi, açık beyan

`scripts/options_alpha_h02_power.py`, **yalnız sayım**: hiçbir P&L, çıkış ya da sonuç
hesaplanmadı.

Zemin: ≥%90 kümesi olan **650** seans-isim; küme büyüklüğü ortalama 7,2, medyan 5.

**Kova elemesi varsayılmadı, girişte fiilen kontrol edildi.** Bu, H10'un koşumunun
açığa çıkardığı boşluğun düzeltilmesidir: orada güç analizi kol atanmış seans-isim
sayısını yalnız risk kapısı oranıyla çarpmış, ankrajın girişte hâlâ listelenmesi,
delta taşıması ve hem DTE 14–60 hem |delta| 0,25–0,70 kovasına düşmesi şartını
atlamıştı; uçtan uca oran %23 yerine %13 çıkmış ve bir kol tabanı iki kayıt
kaçırmıştı. Bu betik o kontrolü yapıyor.

| k | A ≥ | A → kova sonrası | B → kova sonrası | eşli → hayatta | ~kayıt/kol | Taban 30 |
|---|---|---|---|---|---|---|
| 3 | 1 | 484 → 378 | 437 → 324 | 388 → 234 | 53,8 | tutar, **ölü bant yok** |
| 3 | 2 | 395 → 317 | 437 → 324 | 318 → 197 | **45,3** | tutar |
| 3 | 3 | 251 → 198 | 437 → 324 | 203 → 127 | 29,2 | **tutmaz** |
| 5 | 1 | 480 → 371 | 400 → 298 | 367 → 223 | 51,2 | tutar, **ölü bant yok** |
| **5** | **2** | **416 → 328** | **400 → 298** | **319 → 197** | **45,3** | **tutar (seçilen)** |
| 5 | 3 | 325 → 255 | 400 → 298 | 254 → 152 | 34,9 | tutar |
| 10 | 1 | 446 → 333 | 348 → 264 | 326 → 194 | 44,6 | tutar, **ölü bant yok** |
| 10 | 2 | 416 → 316 | 348 → 264 | 308 → 187 | 43,0 | tutar |
| 10 | 3 | 371 → 284 | 348 → 264 | 273 → 165 | 37,9 | tutar |

Kova elemesi gerçek ve küçük değil: seçilen çiftte kol A **%79**, kol B **%75**
hayatta kalıyor, eşli çiftlerde **%62**. H10'un betiği bunu %100 varsayıyordu.

**Seçim kuralı (önceden ilan edilir):** ölü bant korunmak şartıyla **zayıf kolun**
projekte edilen örneklemini en büyük yapan çift. `A ≥ 1` satırları ölü bant
bırakmadığı için elenir (§4 "tekrar = 1 kasten kullanılmaz").

**Eşitlik kuralı (önceden ilan edilir):** k=3/A≥2 ile k=5/A≥2 aynı projeksiyonu
(45,3) veriyor. Eşitlik, **tutma ufkuyla eşleşen geriye bakış** lehine kırılır: iddia
"**dönen** ilgi"dir ve 3 seansta 2 isabet "dönen" değil "ardışık" demektir; tutma
süresi de 5 işlem günü. Seçilen: **k=5**.

### 4b.1 Likidite yığılması ölçüldü — yok

Seçilen çiftte (k=5, A≥2), kova elemesinden sonra isim dağılımı:

| Kol | En çok görülen üç isim | En büyük pay |
|---|---|---|
| A (tekrarlayan) | QQQ 44 · NVDA 43 · SPY 39 | **%13** |
| B (tek seferlik) | QQQ 41 · SPY 36 · NVDA 33 | — |

Uyarı eşiği %50'ydi; en büyük pay %13 ve **iki kolun karışımı birbirine benziyor**.
Yani "tekrar" bu veride tek bir likit ismi seçmiyor. H06 tam bu noktada
dondurulmamıştı (kol A'nın %65–67'si aylık vadeydi); burada o confound
**gerçekleşmedi**.

**Meşruiyet sınırı:** eşik, geriye bakış ve yapı **örneklem büyüklüğü** ve
**ölçülmüş bileşim** gerekçesiyle seçildi, sonucu iyileştirdiği için değil — seçim
anında hiçbir kolun P&L'i hesaplanmamıştı. Denenen dokuz kombinasyonun tamamı
yukarıda, aramanın kendisi olarak.

## 5. Bilgi zamanı

| An | Ne biliniyor |
|---|---|
| Seans **D** kapanışı | D'nin hacmi henüz tamamlanmadı |
| Seans **D+1** | D'nin hacmi yayımlandı |
| Seans **D+1** kapanışı | **GİRİŞ burada** |

Tutma **5 işlem günü**. Hem yüzdelik dağılımı hem tekrar sayısı yalnız **D ve
öncesinden** okunur; D+1'in kendi verisi kol kararında **kullanılmaz**.

## 6. Evren, pencere, veri

**Evren:** profildeki on isim. **Pencere:** giriş seansları
2026-06-12 … 2026-09-15 (H01/H04/H10 ile aynı).

**Veri:** yalnız yerel hasat (850 dosya). **0 UW isteği.**

**Kontrat seçimi:** motorun donmuş kapıları, değiştirilmeden. Yeni parametre yok.

## 7. Birincil ölçüt, taban ve hüküm merdiveni

**Birincil (tek):** yapı başına maliyet sonrası net opsiyon P&L, çıkış
**`time_only`**, 5 işlem günü.

| # | Deneme |
|---|---|
| 1 | `time_only` (**birincil**) |
| 2 | `time_and_stop` (ikincil) |
| 3 | `time_target_stop` (ikincil) |

**Bu ailede 3.** Kapsam toplamı 12 + 3 = **15**. H06 koşulmadığı için deneme
tüketmedi.

**Taban:** her kolda en az **30** tamamlanmış yapı. Altındaysa **INSUFFICIENT_DATA**.

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

**Zorunlu ek teşhis (koşumda da):** kolların ortalama hacmi, açık pozisyonu, makas
yüzdesi, IV, DTE, delta **ve isim dağılımı**. Kol A'nın en büyük isim payı **%50'yi
aşarsa**, sonuç "tekrar işe yarıyor" diye **değil**, **likidite seçimi
ayrıştırılamadı** diye yazılır — §4b.1 bunu şimdi %13 ölçtü, koşum teyit edecek.

## 8. Bu çalışmanın veremeyeceği hüküm

- Gün içi sıra, fill, "şu fiyattan dolardı" yok (B kalite).
- Agresörlük tarafı ve sweep/blok ayrımı yok (§3).
- Aynı hasadın **beşinci** kez sorgulanması; 15 denemelik aramada tek bir ailenin
  eşiği geçmesi tek başına kanıt değildir.
- Evren bugünden seçildi → survivorship; likidite kapıları ayrıca seçim yanlılığı.
- 850 dosyalık pencere dört çeyreklik rejim taraması değil.

## 9. Önceden yazılmış sonuç cümlesi

İki cümle **ayrı** kurulacak: **işlenebilirlik** (kol maliyet sonrası pozitif mi) ve
**göreli etki** (fark sıfırdan ayırt edilebiliyor mu). Biri diğerini ima etmez.
"Kontrolü geçti" tek başına geçiş değildir.
