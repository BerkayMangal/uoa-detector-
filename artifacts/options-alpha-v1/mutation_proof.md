# options-alpha-v1 — mutasyon kanıtı

**Koşum:** 2026-09-23T11:39:51.269349+00:00

Her koruma kasten bozuldu ve adı konmuş testin kırmızıya dönmesi beklendi.
Mutasyondan **sağ çıkan** bir test, testin kendisi hakkında bir bulgudur.

| Koruma | Adı konmuş test | Sonuç | Dosya geri alındı |
|---|---|---|---|
| Long girisi ask'ten fiyatlanir | `test_entry_reads_the_ask_and_never_the_bid` | test KIRMIZI (koruma gercek) | evet |
| Caprazlanmis kotasyon reddedilir | `test_a_crossed_quote_is_refused_rather_than_priced` | test KIRMIZI (koruma gercek) | evet |
| Butceye sigmayan yapi sifir adettir | `test_one_structure_over_budget_sizes_to_zero_and_says_what_it_needs` | test KIRMIZI (koruma gercek) | evet |
| Hedef yapinin tavanini asamaz | `test_a_target_can_never_exceed_the_structures_own_ceiling` | test KIRMIZI (koruma gercek) | evet |
| Replay karti giris olarak sunulmaz | `test_a_replay_card_is_never_offered_as_an_entry` | test KIRMIZI (koruma gercek) | evet |
| Ayni gun iki esik: sira uydurulmaz, stop uygulanir | `test_both_levels_inside_one_day_is_ambiguous_and_takes_the_stop` | test KIRMIZI (koruma gercek) | evet |
| Fiyatlanamayan pozisyon basarili islem sayilmaz | `test_no_priceable_day_is_not_a_successful_trade` | test KIRMIZI (koruma gercek) | evet |

**Toplam:** 7 mutasyon, **0** sorunlu.

Tüm korumalar mutasyonla kırmızıya döndü ve her dosya bayt-birebir geri alındı.
