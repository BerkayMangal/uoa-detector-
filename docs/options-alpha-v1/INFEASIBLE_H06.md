# H06 — bu ölçüyle sınanamaz (hipotez testi KOŞULMADI)

**Faz 5.24 · options_alpha_v1** · ölçüm **2026-09-23**
**Kanıt betiği:** `scripts/options_alpha_h06_power.py` (yalnız sayım, kota harcamaz)

Bu bir **sonuç belgesi değildir.** H06 için hiçbir hipotez testi koşulmadı, hiçbir
P&L hesaplanmadı ve hiçbir hüküm verilmedi. Belge, ailenin **neden
dondurulmadığını** kaydeder.

**Deneme bütçesi değişmedi: 9** (H03 3 + H01 3 + H04 3). Koşulmayan aile deneme
tüketmez.

---

## 1. Hipotez ne olacaktı

Bir hissenin opsiyon ilgisi belirli bir vadede **yoğunlaşıyorsa** — o vadenin
zincirdeki hacim payı kendi geçmişine göre artıyorsa — o vadedeki kontratların
sonraki net opsiyon P&L'i, yoğunlaşmanın olmadığı vadelerdekilerden iyi olur.

Taslak `HYPOTHESES.md` sıralamasında dördüncü sıradaydı ve taslağı yazıldı. Ön-kayıt
**dondurulmadı**; dondurmadan önce koşulan güç analizi aşağıdakini gösterdi.

## 2. Denenen üç yapı ve sayımları

Hepsi **yalnız sayım**: hiçbirinde P&L, çıkış veya sonuç hesaplanmadı.

| # | Yapı | Gözlem | Taban (30) | Asıl sorun |
|---|---|---|---|---|
| 1 | Pay-0 tabanı, **eşli** | 2882 | eşli ~43,9 **tutar** | Medyan artış **+0,061**; dağılım baştan sona pozitif (%10=+0,003) |
| 2 | 20 seans listelenmiş, **eşli** | 523 | ~1,6 **tutmaz** | Eşlenmiş seans-isim yalnız **7–8** |
| 3 | 20 seans listelenmiş, **eşlemesiz** | 523 | ~59,3 / ~37,5 **tutar** | Kol A'nın **%65–67'si aylık vade** |
| 3b | Eşlemesiz, **aylık hariç** | 118 | ~0,7 **tutmaz** | Kol B **3–4** gözleme çöküyor |

### 2.1 Birinci yapı: ölçü yoğunlaşmayı değil listeleme yaşını okuyordu

İlk kurgu, geçmişte listelenmemiş bir vadeye pay **0** yazıyordu; gerekçe "yok
gerçekten pay yok demektir"di. Sayımlar bunu çürüttü: **medyan artış +0,061** ve
dağılımın %10'luk dilimi bile pozitif (+0,003). Gerçek bir yoğunlaşma ölçüsünde
medyan sıfır civarı olmalıdır.

Sebebi mekanik: vadeler zamanla listeye girer, dolayısıyla yeni listelenen her vade
sıfırlar tabanına karşı büyük bir "artış" gösterir. Üstüne, bir vadenin payı ona
yaklaşıldıkça doğal olarak büyür. Ölçülen şey bilgi değil, **listeleme yaşı ve
vadeye yaklaşma sürüklenmesiydi**.

Düzeltme: vadenin trailing **20 seansın tamamında** listelenmiş olmasını şart
koşmak. Sonuç, ölçünün onarıldığını gösteriyor — dağılımda artık gerçek bir negatif
kuyruk var (%10 = **−0,099**, %25 = **−0,030**), yani ilgi **kaybeden** vadeler
görünür hâle geldi. Aylık vade payı da %51'den %12–14'e indi.

### 2.2 Ama onarılan ölçüyle tasarım çöküyor

Filtre gözlemi 2882'den **523**'e indiriyor ve aynı seans-isimde *hem* yoğunlaşan
*hem* yoğunlaşmayan, **ve ikisi de 20 seans boyunca listelenmiş** iki vade bulunması
nadir: eşlenmiş seans-isim **7–8**, projeksiyon ~1,6 kayıt.

Aynı-seans eşlemesi hipotezin gereği değildi; piyasa-günü etkisini elemek için
**benim eklediğim** bir kontroldü. Kaldırılması meşru bir tasarım kararı, ama
gerçek bir zayıflatma ve öyle adlandırılıyor.

### 2.3 Eşlemesiz yapı tabanı tutuyor — ama ölçtüğü şey takvim

Eşlemesiz sayımlar tabanı geçiyor (kol A ~59,3, kol B ~37,5). Bedeli şu: kol A'nın
**%65–67'si üçüncü cuma**, yani aylık vade. Takvimi tasarımdan çıkardığınızda kol B
**3–4 gözleme** çöküyor ve hiçbir eşik tutmuyor.

Bu ikisi birlikte tek bir şeyi söylüyor: bu veride, 20 seans boyunca listelenmiş
vadeler arasında pay **kaybeden** neredeyse yalnızca aylık-vade çevresindeki
artıklardır. "Yoğunlaşma" ağırlıkla **takvim + vadeye yaklaşma** demektir.

## 3. Neden dondurulmadı

Tabanı tutan **tek** yapı, kollarının öncelikle **vade tipine** (aylık / değil) göre
ayrıştığı yapı. Böyle bir aileyi koşmak, ön-kayıt taslağımın §5'inde zaten yazdığım
sonuca varırdı: *"kol A ağırlıklı olarak aylık vadelere düşüyorsa sonuç
'yoğunlaşma işe yarıyor' diye değil, **takvim etkisi ayrıştırılamadı** diye
raporlanır."*

Yani hükmü koşumdan **önce** biliyordum. Yorumlanamayacağı belli bir aileyi koşup
REDDEDİLDİ ya da GEÇTİ etiketi üretmek, sayı üretmek olurdu; bulgu üretmek değil.

**Üçüncü yapısal varyantta duruldu.** Örneklem uyana kadar tasarımı değiştirmeye
devam etmek, eşik alışverişinin daha ince bir hâlidir: her varyant sonuca kör
seçilse bile, arama uzayı büyüdükçe seçim maliyeti gerçek olur. Denenen üç varyantın
tamamı yukarıda, aramanın kendisi olarak duruyor.

## 4. Bunun söylemedikleri

- **Hipotez çürütülmedi.** Sınanmadı. Yoğunlaşmanın bilgi taşıyıp taşımadığı açık
  bir soru olarak kalıyor.
- **`HYPOTHESES.md` yanlış değildi.** O dosya H06'yı **veri erişimi** açısından
  DESTEKLENİR saymıştı ve bu doğru: uç noktalar ve alanlar var, hasat yeterli, kota
  maliyeti sıfır. Çöken şey erişim değil, **kol kurgusunun istatistiksel
  fizibilitesi**. İkisi farklı iddia.
- **Daha uzun pencere yardımcı olabilir.** Pay kaybeden haftalık vadeleri anlamlı
  sayıda görmek için 85 seans yetmiyor. Bu bir dış engel değil, **süre**.

## 5. Yeniden üretme

```bash
uv run python scripts/options_alpha_h06_power.py
uv run python scripts/options_alpha_h06_power.py --out /tmp/h06_anchors.json
```

Betik iki tabloyu (tüm vadeler / aylık hariç) ve denenen sekiz eşik çiftini birlikte
basar; eşli ve eşlemesiz projeksiyonlar yan yana durur. Yayımlanan tablo, geniş bir
aramadan seçilmiş bir satır değil, aramanın tamamıdır.

## 6. Sıradaki

Dondurulmuş sıralamada beşinci: **H10 — uzun prim / konveksite.** Yapı motoru zaten
mevcut. H06 atlanmadı; **ölçülüp kaydedildi**.
