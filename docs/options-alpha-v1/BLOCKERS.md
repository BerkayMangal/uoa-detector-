# options-alpha-v1 — engeller

Yalnız **doğrulanmış dış bağımlılıklar** buraya yazılır. Düzeltilebilir kod
hatası, zor tasarım kararı veya uzun test engel değildir; onlar iş kalemidir.

Her satır: ne engelli, neyi durduruyor, hangi somut kanıt, açılma koşulu.

---

## B1. Yetkili ekran doğrulaması — `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD`

| | |
|---|---|
| **Durum** | BLOCKED — sahip eylemi gerekiyor |
| **Neyi durduruyor** | Canlı sayfa içeriğinin doğrulanması: yeni opsiyon ekranının gerçek tarayıcıda açılması, dürüstlük denetimi, `alfa_` satır sayıları, açılma süresi ölçümü |
| **Kanıt** | 2026-09-23 ölçümü: `/health` → 200 ve doğru SHA; `/`, `/defter`, `/gamma` → **401**. Kimlik bilgileri tanımsız olsaydı uygulama 503 verirdi, yani duvar çalışıyor ve parola sahipte |
| **Neyi durdurmuyor** | Kod, testler, araştırma koşuları, PAPER motoru, matris, backfill. Hepsi bu parolasız ilerler |
| **Açılma koşulu** | İki ortam değişkeni. Sonra: `bash scripts/verify_live_board.sh` |

**Not:** Bu engel, yapılabilecek işlerin arkasına saklanamaz. Ekran kodu yazılır,
test edilir, deploy edilir; yalnız "yetkili ekranı gözümle gördüm" iddiası
bekler.

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
- **Açık pozisyon izleme yok.** Aynı — yazılmamış kod.
- **`take_profit_or_stop` `NotImplementedError`.** Aynı — uygulanacak iş kalemi.
