# Ön-kayıt eki 2 — H03: birinci ekin kontrol kolu tarifi yanlıştı

**Tarih:** 2026-09-23 · **Durum:** koşucu sonuç üretmeden önce yazıldı
**İlgili:** `PREREG_H03.md` (sözleşme, değişmedi) · `PREREG_H03_ADDENDUM.md` (§4 düzeltiliyor)

---

## 1. Çelişki

Ana sözleşme `PREREG_H03.md` §5 kolları şöyle ayırıyor:

> **Teyitli:** Akış kontratının OI'si teyit satırında **arttı**
> **Kontrol:** … **OI artışı yok**; DTE ve delta kovası eşleştirilmiş

Birinci ek §4 ise kontrol kolunu şöyle tarif etti:

> … `volume(D) <= open_interest(D)` olan …

**Bunlar aynı şey değil.** Sözleşme kolları **OI teyidine** göre ayırıyor; ek ise
**olağandışı akış olmamasına** göre ayırmış. Ek yalnızca §1'deki "olağandışı
akış" ifadesini ölçülebilir kılmakla görevliydi; kontrol kolunu yeniden
tanımlayarak yetkisini aştı.

---

## 2. Hangisi geçerli

**Ana sözleşme geçerlidir.** Donmuş belge sözleşmedir; ek onu genişletemez,
yalnız tamamlayabilir. Kollar **OI teyidine** göre ayrılır.

Birinci ekin §1–§3'ü (olağandışı akışın tanımı, seans-isim başına tek aday,
yedek seçim yok) **aynen geçerlidir** — onlar gerçekten §1'i işletiyordu.
Yalnız §4 geçersizdir ve yerine aşağıdaki geçer.

---

## 3. Kolların ve eşleşmenin düzeltilmiş tarifi

Her (isim, seans) için **tek** aday üretilir: uygunluk kapılarını geçen,
`volume(D) > open_interest(D)` sağlayan, hacmi en yüksek kontrat (ek 1 §2–§3).

O tek aday, teyit satırına göre **bir** kola girer:

| Kol | Koşul |
|---|---|
| **Teyitli** | `open_interest(D+1) > open_interest(D)` |
| **Kontrol** | `open_interest(D+1) <= open_interest(D)` |

Yani bir (isim, seans) iki kola birden giremez ve aynı ekonomik olay iki kez
sayılmaz.

**Eşleşme seans içinde değil, analiz aşamasında yapılır.** İki kol DTE kovası
(14–30 / 31–45 / 46–60) ve delta kovası (0,25–0,40 / 0,40–0,55 / 0,55–0,70)
bazında karşılaştırılır. Birinci ekin "aynı seans içinde eşleştirilmiş ikinci bir
kontrat bul" kuralı düşürülmüştür: seans-isim başına tek aday kuralıyla birlikte
uygulanamazdı — ikisi aynı anda doğru olamaz.

**Bir kova tek kollu kalırsa** o kova karşılaştırmadan düşer ve düştüğü
raporlanır; tek kollu kovayı karşılaştırmaya sokmak karşılaştırma değildir.

---

## 4. Bu düzeltmenin değiştirmedikleri

Bilgi zamanı kuralı (giriş D+1 kapanışı), evren, pencere, uygunluk kapıları,
birincil ölçüt (`time_only`, 5 işlem günü, maliyet sonrası net opsiyon P&L),
**üç denemelik bütçe**, maliyet varsayımları, 30 yapılık örneklem tabanı ve
hüküm merdiveni aynen geçerlidir.

Bu düzeltme deneme sayısını **artırmaz**; bir tarifi tutarlı hâle getirir.

---

## 5. Neden bu ayrıca kaydediliyor

Çelişkiyi fark edip sessizce birini seçmek — özellikle sonuçları gördükten sonra
— bu projenin defalarca yakaladığı hatadır. İki belge birbiriyle çelişiyorsa,
hangisinin kazandığı ve neden kazandığı **sonuçlardan önce** yazılır.
