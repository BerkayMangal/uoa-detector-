/* Automatic measurement and separate historical research. The server owns quote arithmetic. */
(() => {
  "use strict";
  const root = document.getElementById("theta-validation");
  if (!root) return;
  const byId = (id) => document.getElementById(id);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const money = (value) => {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const dollars = (value) => {
    const number = money(value);
    return number === null ? "Bilinmiyor" : new Intl.NumberFormat("tr-TR", {
      style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(number);
  };
  const clock = (value) => {
    if (typeof value !== "string" || !value) return "Bilinmiyor";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Bilinmiyor";
    return new Intl.DateTimeFormat("tr-TR", {
      timeZone: "UTC", day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(date) + " UTC";
  };
  const reasonText = (reason) => {
    const direct = {
      terminal_unreachable: "Mac’teki ThetaData Terminal’e ulaşılamadı.",
      authentication_failed: "ThetaData oturumu doğrulanamadı.",
      no_quotes: "İstenen zaman aralığında kullanılabilir kotasyon alınmadı.",
      entry_unavailable: "Giriş kotasyonu olmadığı için sonuç ölçülemedi.",
      entry_eligible_quote_unavailable: "Giriş aralığında uygun kotasyon bulunamadı.",
      exit_eligible_quote_unavailable: "Çıkış aralığında uygun kotasyon bulunamadı.",
      exit_batch_missing: "Çıkış kotasyonları henüz gelmedi.",
      exit_target_outside_regular_session: "Bu tutma süresi normal seansı aşıyor.",
      one_or_more_horizons_unknown: "Tutma sürelerinden en az biri henüz ölçülemedi.",
      unsupported_payload: "Gelen kotasyon verisi doğrulanamadı.",
      pending: "Mac bağlantısından kotasyon bekleniyor.",
    };
    if (direct[reason]) return direct[reason];
    if (typeof reason === "string" && reason.includes("no_quotes")) return direct.no_quotes;
    return "Yeterli ve doğrulanmış kotasyon yok; net sonuç bilinmiyor.";
  };
  const kindText = (symbol) => {
    const match = /^([A-Z]{1,6})(\d{6})([CP])(\d{8})$/.exec(symbol || "");
    return match ? match[1] + " · " + (match[3] === "C" ? "CALL" : "PUT") : "Opsiyon kontratı";
  };
  const requestJSON = async (url, payload) => {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const deadline = controller ? setTimeout(() => controller.abort(), 12000) : null;
    try {
      const response = await fetch(url, {
        method: payload === undefined ? "GET" : "POST",
        credentials: "same-origin", cache: "no-store",
        signal: controller ? controller.signal : undefined,
        headers: payload === undefined ? {Accept: "application/json"} : {Accept: "application/json", "Content-Type": "application/json"},
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      if (!response.ok) {
        const error = new Error("request_failed");
        error.status = response.status;
        throw error;
      }
      const data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("invalid_response");
      return data;
    } finally {
      if (deadline !== null) clearTimeout(deadline);
    }
  };
  const setMessage = (message, isError = false) => {
    const node = byId("theta-form-message");
    node.textContent = message;
    node.dataset.error = String(isError);
  };
  let sources = [];
  let busy = false;
  let refreshing = false;
  let refreshQueued = false;
  let rerunInProgress = false;
  let dayOverride = null;
  let reportDay = null;
  const openQuoteDetails = new Set();
  const revocationsInProgress = new Set();
  const count = (value) => Number.isInteger(value) && value >= 0 ? String(value) : "Bilinmiyor";
  const basis = (value) => value === "observed_committed_source_plus_60_seconds"
    ? "Yeni gözlenen kayıtlar"
    : value === "retrospective_publication_plus_60_seconds" ? "Geçmişten eklenen kayıtlar" : "Zaman dayanağı doğrulanamadı";
  const sourceScope = (context, historical = false) => {
    if (!context || typeof context !== "object") return null;
    if (context.scan_complete === true) return historical ? "Rapor hazırlanırken kaynak taraması tamamlanmıştı." : "Bu kontrolde seçili günün kaynak taraması tamamlandı.";
    const remaining = Number.isInteger(context.unobserved) && context.unobserved >= 0
      ? (context.unobserved_is_lower_bound ? "en az " : "") + context.unobserved : "bilinmiyor";
    return (historical ? "Rapor hazırlanırken işlenmemiş kaynak kaydı: " : "İşlenmemiş kaynak kaydı: ") + remaining
      + ". Sayılar yalnızca kaydedilmiş girişleri kapsar; kaynak taraması henüz tamamlanmadı.";
  };
  const exclusion = (reason) => ({
    unsupported_instrument: "Bu araç otomatik tek-kontrat ölçümüne uygun değil.",
    multiple_legs: "Plan birden fazla opsiyon bacağı içeriyor.",
    missing_contract: "Kayıtlı planda tam opsiyon kontratı yok.",
    invalid_contract: "Kontrat kimliği doğrulanamadı.",
    contract_direction_mismatch: "Kontratın yönü kayıtlı planla eşleşmiyor.",
    source_identity_mismatch: "Kaynak kaydın kimliği eşleşmiyor.",
    source_clock_invalid: "Kaynak kaydın zamanı doğrulanamadı.",
    source_snapshot_mismatch: "Kaynak görüntüsü doğrulanamadı.",
    source_card_missing: "Kaynak karar kartı okunamadı.",
    source_plan_mismatch: "Opsiyon planı kaynak kayıtla eşleşmiyor.",
    source_leg_not_single_long: "Plan tek bir uzun opsiyon bacağı değil.",
    nonstandard_multiplier: "Standart kontrat çarpanı doğrulanamadı.",
    expired_plan_before_target: "Plan, ölçüm başlangıcından önce geçerliliğini yitirmiş.",
    entry_outside_known_regular_session: "Ölçüm başlangıcı bilinen normal seansın dışında.",
    contract_expired_before_entry: "Kontrat, ölçüm başlangıcından önce vadesini doldurmuş.",
    source_payload_too_large: "Kaynak kayıt güvenli okuma sınırını aşıyor.",
  }[reason] || "Bu kayıt otomatik opsiyon ölçümüne alınamadı.");

  function renderDevices(data) {
    const panel = root.querySelector(".theta-connection");
    const title = byId("theta-connection-title");
    const description = byId("theta-connection-detail");
    const evidence = byId("theta-device-evidence");
    const container = byId("theta-devices");
    container.replaceChildren();
    const devices = Array.isArray(data.devices) ? data.devices.filter((row) => row && typeof row === "object") : [];
    const active = devices.find((row) => row.bridge_online === true && !row.revoked_at)
      || devices.find((row) => !row.revoked_at && row.state !== "revoked");
    panel.dataset.connectionState = "awaiting_connection";
    title.textContent = "ThetaData bağlantısı bekleniyor";
    description.textContent = "Mac’i bir kez bağla. İndirilen dosyanın kurulumunu çalıştırınca yeni ölçümler otomatik aktarılır.";
    evidence.textContent = "Henüz doğrulanmış ThetaData kotasyonu alınmadı. Dosya indirmek bağlantı kurmaz.";
    if (data.status === "storage_unavailable") {
      panel.dataset.connectionState = "error";
      title.textContent = "Bağlantı durumu okunamadı";
      description.textContent = "Bağlantı kayıtları şu anda okunamıyor; bağlı veya kapalı olduğu doğrulanamadı.";
    } else if (active && active.bridge_online === true) {
      panel.dataset.connectionState = "observed";
      title.textContent = active.state === "idle" || data.status === "idle" ? "Program bağlı, ölçüm bekliyor" : "Mac bağlantısı çalışıyor";
      description.textContent = "Program zamanı gelen ölçümleri otomatik alır. Yeni kayıtlar için dosyayı tekrar indirmen gerekmez.";
      if (active.terminal_state === "unreachable") {
        panel.dataset.connectionState = "error";
        title.textContent = "Program açık, ThetaData Terminal’e ulaşılamıyor";
        description.textContent = "Mac’te ThetaData Terminal’i açık tut. Bağlantı programı yeniden kontrol edecek.";
      } else if (active.terminal_state === "authentication_failed") {
        panel.dataset.connectionState = "error";
        title.textContent = "ThetaData oturumu doğrulanamadı";
        description.textContent = "Mac’teki ThetaData Terminal oturumunu kontrol et. UOA şifresi veya API anahtarı girmen gerekmiyor.";
      } else if (active.terminal_state !== "reachable") {
        title.textContent = "Program bağlı, Terminal kontrolü bekleniyor";
        description.textContent = "Programdan yanıt geldi; bu, ThetaData erişiminin veya kotasyonun doğrulandığı anlamına gelmez.";
      } else if (!active.last_validated_data_at) {
        description.textContent = "Program Terminal’e erişebildiğini bildiriyor. Henüz gerçek bir giriş kotasyonu doğrulanmadı; zamanı gelen ölçümler bekleniyor.";
      }
      if (active.last_error === "upload_failed" || active.outbox_count > 0) {
        panel.dataset.connectionState = "error";
        description.append(element("span", active.last_error === "upload_failed"
          ? " Son veri aktarımı kabul edilmedi; program saklanan paketi yeniden göndermeyi deneyecek."
          : " Mac’te sunucuya aktarılmayı bekleyen veri var; sonuçların tamamlandığı henüz doğrulanmadı."));
      }
    } else if (active && (active.state === "stale" || data.status === "stale")) {
      panel.dataset.connectionState = "error";
      title.textContent = "Mac’ten güncel yanıt alınamıyor";
      description.textContent = "Mac ve ThetaData Terminal açık olmalı. Eski bağlantı kaydı, programın şimdi çalıştığını kanıtlamaz.";
    } else if (active) {
      title.textContent = "Kurulumun ilk bağlantısı bekleniyor";
      description.textContent = "Dosya indirildiyse, indirme sayfasındaki kurulum komutunu Mac Terminal’de bir kez çalıştır.";
    } else if (data.status === "revoked") {
      title.textContent = "Mac bağlantısı kaldırılmış";
      description.textContent = "Otomatik veri aktarımına devam etmek için Mac’i yeniden bağla. Önceki ölçümler korunur.";
    }
    const evidenceDevice = active || devices.find((row) => row.last_validated_data_at) || devices[0];
    if (evidenceDevice) evidence.textContent = "Programın son yanıtı: " + clock(evidenceDevice.last_heartbeat_at)
      + " · Sunucuda doğrulanan son giriş kotasyonu: " + clock(evidenceDevice.last_validated_data_at);
    devices.forEach((device) => {
      if (!/^thetadev_[a-f0-9]{32}$/.test(device.device_id || "")) return;
      const row = element("div", undefined, "theta-device-row");
      row.append(element("span", "Kurulum: " + clock(device.created_at) + (device.revoked_at ? " · Bağlantısı kaldırılmış" : ""), "theta-help"));
      if (!device.revoked_at && device.state !== "revoked") {
        const revoke = element("button", "Bu bağlantıyı kaldır", "theta-secondary");
        revoke.type = "button";
        revoke.disabled = revocationsInProgress.has(device.device_id);
        revoke.addEventListener("click", async () => {
          if (revocationsInProgress.has(device.device_id)) return;
          revocationsInProgress.add(device.device_id);
          revoke.disabled = true;
          try {
            await requestJSON("/api/theta/devices/" + encodeURIComponent(device.device_id) + "/revoke", {});
            byId("theta-device-message").textContent = "Bu Mac bağlantısı kaldırıldı. Önceki kayıtlar ve sonuçlar korundu.";
            await refresh();
          } catch (_) {
            byId("theta-device-message").textContent = "Bağlantının kaldırıldığı doğrulanamadı. Durumu yenileyerek kontrol et.";
            revoke.disabled = false;
          } finally {
            revocationsInProgress.delete(device.device_id);
          }
        });
        row.append(revoke);
      }
      container.append(row);
    });
    if (data.truncated) container.append(element("p", "Bağlantı listesinin son bölümü gösteriliyor.", "theta-help"));
  }

  function renderReport(report) {
    const container = byId("theta-automatic-report");
    container.replaceChildren();
    if (!report || !Array.isArray(report.groups) || !report.groups.length) {
      container.append(element("p", "Henüz okunabilir ölçüm raporu yok. Zamanı dolan kayıtlar ve doğrulanmış kotasyonlar geldikçe kapsam oluşur.", "theta-empty"));
      return;
    }
    container.append(element("p", "Seans: " + String(report.day || "Bilinmiyor") + " · Son hesap: " + clock(report.created_at), "theta-help"));
    const reportScope = sourceScope(report.source_context, true);
    if (reportScope) container.append(element("p", reportScope, "theta-help"));
    report.groups.forEach((group) => {
      const section = element("section", undefined, "theta-report-group");
      section.append(element("h3", basis(group.entry_basis)));
      const totals = group.counts || {};
      section.append(element("p", "Ölçüme alınan: " + count(totals.admitted) + " · Kapsam dışı: " + count(totals.excluded), "theta-help"));
      const table = element("table", undefined, "theta-report-table");
      const head = element("tr");
      ["Süre", "Ölçülen", "Bekleyen", "Bilinmeyen", "Pozitif / negatif / sıfır", "Bilinen model neti"].forEach((label) => head.append(element("th", label)));
      const thead = element("thead"); thead.append(head); table.append(thead);
      const body = element("tbody");
      (Array.isArray(group.horizons) ? group.horizons : []).forEach((horizon) => {
        if (![15, 60].includes(horizon.minutes)) return;
        const row = element("tr");
        ["+" + horizon.minutes + " dk", count(horizon.evaluated), count(horizon.pending), count(horizon.unknown),
          count(horizon.positive) + " / " + count(horizon.negative) + " / " + count(horizon.zero),
          horizon.evaluated > 0 ? dollars(horizon.modeled_net_pnl_usd_total) : "Bilinmiyor"].forEach((value) => row.append(element("td", value)));
        body.append(row);
      });
      table.append(body);
      const scroll = element("div", undefined, "theta-table-scroll"); scroll.append(table); section.append(scroll);
      container.append(section);
    });
    container.append(element("p", "Her kayıt için bir kontrat varsayılır. Bilinen sonuçların toplamı hesap veya portföy getirisi değildir; eksik kayıtlar toplamda fiyatlanmış sayılmaz. +15 ve +60 dakika sonuçları alternatiflerdir, toplanmaz.", "theta-help"));
  }

  function renderAutomatic(data, legacy) {
    const title = byId("theta-automatic-title");
    const detailNode = byId("theta-automatic-detail");
    const observerStates = {
      disabled: ["Otomatik izleme kapalı", "Otomatik kayıt görevi etkin değil; yeni kayıtların izlendiği söylenemez."],
      storage_unavailable: ["Otomatik kayıtlar okunamadı", "Kayıt kapsamı şu anda doğrulanamadı; kayıt yokluğu veya sıfır sonuç varsayılmıyor."],
      awaiting_observer: ["İlk otomatik kontrol bekleniyor", "Otomatik görevin çalıştığı henüz doğrulanmadı."],
      stale: ["Otomatik kontrol gecikti", "Yeni girişlerin güncel olarak izlendiği doğrulanamadı. Önceki kayıtlar korunuyor."],
      failed: ["Otomatik kontrol tamamlanamadı", "Son kontrolde hata oluştu; yeni kayıtların tümünün alındığı doğrulanamadı."],
      no_source_storage: ["Giriş kayıtları okunamadı", "Otomatik görevin kaynak kayıtlarına erişimi doğrulanamadı; boş kapsam varsayılmıyor."],
    };
    const observerState = observerStates[data.status];
    title.textContent = observerState ? observerState[0] : "Otomatik kayıtlar";
    detailNode.textContent = observerState ? observerState[1] : data.last_tick_at ? "Otomatik kayıt görevi son kontrolde çalıştı." : "Otomatik görevin ilk kontrol kaydı bekleniyor.";
    if (data.last_tick_at) detailNode.append(element("span", " · Son kontrol: " + clock(data.last_tick_at)));
    const counts = data.status === "storage_unavailable" ? {} : data.counts || {};
    const list = byId("theta-automatic-counts"); list.replaceChildren();
    [["Gözlenen", "observed"], ["Ölçüme alınan", "admitted"], ["Ölçülen", "evaluated"], ["Bekleyen", "pending"], ["Bilinmeyen", "unknown"], ["Yeniden deneme gerekli", "retry_limit"], ["Kapsam dışı", "excluded"]].forEach(([label, key]) => detail(list, label, count(counts[key])));
    if (Number.isInteger(counts.pending_maturity) && Number.isInteger(counts.awaiting_connector)) {
      detailNode.append(element("span", " · Zamanı beklenen: " + counts.pending_maturity + " · Kotasyonu beklenen: " + counts.awaiting_connector));
    }
    const currentScope = sourceScope(data.source_context);
    if (currentScope) detailNode.append(element("span", " · " + currentScope));
    reportDay = /^\d{4}-\d{2}-\d{2}$/.test(data.day || "") ? data.day : null;
    if (reportDay) byId("theta-day").value = reportDay;
    byId("theta-rerun").disabled = rerunInProgress || !reportDay || data.status === "storage_unavailable";
    byId("theta-cohort-download").href = "/api/theta/automatic/cohort" + (reportDay ? "?day=" + encodeURIComponent(reportDay) : "");
    const jobs = new Map((legacy && Array.isArray(legacy.jobs) ? legacy.jobs : []).map((job) => [job.request_id, job]));
    const receipts = new Map();
    (legacy && Array.isArray(legacy.recent_results) ? legacy.recent_results : []).forEach((receipt) => {
      const prior = receipts.get(receipt.request_id);
      if (!prior || String(receipt.received_at) > String(prior.received_at)) receipts.set(receipt.request_id, receipt);
    });
    const container = byId("theta-automatic-results"); container.replaceChildren();
    const rows = Array.isArray(data.recent) ? data.recent : [];
    if (data.status === "storage_unavailable") container.append(element("p", "Otomatik kayıtlar okunamadı; sonuç ve kayıt sayısı bilinmiyor.", "theta-empty"));
    else if (!rows.length) container.append(element("p", data.status === "ready"
      ? "Bu gün için henüz otomatik kayıt görünmüyor. Yeni uygun giriş kararları otomatik alınır; inceleme formu doldurman gerekmez."
      : "Bu gün için okunabilen otomatik kayıt görünmüyor. İzleme durumu yukarıda gösteriliyor; kaynakta hiç giriş olmadığı varsayılmıyor.", "theta-empty"));
    rows.forEach((row) => {
      if (!row || typeof row !== "object") return;
      if (row.disposition === "excluded") {
        const card = element("article", undefined, "theta-result-card");
        card.append(element("h3", (row.ticker || "Kaynak kayıt") + " · Ölçüm dışı"), element("p", exclusion(row.reason), "theta-help"), element("p", basis(row.entry_basis), "theta-help"));
        container.append(card);
      } else {
        const job = {...(jobs.get(row.request_id) || row), source_kind: "opening_entry",
          retry_available: row.retry_available, retry_depth: row.retry_depth};
        const card = renderJob(job, receipts.get(row.request_id));
        card.append(element("p", basis(row.entry_basis), "theta-help"));
        if (row.state === "pending_maturity") card.append(element("p", "Giriş ve +15/+60 dakika pencerelerinin tamamlanması bekleniyor.", "theta-help"));
        container.append(card);
      }
    });
    renderReport(data.latest_report);
  }

  function renderSources(data) {
    sources = Array.isArray(data.sources) ? data.sources.filter((row) => row && row.source_kind === "decision_card" && typeof row.source_id === "string") : [];
    const select = byId("theta-source");
    const selected = select.value;
    select.replaceChildren(element("option", sources.length ? "Bir kayıt seç…" : "Uygun kontrat kaydı yok"));
    select.firstChild.value = "";
    sources.forEach((row) => {
      const option = element("option", row.option_symbol + " · " + clock(row.planned_entry_at));
      option.value = row.source_id;
      select.append(option);
    });
    select.value = sources.some((row) => row.source_id === selected) ? selected : "";
    select.disabled = sources.length === 0;
    byId("theta-source-submit").disabled = !select.value || busy;
    byId("theta-source-help").textContent = sources.length
      ? "Kontrat ve başlangıç zamanı kayıtlı karardan alınır. Bu inceleme, geçmiş kararı değiştirmez."
      : "Karar defterinde opsiyon kontratı bulunan bir kayıt henüz yok. Aşağıdan tarihsel bir kontratı elle ekleyebilirsin.";
  }

  function detail(dl, title, value) {
    const group = element("div");
    group.append(element("dt", title), element("dd", value));
    dl.append(group);
  }

  function renderJob(job, receipt) {
    const result = receipt && receipt.result && receipt.result.source === "thetadata" ? receipt.result : null;
    const card = element("article", undefined, "theta-result-card");
    card.dataset.requestId = job.request_id;
    const heading = element("div", undefined, "theta-result-header");
    const identity = element("div");
    identity.append(element("h3", job.option_symbol));
    const sourceLabel = job.source_kind === "opening_entry" ? "Otomatik kayıtlı giriş" : job.source_kind === "decision_card" ? "UOA karar kaydı" : "Elle seçilmiş tarihsel kontrat";
    identity.append(element("p", kindText(job.option_symbol) + " · " + sourceLabel, "theta-help"));
    identity.append(element("p", "Başlangıç: " + clock(job.planned_entry_at), "theta-help"));
    const labels = {EVALUATED: "İki süre ölçüldü", PARTIAL: "Kısmi ölçüm", UNKNOWN: "Sonuç bilinmiyor"};
    const resultUnavailable = !receipt && (job.finalized || ["completed", "evaluated", "unknown"].includes(job.state));
    const waiting = !receipt && ["pending", "pending_maturity", "awaiting_connector"].includes(job.state);
    const state = result ? (labels[result.status] || "Sonuç bilinmiyor") : resultUnavailable ? job.state === "evaluated" ? "Ölçüm tamamlandı; ayrıntı bu kısa listede yok" : job.state === "unknown" ? "Sonuç bilinmiyor; kayıt tamamlandı" : "Sonuç bu listede okunamadı" : job.state === "retry_limit" ? "Bağlantı kontrolü gerekli" : receipt ? "Veri kontrolü gerekli" : "Kotasyon bekleniyor";
    heading.append(identity, element("span", state, "theta-chip"));
    card.append(heading);
    const marks = result && Array.isArray(result.markouts) ? result.markouts : [];
    const grid = element("div", undefined, "theta-markouts");
    [15, 60].forEach((minutes) => {
      const mark = marks.find((row) => row && row.horizon_minutes === minutes);
      const number = mark && mark.status === "EVALUATED" ? money(mark.modeled_net_pnl_usd) : null;
      const cell = element("section", undefined, "theta-markout");
      cell.append(element("h4", "+" + minutes + " dakika · 1 kontrat net"));
      const figure = element("strong", number === null ? resultUnavailable ? "Ayrıntı yok" : waiting ? "Bekliyor" : "Ölçülemedi" : dollars(number));
      figure.dataset.sign = number === null ? "unknown" : number < 0 ? "negative" : number > 0 ? "positive" : "zero";
      cell.append(figure);
      cell.append(element("p", number === null ? resultUnavailable ? "Kayıt tamamlanmış, ancak sonuç ayrıntısı bu listede yok. Günlük ölçüm özetine bakabilirsin." : reasonText(mark ? mark.reason : receipt ? receipt.status : "pending") : "Spread, kayma ve komisyon dahil."));
      if (number !== null && mark.exit_quote) cell.append(element("p", "Çıkış kotasyonu: " + clock(mark.exit_quote.quote_at)));
      grid.append(cell);
    });
    card.append(grid);
    const automaticJob = job.source_kind === "opening_entry";
    const retryAvailable = !automaticJob || (job.retry_available === true
      && Number.isInteger(job.retry_depth) && job.retry_depth >= 0 && job.retry_depth < 8);
    if (automaticJob && Number.isInteger(job.retry_depth) && job.retry_depth > 0) {
      card.append(element("p", "Aynı kayıt için yeniden başlatma: " + job.retry_depth + ". Kontrat ve hedef zaman korunur.", "theta-help"));
    }
    if (job.state === "retry_limit" && automaticJob) {
      card.append(element("p", retryAvailable
        ? "Bu kaydın denemeleri durdu. Bağlantıyı düzelttikten sonra aynı kontrat ve zaman için yeniden başlatabilirsin; önceki denemeler korunur."
        : "Bu kaydın deneme sınırına ulaşıldı. Sonuç bilinmiyor; önceki denemeler günlük kapsamda korunur.", "theta-help"));
    }
    if (job.state === "retry_limit" && retryAvailable) {
      const retry = element("button", "Bağlantıyı düzelttim, yeniden dene", "theta-secondary");
      retry.type = "button";
      const retryMessage = (message, isError = false) => {
        if (automaticJob) {
          byId("theta-device-message").textContent = message;
          byId("theta-device-message").dataset.error = String(isError);
        } else setMessage(message, isError);
      };
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          await requestJSON("/api/theta/requests/" + encodeURIComponent(job.request_id) + "/retry", {});
          retryMessage("Yeni deneme kaydedildi. Önceki bağlantı kayıtları korundu.");
          await refresh();
        } catch (_) {
          retryMessage("Yeni denemenin kaydı doğrulanamadı. Yeniden göndermeden önce listeyi yenile.", true);
          retry.disabled = false;
        }
      });
      card.append(retry);
    }
    if (result && result.entry_quote) {
      const disclosure = element("details", undefined, "theta-quote-detail");
      disclosure.open = openQuoteDetails.has(job.request_id);
      disclosure.addEventListener("toggle", () => {
        if (disclosure.open) openQuoteDetails.add(job.request_id);
        else openQuoteDetails.delete(job.request_id);
      });
      disclosure.append(element("summary", "Kotasyonları ve maliyetleri gör"));
      const dl = element("dl", undefined, "theta-detail-grid");
      detail(dl, "Giriş kotasyonu", clock(result.entry_quote.quote_at));
      detail(dl, "Giriş bid / ask — hisse başına", dollars(result.entry_quote.bid) + " / " + dollars(result.entry_quote.ask));
      detail(dl, "Kayma dahil alış — hisse başına", dollars(result.modeled_entry_price));
      detail(dl, "Aynı kotasyonda giriş-çıkış maliyeti", dollars(result.entry_same_quote_roundtrip_friction_usd));
      detail(dl, "Her yönde kayma — hisse başına", dollars(result.slippage_per_share_per_side));
      detail(dl, "Her yönde komisyon — kontrat başına", dollars(result.fee_per_contract_per_side_usd));
      marks.forEach((mark) => {
        if (mark && mark.status === "EVALUATED" && mark.exit_quote) {
          detail(dl, "+" + mark.horizon_minutes + " dk çıkış bid / ask — hisse başına", dollars(mark.exit_quote.bid) + " / " + dollars(mark.exit_quote.ask));
          if (money(mark.exit_delay_seconds) !== null) detail(dl, "+" + mark.horizon_minutes + " dk hedefinden kotasyona gecikme", Number(mark.exit_delay_seconds).toFixed(3) + " saniye");
        }
      });
      detail(dl, "Ürüne ulaşma zamanı", clock(receipt.received_at));
      disclosure.append(dl, element("p", "100 hisselik standart kontrat varsayımı kullanılır. Kotasyon anı, bid ve ask taraflarının ayrı güncelleme yaşlarını veya gerçekleşmiş emri kanıtlamaz.", "theta-help"));
      card.append(disclosure);
    }
    return card;
  }

  function renderStatus(data) {
    const jobs = Array.isArray(data.jobs) ? data.jobs.filter((row) => row.source_kind !== "opening_entry") : [];
    const receipts = Array.isArray(data.recent_results) ? data.recent_results : [];
    const latest = new Map();
    receipts.forEach((row) => {
      if (!row || typeof row.request_id !== "string") return;
      const prior = latest.get(row.request_id);
      if (!prior || String(row.received_at) > String(prior.received_at)) latest.set(row.request_id, row);
    });
    const container = byId("theta-results");
    container.replaceChildren();
    if (data.status === "storage_unavailable") container.append(element("p", "İncelemeler okunamadı. Bağlantı düzeldiğinde durumu yenile.", "theta-empty"));
    else if (!jobs.length) container.append(element("p", "Henüz geçmiş inceleme yok. Bu alan isteğe bağlıdır; otomatik kayıtlar yukarıda gösterilir.", "theta-empty"));
    else jobs.forEach((job) => container.append(renderJob(job, latest.get(job.request_id))));
    byId("theta-count").textContent = data.status === "storage_unavailable" ? "" : jobs.length + " inceleme gösteriliyor";
  }

  async function refresh() {
    if (refreshing) { refreshQueued = true; return; }
    refreshing = true;
    const button = byId("theta-refresh");
    button.disabled = true;
    const requestedDay = dayOverride;
    const suffix = requestedDay ? "?day=" + encodeURIComponent(requestedDay) : "";
    try {
    const results = await Promise.allSettled([
      requestJSON("/api/theta/status"), requestJSON("/api/theta/sources"),
      requestJSON("/api/theta/devices/status"), requestJSON("/api/theta/automatic/status" + suffix),
    ]);
    const legacy = results[0].status === "fulfilled" ? results[0].value : null;
    if (legacy) renderStatus(legacy);
    else byId("theta-results").replaceChildren(element("p", "İnceleme kayıtları okunamadı; kayıt sayısı ve sonuçlar bilinmiyor.", "theta-empty"));
    if (results[1].status === "fulfilled") renderSources(results[1].value);
    else {
      sources = [];
      byId("theta-source").replaceChildren(element("option", "UOA kayıtları okunamadı"));
      byId("theta-source").disabled = true;
      byId("theta-source-submit").disabled = true;
      byId("theta-source-help").textContent = "Karar defteri şu anda okunamadı. Bu, kayıt olmadığı anlamına gelmez.";
    }
    if (results[2].status === "fulfilled") renderDevices(results[2].value);
    else renderDevices({status: "storage_unavailable", devices: []});
    if (requestedDay !== dayOverride) refreshQueued = true;
    else if (results[3].status === "fulfilled") renderAutomatic(results[3].value, legacy);
    else {
      renderAutomatic({status: "storage_unavailable"}, legacy);
      byId("theta-automatic-report").replaceChildren(element("p", "Ölçüm özeti okunamadı; sonuçlar bilinmiyor.", "theta-empty"));
    }
    } finally {
      button.disabled = false;
      refreshing = false;
      if (refreshQueued) { refreshQueued = false; refresh(); }
    }
  }

  async function create(payload, button) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    setMessage("İnceleme kaydediliyor…");
    try {
      await requestJSON("/api/theta/requests", payload);
      setMessage("İnceleme kaydedildi. ThetaData kotasyonları aktarılınca sonuç burada görünecek.");
      await refresh();
    } catch (error) {
      const message = error.status === 401 || error.status === 403 ? "İnceleme kaydedilemedi. Oturum ve erişim yetkisini kontrol et."
        : error.status === 400 || error.status === 422 ? "Kontrat ve zamanı kontrol et. Başlangıç, geçmiş bir işlem seansında ve vade öncesinde olmalı."
        : "Kaydın sonucu doğrulanamadı. Yeniden göndermeden önce inceleme listesini yenileyerek kontrol et.";
      setMessage(message, true);
    } finally {
      busy = false;
      button.disabled = button.id === "theta-source-submit" ? !byId("theta-source").value : false;
    }
  }

  byId("theta-source").addEventListener("change", () => { byId("theta-source-submit").disabled = !byId("theta-source").value || busy; });
  byId("theta-source-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const source = sources.find((row) => row.source_id === byId("theta-source").value);
    if (source) create({source_kind: "decision_card", source_id: source.source_id}, byId("theta-source-submit"));
  });
  byId("theta-manual-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const symbol = byId("theta-symbol").value.trim().toUpperCase();
    const raw = byId("theta-entry").value;
    if (!/^[A-Z]{1,6}\d{6}[CP]\d{8}$/.test(symbol) || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(raw)) {
      setMessage("Tam OCC kontrat kodunu ve başlangıç zamanını gir.", true); return;
    }
    const date = new Date(raw + "Z");
    if (Number.isNaN(date.getTime())) { setMessage("Başlangıç zamanı geçerli değil.", true); return; }
    create({source_kind: "manual_research", source_label: "Elle seçilmiş tarihsel kontrat", option_symbol: symbol, planned_entry_at: date.toISOString()}, byId("theta-manual-submit"));
  });
  byId("theta-refresh").addEventListener("click", refresh);
  byId("theta-day").addEventListener("change", () => {
    const day = byId("theta-day").value;
    if (/^\d{4}-\d{2}-\d{2}$/.test(day)) { dayOverride = day; refresh(); }
  });
  byId("theta-today").addEventListener("click", () => { dayOverride = null; refresh(); });
  byId("theta-device-pairing-create").addEventListener("click", async () => {
    const button = byId("theta-device-pairing-create");
    const message = byId("theta-device-message");
    button.disabled = true;
    message.textContent = "Sürekli Mac bağlantısı hazırlanıyor…";
    try {
      const pairing = await requestJSON("/api/theta/devices/pairings", {});
      if (typeof pairing.pairing_token !== "string" || !/^[a-f0-9]{64}$/.test(pairing.pairing_token)) throw new Error("invalid_pairing_response");
      window.location.assign("/theta-otomatik#" + pairing.pairing_token);
    } catch (_) {
      message.textContent = "Bağlantı hazırlanamadı. Durumu yenileyip tekrar dene; inceleme eklemen veya şifre girmen gerekmiyor.";
      button.disabled = false;
    }
  });
  byId("theta-rerun").addEventListener("click", async () => {
    if (!reportDay || rerunInProgress) return;
    rerunInProgress = true;
    const day = reportDay;
    const button = byId("theta-rerun");
    button.disabled = true;
    byId("theta-rerun-message").textContent = "Kayıtlı veriden hesaplanıyor…";
    try {
      const report = await requestJSON("/api/theta/automatic/reruns", {day});
      if (reportDay === day) renderReport(report);
      byId("theta-rerun-message").textContent = day + " yeniden hesaplandı. Yeni sağlayıcı çağrısı veya strateji ayarı yapılmadı.";
    } catch (_) {
      byId("theta-rerun-message").textContent = "Yeniden hesaplama doğrulanamadı. Önceki kayıtlar değiştirilmedi; durumu yenile.";
    } finally {
      rerunInProgress = false;
      button.disabled = !reportDay;
    }
  });
  byId("theta-pairing-create").addEventListener("click", async () => {
    const button = byId("theta-pairing-create");
    const message = byId("theta-pairing-message");
    button.disabled = true;
    message.textContent = "Şifresiz bağlantı hazırlanıyor…";
    try {
      const pairing = await requestJSON("/api/theta/pairings", {});
      if (!pairing || typeof pairing.pairing_token !== "string" || !/^[a-f0-9]{64}$/.test(pairing.pairing_token)) throw new Error("invalid_pairing_response");
      window.location.assign("/theta-bagla#" + pairing.pairing_token);
    } catch (error) {
      message.textContent = error.status === 400 || error.status === 409 || error.status === 422
        ? "Bağlantı hazırlanamadı. Önce bekleyen bir kontrat incelemesi ekle, ardından yeniden dene."
        : "Bağlantı hazırlanamadı. Sayfayı yenileyip yeniden dene; hiçbir anahtar veya şifre girmen gerekmiyor.";
      button.disabled = false;
    }
  });
  refresh();
  if (typeof window.setInterval === "function") window.setInterval(refresh, 15000);
})();
