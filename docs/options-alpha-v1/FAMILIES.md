# options-alpha-v1 — on iki ailenin tek tablosu

**Tarih:** 2026-09-23 · **Motor:** düzeltilmiş `exits.py` (v2, `AUDIT_H10_TRADES.md`)
**Kaynaklar:** `HYPOTHESES.md` (fizibilite), `PREREG_*.md` (protokol),
`RESULT_*.md` + `artifacts/options-alpha-v1/h??_result{,_v2}.json` (sonuç)

Bu dosya yeni bir hüküm vermez; var olan hükümleri tek yerde toplar. Sayılar
artefaktlardan okunmuştur. v1 ile v2 ayrıldığında **geçerli olan v2'dir**; v1
dosyaları değiştirilmedi.

---

## 1. Tablo

| # | Aile | Fizibilite | Durum | Hüküm (v2) | Kollar: tamamlanmış n · ortalama (A / B) | A − B · %95 blok-bootstrap | Deneme |
|---|---|---|---|---|---|---|---|
| **H03** | T+1 OI uyumu | DESTEKLENİR | Koştu | **REJECTED** | 96 · −83,49 $ / 4 · −37,15 $ | — ¹ | 3 |
| **H01** | Göreli olağandışı hacim | DESTEKLENİR | Koştu | **REJECTED** | 80 · −55,18 $ / 78 · −75,77 $ | +20,59 $ · [−2,26 , +46,03] | 3 |
| **H04** | Akış/fiyat gecikmesi | DESTEKLENİR | Koştu | **INSUFFICIENT_DATA** ² | 31 · −58,68 $ / 23 · −53,06 $ | −5,62 $ · [−52,12 , +37,05] | 3 |
| **H10** | Uzun prim / konveksite | DESTEKLENİR | Koştu | **INSUFFICIENT_DATA** | 32 · −53,79 $ / 20 · −46,13 $ | −7,66 $ · [−47,22 , +43,82] | 3 |
| **H02** | Tekrarlayan akış | DESTEKLENİR | Koştu | **REJECTED** | 67 · −53,47 $ / 57 · −58,47 $ | +5,00 $ · [−20,31 , +27,28] | 3 |
| **H06** | Vade/strike yoğunlaşması | DESTEKLENİR (veri) | Ölçüldü, **dondurulmadı** | — | kol kurgusu takvimden ayrışmadı | — | 0 |
| **H05** | Sektör-göreli akış | KISITLI | Koşulmadı | — | sektör üyeliği replay-güvenli değil | — | 0 |
| **H07** | Çok bacaklı işlem bağlamı | DESTEKLENİR | Koşulmadı | — | 24 saatlik pencere, hasat pahalı | — | 0 |
| **H08** | Olay sonrası devam | KISITLI | Koşulmadı | — | kazanç takvimi revize ediliyor | — | 0 |
| **H09** | Olay öncesi prim davranışı | KISITLI | Koşulmadı | — | takvim + IV-rank geçmişi 2026-05-04'ten | — | 0 |
| **H11** | Skew / vade yapısı | DESTEKLENİR | Koşulmadı | — | ilk dalgaya girmedi | — | 0 |
| **H12** | Rejime bağlı akış (ablation) | DESTEKLENİR | Koşulmadı | — | reddedilmiş gamma-yön kuralını diriltmemek şartıyla | — | 0 |

**Toplam ilan edilmiş deneme: 15** (koşan beş aile × 3 çıkış varyantı). Koşulmayan
veya dondurulmayan aile deneme tüketmez.

¹ H03'ün kontrol kolu 4 kayıtla tabanın (30) çok altında; kol farkı ve aralığı
anlamlı bir karşılaştırma değildir. Hüküm, teyitli kolun maliyet sonrası negatif
olmasına dayanır (`RESULT_H03.md`).

² H04 v1'de REJECTED'tı. v2'de `no_exit_data` düzeltmesi B kolunu 23 tamamlanmış
yapıya indirdi (taban 30). Etiket değişti, yön değişmedi: A yine B'nin gerisinde
ve iki kol da negatif. Bu, gevşek tabanla yeniden koşuya davet **değildir**
(`AUDIT_H10_TRADES.md` §5).

## 2. v1 → v2 değişimi

| Aile | v1 hüküm | v2 hüküm | Tamamlanmış A/B v1 → v2 | Ortalama A / B v1 → v2 |
|---|---|---|---|---|
| H03 | REJECTED | REJECTED | 134/7 → 96/4 | −75,31 / −52,57 → −83,49 / −37,15 |
| H01 | REJECTED | REJECTED | 111/91 → 80/78 | −54,52 / −70,75 → −55,18 / −75,77 |
| H04 | REJECTED | **INSUFFICIENT_DATA** | 42/33 → 31/23 | −60,47 / −51,93 → −58,68 / −53,06 |
| H10 | INSUFFICIENT_DATA | INSUFFICIENT_DATA | 45/28 → 32/20 | −49,33 / −49,78 → −53,79 / −46,13 |
| H02 | — | REJECTED | yalnız v2 motorla koştu | −53,47 / −58,47 |

Hiçbir aile geçişe doğru hareket etmedi.

## 3. Ne söylüyor

- **Maliyet sonrası pozitif tek bir kol yok.** Koşan beş ailenin on kolunun
  ortalaması, tüm kollar dahil, **−37 $ ile −84 $** arasında. −37 $, H03'ün
  n=4'lük kontrol kolu; tabanı tutan kollarda aralık −53 $ ile −84 $.
- **Göreli etki de yok.** İki kolu da tabanı tutan iki karşılaştırmada (H01, H02)
  %95 aralık sıfırı içeriyor; H04 ve H10'da aralıklar ±40 $'tan geniş. En yakın olan H01: +20,59 $, alt sınır −2,26 $.
  15 denemelik bir aramada bu tek başına kanıt değildir.
- **Ortak payda.** Beş farklı kol değişkeni (OI teyidi, hacim yüzdeliği, fiyat
  gecikmesi, IV/RV ucuzluğu, tekrar) aynı sonuca varıyor: yapı başına ~−50 $
  civarında bir kayıp. Bu, kol değişkenlerinden çok **ortak kurgu** hakkında bir
  işarettir: dikey debit / seçici long yapılar, B kalite maliyet modeli, 100 $'lık
  R ve 5 işlem günü tutma. Altıncı bir aile bu paydayı değiştirmez.

## 4. Açık kalan

- **Bilinmeyen çıkışlar.** v2'de `no_exit_data` olan kayıtlar kayıp da sıfır da
  değil, **bilinmiyor**. Kontrat başına günlük NBBO geçmişi VERIFIED; birkaç yüz
  istekle kapatılabilir (`AUDIT_H10_TRADES.md` §6). Kapatılana kadar v2 sayıları
  yalnız **fiyatlanabilen** kayıtlar üzerindendir.
- **Pencere.** Tüm aileler aynı 85 seanslık hasadı sorguladı; dört çeyreklik
  rejim taraması yok.
- **Sıradaki karar Berkay'ın** (CLAUDE.md, strateji revizyonu): yeni aile mi,
  yoksa ortak kurgunun (yapı / tutma / maliyet) ayrı bir ön-kayıtla sorgulanması mı.
