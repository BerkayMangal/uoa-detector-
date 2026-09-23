# options-alpha-v1 — durum

**Faz 5.24** · dal `p69-options-alpha-exit-engine` · taban `main` 3143e12
**Son güncelleme:** 2026-09-23 12:10Z

Ana ürün **opsiyon sinyal terminali**. Hisse tarafı yalnız karşılaştırma kolu.

---

## Kilometre taşları

| # | İş | Durum | Kanıt |
|---|---|---|---|
| **M0** | API yetenek matrisi | **DONE** (merge, canlı) | `artifacts/.../capability_matrix.json` · 9/9 VERIFIED |
| **M1** | Uçtan uca: veri → aday → kontrat/spread → maliyet/risk → PAPER kart | **DONE** (merge, canlı) | `replay/2026-09-22/paper_card_{SPY,QQQ}.json` |
| **M2** | Dört temel yapı | **KISMEN** | `structures.py` dördünü de fiyatlıyor ve test ediyor; seçici long + dikey debit üretiyor |
| **M3** | Çıkış motoru + gerçek sonuç kaydı | **DONE** | `exits.py` · 9 elle-hesap test · `replay/2026-09-08/*_outcome.json` |
| **M4** | 12 hipotez ailesine veri fizibilitesi | **DONE** | `HYPOTHESES.md` — 8 tam, 3 kısıtlı, 0 erişilemez |
| **M5** | Desteklenen ailelerin koşan uygulaması ve sonuçları | TODO | — |
| **M6** | Track B replikasyonu | **ENGEL ÖLÇÜLDÜ** | Pencere ~95 seans, şartı 4 çeyrek → kısmi test / yetersiz süre |
| **M7** | Ayrı opsiyon ekranı + tarayıcı doğrulaması | TODO | B1 yalnız *gözle doğrulamayı* tutar |

---

## M3 — çıkış motoru ve ilk gerçek sonuçlar

`take_profit_or_stop` bu depoda hiç uygulanmamıştı; legacy motor
`NotImplementedError` fırlatıyor ve **öyle kalıyor** — yeni motor onun yerine
geçmiyor, yanına ekleniyor.

Üç çıkış varyantı, sonuçlar görülmeden donduruldu: `time_only`,
`time_and_stop`, `time_target_stop`. Stop ve hedef **yapının kendi çıkış
değerini** izler, dayanak fiyatını değil.

### Gerçek koşu: giriş 2026-09-08, tamamlanmış beş seansa karşı

| Kart | Yapı | Giriş | Sonuç | P&L |
|---|---|---|---|---|
| **SPY** `sig_d1ce1ed7` | 768/769 call debit | 0,62 | süreli çıkış, 0,34 | **−30,60 $ (−%47,4)** |
| **QQQ** `sig_591e4d83` | 700/699 put debit ×2 | 0,41 | stop, 1. günde | **−47,20 $ (−%54,1)** |

SPY'da üç varyant da aynı sonucu verdi — değer stop seviyesinin (0,31) altına hiç
inmedi. QQQ'da süreli varyant sonuna kadar taşıdı ve **−49,20 $** ile daha kötü
bitirdi; stop erken çıkmanın işe yaradığı tek örnek bu, ama tek örnekten kural
çıkmaz.

**İkisi de zarar. Bu bir sonuçtur, kusur değil** ve hiçbir şekilde
güzelleştirilmedi.

### Ölçüme dair iki dürüstlük kararı

**Paket gün içi aralığı üretilmedi.** `/historic` her bacağın günlük yüksek ve
düşüğünü veriyor; uzun bacağın yükseğinden kısa bacağın düşüğünü çıkarmak
paketin "olabileceği en iyi değeri" verirdi — ama o kombinasyon **hiç var
olmadı**, iki uç aynı ana denk gelmek zorunda değil. Farklı zamanların en iyi iki
fiyatını birleştirmek yasak. Bu yüzden yalnız kapanış kullanıldı ve motor
"gün içi dokunuşlar görünmez" notunu kartın kendisine düşüyor.

**Aynı gün iki eşiğe de dokunulursa sıra uydurulmuyor.** Günlük veri hangisinin
önce olduğunu söyleyemez; motor belirsizliği işaretliyor ve **muhafazakâr** dalı
(stop) uyguluyor, kârlı olanı değil.

### M3 sırasında yakalanan iki kusur

1. **Belirsizlik dalı hiç tetiklenemiyordu.** Günde tek kapanış değeriyle bir
   gözlem aynı anda hedefin üstünde ve stopun altında olamaz — hedef stoptan
   büyük. Yani §14'ün istediği koruma, çalışamayacak bir kod parçasıydı.
   Gözlem artık gün içi aralık taşıyor ve dal gerçekten çalışıyor.
2. **Tetik metni sağlanmamış bir koşulu iddia ediyordu.** QQQ'da hiçbir uygun
   kontratta hacim açık pozisyonu aşmıyordu (3.008'e karşı 40.509), kod yedek
   seçime düşüyordu, ama kart yine de "hacim>OI (yeni pozisyonlanma)" yazıyordu.
   Metin artık hangi dalın çalıştığını söylüyor ve yedek seçimde koşulun
   **sağlanmadığını** açıkça yazıyor.

---

## M4 — 12 ailenin fizibilitesi

`HYPOTHESES.md`: **8 tam desteklenir**, **3 kısıtlı** (H05/H08/H09 — sektör
üyeliği, kazanç takvimi ve IV-rank geçmişi nokta-zaman güvenli değil),
**0 erişilemez**. §9'un "en az altı aile" hedefi veri açısından karşılanabilir.

Sıralama ekonomik gerekçeye göre: H03 → H01 → H04 → H06 → H10 → H02.

---

## M6 — Track B: ölçülmüş engel

Özgün profil ve kabul şartları değiştirilmeden koşulur. Ama pencere **~95 seans**
(2026-05-13'ten bugüne), Track B'nin kendi şartı **4 çeyrekte 3'ü pozitif** ve
yılda ≥30 işlem. 95 seans ≈ 4,5 ay, dört çeyreği kapsamıyor.

**Hüküm: KISMİ TEST / YETERSİZ SÜRE.** Yeni hipotez sonuçları Track B başarısı
diye yazılamaz.

---

## Kaynak bütçesi (ölçüldü)

| Kaynak | Değer |
|---|---|
| UW günlük limit | **30.000** |
| Bugün harcanan | **211** |
| Güvenli tavan | 5.000/gün |
| Disk | artifact'lar ~3 MB |

Kısıt kota değil: **veri kalitesi** (B seviyesi) ve **risk bütçesi** (100 $ R).

---

## Tekrar üretme

```bash
uv run python scripts/probe_uw_option_capability.py
uv run python scripts/options_alpha_replay_card.py --session 2026-09-08 --ticker SPY
uv run python scripts/options_alpha_score_card.py \
  artifacts/options-alpha-v1/replay/2026-09-08/paper_card_SPY.json
```

---

## Henüz yapılmamış olanlar (açıkça)

- Hiçbir hipotez ailesi koşmadı. Tetik `H00_pipeline_smoke` ve **edge iddiası
  taşımıyor**; iki gerçek sonuç da zarar.
- Açık PAPER pozisyonları canlı zamanlayıcıda izlenmiyor — motor var, iş yok.
- Opsiyon ekranı yok.
- Mutasyon kanıtı tablosu yazılmadı.

Bunların hiçbiri dış engel değil; yazılmamış kod. Engeller `BLOCKERS.md`'de.
