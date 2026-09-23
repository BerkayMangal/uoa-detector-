# Ön-kayıt eki 3 — H03: seçim filtresi ile kol değişkeni mekanik olarak korele

**Tarih:** 2026-09-23 · **Durum:** çalışma sonucu üretilmeden önce yazıldı
**İlgili:** `PREREG_H03.md` (sözleşme) · ek 1 ve ek 2
**Protokolde değişiklik:** **YOK.** Bu ek yalnızca bir sınırı kayda geçirir.

---

## 1. Fark edilen şey

Koşucunun duman testi — eksik pencerede, sayıları atılmak üzere koşulan bir
deneme — kolların **9'a 1** dağıldığını gösterdi. Bu tesadüf değil ve nedeni
protokolün kendisinde.

Aday seçimi (ek 1 §2) şu koşulu kullanıyor:

```
volume(D) > open_interest(D)
```

Kolları ayıran değişken (sözleşme §5, ek 2 §3) ise şu:

```
open_interest(D+1) > open_interest(D)
```

Açık pozisyon **gün başı** duruşudur. Bir kontratın o günkü hacmi, seans
başlamadan önceki açık pozisyonu aşıyorsa, dönen hacmin önemli bir kısmı
neredeyse zorunlu olarak **pozisyon açıyordur** — dolayısıyla D+1'in açık
pozisyonu büyük olasılıkla daha yüksek olur.

Yani **seçim filtresi, sonucu ölçen değişkeni büyük ölçüde önceden belirliyor.**

---

## 2. Bunun sonucu

- **Kontrol kolu inşa gereği küçük kalacak.** Az örneklemli bir kola karşı
  yapılan karşılaştırma zayıf güçlüdür; fark bulunmaması "fark yok" demek
  olmayacak.
- **Teyit sinyalinin kendine özgü bilgisi ölçülemeyebilir.** İki değişken
  örtüşüyorsa, teyidin akış filtresinin üstüne ne eklediği bu tasarımla
  ayrıştırılamaz.
- Sözleşme §9'daki **30 yapılık taban** muhtemelen kontrol kolunda
  sağlanmayacak, ve bu durumda hüküm **INSUFFICIENT_DATA**'dır — sözleşmede
  zaten böyle yazıyor.

---

## 3. Neden protokol DEĞİŞTİRİLMİYOR

Çünkü sonuçları daha iyi göstermek için tasarımı değiştirmek, bu projenin
tekrar tekrar yakaladığı hatanın ta kendisi. Ek 1 ve ek 2 zaten commit'lendi;
seçim koşulunu şimdi gevşetmek (ör. hacim eşiğini kaldırmak, ya da kolları başka
bir değişkene bağlamak) **sonucu görmeden bile** aramayı genişletmek olur ve
deneme bütçesini bozar.

Doğru davranış: **donmuş hâliyle koşmak**, dengesizliği ve güç kaybını sonucun
kendisiyle birlikte raporlamak.

---

## 4. Bunun bir sonraki çalışmaya bıraktığı

Teyit sinyalinin bağımsız bilgisini ölçmek isteyen bir tasarım, aday seçimini
kol değişkeninden **ayrıştırmak** zorunda: örneğin adayları hacim/OI koşulundan
bağımsız bir eşikle (prim, agresiflik, kümelenme) seçip kolları yine OI teyidine
göre ayırmak.

Bu **yeni bir hipotezdir**, H03'ün düzeltilmesi değil. Kendi ön-kaydını, kendi
deneme bütçesini ve kendi penceresini gerektirir. H03'ün sonucu ne olursa olsun
o çalışmanın yerine geçmez.

---

## 5. Kayda geçen şey

H03'ün sonucu, bu yapısal sınırla **birlikte** okunacaktır. Kontrol kolu zayıf
çıkarsa bu, hipotezin yanlışlandığı değil, **bu tasarımın onu ayrıştıramadığı**
anlamına gelir — ve iki cümle arasındaki fark, dürüst bir araştırma kaydının
tamamıdır.
