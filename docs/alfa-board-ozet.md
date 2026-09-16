# Alfa Board — Berkay için özet

**Son güncelleme: 2026-09-16 08:45Z (TRT 11:45).** Bu dosya çalışma sürdükçe tazelenir; en son değil,
sürekli yazılır. Doğrulanmamış her şey "DOĞRULANMADI" diye etiketlidir.

---

## 1. BOARD DURUMU

- **Durum: ÇALIŞIYOR** (FAZ A canlıda).
- **URL:** https://uoa-detector-production.up.railway.app/ — kullanıcı adı/şifre masaüstündeki
  `uoa-dashboard-login.txt` dosyasında.
- **Canlı SHA:** `a01406c` (FAZ A merge'i, PR #10).
- **Son doğrulama:** 2026-09-16 06:00Z / 09:00 TRT.
- **Durma sebebi:** — (çalışma sürüyor).
- **Sırada:** FAZ B+D için PR #11 açık, CI bekleniyor; FAZ C1 (karar kartları) paralel yazılıyor.

## 2. GERİ ALMA KARTI

**Son bilinen iyi SHA: `a01406c`.**

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

## 4. ŞU AN NORMAL OLAN ŞEYLER (bug sanma)

| Gördüğün | Neden | Ne zaman düzelir |
|---|---|---|
| Vol-premium board boş | Gamma tablosu her yeniden başlatmada düşürülüp yeniden kuruluyor (REG-1) ve yalnızca seans içinde doluyor | 13:30Z / 16:30 TRT açılıştan ~4 dk sonra |
| Tüm çipler `kotasyon yok`, maliyet hücresi yok | Kotasyon tazeleyici yalnızca seans saatlerinde çalışır; 15 dakikadan eski kotasyon dürüstçe "yok" sayılır | 13:30Z açılışta |
| Kanıt aileleri `bilinmiyor` | Aşama telemetrisi yalnızca FAZ A deploy'undan (06:00Z) sonra yazılan baskılar için var | Açılıştan sonra gelen yeni baskılarda |
| Gecikmeli aileler (Kongre, İçeriden, Short/FTD) sayıma girmiyor | Tasarım: gecikmeli kanıt asla sayılmaz | Hiç — kasıtlı |

## 5. CANLIDA VAR / CANLIDA YOK

| Canlıda VAR | Canlıda YOK (henüz) |
|---|---|
| Hisse+yön başına tek satır, tüm baskılar toplanmış | Pozisyon büyüklüğü ($ ve sermaye %'si) |
| İşlem çipi + maliyet kapısı ("Alabileceklerimi göster") | Başabaş vs ATM straddle karşılaştırması |
| Alış/satış tarafından yön okuması | Kovalama hükmü ("geç kaldın") |
| Altı aileli kanıt şeridi, bilinmeyen = taranmış ve sayılmaz | Rejim bandı (piyasa gelgiti, dealer gamma, VIX) |
| Zorunlu `AMA` karşı-argümanı | Portföy örtüşmesi ("zaten bu bahittesin") |
| Ceza defteri + skor yalnızca Denetim bloğunda | Gecikmeli kanıt kovası (Kongre / İçeriden / Short-FTD) |
| | *(yukarıdaki 6 satır PR #11'de hazır ve yeşil, canlıya alınmayı bekliyor)* |
| "Bugün temiz aday yok" durumu | **Karar kartları (Logla / Pas geç) — en kritik eksik** |
| Vol board + "bu 'vol sat' demek değildir" cümlesi | Pas defteri `/defter` ve dolum kaydı |

## 6. YOKLUĞUNDA ALDIĞIM KARARLAR

Henüz bu oturumda yeni karar yok; FAZ A kararları `docs/alfa-board-decisions.md` P1–P20'de.
Bu bölüm her fazdan sonra dolacak (en fazla 7 madde, her biri "Claude: P## geri al" diye yapıştırılabilir).

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

- **Karar kartları kapalı:** bugün pas geçtiğin işlemler hiçbir yere kaydedilmiyor — o veri geri gelmez.
  Bu, kalan sürede ilk hedefim.
- **FAZ B/D arayüzü henüz canlıda değil:** büyüklük, başabaş/straddle, kovalama, açılış-kapanış,
  katalizör çipi, rejim bandı, portföy örtüşmesi ve gecikmeli kanıt kovası tek dalda birleşti,
  3.645 test yeşil, PR #11 açık. CI yeşil olunca canlıya alınacak ve doğrulanacak.
- **Yeni güvenlik rayları (PR #11 içinde):** `/health` artık hangi commit'in ayakta olduğunu söylüyor ve
  `scripts/verify_live_board.sh` tek komutla deploy sonrası tüm kontrolleri (sağlık, şifre duvarı, render,
  dürüstlük denetimi, `alfa_` satır sayıları) koşuyor.

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
Alfa Board (Phase 5.2) devam. Oku: docs/alfa-board-ozet.md, docs/alfa-board-decisions.md,
docs/phase-5.2-alfa-board-acceptance.md (donmuş), docs/phase-5.2-decision-cards-acceptance.md (donmuş).
Canlı: https://uoa-detector-production.up.railway.app/ · main = a01406c (FAZ A).
Sıradaki iş: FAZ C1 karar kartları (Logla/Pas geç), sonra FAZ B/D arayüzü, sonra C3 dolum, C2 /defter.
Kurallar: eşik değiştirme, donmuş sözleşmeyi düzenleme, alfa_ tablolarını düşürme, tek seferde tek PR,
her merge'den sonra canlıyı doğrula, bozuksa geri al.
```

## 12. EK — mühendislik detayı (okumana gerek yok)

- **PR'lar:** #9 (iv-rank hotfix), #10 (FAZ A, merge `a01406c`).
- **Testler:** FAZ A tepesinde 3.134 test yeşil; mypy --strict 162 dosya; ruff temiz. Her commit izole gate'ten geçti.
- **Yeni tablolar:** `alfa_quote`, `alfa_contract_depth`, `alfa_net_prem`, `alfa_ticker_info`,
  `alfa_stage_telemetry`, `alfa_print_meta` (hepsi oluştu, açılışta dolacak).
- **Branch'ler:** `p52-faz-b` (B1–B6b), `p52-faz-d` (D1–D3 + 3 düzeltme), `p52-wf2-base` (entegrasyon tabanı).
- **Kayıt defteri:** REG-1 gamma tablosunun restart'ta düşürülmesi; REG-2 vol board boş-durum metni İngilizce;
  REG-3 `IV-rank 75` sabiti profilden okunmalı.
