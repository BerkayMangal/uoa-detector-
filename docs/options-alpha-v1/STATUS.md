# options-alpha-v1 — durum

**Faz 5.24** · dal `p68-options-alpha-v1-capability` · taban `main` 5b7917c
**Son güncelleme:** 2026-09-23 11:20Z

Ana ürün **opsiyon sinyal terminali**. Hisse tarafı yalnız karşılaştırma kolu.

---

## Kilometre taşları

| # | İş | Durum | Kanıt |
|---|---|---|---|
| **M0** | API yetenek matrisi — ölçülmüş, uydurulmamış | **DONE** | `artifacts/options-alpha-v1/capability_matrix.json` · 9/9 VERIFIED · 19 istek |
| **M1** | Uçtan uca: gerçek veri → aday → kontrat/spread seçimi → maliyet/risk → PAPER kart | **DONE** | `artifacts/.../replay/2026-09-22/paper_card_{SPY,QQQ}.json` · 2 kart, 4 dürüst ret |
| **M2** | Dört temel yapı (long call/put, bull call debit, bear put debit) | **KISMEN** | `structures.py` dördünü de fiyatlıyor ve test ediyor; seçici şu an yalnız long+dikey debit üretiyor |
| **M3** | Çıkış motoru: `take_profit_or_stop` gerçek kodla + açık pozisyon izleme | TODO | — |
| **M4** | 12 hipotez ailesine veri fizibilitesi | TODO | — |
| **M5** | Desteklenen ailelerin koşan uygulaması ve gerçek sonuçları | TODO | — |
| **M6** | Track B replikasyonu (değişmeden) veya kesin örneklem engeli | TODO | — |
| **M7** | Ayrı opsiyon ekranı + tarayıcı doğrulaması | TODO | B1 engeli yalnız *gözle doğrulamayı* tutar, kodu değil |

---

## M1 — gerçek veriden üretilen kartlar

Seans **2026-09-22**, dondurulmuş zincir anlık görüntüleri, altı isme aynı donmuş
kurallar uygulandı.

| İsim | Tarandı → uygun | Sonuç |
|---|---|---|
| **SPY** | 1.142 → 123 | **KART** · 775/776 call debit · net debit 0,58 · azami zarar 60,60 $ |
| **QQQ** | 1.374 → 97 | **KART** · 760/761 call debit · net debit 0,48 · azami zarar 50,60 $ |
| AAPL | 442 → 26 | risk kapısında ret |
| AMD | 948 → 31 | risk kapısında ret |
| NVDA | 492 → 35 | risk kapısında ret |
| TSLA | 688 → 41 | risk kapısında ret |

**Dört ret gerçek ve doğru.** 100 $'lık R, bu isimlerde 0,25–0,70 delta bandındaki
hiçbir yapıyı alamıyor. Kural gereği adet sıfır yazıldı ve **gereken asgari bütçe**
bildirildi; bütçe büyütülmedi, daha ucuz görünen uzak-OTM kontrata kaçılmadı.

Kartların ikisi de `[RESEARCH_ONLY] [WATCH]` — `PAPER_ENTRY_READY` **değil**.
Kapalı bir seansın gün sonu anlık görüntüsünden üretildiler; giriş gibi sunmak
"eski kartı şimdi al diye göstermek" olurdu.

**Tetik kasten en sade olan:** hacmi açık pozisyonunu aşan, seansın en çok işlem
gören uygun kontratı. **Doğrulanmış edge'i yok** ve kart bunu kendi karşı
argümanında yazıyor. Bu kilometre taşı hattın çalıştığının kanıtı, sinyal değil.

### M1 sırasında yakalanan ve düzeltilen üç kusur

1. **Kayma modeli tersti.** Haircut paket ortasına uygulanıyordu, yani makas
   genişledikçe kayma *azalıyordu* — kötü piyasa ucuz görünüyordu. Artık gerçekten
   işlem gören fiyata uygulanıyor. Kendi yazdığım test yakaladı.
2. **Spread istemeden genişliyordu.** Strike merdiveni *uygun* kontratlardan
   kuruluyordu, o yüzden SPY'da bir sonraki strike 776 değil 777 çıkıyor ve
   `width_strikes: 1` iki dolarlık spread üretiyordu. Kısa bacak yönsel bir ifade
   değil hedge'dir; delta bandından geçmesi gerekmez. Merdiven artık tam zincirden
   kuruluyor — düzeltmeden sonra SPY 60,60 $'a düştü ve bütçeye sığdı.
3. **Hedef ulaşılamazdı.** İlk kart 60,60 $ hedef yazıyordu ama yapının azami kârı
   39,40 $'dı. Tanımlı-riskli yapıda hedef artık yapısal tavanla sınırlanıyor ve
   sınırlandığı kartın kendi metninde görünüyor.

---

## M0 — ölçülen yetenekler

Dokuz satırın hepsi gerçek isteklerle **VERIFIED**. Tasarımı belirleyen dört bulgu:

**1. Tarihli opsiyon zinciri gerçekten tarihsel.** Kanıt satır sayısı değil — iki
tarihte de bulunan 11.218 kontratın **10.927'sinin değerleri farklı**.

**2. Kontrat geçmişi çıkış fiyatlamasının omurgası.** `/historic` günlük
`nbbo_bid`/`nbbo_ask` serisi veriyor, örnek kontratta **92 gün**.

**3. Aday üreteci hazır.** `screener/option-contracts` nokta-zaman çalışıyor,
~48 alan ve ~90 filtre parametresi.

**4. Çok bacaklı işlemler gerçek** — `strategy`, `net_bid`/`net_ask`, `breakevens`,
`max_loss`/`max_profit`. **24 saatlik sorgu penceresi** sınırı var.

**Araştırma penceresi: 2026-05-13 → bugün.** 05-13 doğru tarihli 13.604 satır
veriyor, 05-12 → 403.

**Kalite seviyesi B, A değil.** Zaman alanı `last_tape_time` yani **son işlem**
zamanı. Gün içi fill sırası veya stop tetiklenme sırası iddiası yapılamaz.

---

## Kaynak bütçesi (ölçüldü, varsayılmadı)

| Kaynak | Değer |
|---|---|
| UW günlük limit | **30.000** (`x-uw-token-req-limit`) |
| Bugün harcanan | **207** (M0 19 + anlık görüntüler) |
| Bu iş için güvenli tavan | 5.000/gün — üretim sorgularına öncelik |
| Paralellik | en fazla 3 alt ajan |
| Disk | artifact'lar ~1 MB |

Kısıt kota değil, **veri kalitesi** (B seviyesi) ve **risk bütçesi** (100 $ R).

---

## Tekrar üretme

```bash
uv run python scripts/probe_uw_option_capability.py
uv run python scripts/options_alpha_replay_card.py --session 2026-09-22 --ticker SPY
```

---

## Henüz yapılmamış olanlar (açıkça)

- Açık pozisyon izleme ve sonuç kaydı yok — kart üretiliyor, izlenmiyor
- `take_profit_or_stop` hâlâ `NotImplementedError`
- Opsiyon ekranı yok
- Hiçbir hipotez ailesi koşmadı; tetik `H00_pipeline_smoke` ve edge iddiası taşımıyor

Bunların hiçbiri dış engel değil; yazılmamış kod. Engeller `BLOCKERS.md`'de.
