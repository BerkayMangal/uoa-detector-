# options-alpha-v1 — durum

**Faz 5.24** · dal `p77-options-alpha-h10-run` · taban `main` 3eccb18
**Son güncelleme:** 2026-09-23 — H10 koştu

> Bu başlık bir süre bayat kaldı (`p71` / `eb8b766` yazıyordu) ve arada dört PR
> merge edildi. Doğru olan, `main`'in canlı SHA'sıdır: `curl -s .../health`.

Ana ürün **opsiyon sinyal terminali**. Hisse tarafı yalnız karşılaştırma kolu.

---

## Kilometre taşları

| # | İş | Durum | Kanıt |
|---|---|---|---|
| **M0** | API yetenek matrisi | **DONE** (merge, canlı) | `capability_matrix.json` · 9/9 VERIFIED |
| **M1** | Uçtan uca: veri → aday → kontrat/spread → maliyet/risk → PAPER kart | **DONE** (merge, canlı) | `replay/2026-09-22/paper_card_{SPY,QQQ}.json` |
| **M2** | Dört temel yapı | **KISMEN** | `structures.py` dördünü de fiyatlıyor ve test ediyor; seçici long + dikey debit üretiyor |
| **M3** | Çıkış motoru + gerçek sonuç kaydı | **DONE** (merge, canlı) | `exits.py` · 9 elle-hesap test · `replay/2026-09-08/*_outcome.json` |
| **M4** | 12 hipotez ailesine veri fizibilitesi | **DONE** | `HYPOTHESES.md` — 8 tam, 3 kısıtlı, 0 erişilemez |
| **M5** | İlk aileyi koş ve hükme bağla | **DONE — REDDEDİLDİ** | `RESULT_H03.md` · `h03_result.json` |
| **M5b** | İkinci aile (bağımsız kol değişkeni) | **DONE — REDDEDİLDİ** | `RESULT_H01.md` · `h01_result.json` |
| **M5c** | Üçüncü aile (akış öncü mü, tepki mi) | **DONE — REDDEDİLDİ** | `RESULT_H04.md` · `h04_result.json` |
| **M5d** | Dördüncü aile (prim ucuzken mi alınır) | **KOŞTU — INSUFFICIENT_DATA** | `RESULT_H10.md` · `h10_result.json` |
| **M6** | Track B replikasyonu | **ENGEL ÖLÇÜLDÜ** | Pencere ~95 seans, şartı 4 çeyrek → kısmi test / yetersiz süre |
| **M7** | Ayrı opsiyon ekranı + tarayıcı doğrulaması | **KISMEN** | `/opsiyon` kodlandı + test edildi; *gözle* canlı doğrulama B1'e bağlı |
| **M8** | Mutasyon kanıtı tablosu | **DONE** | `mutation_proof.md` — 7/7 koruma kırmızıya döndü, 0 sağ kalan |

---

## M8 — korumalar gerçekten kırılabiliyor mu

Yeşil test paketi, bir korumanın bir şeyi koruduğunun kanıtı değildir. Bu proje
kırılamayan nöbetçileri defalarca yakaladı — ve bu kapsamda da bir tane çıktı:
belirsizlik dalı günlük **kapanışı** iki eşiğe karşı sınıyordu, oysa kapanış
ikisinin birden tarafında olamaz.

Yedi kritik koruma kasten bozuldu; her birinde **adı konmuş** testin kırmızıya
döndüğü, sonra dosyanın **bayt-birebir** geri alındığı doğrulandı.

| Koruma | Mutasyon | Sonuç |
|---|---|---|
| Long girişi ask'ten fiyatlanır | giriş tarafını çıkış tarafıyla değiştir | test kırmızı |
| Çaprazlanmış kotasyon reddedilir | kontrolü `if False:` yap | test kırmızı |
| Bütçeye sığmayan yapı sıfır adettir | adedi `max(1, …)` ile yukarı zorla | test kırmızı |
| Hedef yapının tavanını aşamaz | tavan sınırını kaldır | test kırmızı |
| Replay kartı giriş olarak sunulmaz | durumu hep `PAPER_ENTRY_READY` yap | test kırmızı |
| Aynı gün iki eşik: sıra uydurulmaz | stop yerine hedefi uygula | test kırmızı |
| Fiyatlanamayan pozisyon başarılı sayılmaz | `NO_EXIT_DATA` yerine `TIME` yaz | test kırmızı |

**7 mutasyon, 0 sağ kalan.** Betik ayrıca mutasyondan önce testin zaten yeşil
olduğunu doğruluyor — kırmızı bir testin mutasyonla kırmızı kalması hiçbir şey
kanıtlamaz. Satır silme mutasyonu kullanılmadı: ayrıştırılamayan dosya, testin
kırılabildiğini göstermez.

```
uv run python scripts/options_alpha_mutation_proof.py
```

---

## M5 — H03 koştu ve reddedildi

**Hipotez:** akışın kontratında ertesi yayımda açık pozisyon **artmışsa** (pozisyon
kapanmayıp açılmışsa), sonraki net opsiyon P&L'i teyitsiz eşleştirilmiş
kontratlardan iyi olur.

**Pencere:** 2026-05-14 … 2026-09-15, **85 seans**, 10 isim — ön-kayıttaki gibi.

### Eleme zinciri

1.544.478 kontrat-satırı tarandı → **58.787** uygun → **554** yapı kuruldu →
1.108 fiyatlandı (her aday long + spread) → **141** risk kapısını geçti.

### Kollar (birincil: `time_only`, 5 işlem günü, maliyet sonrası)

| Kol | n | Ortalama | Medyan | Kazanma |
|---|---|---|---|---|
| Teyitli (OI arttı) | **134** | **−75,31 $** | −46,90 $ | %7,5 |
| Kontrol (OI artmadı) | **7** | **−52,57 $** | −49,60 $ | %0 |

**Teyitli kol kontrolden kötü → REDDEDİLDİ.**

### Ek 3 koşumdan önce bunu söylemişti

Seçim filtresi (`volume(D) > OI(D)`) ile kolları ayıran değişken
(`OI(D+1) > OI(D)`) mekanik olarak korele. Açık pozisyon gün başı duruşu olduğu
için, hacmi onu aşan bir kontratta hacmin çoğu zaten pozisyon açıyor demektir.

Gerçekleşen: **134'e 7**, ve yedi kovanın altısı tek kollu kaldığı için düştü.

Dolayısıyla dürüst tam hüküm: **birincil ölçütte reddedildi, ve bu tasarım teyit
sinyalinin kendine özgü bilgisini ayrıştıramadı.** İkisi farklı cümle.

### Reddedilen cazibe

| Varyant | Teyitli | Kontrol |
|---|---|---|
| `time_only` **(birincil)** | −75,31 $ | −52,57 $ |
| `time_and_stop` | −34,23 $ | −46,71 $ |
| `time_target_stop` | −34,25 $ | −46,71 $ |

İkincil varyantlarda teyitli kol **daha iyi** görünüyor. Birincil ölçüt veriye
bakılmadan sabitlenmişti; kazanana geçmedim. Takip edilecekse yeni ön-kayıt ve
seçim maliyetinin tasarıma dahil edilmesi gerekir.

### Yapısal gerçek

554 yapının yalnız 141'i risk kapısını geçti; sebep tek — bir yapı 100 $ bütçeyi
aşıyor (örn. 221,60 $). Bu veri ya da hipotez sorunu değil, **sermaye ölçeği**.
Bütçe bu çalışma için büyütülmedi.

---

## M5b — H01 koştu ve reddedildi

**Hipotez:** bir kontratın seans hacmi **kendi hissesinin** son 20 seanslık
dağılımına göre olağandışı yüksekse (≥ %90'lık), sonraki net opsiyon P&L'i normal
seviyedeki (%40–60) kontratlardan iyi olur.

H03'ün yapısal kusuru burada kasten düzeltildi: **uygunluk kapıları** hangi
kontratın işlenebilir olduğunu, **ayrı** bir değişken (göreli hacim yüzdeliği)
hangi kola gireceğini söylüyor. H03'te seçim filtresiyle kol değişkeni mekanik
korelasyondaydı ve kontrol kolu 7'de kalmıştı; burada **111'e 91**.

**Giriş D+1 kapanışı** — bir seansın hacmi ancak o seans kapandıktan sonra
tamamlanır. Ara bant (%60–90) kasten kullanılmadı.

### Eleme zinciri

1.655.820 satır tarandı → **62.615** uygun → **879** yapı kuruldu → 1.758
fiyatlandı → **202** risk kapısını geçti. Kayıt sayısı tam 202.

### Kollar (birincil: `time_only`, 5 işlem günü, maliyet sonrası)

| Kol | n | Ortalama | Medyan | Kazanma |
|---|---|---|---|---|
| Yüksek (≥%90) | **111** | **−54,52 $** | −45,20 $ | %10,8 |
| Kontrol (%40–60) | **91** | **−70,75 $** | −46,80 $ | %4,4 |

### Neden bu bir geçiş değil

`EXPLORATORY_PASS` üç şartı **birden** istiyordu; ikisi düştü:

| §7 şartı | Durum |
|---|---|
| Kontrolü geçiyor | sağlandı |
| Maliyet sonrası **pozitif** | **DÜŞTÜ** (−54,52 $, 111'de 12 kazanan) |
| Bootstrap aralığı sıfırı **içermiyor** | **DÜŞTÜ** ([−3,28, +37,58]) |

Üç teşhis ham +16,23 $'lık farkı çözüyor: **medyan farkı yalnız +1,60 $**
(fark merkezde değil kuyrukta), kontrolün tek −699,20 $'lık işlemi çıkarılınca
fark **+9,25 $**'a iniyor, ve **en kalabalık kovada yüksek kol daha kötü**
(−49,13'e −45,89). Bileşim ayıklanınca fark +11,57 $ ve yüksek kol 6 kovanın
5'inde iyi — yani etki tamamen Simpson değil, ama ağırlığın en büyük olduğu tek
kovada işaret ters.

Dolayısıyla dürüst tam hüküm: **işlenebilirlik reddedildi** (iki kol da ağır
negatif) **ve göreli etki kurulamadı** (aralık sıfırı içeriyor). İkisi farklı
cümle.

### Kendi koşucumda bulunan kusur

İlk hâli §7'yi yanlış uyguluyordu: yalnız birinci şarta bakıyor, merdivende
**var olmayan** bir etiket (`EXPLORATORY_PASS_CANDIDATE`) üretiyor, ve §7'nin
açıkça istediği bootstrap'i **hiç hesaplamıyordu**. Düzeltilmeseydi bu aile
yanlış etiketle yayımlanacaktı.

---

## M5c — H04 koştu, yön hipotezin tersine çıktı

**Hipotez:** olağandışı hacim varken dayanak **henüz hareket etmemişse** akış
fiyatı önceliyor olabilir (kol A); zaten hareket etmişse akış **tepki**dir (kol B).
Ön-kayıtta yön sabitlendi: A, B'yi geçecek.

| Kol | n | Ortalama | Medyan | Kazanma | Ort. z |
|---|---|---|---|---|---|
| A — hareket etmemiş (z ≤ 0,40) | **42** | **−60,47 $** | −40,90 $ | %11,9 | 0,196 |
| B — zaten hareket etmiş (z ≥ 0,90) | **33** | **−51,93 $** | −46,60 $ | %15,2 | 1,616 |

**A < B, yani yön tahminin tersi.** Ön-kayıt bunu önceden bağlamıştı: ters yön
hipotezin **reddidir**, "aslında şunu bulduk" diye sunulamaz. §7'nin üç şartının
**üçü** düştü (geçmiyor · pozitif değil · aralık [−44,42, +25,70] sıfırı içeriyor).

**Simpson yok:** en kalabalık kovada da (n=24'e 25) B daha iyi; yön toplamla
tutarlı. H01'de manşeti çeviren kova sorunu burada yaşanmadı, red daha net.

### Güç analizi çalışmayı kurtardı

Taslak bantlar `z ≤ 0,5 / z ≥ 1,5`'ti ve B kolunu ~15 kayıtta bırakıyordu → hüküm
**INSUFFICIENT_DATA** olacak, hipotez sınanamadan kapanacaktı. Eşikler
dondurmadan **önce**, yalnız **sayım** üzerinden 0,40/0,90'a çekildi. Gerçekleşen
42 ve 33; taban tuttu. Çapraz doğrulama: güç betiği ölü banda 194 aday demişti,
koşucu bağımsız olarak **194** buldu.

### Provenance H01'den güçlü

Ön-kayıt **kendi commit'iyle** (`17677a5`) ve **kendi merge'iyle** (`faf6982`),
koşucu dosyası var olmadan dondurulmuştur. **Ek yazılmadı** — H03 üç, H01 bir ek
gerektirmişti.

---

## M5d — H10 koştu, taban iki kayıt eksik kaldı

**Hipotez:** bir kontratın örtük oynaklığı dayanağın kendi gerçekleşen oynaklığına
göre **ucuzsa**, onu satın almanın sonraki net P&L'i pahalı olduğu durumdan iyi olur.
Yön ön-kayıtta sabitlendi: A (ucuz) > B (pahalı).

**Hüküm: INSUFFICIENT_DATA.** §7 her kolda en az 30 yapı istiyordu.

| Kol | n | Ortalama | Medyan | Kazanma | Ort. ucuzluk | Ort. makas |
|---|---|---|---|---|---|---|
| A (ucuz, ≤0,90) | **45** | −49,33 $ | −51,60 $ | %2,2 | 0,746 | %2,10 |
| B (pahalı, ≥1,10) | **28** | −49,78 $ | −40,20 $ | %17,9 | 1,316 | %1,93 |

Taban kapısı üç şartlı merdivenden **önce** gelir; şartlar hiç değerlendirilmedi.

### Zaten gösterilecek bir etki yok

Fark **+0,45 $**, bootstrap aralığı **[−30,88, +37,56]**, farkın ≤0 çıkma oranı
**0,501** — yazı tura. İki kol da yapı başına ~−49 $. Taban tutsaydı 1. şart 0,45 $
ile teknik olarak sağlanır, 2. ve 3. şart **düşerdi**; yani hüküm yine REJECTED
olurdu. Bu bir geçiş senaryosu değil. En kalabalık kovada da (n=28'e 18) B daha iyi.

### Reddedilen üç hamle

Tabanı 28'e çekmek · bandı genişletip B'ye kayıt toplamak · ortalamaları geçerli bir
karşılaştırma gibi sunmak. Üçü de sonucu gördükten sonra eşik oynatmaktır.

### Güç analizinde modelleme boşluğu (bu kez koşum yakaladı)

Projeksiyon 77,7 / 51,2 demişti, gerçekleşen 45 / 28. Risk kapısı varsayımı
**doğruydu** (73/309 = %23,6, varsayılan %23); eksik olan **ankraj → kayıt**
elemesiydi — girişte ankrajın hâlâ listelenmesi, delta taşıması ve hem DTE hem delta
kovasına düşmesi gerekiyor. Uçtan uca oran %13, varsayılan %23 değil.

H04 ve H06'da güç analizi tasarımı kurtarmıştı; burada eksik kaldı ve bunu koşum
gösterdi. Betiğin hesabı **değiştirilmedi** (§4b'nin tablosu ona atıf yapıyor ve
yeniden üretilebilir kalmalı), boşluk docstring'ine not edildi.

### Beklentinin tersi: makas dezavantajı kayboldu

Dondurma öncesi ucuz kol **+0,7 puan** daha geniş makasa düşüyordu ve §4b.1 buna bir
yorum kuralı bağlamıştı. Gerçekleşen örneklemde fark **+0,17 puan** — kayıtlar
kova-filtreli bir altküme olduğu için bileşim değişti. Kural teknik olarak
"muhafazakâr" dalını tetikledi, ama fark 0,45 $ olduğundan taşıdığı ağırlık yok ve
şişirilmiyor.

---

## Protokol disiplininin kaydı

H03 dört belgeyle donduruldu ve **dördü de koşumdan önce commit'lendi**:

| Belge | Ne dondurdu |
|---|---|
| `PREREG_H03.md` | Hipotez, bilgi zamanı, evren, pencere, birincil ölçüt, 3 deneme, hüküm merdiveni |
| ek 1 | "Olağandışı akış" = `volume(D) > OI(D)`; seans-isim başına tek aday; **yedek seçim yok** |
| ek 2 | Ek 1 kontrol kolunu yanlış tarif etmişti — sözleşme kazandı, kollar OI teyidine göre ayrılır |
| ek 3 | Seçim filtresi ile kol değişkeninin mekanik korelasyonu; protokol **değiştirilmedi** |

Ek 2 ve ek 3, uygulama sırasında kendi belgelerimde bulduğum kusurları
sonuçlardan **önce** kayda geçiriyor. Çelişkiyi görüp sessizce uygun olanı seçmek
bu projenin defalarca yakaladığı hata.

---

## M0–M3 özet

**Yetenek:** tarihli opsiyon zinciri gerçekten tarihsel (iki tarihte ortak 11.218
kontratın 10.927'sinin değeri farklı). Kontrat geçmişi günlük NBBO serisi veriyor.
Araştırma penceresi **2026-05-13**'te başlıyor. Kalite **B** — zaman alanı
`last_tape_time`, yani son işlem zamanı.

**İlk kartlar:** SPY 775/776 call debit (azami zarar 60,60 $), QQQ 760/761
(50,60 $). Dört isim risk kapısında doğru şekilde reddedildi.

**İlk gerçek sonuçlar:** 8 Eylül girişi, beş seans sonra SPY **−30,60 $**,
QQQ **−47,20 $**.

---

## Kaynak bütçesi

| Kaynak | Değer |
|---|---|
| UW günlük limit | 30.000 |
| Bugün harcanan | ~1.060 (hasat 845 + probe/kart ~215) |
| Hasat | 850 dosya, 85 seans, 384 MB — **gitignore'da**, komutla yeniden üretilir |
| Güvenli tavan | 5.000/gün |

---

## Henüz yapılmamış olanlar (açıkça)

- Açık PAPER pozisyonları canlı zamanlayıcıda **izlenmiyor**: `/opsiyon` ekranı
  commit'li artefaktları okur, kendisi yeni fiyat çekmez.
- Kalan 8 hipotez ailesi koşmadı. Bunlardan **H06 ölçüldü ve bilinçli olarak
  dondurulmadı**: güç analizi, kol kurgusunun yoğunlaşmayı takvimden ayıramadığını
  gösterdi (tabanı tutan tek yapıda kol A'nın %65–67'si aylık vade; takvim
  çıkarılınca kol B 3–4 gözleme çöküyor). Kayıt: `INFEASIBLE_H06.md`. Hipotez
  çürütülmedi, **sınanmadı**; ilan edilmiş deneme sayısı 9'da kaldı çünkü
  koşulmayan aile deneme tüketmez. Sıra **H10**'a geçti.
- `/opsiyon` ekranının **gözle** canlı doğrulaması yapılmadı — auth duvarının
  arkasında ve kimlik bilgileri bende değil (`BLOCKERS.md` B1). Kod tarafı
  test edildi: rota render ediyor, auth testleri rotayı otomatik kapsıyor.

Hiçbiri dış engel değil; yazılmamış kod. Engeller `BLOCKERS.md`'de.
