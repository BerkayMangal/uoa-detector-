# Ön-kayıt — S01: ortak kurgunun kayıp ayrıştırması

**Faz 5.24 · options_alpha_v1** · donduruldu **2026-09-23**, ayrıştırmanın tek bir
bileşeni hesaplanmadan. Koşucu yazılmadan sabitlenir ve sonuçlara göre
değiştirilmez.

---

## 1. Neden bu çalışma

`FAMILIES.md`: koşan beş ailenin on kolunun hiçbiri maliyet sonrası pozitif değil;
kol ortalamaları −37 $ ile −84 $ arasında, beş farklı kol değişkeni aynı ~−50 $
kayba varıyor. Kazanma oranları **%3–12**. Rastgele yönlü bir dikey spread'in 5
günde ~%40 civarı kazanması beklenir; bu kadar düşük oran, kaybın kol
değişkenlerinden değil **ortak kurgudan** geldiğine işaret ediyor.

Ortak kurgu, sayımla teyit edildi (§9): 488 kaydın **tamamı** 1 strike genişlikli
debit dikey spread (`bull_call_debit` / `bear_put_debit`); tek bir çıplak long
yok. Giriş D+1 kapanışı ask/bid + %2 gecikme payı, çıkış 5. seans kapanışında
uzun bid − kısa ask, komisyon bacak başına 0,65 $ gidiş-dönüş, R = 100 $.

**Bu çalışma bir hipotez ailesi değildir.** Edge iddiası üretmez, `EXPLORATORY_PASS`
veremez ve 15'lik deneme bütçesine **eklenmez**. Yalnız kapı kapatabilir: kaybın
nereden geldiğini muhasebeleştirir ve sıradaki adımın aile mi, kurgu revizyonu mu
olacağını belirler.

**Dürüstlük notu:** kayıtların net P&L'i zaten biliniyor (aileler koştu). Bilinmeyen
ve burada sabitlenen, o P&L'in **bileşenlere nasıl ayrıldığı** ve ayrışmanın nasıl
**yorumlanacağı**dır.

## 2. Sorular

1. **Ayrıştırma:** ortalama kaybın ne kadarı piyasa hareketinden (mid → mid), ne
   kadarı maliyetten (makas, gecikme payı, komisyon)?
2. **Mid'de kazanma:** maliyetler sıfırken kazanma oranı ne?
3. **Rastgele taban:** aynı kapılar, aynı giriş/çıkış kuralı, ama ankraj **rastgele**
   seçilince kurgu aynı kaybı üretiyor mu? Aile sinyalleri rastgele girişten
   ayırt edilebiliyor mu?

## 3. Veri

**Sinyal kümesi:** v2 motorla koşan beş ailenin `time_only` çıkışında **fiyatlanmış**
(P&L'i `null` olmayan) kayıtları:
`h03_result_v2.json`, `h01_result_v2.json`, `h04_result_v2.json`,
`h10_result_v2.json`, `h02_result.json`.

**Birincil görünüm: tekil işlem.** Aynı (isim, giriş seansı, ankraj) birden fazla
ailede görünebilir; birincil ölçütlerde her işlem **bir kez** sayılır. Aile
başına görünüm ikincil olarak raporlanır.

**Hasat:** yerel, 85 seans. **0 UW isteği.**

## 4. Ayrıştırma (dondurulmuş özdeşlik)

Her kayıt için bacaklar motorun kendi `build_candidate`'iyle giriş seansında
yeniden kurulur (H10 denetimiyle aynı yol). `q` = yapı adedi, çarpan 100.

```
M0   = giriş seansı paket mid'i          = Σ(uzun mid) − Σ(kısa mid)
M5   = çıkış seansı paket mid'i           (motorun time_only çıkış seansı)
RAW  = giriş ham borcu                    = uzun ask − kısa bid
DEB  = motorun giriş borcu                = RAW × 1,02 (yuvarlanmış)
EXIT = motorun çıkış değeri               = uzun bid − kısa ask
COM  = gidiş-dönüş komisyon (toplam)

piyasa        = (M5 − M0)   × 100 × q
giris_makasi  = (RAW − M0)  × 100 × q
giris_gecikme = (DEB − RAW) × 100 × q
cikis_makasi  = (M5 − EXIT) × 100 × q
komisyon      = COM

net = piyasa − giris_makasi − giris_gecikme − cikis_makasi − komisyon
```

**Mutabakat kapısı:** her kaydın `net`'i motorun `time_only` P&L'iyle **kuruşu kuruşuna**
(|fark| ≤ 0,02 $, yuvarlama) eşleşmeli. Eşleşmeyen kayıt oranı **%2'yi aşarsa**
çalışma **durur**, yorum yazılmaz, uyuşmazlık raporlanır. %2'nin altındaki
uyuşmazlıklar tek tek listelenir ve ortalamalardan çıkarılmaz.

Mid'i hesaplanamayan (bid veya ask eksik) bacak varsa kayıt **ayrıştırılamaz**
olarak sayılır ve raporlanır; P&L'i uydurulmaz.

## 5. Ölçütler

Tekil işlem üzerinden:

- Her bileşenin ortalaması (işlem başına $) ve **ortalama net kayba oranı**.
- `maliyet_toplam = giris_makasi + giris_gecikme + cikis_makasi + komisyon`.
- **Mid'de kazanma oranı:** `piyasa > 0` olan işlem payı. Net kazanma oranıyla yan yana.
- `piyasa` ortalaması için **seans-blok bootstrap** %95 aralık (blok = giriş
  seansı), tohum **20260923**, tekrar **10.000**.
- Kırılım: yapı türü (`bull_call_debit` / `bear_put_debit`), isim (QQQ+SPY vs
  diğerleri), giriş makasının M0'a oranı (dörde bölünmüş).

## 6. Rastgele taban (dondurulmuş)

**Pencere ve döngü:** H01 ile aynı akış seansları (20 seanslık geçmiş sonrası),
giriş D+1 kapanışı, 5 işlem günü tutma, aynı `eligible_contracts` ve
`build_candidate` kapıları, **hiçbir hacim veya akış filtresi yok**.

**Ankraj seçimi:** `random.Random(20260923)`; seanslar kronolojik, isimler profil
sırasında (`universe.tickers`) gezilir; her (isim, akış seansı) için o seansın
uygun kontratları **`option_symbol`'e göre sıralanır** ve tek bir ankraj
`rng.choice` ile seçilir. Uygun kontratı olmayan seans-isim atlanır ama RNG
çağrılmaz. Yeniden örnekleme veya ikinci deneme yok.

Kayıtlar sinyal kümesiyle **aynı** ayrıştırmaya tabi tutulur.

**Karşılaştırma:** sinyal kümesi (tekil) net ortalaması − rastgele net ortalaması,
aynı bootstrap (blok = giriş seansı, havuzlanmış), aynı tohum.

## 7. Yorum kuralları (sonuçtan önce sabitlenir)

Sinyal kümesi, tekil işlem, `time_only`:

| Etiket | Koşul |
|---|---|
| **MALIYET_BAGLI** | `maliyet_toplam` ortalaması, ortalama net kaybın **≥ %75**'i **ve** `piyasa` aralığı sıfırı içeriyor |
| **PIYASA_SURUKLEMESI** | `piyasa` aralığının üst sınırı **< 0** |
| **KARMA** | ikisi de değil |

Sonuç cümleleri önceden yazılır:

- **MALIYET_BAGLI** → "Bu kurguda (1 strike dikey, B-kalite giriş/çıkış, R=100 $)
  maliyet, sinyalin taşıyabileceği her şeyi yiyor. Yeni aile koşmak anlamsız; bir
  sonraki adım **yapı/maliyet revizyonu**dur ve Berkay'ın kararıdır (CLAUDE.md,
  strateji revizyonu), ayrı bir ön-kayıtla."
- **PIYASA_SURUKLEMESI** → "Mid'de bile sistematik kayıp var. Kaynak (yön karışımı,
  theta, skew, seçim) ayrı bir ölçümle bulunmadan hiçbir revizyon önerilmez."
- **KARMA** → iki bileşen de raporlanır, öneri yazılmaz.

Rastgele taban:

- Fark aralığı sıfırı **içeriyorsa** → "Aile sinyalleri rastgele girişten ayırt
  edilemiyor." Bu, `FAMILIES.md`'nin göreli-etki bulgusunu genelleştirir.
- Aralık sıfırı **dışlıyorsa** → yönü ile raporlanır; **edge iddiası değildir**
  (sinyal kümesi 15 denemenin birleşimidir, bu bir seçim etkisi olabilir).

## 8. Bu çalışmanın veremeyeceği hüküm

- Başka bir yapı (çıplak long, geniş dikey, farklı tutma) **denenmez**; yalnız
  mevcut kurgu muhasebeleştirilir. Revizyon ayrı ön-kayıt ister.
- Gün içi fill yok (B kalite); "mid'de alınabilirdi" iddiası yok. Mid bir
  muhasebe ekseni, uygulanabilir bir fiyat değil.
- `no_exit_data` kayıtları bilinmiyor (`AUDIT_H10_TRADES.md` §6); ayrıştırma
  yalnız fiyatlanmış kayıtlar üzerindendir.
- Sinyal kümesi beş ailenin birleşimi; bağımsız bir örneklem değil.

## 9. Sayımlar (dondurmadan önce, P&L hesaplanmadan)

| Artefakt | Fiyatlanmış kayıt | bull_call_debit | bear_put_debit |
|---|---|---|---|
| h03_result_v2 | 100 | 36 | 64 |
| h01_result_v2 | 158 | 80 | 78 |
| h04_result_v2 | 54 | 26 | 28 |
| h10_result_v2 | 52 | 30 | 22 |
| h02_result | 124 | 56 | 68 |
| **Toplam** | **488** | | |
| **Tekil işlem** | **285** | | |

Çıplak long: **0**. `min_debit_reduction_pct` (%20) her kayıtta spread'i seçtirmiş.
