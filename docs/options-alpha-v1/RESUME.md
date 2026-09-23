# options-alpha-v1 — devam etme

Bu iş kesilirse buradan devam edilir. Hafızaya değil, koşulabilir komutlara ve
diskteki kanıta dayanır.

---

## 1. Nerede olduğunu anla

```bash
cd /Users/berkay/uoa-yeni
git log --oneline -1
git status --short
curl -s https://uoa-detector-production.up.railway.app/health
gh pr list --state open
```

Sonra bu sırayla oku: `docs/options-alpha-v1/STATUS.md` (kilometre taşları ve
bütçe) → `DECISIONS.md` (neden böyle kurulduğu) → `BLOCKERS.md` (neyin gerçekten
engelli olduğu).

Raporun tarihli tespitleriyle deponun bugünkü hâlini karıştırma. Canlı SHA'yı
`/health` söyler, hiçbir dosya değil.

---

## 2. Yetenek matrisini yeniden üret

Matris hatırlanmaz, ölçülür. ~19 istek:

```bash
set -a && . ./.env && set +a
uv run python scripts/probe_uw_option_capability.py
```

Çıktı: `artifacts/options-alpha-v1/capability_matrix.json`. Her satır VERIFIED /
PARTIAL / DENIED / UNAVAILABLE / UNVERIFIED taşır ve `quota` alanı hesabın o
andaki gerçek harcamasını gösterir.

**Bir satır VERIFIED değilse üzerine bir şey inşa etme.** Önce neden değiştiğini
bul: sağlayıcı mı değişti, hesap erişimi mi, yoksa senin çağrın mı yanlış.

---

## 3. Kapı (her commit'ten önce)

```bash
env -u UNUSUAL_WHALES_API_KEY -u THETADATA_API_KEY -u THETADATA_USERNAME \
  uv run pytest -q
uv run mypy --strict src/ webapp/
uv run ruff check .
uv lock --check
```

**Her komutun çıkış kodunu ayrı yakala.** `| tail -1` yazarsan pipeline'ın kodu
`tail`'inki olur ve düşen kapı başarı görünür — bu depoda bir kez oldu.
`set -e` de zinciri durdurmadı. Doğrusu: her komutu kendi log dosyasına yönlendir,
`$?`'i hemen bir değişkene al, yakalanan kodlara göre dallan.

---

## 4. Dokunulmayacaklar

- `profiles/v5_*.yaml`, `v6_*.yaml` eşikleri — test koşulu, ayar değil
- Donmuş kabul dokümanları
- Study F / G özellik listesi ve model ızgarası
- `alfa_` tablolarında DROP / DELETE / ALTER
- Mevcut v5 karar-destek ekranının anlamı — yeni opsiyon ekranı **ayrı** eklenir
- Emir yolu: broker bağlantısı ve gerçek emir kapsam dışı, PAPER dışına çıkılmaz

Yeni strateji için **yeni** profil ve **yeni** ön-kayıt açılır; eskisi
değiştirilmez.

---

## 5. Veri kalitesi kuralı

Bu kapsamdaki opsiyon verisi **B seviyesidir** (gün sonu / seyrek zamanlı gerçek
snapshot). Zaman alanı `last_tape_time` son *işlem* zamanı, kotasyon zaman
damgası değil.

Yapılabilir: muhafazakâr varsayımlı, gecikmesi açıkça modellenmiş araştırma.
Yapılamaz: gün içi gerçekleşme, stop/hedef sıralaması, "şu fiyattan dolardı".

Her sonuç bu etiketi taşır. Düşük kaliteyle yüksek güvenli hüküm verilmez.

---

## 6. Sıradaki iş

`STATUS.md`'deki kilometre taşı tablosu tek doğrudur; bu bölüm onu tekrarlamaz,
yalnız o an açık olanı işaret eder. **2026-09-23 itibarıyla** M0/M1/M3/M4/M5/M5b/M8
kapandı, M2 ve M7 kısmen, M6'nın engeli ölçüldü. Açık olanlar:

1. **Açık PAPER pozisyonlarının izlenmesi.** `/opsiyon` ekranı commit'li
   artefaktları okur; kendisi yeni fiyat çekmez ve açık pozisyonu ilerletmez.
   Canlı zamanlayıcıya bağlanması yazılmamış koddur, dış engel değil.
2. **Kalan 10 hipotez ailesi** (`HYPOTHESES.md`). Sıradaki her aile için önce
   ön-kayıt yazılıp **commit edilir**, sonra koşucu yazılır. H03 ve H01'de bu
   sıra tutuldu; bozulmaz.
3. **M2'nin kalan yapıları.** `structures.py` dördünü de fiyatlıyor, ama seçici
   yalnız çıplak long ve dikey debit üretiyor.

Piyasa kapalıysa akış tamamlanmış bir seans üzerinden tarihli uç noktalarla
koşulur ve kartta **replay** olarak etiketlenir. Eski bir kartı "şimdi al" gibi
sunma; gerçek fırsat yoksa güncel fiyat veya kontrat uydurma.

**Hüküm durumu:** bu kapsamda koşan iki araştırma ailesinin ikisi de
REDDEDİLDİ (`RESULT_H03.md`, `RESULT_H01.md`). Çalışan bir hat var; kanıtlanmış
bir edge yok. İkisi farklı cümle ve karıştırılmaz.
