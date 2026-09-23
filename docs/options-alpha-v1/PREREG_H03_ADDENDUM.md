# Ön-kayıt eki — H03: "olağandışı akış" neyle işletilir

**Tarih:** 2026-09-23 · **Durum:** koşucu yazılmadan ve tek bir sonuç görülmeden
önce donduruldu · **Ana belge:** `PREREG_H03.md` (değiştirilmedi)

---

## 1. Neden bu ek var

`PREREG_H03.md` §1 hipotezi "bir seansta **olağandışı akış gören** uygun opsiyon
kontratları" üzerine kuruyor. Ana belge bu ifadeyi **hangi gözlemle** işleteceğimi
yeterince sabitlememiş.

Bu bir boşluk ve tehlikeli bir boşluk: uygulama aşamasında birkaç makul tanım var
ve sonuçlara bakıp aralarından seçmek, D4'ün açıkça yasakladığı şey. Study F'in
purge eki de aynı sebeple, koşucu var olmadan yazılmıştı.

Bu yüzden tanım burada, **koşucu yazılmadan** sabitleniyor. Ana belge
değiştirilmiyor; donmuş bir sözleşme sonradan düzenlenmez, ekle tamamlanır.

---

## 2. Dondurulan tanım

**Seans D'de "olağandışı akış gören kontrat" =** motorun donmuş uygunluk
kapılarını geçen **ve** o seansın **hacmi açık pozisyonunu aşan** kontrat:

```
volume(D) > open_interest(D)
```

**Neden bu:** açık pozisyon gün başı semantiğindedir, yani `open_interest(D)`
seans başlamadan önceki duruştur. Hacmin onu aşması, o gün kontratta mevcut
stoğun üstünde işlem döndüğü anlamına gelir — yani yeni pozisyonlanma olasılığı.
Bu, UW'nin kendi "unusual" ön-ayarının da taşıdığı koşullardan biridir ve
zincirden doğrudan gözlemlenir; ayrı bir uç nokta veya yeni eşik gerektirmez.

**Neden alternatifleri değil:**
- *Prim büyüklüğü eşiği* — yeni bir sayı gerektirir ve o sayı bu çalışmanın
  dışında hiçbir yerde donmuş değil.
- *Flow-alerts akışı* — UW'nin kendi alarm kuralını taşır; hipotez zaten OI
  teyidini test ediyor, üstüne ikinci bir satıcı filtresi koymak ölçülen şeyi
  bulanıklaştırır.
- *"En çok işlem gören kontrat"* — M1'de bu yedek seçim kullanıldığında kartın
  koşulu **sağlanmadan** sinyal ürettiği görüldü. Bu çalışmada yedek seçim
  **yoktur**.

---

## 3. Seans-isim başına tek aday

Bir (isim, seans) için en fazla **bir** aday üretilir: koşulu sağlayanlar
arasında **hacmi en yüksek** olan.

**Neden:** aynı ekonomik olay birden çok kontratta iz bırakır. Hepsini ayrı işlem
saymak, tek bir olayı bağımsız kanıt yığınına çevirir ve hem örneklemi hem riski
şişirir. Ana belge §5'teki kontrol kolu da aynı kuralla seçilir, böylece iki kol
aynı yapıdadır.

**Aday yoksa** o (isim, seans) hiçbir şey üretmez. Yedek seçime düşülmez.

---

## 4. Kontrol kolunun eşleşmesi

Kontrol adayı, aynı (isim, seans) içinde:
- uygunluk kapılarını geçen,
- `volume(D) <= open_interest(D)` olan,
- teyitli adayla **aynı DTE kovası** (14–30, 31–45, 46–60) ve **aynı delta
  kovası** (0,25–0,40 / 0,40–0,55 / 0,55–0,70) içindeki,
- hacmi en yüksek kontrat.

Eşleşme bulunamazsa o seans **her iki koldan da** düşer. Tek kollu seans
karşılaştırmayı bozar.

---

## 5. Bu ekin değiştirmedikleri

Ana belgedeki hiçbir şey: bilgi zamanı kuralı (giriş D+1 kapanışı), evren,
pencere, kontrat seçimi kapıları, birincil ölçüt, üç denemelik bütçe, maliyet
varsayımları, 30 yapılık örneklem tabanı ve hüküm merdiveni aynen geçerlidir.

Bu ek yalnızca §1'deki ifadeyi ölçülebilir hâle getirir ve **deneme sayısını
artırmaz**.
