# Options Alpha v1 — sonuç raporu

**Tarih:** 2026-09-24 · **Okuyucu:** Berkay · **Kısa cevap aşağıda, ayrıntı altında.**

---

## Kısa cevap

**Elimizdeki veride, işlem maliyetlerinden sonra para kazandıran mekanik bir opsiyon
stratejisi yok.** Yedi ayrı test yapıldı; hiçbiri geçmedi. Bu artık "biraz daha
ayarlarsak çıkar" durumu değil. İki sebep var ve ikisi de ölçüldü:

1. **Akış sinyalleri (UOA) opsiyonun yönünü bilmiyor.** Sinyalle seçilen kontrat ile
   rastgele seçilen kontrat, maliyet öncesinde bile aynı sonucu veriyor.
2. **Maliyet, var olan küçük kenarların hepsini yiyor.** Dar dikey spread'lerde
   gidiş-dönüş maliyet paketin değerinin **%134**'ü; iron butterfly'da kredinin
   **%12**'si. İkisinde de maliyeti karşılayacak kadar prim/hareket yok.

Bu sonuç kötü haber ama **para kaybettirmeden** öğrenildi: hepsi geçmiş veriyle,
sıfır sermayeyle, önceden kayda geçirilmiş kurallarla.

---

## 1. Neyi sınadık

Her test **önce** kurallarıyla yazıldı ve dondu, sonra koşuldu. Sonucu görüp
kuralı değiştirmek yasaktı. Veri: 10 likit isim (SPY, QQQ, AAPL, NVDA, MSFT,
AMZN, META, TSLA, AMD, GOOGL), 14 Mayıs – 15 Eylül 2026, günlük opsiyon zincirleri.

| # | Soru (sade dille) | Sonuç |
|---|---|---|
| H03 | Akıştan sonraki gün **açık pozisyon artmışsa** (pozisyon açılmış demek) opsiyon kazandırır mı? | Hayır — reddedildi |
| H01 | Hisse için **olağandışı yüksek** hacim görülen opsiyon kazandırır mı? | Hayır — reddedildi |
| H04 | Akış, hisse **henüz hareket etmemişken** mi geliyor, yoksa hareketten sonra mı? | Veri yetmedi; eğilim hipotezin tersine |
| H10 | Opsiyon, hissenin gerçek oynaklığına göre **ucuzken** almak işe yarar mı? | Veri yetmedi; etki yok |
| H02 | Aynı kontrata **birkaç gün üst üste** olağandışı hacim gelmesi bir şey söyler mi? | Hayır — reddedildi |
| **S01** | Hepsi neden aynı ~50–80 $ kaybediyor? | **Kaybın tamamı maliyet** — piyasa tarafı sıfır |
| **S02** | Projenin tek sağlam bulgusu **vol primini** gerçek maliyetle satmak para eder mi? | Hayır — reddedildi, net şekilde |

Tüm ailelerin tek tablosu: `FAMILIES.md`.

## 2. Neden işe yaramadı — mantık zinciri

### 2.1 Akış sinyalleri: yön bilgisi yok

S01'de her işlemin sonucunu ikiye ayırdık:
**piyasa kısmı** (opsiyonun orta fiyatı ne kadar değişti) ve **maliyet kısmı**
(alış-satış makası, gecikme payı, komisyon).

| İşlem başına | Sinyalle seçilen (285 işlem) | Rastgele seçilen (75 işlem) |
|---|---|---|
| Piyasa kısmı | +3 $ (sıfırdan ayırt edilemez) | +1 $ |
| Maliyet | −74 $ | −70 $ |
| Net | −71 $ | −70 $ |
| Orta fiyatta kazanma oranı | %45 | %49 |

Okuma: **para atıp yazı-tura** gibi. Sinyal, rastgele seçimden daha iyi bir yön
bulmuyor. Bu, projenin eski bulgusuyla da aynı: "yönlü UOA akışında mekanik edge yok"
(Faz 3.6).

### 2.2 Dar dikey spread: maliyet makinesi

Ailelerin hepsi 1-strike genişliğinde dikey spread kurdu (motorun kuralı bunu seçtirdi).
Sorun şu:

- Her bacağın kendi alış-satış makası küçük: fiyatının **%1–2**'si.
- Ama 1-strike spread'in değeri, bacak fiyatlarının kabaca **onda biri**.
- Yani iki bacağın makası, paketin değerine göre **~10 kat büyüyor**.
- Üstüne, çıkışta makas girişe göre **1,5–2 kat** daha geniş (giriş kapıları dar
  makas seçiyor, çıkışta seçme şansın yok).

Sonuç: pozisyon açılır açılmaz, değerinin **%134**'ü kadar bir engelle başlıyor.
Bu yapıyla hiçbir sinyal para kazandıramaz.

### 2.3 Vol primi: vardı, bu dönemde yoktu

Projenin tek istatistiksel olarak sağlam bulgusu: opsiyonlar **ortalamada pahalı**
(örtük oynaklık gerçekleşenden yüksek). 2024–25 verisinde bu çok güçlüydü.
Daha önce "maliyetten sonra para değil" denmişti, ama o karar **varsayılan %10
makasa** dayanıyordu. Biz gerçek makası ölçtük: **%3,6**. Bu yüzden en umutlu yol
buydu ve S02 ile sınadık.

| Kısa iron butterfly, işlem başına | Değer |
|---|---|
| Satıcının orta fiyatta kazandığı prim | **−16 $** (sıfır) |
| Maliyet | −371 $ (kredinin %12'si) |
| Net | −387 $ |
| Risk başına getiri | **−%9,7** (güven aralığı tamamen negatif) |
| Maliyet sıfır olsa bile | −%0,5 |

Neden: bu dört aylık dönemde gerçekleşen hareket, örtük hareketin **~%97**'si kadardı.
Yani **satacak prim yoktu**. Makas düşük olsa da taşıyacak kenar yok.

Önemli: bu "vol primi hiç yoktur" demek değil. 2024–25'te vardı, 2026 yazında yoktu.
Rejime bağlı bir şey ve kısa pencerede sıfır olabiliyor. Ama **mekanik olarak her gün
satmak** bu dönemde para kaybettirdi.

## 3. Para gözüyle

10.000 $ hesap, S02'deki gibi her 5 günde bir 10 isimde birer iron butterfly
(her biri ~3.900 $ azami risk) bu hesaba zaten sığmaz. Ölçeklendirilmiş haliyle
bile tablo net: **16 giriş tarihinin 14'ü negatif** (pozitif iki tarih +328 $ ve
+385 $), en kötü tarih 10 pozisyonda toplam −14.293 $. Kaldıraçla bu, hesabı hızla
eritirdi.

## 4. Ne yapmamalı

1. **Aynı veride yeni strateji aramaya devam etmek.** 14 Mayıs – 15 Eylül penceresi
   yedi kez sorgulandı; artık "yanmış" (`docs/INDEX.md` §6'ya işlendi). Burada
   bulunacak bir "geçen" test, şans eseri geçmiş olma ihtimali yüksek bir test olur.
2. **Eşikleri gevşetip yeniden koşmak.** Ör. "taban 30 değil 25 olsun", "genişlik
   2 strike olsun". Sonucu gördükten sonra yapılan her ayar sahte edge üretir.
3. **Dar dikey spread'lerle yönlü opsiyon oynamak.** Maliyet matematiği ölçüldü:
   %134.

## 5. Ne yapmalı (öncelik sırasıyla)

1. **Ekranı karar desteği olarak kullan, sinyal olarak değil.** `/opsiyon` ve akış
   kartları bağlam veriyor (ne hareket ediyor, ne pahalı). Otomatik "al" sinyali
   değiller; S01 bunu ölçtü.
2. **İleriye dönük veri biriktir.** 16 Eylül'den sonrası temiz (yanmamış). Diğer
   oturumun kurduğu PAPER izleyici her gün çalışıyor. Kasım–Aralık gibi yeterli
   veri birikince **tek bir** önceden kayıtlı test (ör. S02'nin vadeye kadar tutan
   versiyonu, ya da IV-rank koşullu versiyonu) temiz pencerede koşulabilir.
3. **Gerçek fill ölç.** Maliyetin ~üçte biri "%2 gecikme payı" **varsayımından**
   geliyor. Gerçek broker'da birkaç küçük limit emriyle (gerçek para, en küçük
   boyut) fill kalitesini ölçmek, bu varsayımı gerçek sayıyla değiştirir. Bu,
   bir sonraki testin maliyet modelini doğru kurar. Emir kararı senin.
4. **Kendi diskresyonel okumanı ölç.** Projede açık kalan tek edge sorusu bu:
   senin gözünle seçtiğin işlemlerin bir kenarı var mı? Journal (`/journal`)
   bunun için kuruldu; en az 10 kayıt olmadan hüküm vermiyor.

## 6. Dosyalar

| Ne | Nerede |
|---|---|
| Bu rapor | `docs/options-alpha-v1/SONUC_RAPORU.md` |
| 12 aile tek tablo | `docs/options-alpha-v1/FAMILIES.md` |
| Maliyet ayrıştırması | `docs/options-alpha-v1/RESULT_S01.md` |
| Vol primi testi | `docs/options-alpha-v1/RESULT_S02.md` |
| Her testin ön-kaydı | `docs/options-alpha-v1/PREREG_*.md` |
| Ham sonuçlar (işlem işlem) | `artifacts/options-alpha-v1/*_result*.json` |
| Günlük durum | `docs/options-alpha-v1/STATUS.md` |

Her sayı tek komutla yeniden üretilir (her sonuç dosyasının sonunda yazıyor);
UW kotası harcamaz.
