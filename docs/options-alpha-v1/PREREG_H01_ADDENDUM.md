# Ön-kayıt eki 1 — H01: merdivende adı konmamış dal

**Faz 5.24 · options_alpha_v1** · yazıldı **2026-09-23**

> **Bu ek, sonuçlar görüldükten SONRA yazıldı.** H03'ün üç eki koşumdan önce
> yazılmıştı; bu ek öyle değil ve öyleymiş gibi sunulmuyor. Aşağıdaki çözüm, hiç
> sayı yokken dondurulmuş `PREREG_H01.md` §7 metninden çıkıyor, ama eki yazarken
> kolların ortalamalarını biliyordum. Okuyucu bunu bilerek değerlendirsin.

---

## 1. Boşluk ne

`PREREG_H01.md` §7'de hüküm merdiveni şöyle donmuştu:

| Hüküm | Koşul |
|---|---|
| **REJECTED** | Yüksek kolun ortalama net P&L'i kontrolü **geçmiyor** |
| **INSUFFICIENT_DATA** | Bir kol n < 30, ya da belirsizlik işaret veremeyecek kadar geniş |
| **EXPLORATORY_PASS** | Geçiyor **ve** maliyet sonrası **pozitif** **ve** blok bootstrap aralığı sıfırı içermiyor |

Koşumun verdiği durum bu üç satırın hiçbirine tam oturmuyor:

- Yüksek kol kontrolü **geçiyor** (−54,52 $ > −70,75 $) → REJECTED'ın yazılı
  koşulu sağlanmıyor.
- Maliyet sonrası **pozitif değil** (−54,52 $) → EXPLORATORY_PASS'in ikinci
  şartı düşüyor.
- Blok bootstrap aralığı **sıfırı içeriyor** ([−3,28, +37,58]) → üçüncü şart da
  düşüyor.

Yani "kontrolü geçiyor ama para kaybediyor" hâli için merdivende adı konmuş bir
dal yok.

## 2. Çözüm

**Maliyet sonrası pozitiflik şartı yönetir. Hüküm: REJECTED.**

Gerekçe, §7'nin kendi yapısında: pozitiflik EXPLORATORY_PASS'in şartı olarak
sayılar yokken yazılmıştı. Kaybeden bir kol işleme sokulamaz; daha çok kaybeden
bir kontrolü geçmek, geçiş değildir. Bu yüzden ticari sonuç reddedilir.

Ayrıca iddia **iki cümleye** ayrılır, H03'te olduğu gibi:

1. **İşlenebilirlik: REDDEDİLDİ.** İki kol da maliyet sonrası ağır negatif.
2. **Göreli etki: KURULAMADI.** Fark sıfırdan ayırt edilemiyor; aralık sıfırı
   içeriyor ve ham farkın yarısına yakını tek bir işlemden geliyor.

İkisi farklı cümle ve biri diğerini ima etmiyor.

## 3. Bu, yüze gülen dalı seçmek değil

Boşluğun diğer iki okuması **daha lehte** olurdu:

- EXPLORATORY_PASS'i "kontrolü geçti" diye vermek — yasak, iki şart açıkça
  düşüyor.
- INSUFFICIENT_DATA demek — "belirsizlik geniş" cümlesine oturtulabilirdi ve
  kapıyı açık bırakırdı. Ama −54,52 $ bir belirsizlik sorunu değil: 111
  gözlemde 12 kazanan ve toplam −6.051 $ ile büyük, net ve tek yönlü bir bulgu.

REJECTED, üç okumanın en lehte olmayanı. Seçilme sebebi de bu değil; iki
bağımsız şartın düşmesi.

## 4. Koşucudaki kusur (kaydedilir)

`scripts/options_alpha_h01_study.py`'nin ilk hâli §7'yi yanlış uyguluyordu:

- yalnız **birinci** şarta bakıyordu (kontrolü geçiyor mu),
- merdivende **var olmayan** bir etiket üretiyordu: `EXPLORATORY_PASS_CANDIDATE`,
- §7'nin açıkça istediği **blok bootstrap'i hiç hesaplamıyordu**.

Düzeltildi: üç şart birlikte sınanıyor, düşen şartlar artefakta
`verdict_failed_clauses` olarak yazılıyor, ve bootstrap koşucunun içinde
tohumlanmış (20260923) olarak hesaplanıyor. Aralığı üreten şey bu düzeltmedir —
yani kusur bulunmasa hüküm yanlış etiketle yayımlanacaktı.

## 5. Değişmeyenler

Hiçbir eşik, pencere, kol tanımı, evren, tutma süresi veya birincil ölçüt
değişmedi. Deneme bütçesi **6** (H03'ün 3'ü + H01'in 3'ü) — bu ek yeni deneme
eklemiyor.
