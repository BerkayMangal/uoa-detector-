# options-alpha-v1 — engeller

Yalnız **doğrulanmış dış bağımlılıklar** buraya yazılır. Düzeltilebilir kod
hatası, zor tasarım kararı veya uzun test engel değildir; onlar iş kalemidir.

Her satır: ne engelli, neyi durduruyor, hangi somut kanıt, açılma koşulu.

---

## B1. Yetkili ekran doğrulaması — `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`

> **2026-09-23 — ÇÖZÜLDÜ, auth zayıflatılmadan.** Engelin kesin nedeni parolanın
> yokluğu değildi. `scripts/verify_live_board.sh` kimlik bilgilerini önce ortamdan,
> sonra `RAILWAY_DIR` ile Railway CLI'dan okur. Makinedeki CLI oturum açıktı
> (`railway whoami`), ama hiçbir dizine bağlı değildi. Karalama bir dizin servise
> bağlandı (`railway link --project unique-balance --service uoa-detector-`) ve
> betik `RAILWAY_DIR=<o dizin>` ile koşuldu. Parola hiçbir çıktıya yazılmadı.
> Sonuç, canlı `main` üzerinde: `/` ve `/opsiyon` kimliksiz **401**, kimlikli **200**;
> `/opsiyon state ok`; dürüstlük denetimi 20 satırda **PASS**; **VERIFY: PASS**.
> Aynı yoldan alınan `/opsiyon` HTML'i tarayıcıda da açılıp gözle görüldü. İki
> içerik kusuru bulundu: "ailelerin tamamı reddedildi" cümlesi (M5d zaten
> INSUFFICIENT_DATA'ydı) ve H04'ün düzeltilmemiş v1 hükmü. İkisi de
> `p80-options-paper-tracker`'da düzeltildi.
> **Tekrar etmek için tek komut** (bağlı bir dizin varken):
> `RAILWAY_DIR=<bağlı dizin> bash scripts/verify_live_board.sh`
> Aşağıdaki tablo tarihsel kayıt olarak duruyor.

| | |
|---|---|
| **Durum** | ~~BLOCKED — sahip eylemi gerekiyor~~ → ÇÖZÜLDÜ 2026-09-23 (yukarıda) |
| **Neyi durduruyor** | Canlı sayfa içeriğinin doğrulanması: yeni opsiyon ekranının gerçek tarayıcıda açılması, dürüstlük denetimi, `alfa_` satır sayıları, açılma süresi ölçümü |
| **Kanıt** | 2026-09-23 ölçümü, `main` 929a624 canlı: `/health` → 200 ve doğru SHA; `/`, `/defter`, `/gamma`, **`/opsiyon`** → **401**. Kimlik bilgileri tanımsız olsaydı uygulama 503 verirdi, yani duvar çalışıyor ve parola sahipte |
| **Neyi durdurmuyor** | Kod, testler, araştırma koşuları, PAPER motoru, matris, backfill. Hepsi bu parolasız ilerler |
| **Açılma koşulu** | İki ortam değişkeni. Sonra: `bash scripts/verify_live_board.sh` — script 2026-09-23'ten beri `/opsiyon`'u da kontrol ediyor: kimlik bilgisiz 401, kimlik bilgisiyle 200, `data-state` işareti ve hüküm satırlarının görünürlüğü |

**Not:** Bu engel, yapılabilecek işlerin arkasına saklanamaz. Ekran kodu yazılır,
test edilir, deploy edilir; yalnız "yetkili ekranı gözümle gördüm" iddiası
bekler.

**2026-09-23 durumu:** o not artık geçmiş zaman değil, yapıldı. `/opsiyon` yazıldı
(`webapp/options_board.py`, 11 test), merge edildi (PR #70) ve **canlıda** auth
duvarının arkasında 401 dönüyor. Doğrulama rail'i de ekranı kapsayacak şekilde
genişletildi. B1 artık tek bir şeye indi: iki ortam değişkeni girilip script'in
koşulması. Bekleyen iddia yalnızca **gözle görme**; kod, test, deploy ve rail
tarafında bekleyen iş kalmadı.

---

## B2. ThetaData — kimlik doğrulama yok

| | |
|---|---|
| **Durum** | BLOCKED — `THETADATA_AUTH_REQUIRED` |
| **Neyi durduruyor** | Gün içi kotasyon serisi (A kalite veri), tick seviyesi opsiyon geçmişi |
| **Kanıt** | 2026-09-15 yetenek probe'u: terminal girişi `Invalid credentials` ile reddedildi, port 25503 hiç açılmadı, planlanan 6 istekten 0'ı gönderilebildi (`docs/thetadata-capability-probe.md`) |
| **Neyi durdurmuyor** | Opsiyon araştırmasının tamamı. UW yolu gün sonu NBBO ve kontrat geçmişi veriyor — B kalite, ama çalışıyor |
| **Açılma koşulu** | Geçerli kimlik bilgisi. **Yeni abonelik açmak bu görevin kapsamı dışında** |

**Kapsam notu:** Terminalin localhost'a bağlanması mevcut topolojinin çalışmadığını
gösterir; "her mimari imkânsız" demek değildir. Ama yeni host/ücret gerektiren
çözüm bu görevde açılmaz.

---

## B3. Gün içi kotasyon zaman damgası — sağlayıcı sınırı

| | |
|---|---|
| **Durum** | PARTIAL — sınır, engel değil |
| **Neyi sınırlıyor** | Kanıt seviyesi. Gün içi fill sırası, stop/hedef tetiklenme sırası ve "şu fiyattan dolardı" iddiası bu veriyle yapılamaz |
| **Kanıt** | `option-chains` ve `/historic` satırlarındaki zaman alanı `last_tape_time` — **son işlem** zamanı, kotasyon zaman damgası değil. Kotasyonun hangi an geçerli olduğu bilinmiyor |
| **Sonucu** | Tüm opsiyon sonuçları **B kalite** etiketi taşır (bkz. `DECISIONS.md` D4) |
| **Açılma koşulu** | Zaman damgalı gün içi kotasyon kaynağı (ThetaData → B2) |

---

## Engel OLMAYANLAR

Kayda geçsin diye: aşağıdakiler bu dosyaya girmez ve girmedi.

- **`/historic` boş dönüyordu.** Kendi ayrıştırma hatamdı — zarf `data` değil
  `chains`. Düzeltildi, 92 günlük seri geliyor. Sağlayıcı engeli diye
  kaydedilmedi (`DECISIONS.md` D3).
- **Piyasa kapalı.** Uçtan uca akış tarihli uç noktalarla tamamlanmış seans
  üzerinden koşuluyor; zaman sorunu, engel değil.
- **Çok bacaklı/spread mantığı yok.** Yazılmamış kod, dış engel değil.
- **Açık pozisyon izleme yok.** Aynı — yazılmamış kod. 2026-09-23'te yazıldı:
  `PAPER_TRACKER.md` (canlıda koşması merge'e bağlı; bu bir engel değil, sıra).
- **`take_profit_or_stop` `NotImplementedError`.** Aynı — uygulanacak iş kalemi.
