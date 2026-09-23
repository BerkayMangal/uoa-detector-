# Ön-kayıt — H03: T+1 açık pozisyon teyidi

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, tek bir sonuç
görülmeden. Bu dosya çalıştırmadan önce sabitlenir ve sonuçlara göre
değiştirilmez.

---

## 1. Hipotez

Bir seansta olağandışı akış gören uygun opsiyon kontratlarından, **ertesi yayımda
açık pozisyonu ARTAN** olanlar (yani pozisyonun kapanmayıp açıldığı teyit
edilenler), teyidi olmayan eşleştirilmiş kontratlara kıyasla daha iyi **net
opsiyon P&L**'i üretir.

Ekonomik gerekçe: aynı akış hem yeni bir bahis hem mevcut bir pozisyonun
kapatılması olabilir. Açık pozisyon artışı bu ikisini ayıran tek doğrudan
gözlemdir.

**Başarısızlık ihtimali dürüstçe:** OI artışı tek bir işlemin kesin açılışı
demek değildir; aynı gün birden çok karşı taraf işlem yapar. Teyit sinyali
zayıflatabilir de — çünkü teyidi beklemek girişi bir seans geciktirir ve
hareketin bir kısmı fiyatlanmış olur.

---

## 2. Bilgi zamanı — bu çalışmanın en kritik kuralı

UW günlük açık pozisyonu **gün başı** semantiğindedir: `date = D` satırı,
**D-1**'in işlemleri sonrası oluşan OI'yi taşır ve D'nin açılışından önce
yayımlanır.

Dolayısıyla:

| An | Ne biliniyor |
|---|---|
| Seans **D** | Akış görülür. OI teyidi **BİLİNMEZ** |
| Seans **D+1** açılışı | `date = D+1` satırı yayımlanır → D'nin OI değişimi görünür |
| Seans **D+1** kapanışı | **GİRİŞ burada yapılır** — teyidin bilindiği ilk kapanış |

**D anında T+1 OI kullanmak yasaktır.** Bu çalışma girişi bilerek bir seans
geciktirir; gecikmenin maliyeti sonuca dahildir ve mazeret olarak kullanılamaz.

Tutma: girişten itibaren **5 işlem günü**.

---

## 3. Evren ve pencere

**Evren:** `profiles/options_alpha_v1.yaml` içinde donmuş on isim
(SPY, QQQ, AAPL, NVDA, MSFT, AMZN, META, TSLA, AMD, GOOGL). Genişletilmeyecek.

**Pencere:** giriş seansları **2026-05-14 … 2026-09-15**.
- Alt sınır: zincir tabanı 2026-05-13 ölçüldü; ilk giriş bir sonraki seans.
- Üst sınır: 5 işlem günlük tutmanın 2026-09-22'ye kadar tamamlanmış olması.
- Yaklaşık **85 seans**.

**Survivorship sınırı, açıkça:** evren bugünden seçildi. O dönemde likit olup
sonradan düşen isimler yok. Bu düzeltilemez, raporlanır.

---

## 4. Kontrat seçimi

Motorda **zaten donmuş** kurallar, değiştirilmeden: 14–60 DTE, 0,25–0,70 mutlak
delta, OI ≥ 250, hacim ≥ 100, makas ≤ mid'in %15'i, bid ≥ 0,10, standart çarpan.
Yapı seçimi de değişmez (bare long vs dikey debit, %20 debit düşüşü eşiği).

**Bu çalışma için tek bir yeni parametre tanımlanmaz.**

---

## 5. Kollar

| Kol | Tanım |
|---|---|
| **Teyitli** | Akış kontratının OI'si teyit satırında **arttı** |
| **Kontrol** | Aynı seans, aynı isim, aynı uygunluk kapıları, OI artışı **yok**; DTE kovası ve delta kovası eşleştirilmiş |

Kontrol kolu zorunludur. Net pozitif P&L tek başına alfa değildir; aynı risk ve
yapı koşullarındaki basit karşılaştırmayı geçmesi gerekir.

---

## 6. Birincil ölçüt (tek)

**Yapı başına net opsiyon P&L (USD), maliyetler düşülmüş**, çıkış varyantı
**`time_only`**, ufuk **5 işlem günü**.

Tek birincil seçildi çünkü en az serbestlik derecesine sahip olan bu. Hisse
getirisi, IC ve vol puanı **ikincil teşhistir**; opsiyon kazancının yerine
geçemez.

---

## 7. Deneme bütçesi — önceden ilan

| # | Deneme |
|---|---|
| 1 | `time_only` (**birincil**) |
| 2 | `time_and_stop` (ikincil) |
| 3 | `time_target_stop` (ikincil) |

**Toplam 3.** Başka varyant eklenmeyecek. Eklenirse rapora **eklendiği
belirtilerek** girer; raporlanmayan deneme üretilmez.

Ufuklar arasında sonradan kazanan seçilmeyecek: birincil ufuk şimdi sabitlendi.

---

## 8. Maliyet varsayımları

Donmuş profilden: uzun giriş ask'ten, uzun çıkış bid'den; kısa bacak açılışta
bid, kapanışta ask. Bacak başına yön başına **0,65 $**. İşlem gören fiyata
**%2** gecikme haircut'ı. Makas bu fiyatların içinde, **ikinci kez düşülmez**.

---

## 9. Örneklem tabanı ve hüküm koşulları

**Taban:** teyitli kolda **en az 30** tamamlanmış yapı. Altındaysa hüküm
**INSUFFICIENT_DATA** — sonuç değil.

**Hüküm merdiveni (önceden sabit):**

| Hüküm | Koşul |
|---|---|
| **REJECTED** | Teyitli kolun ortalama net P&L'i kontrol kolunu geçmiyor |
| **INSUFFICIENT_DATA** | n < 30, ya da belirsizlik aralığı işaret veremeyecek kadar geniş |
| **EXPLORATORY_PASS** | Teyitli kol kontrolü geçiyor, maliyet sonrası pozitif, ve seans bazlı blok-bootstrap aralığı sıfırı içermiyor |
| **OOS_PASS / FORWARD_PASS** | **Bu çalışmadan ÇIKAMAZ** — bkz. §10 |

Belirsizlik seans bazlı **blok bootstrap** ile ölçülür (aynı seansın kartları
bağımsız değildir). Yöntem şimdi seçildi, sonuçlara göre değiştirilmeyecek.

---

## 10. Bu çalışmanın veremeyeceği hüküm

Veri **B kalitesinde**: zincirin zaman alanı `last_tape_time`, yani son *işlem*
zamanı; kotasyonun hangi an geçerli olduğu bilinmiyor. Bu yüzden:

- Gün içi gerçekleşme, stop/hedef sıralaması veya "şu fiyattan dolardı" iddiası
  **yapılamaz**.
- En iyi ihtimalle **EXPLORATORY_PASS** çıkar. `FORWARD_PASS` ancak ileriye
  dönük, canlı kotasyonla puanlanan bir protokolden gelebilir.
- Akış popülasyonu UW'nin **kendi** olağandışılık tanımıdır. Bu bir seçim
  yanlılığıdır ve hiçbir örneklem büyüklüğü onu kaldırmaz.

---

## 11. Önceki çalışmalarla çakışma

Pencere (2026-05-14 … 2026-09-15) **opsiyon** araştırması için yakılmamıştır:
yakılmış paneller ThetaData zincir anlık görüntüleri (2025-05…2026-04) ve
Study E/F'in günlük **dayanak kapanış** panelleridir.

Dürüst uyarı: bu pencerenin **dayanak fiyat yolu** Study F/G çalışmasında
görüldü. H03'ün etiketi dayanak sıralaması değil **opsiyon P&L**'i olduğu için
kirlenme riski düşüktür, ama sıfır değildir ve sonuçta belirtilir.

---

## 12. Koşulacak komut

Uygulama bu ön-kayıt onaylanıp donduktan **sonra** yazılır. Hiçbir sonuç bu
dosya değişmeden raporlanamaz.
