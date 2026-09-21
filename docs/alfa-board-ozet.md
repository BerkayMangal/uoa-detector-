# Alfa Board — Berkay için özet

**Son güncelleme: 2026-09-21 06:45Z (TRT 09:45).** Bu dosya çalışma sürdükçe tazelenir; en son değil,
sürekli yazılır. Doğrulanmamış her şey "DOĞRULANMADI" diye etiketlidir.

---

## 1. BOARD DURUMU

- **Durum: ÇALIŞIYOR** — FAZ A, B, C ve D canlıda.
- **URL:** https://uoa-detector-production.up.railway.app/ — kullanıcı adı/şifre masaüstündeki
  `uoa-dashboard-login.txt` dosyasında.
- **Canlı SHA:** `/health` ne diyorsa odur. Buraya sabit bir SHA yazmak işe yaramıyor: her merge Railway'i
  yeniden deploy ettiği için yazdığın an bir sonraki merge'de yanlışa dönüyor (21 Eylül'de iki kez oldu).
  Kontrol: `curl -s https://uoa-detector-production.up.railway.app/health`
  **Kapsam** (21 Eylül itibarıyla): tahta ve rayların üstüne Faz 5.3 spot karar çerçevesi (ATR durdurma,
  hisse adedi, hedef), Study E/F/G, çıkış-kotasyonu sızıntısının kapatılması ve bayat-metin düzeltmeleri.
- **Son doğrulama:** 2026-09-21 07:44Z, PR #62 merge'inden hemen sonra — Railway `1ef8d89`'u **~60 saniyede** aldı,
  `/health` 200 ve doğru commit'i bildiriyor, `/` ve `/defter` şifresiz 401 (duvar deploy'dan sağ çıktı;
  kimlik bilgileri tanımsız olsaydı 503 derdi). **DOĞRULANMADI:** sayfa içeriği, açılma süresi, dürüstlük
  denetimi ve `alfa_` satır sayıları — bunlar `WEB_AUTH_USER`/`WEB_AUTH_PASSWORD` gerektiriyor ve bende yok.
- **Durma sebebi:** — (çalışma sürüyor).
- **Sırada:** bugün 13:30Z (16:30 TRT) açılışta canlı veriyle doğrulama; kapanıştan sonra Study G'nin ilk seans satırı.

## 2. GERİ ALMA KARTI

**Son bilinen iyi SHA: `f8d9821`.**

Tahta bozuksa, sadece şunu yapıştır:

```
git revert -m 1 <bozuk-merge-sha> && git push origin main
```

Railway ~2 dakikada eski hâle döner. Bozuk merge'in SHA'sını bilmiyorsan `git log --oneline -5 origin/main`
ile en üstteki merge satırına bak. `a01406c`'nin kendisi bozulursa aynı komutu onunla çalıştır; tahta
FAZ A öncesine (eski dashboard) döner, veri kaybı olmaz.

## 3. 60 SANİYEDE NE GÖRECEKSİN

1. URL'i aç, şifreyi gir → başlıkta **Alfa Board**, altında `Son baskı … ET … önce` satırı.
2. İlk satıra bak → hisse + yön, yanında **işlem çipi** (`İŞLENİR` / `DAR` / `İŞLENMEZ` / `kotasyon yok`)
   ve **kanıt şeridi** (altı aile: Akış, Dealer gamma, Karanlık havuz, Sektör, Fiyat teyidi, Açık pozisyon).
3. Her satırda **`AMA …`** ile başlayan karşı-argüman cümlesi olmalı. Yoksa bu bir hatadır.
4. `Denetim` bloğunu aç → 0–1 skoru ve ceza defteri **yalnızca burada** görünür; satır yüzünde asla.

## 3.5 AÇILIŞTAN SONRA NE ÖLÇTÜM (16:38 TRT, canlı veri)

Sabahki kontroller piyasa kapalıyken yapılmıştı; kapalıyken her şeyin "bilinmiyor" demesi hiçbir şey
kanıtlamaz. Açılıştan sonra ölçtüklerim:

| Ne | Gerçek değer |
|---|---|
| Satırlar ve çipler | 17 satır: 9 İŞLENİR, 3 DAR, 2 İŞLENMEZ, 3 kotasyon yok |
| Kanıt aileleri | 17 lehte, 15 aleyhte, 48 nötr (ölçüldü), 18 bilinmiyor — artık ölçülüyor |
| Maliyet | `Gidiş-dönüş (1 kontrat, iki bacak, komisyon dahil) $11.30`, makas %1,4, bid $6.95 / ask $7.05 |
| Kotasyon yaşı | `kotasyon 179 sn önce alındı · son işlem 3 dk önce` |
| Başabaş / straddle | 14 satırda, ör. `Başabaş için %3.8 gerekir` |
| Kovalama | 14 satırda, ör. `baskı $14.15 → şimdi $14.65 (ask), %3.5 yukarıda` |
| Rejim bandı | `IV vadesi contango (IV30 %13.7…)` |
| Gecikmeli kova | 17 satırın hepsinde `ek kanıt (gecikmeli)` |
| Çıkış derinliği | ör. `712 kontrat (son işlem anında)` |
| Veri tabloları | kotasyon 44, ATM 27, vade 238, net prim 54, telemetri 858, rejim 13, derinlik 10 satır |

**Dikkatini çekecek tek şey:** pozisyon büyüklüğü her satırda **"0 lot"** diyor. Bu bir hata değil —
varsayılan R değeri 100 $ ve bir kontrat ~705 $, yani 1 lot bile riski aşıyor. Gerçek sermaye ve R
değerlerini söylediğin anda bu hücre anlamlı sayıya döner (aşağıda 1. madde).

## 4. ŞU AN NORMAL OLAN ŞEYLER (bug sanma)

| Gördüğün | Neden | Ne zaman düzelir |
|---|---|---|
| Vol-premium board boş | Gamma tablosu her yeniden başlatmada düşürülüp yeniden kuruluyor (REG-1) ve yalnızca seans içinde doluyor | 13:30Z / 16:30 TRT açılıştan ~4 dk sonra |
| Tüm çipler `kotasyon yok`, maliyet hücresi yok | Kotasyon tazeleyici yalnızca seans saatlerinde çalışır; 15 dakikadan eski kotasyon dürüstçe "yok" sayılır | 13:30Z açılışta |
| Kanıt aileleri `bilinmiyor` | Aşama telemetrisi yalnızca FAZ A deploy'undan (06:00Z) sonra yazılan baskılar için var | Açılıştan sonra gelen yeni baskılarda |
| Gecikmeli aileler (Kongre, İçeriden, Short/FTD) sayıma girmiyor | Tasarım: gecikmeli kanıt asla sayılmaz | Hiç — kasıtlı |

## 5. CANLIDA VAR / CANLIDA YOK

**Canlıda olanlar** (hepsi 10:17Z'de doğrulandı, tahta 0,94 saniyede açılıyor):

| Ne | Ne işe yarar |
|---|---|
| Hisse + yön başına tek satır | Bir ismin tüm baskıları tek yerde toplanır, dağınık kart yığını yok |
| İşlem çipi + maliyet kapısı ("Alabileceklerimi göster") | Alış ask'ten, çıkış bid'den, komisyon dahil; kotasyonun yaşı yazılı |
| Altı aileli kanıt şeridi | Bilinmeyen aile taranmış ve sayılmaz; 3+ bilinmeyende "Güçlü" yasak |
| Zorunlu `AMA` karşı-argümanı | Her satır kendi aleyhine en güçlü cümleyi de söyler |
| Ceza defteri + 0–1 skoru **yalnız Denetim bloğunda** | Skor satır yüzünde, çipte veya sıralamada asla görünmez |
| **Karar kartları: `Logla` / `Pas geç`** | Bastığın an satırın gördüğün hâli dondurulur ve kalıcı saklanır — pas geçtiklerin de |
| Pozisyon büyüklüğü (1 lot $ ve sermaye %'si, risk kovası) | Sermaye/R değerleri henüz senin onayında: "(varsayılan değer)" yazıyor |
| Başabaş vs ATM straddle | "Başabaş için %X gerekir · straddle bu vadeye %Y fiyatlıyor" — olasılık iddiası yok |
| Kovalama hükmü | "hâlâ makul / dikkat / geç kaldın"; "geç kaldın" doğrudan `AMA`ya düşer |
| Açılış-kapanış (T+1 OI teyidi) + katalizör çipi | Pozisyon açılıyor mu kapanıyor mu; vadeye kadar katalizör var mı |
| Rejim bandı | Piyasa gelgiti, SPY/QQQ dealer gamma, IV vade yapısı + 3 tetikleyici. Kanıt sayımına **girmez** |
| Portföy örtüşmesi | "zaten bu bahittesin" rozeti, sermaye başlığı, tek-bahis şeridi |
| Gecikmeli kanıt kovası (Kongre / İçeriden / Short-FTD) | Bildirim tarihi ve gecikmesiyle; **asla sayılmaz** |
| Günlük iş saati | Seans öncesi/sonrası işler restart'ta ne atlıyor ne tekrarlıyor |
| Deploy doğrulama rayları | Her deploy sonrası sağlık + şifre duvarı + **açılma süresi** + dürüstlük denetimi tek komutta |
| **Pas defteri `/defter`** | Logladıkların ve pas geçtiklerin aynı tabloda; her karta 1 ve 5 işlem günü sonra **piyasadan arındırılmış** sonuç yazılır (SPY'a göre fark). Örnek sayısı 20'nin altındayken sadece adet gösterilir, oran/ortalama yok |
| **Dolum kaydı (`Dolum gir`)** | Gerçek dolum fiyatını kartın donmuş kotasyonuyla karşılaştırır: giriş ask'e, çıkış bid'e göre kayma, $ ve mid'e oran olarak. Maliyet varsayımının canlı testi |

**Canlıda olmayanlar:**

| Ne yok | Sana maliyeti |
|---|---|
| VIX vade yapısı | UW volatilite eklentisi yok; tahtada "kapsam-dışı" yazıyor |
| ThetaData bağlantısı | Bilinçli: tahta ThetaData'ya bağlı değil (bkz. para kararı) |

## 6. YOKLUĞUNDA ALDIĞIM KARARLAR

Hepsi `docs/alfa-board-decisions.md` P21–P33'te, gerekçesi ve nasıl geri alınacağıyla.

| # | Karar | Geri almak için |
|---|---|---|
| P24 | Render bütçesi testi artık makineyi değil **ölçeği** ölçüyor; 1,5 s hedefi canlıda ölçülüyor | "Claude: P24'ü geri al" |
| P25 | `/health` artık hangi commit'in ayakta olduğunu söylüyor; `verify_live_board.sh` deploy sonrası her şeyi tek komutta kontrol ediyor | "Claude: P25'i geri al" |
| P22 | Günlük işler için saat + kalıcı gün işareti (restart'ta ne atlıyor ne tekrarlıyor) | "Claude: P22'yi geri al" |
| P21 | Gecikmeli ailelerde "hiç sorulmadı" ile "soruldu, kayıt yok" ayrımı için ek tablo | "Claude: P21'i geri al" |
| P26 | Karar kartları FAZ B beklenmeden yazıldı (tek geri dönüşsüz madde o) | "Claude: P26'yı geri al" |
| — | FAZ B+D gün içinde 9,4 s render yüzünden geri alındı, sebebi bulundu (satır şablonu her satırda derleniyordu), düzeltildi ve **geri getirildi; şu an canlıda** | "Claude: FAZ B/D'yi tekrar geri al" |
| P32 | FAZ C'nin günlük sonuç işi kayıt defterine eklendi — yazılmıştı ama **hiç çalışmamıştı** | "Claude: P32'yi geri al" |
| P33 | Canlı dürüstlük denetçisi, karşı argüman bulunamayan satırları artık haksız yere düşürmüyor | "Claude: P33'ü geri al" |
| P36 | Render dalgalanmasının sebebi ölçüldü: veritabanı büyük bir uygulamayla paylaşılıyor | geri alınacak bir şey yok, karar senin |

## 7. PARA KARARI — THETADATA

**Tavsiye: dondur veya iptal et.** Gerekçe: terminal girişi mevcut kimlik bilgileriyle reddedildi
(`Invalid credentials`), yani abonelik seviyesi doğrulanamıyor; tahta ThetaData'ya bağlı değil ve
bağlanması da planlanmıyor. Gecikmenin maliyeti: aylık abonelik ücreti kadar — veri kaybı yok, çünkü
tarihsel veri her zaman yeniden çekilebilir. **Hesabına dokunmadım**: hiçbir abonelik iptal/değişiklik
yapılmadı, indirme başlatılmadı. Ayrıntı: `docs/thetadata-decision.md`.

## 8. SENDEN GEREKEN — EN FAZLA 5 ŞEY

| # | Soru | Cevap vermezsen varsayılan |
|---|---|---|
| 1 | Gerçek sermaye, R ($) ve komisyon değerleri nedir? | 10.000 $ / 100 $ / 0,65 $ kullanılır ve her hücrede "(varsayılan değer)" yazar |
| 2 | ThetaData aboneliği: dondur / iptal / tut? | Hiçbir şey yapılmaz, ücret işlemeye devam eder |
| 3 | Katalizör sağlayıcısındaki FOMC/FDA hatası düzeltilsin mi? (skoru değiştirir) | Düzeltilmez; tahtanın kendi katalizör çipi doğru, skor girdisi eksik kalır |
| 4 | Haziran'dan kalma 2 açık journal işlemi kapatılsın mı? | Açık sayılır, sermaye başlığında "(vadesi geçti)" olarak görünür |
| 5 | UW volatilite eklentisi alınsın mı? (VIX vade yapısı) | Alınmaz; VIX vade yapısı "kapsam-dışı" yazar |

## 9. OLMAYANLAR / BOZULANLAR

- **21 Eylül'de düzeltilen üç bayat metin (hiçbiri eşik veya davranış değişikliği değil):**
  1. `/gamma` sayfası kendi veri kaynağı hakkında yanlış söylüyordu — "günlük ThetaData
     snapshot işinden tazelenir (UW'nin gamma uç noktaları bizim seviyemizde yok)". İkisi de
     artık yanlış: sayfayı besleyen, seans içinde 240 saniyede bir dönen canlı UW gamma
     döngüsü. Boş-durum metni de artık var olmayan bir betiği işaret ediyordu.
  2. `docs/BACKTEST.md`, replay motorunun çıkış kotasyonunu 3 aya kadar geriye yürüttüğünü
     anlatıyordu (`_MAX_WALKBACK_MONTHS = 3`). O sabit kodda hiçbir yerde yok ve aynı-gün
     kuralı 2026-09-20'de uygulandı; doküman sızıntının kapatılmasından önceye kalmıştı.
  3. Bu dosyanın kendisi canlı SHA olarak `0f71140` diyordu; canlı `d42e60b`.
- **Study G'nin ilk veri çekimi koşuldu (21 Eylül 06:40Z).** 71 istek, sıfır satır — ve bu
  doğru sonuç: `since 2026-09-19` filtresi çalışıyor, son mevcut seans 18 Eylül Cuma ve
  bugünkü seans henüz kapanmadı. Boru hattı aynı anda kanıtlandı: sınırsız tek-ticker
  denemesi SPY için 252 seans (2025-09-18..2026-09-18) döndürdü. İlk gerçek satır bu akşam
  kapanıştan sonra düşer.

- **17 Eylül'de üretimde bulunan ve düzeltilen iki şey:**
  1. **Pas defterinin günlük sonuç işi hiç çalışmamıştı.** İş yazılmış, test edilmiş ve canlıya çıkmıştı, ama günlük iş listesine hiç eklenmemişti; `alfa_job_run` tablosunda tek bir `outcomes` kaydı yoktu ve `alfa_outcome` tablosu veritabanında hiç oluşmamıştı. Bunu yakalaması gereken test, kontrol ettiği listeyi **kendi kuruyordu**, yani hiçbir zaman kırılamazdı. Düzeltildi; kanıtı bu akşam 17:30 ET'de `alfa_job_run`'da `outcomes` satırının düşmesi.
  2. **Canlı denetçi doğru bir sayfayı düşürdü** (bu ikinci kez): karşı argüman bulunamayan satırlar sözleşmenin ikinci biçimini basıyor, denetçi onu tanımıyordu. Düzeltildi ve teste bağlandı.
- **Dünkü akşam işleri çalıştı** (16 Eylül 21:02Z): günlük kapanışlar 2.520 satır, gecikmeli aileler 857 satır, ETF içerikleri 216 satır. Sabah işleri de çalıştı (17 Eylül 11:15Z).

- **Eksik özellik yok.** Karar kartları, pas defteri `/defter`, dolum kaydı, FAZ B ve FAZ D'nin tamamı
  canlıda ve dürüstlük denetiminden geçiyor.
- **Hız: şu an sorun yok, ama tam açıklayamadığım bir dalgalanma var.** Son ölçüm (deploy'dan 5 dk sonra,
  seans içinde): tahta **0,56-0,80 saniye**, sunucu tarafı 0,30 saniye, `/defter` 0,22 saniye — hedefin
  belirgin altında.
- **Dün "sebebini bulamadım" dediğim yavaşlamanın sebebi bulundu ve bizde değil.**
  17 Eylül 13:10Z'de üretimde ölçtüm: tahtanın her açılışta koştuğu run-listesi sorgusu
  **20,8 milisaniye** sürüyor, ama o aşama **440 milisaniye** ölçüyor. Aradaki fark disk.
  Sorgu planı sebebini söylüyor: 31 MB'lık tablo önbellekte değil, her seferinde diskten
  okunuyor.
  **Neden önbellekte değil:** tahtanın veritabanı 18 GB ve **başka, çok daha büyük bir
  uygulamayla paylaşılıyor** (at soyağacı tabloları: `registry_pedigree_edges` 7,46 milyon
  satır, `match_score_flags` 6,31 milyon, `candidate_prediction` 4,65 milyon). Onların okuma
  hacmi başka bir ölçekte — tek bir tabloda 1,62 **milyar** disk bloğu okuması. Postgres'in
  önbelleği 128 MB; o iş yükü çalıştıkça tahtanın küçük tabloları önbellekten atılıyor.
  Tahtanın `signal` tablosunun önbellek isabeti %63,5.
  **Bu tek mekanizma açıkta kalan her şeyi açıklıyor:** aynı kodun dakikalar arayla 0,014 s
  ve 0,44 s ölçülmesi, yerelde hiç tekrarlanamaması, bağlantı havuzu denemelerinin işi
  **kötüleştirmesi** (maliyet bağlantı değil disk), `/defter`'in hızlı kalması ve dünkü iki
  epizot. Dün elediklerim (veritabanı sorgusu, şablon, havuz, veri hacmi) doğruydu; eksik
  olan, sorunun bizim kodumuzda değil komşuda olmasıydı.
- **Senin için pratik anlamı ve KARAR GEREKTİREN KISIM:** tahta seans içinde 1,4-1,6
  saniyede açılıyor (hedef 1,5 s; bunun ~0,2 saniyesi benim buradan ölçtüğüm ağ gecikmesi).
  Gün içinde 2,6-4,5 saniyeye çıkan anlık sıçramalar da ölçtüm; o anların aşama dökümünü
  yakalayamadım, yani sebebi aynı mekanizma olabilir ama kanıtlamadım. Sebep bizde değil
  ama çözümü seçmek sende, çünkü biri para:
  **(1)** tahtaya kendi Postgres'ini al — temiz ve anında çözer, ikinci bir Railway
  veritabanı kadar tutar; **(2)** sıcak okumayı ortadan kaldır — tahtanın run listesinden
  ihtiyacı olan tek şey `run_id`, sayı ve son zaman; bunu worker'ın güncellediği küçük bir
  `alfa_` tablosunda tutmak her sayfa yüklemesinden 31 MB'lık taramayı siler. (2) bizim
  elimizde, eşik veya sözleşme değişikliği değil. Tavsiyem: (2)'yi her hâlükârda yap,
  komşu yoğunken de hızlı kalması gerekiyorsa (1)'i de.
- **Ölçüm kuralı (bugünün en pahalı dersi):** her deploy'dan sonra 3-5 dakika bekle; aynı sürüm o aralıkta
  5-13 saniye ölçülebiliyor. Bugün bir kez erken ölçüp doğru bir şeyi geri aldım, bir kez de erken ölçümü
  "soğuk" sayıp yanlış bir şeyi tuttum.
- **Doğrulama rayları canlıda:** `/health` hangi commit'in ayakta olduğunu söylüyor;
  `scripts/verify_live_board.sh` deploy sonrası sağlık + şifre duvarı + açılma süresi + dürüstlük denetimi +
  `alfa_` satır sayılarını tek komutta koşuyor; `scripts/audit_board_html.py` kuralları canlı HTML üzerinde
  denetliyor (şimdiye kadar iki yanlış alarmı düzeltildi; ikisi de artık teste bağlandı).

## 10. YAPMADIKLARIM (negatif teyit)

- Abonelik değiştirilmedi/iptal edilmedi.
- `profiles/*.yaml` eşiklerine dokunulmadı.
- Donmuş sözleşme dosyaları düzenlenmedi.
- ThetaData toplu indirme başlatılmadı.
- Hiçbir emir/işlem kodu çalıştırılmadı.
- Secret commit edilmedi, log'a basılmadı.
- Hiçbir `alfa_` tablosu düşürülmedi/sıfırlanmadı.

## 11. SIRADAKİ OTURUMA YAPIŞTIR

```
Alfa Board (Phase 5.2) devam. Oku, bu sırayla: docs/alfa-board-ozet.md,
docs/alfa-board-decisions.md (P1-P46), CLAUDE.md (D1-D12).
Donmuş, asla düzenleme: docs/phase-5.2-alfa-board-acceptance.md,
docs/phase-5.2-decision-cards-acceptance.md.
Canlı: https://uoa-detector-production.up.railway.app/ · ayakta olan SHA'yı `/health` söyler
İlk iş: RAILWAY_DIR=<railway dizini> bash scripts/verify_live_board.sh
(SHA argümanı opsiyonel; vereceksen `/health`'in bildirdiğini ver, buraya yazılmış bayat bir SHA'yı değil.)
(Railway CLI yoksa: WEB_AUTH_USER / WEB_AUTH_PASSWORD / DATABASE_URL ortam
değişkenlerinden okunur; eksik olan kontrol SKIP der ve çıkış kodu 2 = PARTIAL.)

Açık işler, sırayla:
1. alfa_job_run'da `outcomes` kaydı var mı (17:30 ET). Yoksa: iş çalıştı mı, hata mı verdi?
2. Render dalgalanması: gün içinde iki kez 9-10 s görüldü, sebebi isimlenmedi.
   Elenenler (TEKRAR DENEME): veritabanı (1,2 ms, indeksli), şablon (düzeltildi, 0,02 s),
   pool_pre_ping (iki kez denendi, iki kez 12 s, iki kez geri alındı), veri hacmi (çürütüldü).
   Sıradaki adım: build_alfa_page içinde kaynak-başına süre, webapp.main'den loglanır.
3. Berkay cevapladıysa: sermaye/R/komisyon (profiles/board_v1.yaml sizing.*).

Kurallar: eşik değiştirme, donmuş sözleşmeyi düzenleme, alfa_ tablolarını düşürme,
aynı anda tek PR, her merge'den sonra canlıyı doğrula, bozuksa 15 dk içinde geri al.
Ölçümü deploy'dan EN AZ 5 dakika sonra al (soğuk pencere aynı sürümü 5-13 s gösterebiliyor).
Yama uyguladıysan grep ile doğrula; sessizce uygulanmayan yama bu projede üç kez oldu.
```

## 12. EK — mühendislik detayı (okumana gerek yok)

- **PR'lar:** #9 (iv-rank hotfix), #10 (FAZ A, merge `a01406c`).
- **Testler:** FAZ A tepesinde 3.134 test yeşil; mypy --strict 162 dosya; ruff temiz. Her commit izole gate'ten geçti.
- **Yeni tablolar:** `alfa_quote`, `alfa_contract_depth`, `alfa_net_prem`, `alfa_ticker_info`,
  `alfa_stage_telemetry`, `alfa_print_meta` (hepsi oluştu, açılışta dolacak).
- **Branch'ler:** `p52-faz-b` (B1–B6b), `p52-faz-d` (D1–D3 + 3 düzeltme), `p52-wf2-base` (entegrasyon tabanı).
- **Kayıt defteri:** REG-1 gamma tablosunun restart'ta düşürülmesi; REG-2 vol board boş-durum metni İngilizce;
  REG-3 `IV-rank 75` sabiti profilden okunmalı.
