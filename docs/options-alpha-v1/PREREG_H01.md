# Ön-kayıt — H01: göreli agresif akış

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, tek bir sonuç
görülmeden. Koşucu yazılmadan sabitlenir ve sonuçlara göre değiştirilmez.

---

## 1. Hipotez

Bir kontratın o seanstaki hacmi, **kendi hissesinin geçmiş dağılımına göre**
olağandışı yüksekse, sonraki net opsiyon P&L'i normal seviyedeki kontratlardan
iyi olur.

Ekonomik gerekçe: mutlak prim büyüklüğü hisseyi değil hissenin ölçeğini ölçer.
SPY'da 5.000 lot sıradan, küçük bir isimde olağanüstüdür. Karşılaştırmanın
hissenin **kendi** normaline göre yapılması, bu ölçek etkisini eler.

**Başarısızlık ihtimali dürüstçe:** göreli yüksek hacim, likidite olayının
kendisi olabilir (endeks yeniden dengelenmesi, vade, hedge akışı) ve yön
taşımayabilir. Ayrıca yüksek hacim genelde geniş makasla gelir; maliyet kapısı
sinyali yiyebilir.

---

## 2. H03'ten alınan ders: seçim, sonucu belirlememeli

H03'te aday seçimi (`volume > OI`) ile kolları ayıran değişken (`OI artışı`)
mekanik olarak koreleydi; kontrol kolu 7'de kaldı ve tasarım etkiyi
ayrıştıramadı.

Bu çalışmada **uygunluk kapıları** (DTE, delta, likidite, makas) ile **kolu
belirleyen değişken** (göreli hacim yüzdeliği) birbirinden bağımsızdır. Kapılar
hangi kontratın işlenebilir olduğunu, yüzdelik ise hangi kola gireceğini söyler.

---

## 3. Bilgi zamanı

Bir seansın hacmi ancak **kapanıştan sonra** tamamlanır. Seans D'nin hacmine
bakıp D'nin kapanışında işlem yapmak look-ahead'dir.

| An | Ne biliniyor |
|---|---|
| Seans **D** kapanışı | D'nin tam hacmi henüz kullanılamaz |
| Seans **D+1** | D'nin hacmi yayımlanmış durumda |
| Seans **D+1** kapanışı | **GİRİŞ burada** |

Tutma: girişten itibaren **5 işlem günü**.

Göreli dağılım **yalnız D'den önceki** seanslardan hesaplanır; D'nin kendisi
dağılıma dahil edilmez.

---

## 4. Göreli hacmin tanımı (dondurulmuş)

Seans D'de, bir hisse için:

1. Trailing pencere: D'den **önceki 20 seans**.
2. O pencerede, o hissenin **uygunluk kapılarını geçen** kontratlarının seans
   hacimleri toplanır → dağılım.
3. Kontratın D'deki hacminin bu dağılımdaki yüzdeliği hesaplanır.

| Kol | Koşul |
|---|---|
| **Yüksek** | yüzdelik **≥ 90** |
| **Kontrol** | yüzdelik **40 – 60** |

20 seanslık pencere ve 90/40–60 eşikleri **şimdi** sabitlenmiştir; sonuçlara
göre oynatılmayacaktır. Ara bant (60–90) kasten kullanılmaz: iki kolun ayrık
olması karşılaştırmayı keskinleştirir.

**Pencere dolmamışsa** (D'den önce 20 seans yoksa) o seans üretmez.

---

## 5. Evren, pencere, kontrat seçimi

**Evren:** profildeki on isim. **Pencere:** giriş seansları
2026-06-12 … 2026-09-15 — alt sınır, 20 seanslık trailing pencerenin hasadın
başlangıcından (2026-05-14) sonra dolduğu ilk seans.

**Kontrat seçimi:** motorun donmuş kapıları, değiştirilmeden. Yeni parametre yok.

**Seans-isim-kol başına tek aday:** her kolda, koşulu sağlayanlar arasında
hacmi en yüksek kontrat. Yedek seçim yok. Bir kolda aday yoksa o seans-isim
**her iki koldan da** düşer.

---

## 6. Birincil ölçüt ve deneme bütçesi

**Birincil (tek):** yapı başına maliyet sonrası net opsiyon P&L, çıkış varyantı
**`time_only`**, **5 işlem günü**.

| # | Deneme |
|---|---|
| 1 | `time_only` (**birincil**) |
| 2 | `time_and_stop` (ikincil) |
| 3 | `time_target_stop` (ikincil) |

**Toplam 3.** Başka varyant eklenmeyecek; eklenirse raporda eklendiği belirtilir.
Sonradan kazanan ufka veya varyanta geçilmeyecektir.

---

## 7. Örneklem tabanı ve hüküm

**Taban:** her kolda **en az 30** tamamlanmış yapı. Altındaysa
**INSUFFICIENT_DATA**.

| Hüküm | Koşul |
|---|---|
| **REJECTED** | Yüksek kolun ortalama net P&L'i kontrolü geçmiyor |
| **INSUFFICIENT_DATA** | Bir kol n < 30, ya da belirsizlik işaret veremeyecek kadar geniş |
| **EXPLORATORY_PASS** | Yüksek kol kontrolü geçiyor, maliyet sonrası pozitif, ve seans bazlı blok bootstrap aralığı sıfırı içermiyor |
| **FORWARD_PASS** | **Çıkamaz** — B kalite veri |

Karşılaştırma DTE ve delta kovalarında yapılır; tek kollu kova düşer ve
düştüğü raporlanır.

---

## 8. Bu çalışmanın veremeyeceği hüküm

B kalite veri: zaman alanı `last_tape_time`, yani son **işlem** zamanı. Gün içi
gerçekleşme, stop/hedef sıralaması ve fill iddiası yok. En iyi ihtimalle
**EXPLORATORY_PASS**.

Evren bugünden seçildi → survivorship. Likidite kapıları ayrıca seçim yanlılığı
taşır.

---

## 9. Çakışma

Pencere H03 ile **aynı hasadı** kullanır. Bu, aynı verinin ikinci kez
sorgulanmasıdır ve **çoklu deneme** açısından kayda geçer: options_alpha_v1
kapsamında bu ikinci ailedir, toplam ilan edilmiş deneme 3 (H03) + 3 (H01) = 6.

Hüküm eşikleri bu sayıya göre yorumlanacak; tek bir ailenin "geçmesi" altı
denemelik bir aramada tek başına kanıt değildir.
