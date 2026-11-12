/* Retrospective ThetaData view. The server owns all quote validation and arithmetic. */
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
    const response = await fetch(url, {
      method: payload === undefined ? "GET" : "POST",
      credentials: "same-origin", cache: "no-store",
      headers: payload === undefined ? {Accept: "application/json"} : {Accept: "application/json", "Content-Type": "application/json"},
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = new Error("request_failed");
      error.status = response.status;
      throw error;
    }
    return response.json();
  };
  const setMessage = (message, isError = false) => {
    const node = byId("theta-form-message");
    node.textContent = message;
    node.dataset.error = String(isError);
  };
  let sources = [];
  let busy = false;

  function renderSources(data) {
    sources = Array.isArray(data.sources) ? data.sources.filter((row) => row && row.source_kind === "decision_card" && typeof row.source_id === "string") : [];
    const select = byId("theta-source");
    select.replaceChildren(element("option", sources.length ? "Bir kayıt seç…" : "Uygun kontrat kaydı yok"));
    select.firstChild.value = "";
    sources.forEach((row) => {
      const option = element("option", row.option_symbol + " · " + clock(row.planned_entry_at));
      option.value = row.source_id;
      select.append(option);
    });
    select.disabled = sources.length === 0;
    byId("theta-source-submit").disabled = true;
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
    identity.append(element("p", kindText(job.option_symbol) + " · " + (job.source_kind === "decision_card" ? "UOA karar kaydı" : "Elle seçilmiş tarihsel kontrat"), "theta-help"));
    identity.append(element("p", "Başlangıç: " + clock(job.planned_entry_at), "theta-help"));
    const labels = {EVALUATED: "İki süre ölçüldü", PARTIAL: "Kısmi ölçüm", UNKNOWN: "Sonuç bilinmiyor"};
    const resultUnavailable = !receipt && (job.finalized || job.state === "completed");
    const state = result ? (labels[result.status] || "Sonuç bilinmiyor") : resultUnavailable ? "Sonuç bu listede okunamadı" : job.state === "retry_limit" ? "Bağlantı kontrolü gerekli" : receipt ? "Veri kontrolü gerekli" : "Kotasyon bekleniyor";
    heading.append(identity, element("span", state, "theta-chip"));
    card.append(heading);
    const marks = result && Array.isArray(result.markouts) ? result.markouts : [];
    const grid = element("div", undefined, "theta-markouts");
    [15, 60].forEach((minutes) => {
      const mark = marks.find((row) => row && row.horizon_minutes === minutes);
      const number = mark && mark.status === "EVALUATED" ? money(mark.modeled_net_pnl_usd) : null;
      const cell = element("section", undefined, "theta-markout");
      cell.append(element("h4", "+" + minutes + " dakika · 1 kontrat net"));
      const figure = element("strong", number === null ? "Ölçülemedi" : dollars(number));
      figure.dataset.sign = number === null ? "unknown" : number < 0 ? "negative" : number > 0 ? "positive" : "zero";
      cell.append(figure);
      cell.append(element("p", number === null ? resultUnavailable ? "Kayıt tamamlanmış, ancak sonuç ayrıntısı bu listede yok." : reasonText(mark ? mark.reason : receipt ? receipt.status : "pending") : "Spread, kayma ve komisyon dahil."));
      if (number !== null && mark.exit_quote) cell.append(element("p", "Çıkış kotasyonu: " + clock(mark.exit_quote.quote_at)));
      grid.append(cell);
    });
    card.append(grid);
    if (job.state === "retry_limit") {
      const retry = element("button", "Bağlantıyı düzelttim, yeniden dene", "theta-secondary");
      retry.type = "button";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          await requestJSON("/api/theta/requests/" + encodeURIComponent(job.request_id) + "/retry", {});
          setMessage("Yeni deneme kaydedildi. Önceki bağlantı kayıtları korundu.");
          await refresh();
        } catch (_) {
          setMessage("Yeni denemenin kaydı doğrulanamadı. Yeniden göndermeden önce listeyi yenile.", true);
          retry.disabled = false;
        }
      });
      card.append(retry);
    }
    if (result && result.entry_quote) {
      const disclosure = element("details", undefined, "theta-quote-detail");
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
    const panel = root.querySelector(".theta-connection");
    const title = byId("theta-connection-title");
    const detailNode = byId("theta-connection-detail");
    if (data.status === "data_received") {
      panel.dataset.connectionState = "observed";
      title.textContent = "ThetaData verisi alındı";
      detailNode.textContent = "Bağlantı programının son aktarımı: " + clock(data.last_connector_contact) + ". Bu, sürekli canlı bağlantı garantisi değildir.";
    } else if (data.status === "connection_or_data_error") {
      panel.dataset.connectionState = "error";
      title.textContent = "Bağlantı veya veri kontrolü gerekiyor";
      detailNode.textContent = "Bağlantı programından yanıt geldi, ancak son isteklerde bağlantı veya veri sorunu var. Eksik ölçümler aşağıda ayrı gösterilir.";
    } else if (data.status === "storage_unavailable") {
      panel.dataset.connectionState = "error";
      title.textContent = "İnceleme kayıtları okunamadı";
      detailNode.textContent = "Kayıt deposuna şu anda erişilemiyor; bu, kayıt olmadığı anlamına gelmez.";
    } else {
      panel.dataset.connectionState = "awaiting_connection";
      title.textContent = "ThetaData bağlantısı bekleniyor";
      detailNode.textContent = "Henüz doğrulanmış ThetaData kotasyonu alınmadı. Mac’teki Terminal ve UOA bağlantı programı çalıştığında sonuçlar burada görünecek.";
    }
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
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
    else if (!jobs.length) container.append(element("p", "Henüz inceleme yok. Yukarıdan bir UOA kaydı veya tarihsel kontrat ekle.", "theta-empty"));
    else jobs.forEach((job) => container.append(renderJob(job, latest.get(job.request_id))));
    byId("theta-count").textContent = data.status === "storage_unavailable" ? "" : jobs.length + " inceleme gösteriliyor";
  }

  async function refresh() {
    const button = byId("theta-refresh");
    button.disabled = true;
    const results = await Promise.allSettled([requestJSON("/api/theta/status"), requestJSON("/api/theta/sources")]);
    if (results[0].status === "fulfilled") renderStatus(results[0].value);
    else {
      root.querySelector(".theta-connection").dataset.connectionState = "error";
      byId("theta-connection-title").textContent = "Bağlantı durumu okunamadı";
      byId("theta-connection-detail").textContent = "Durum servisine erişilemedi. Ekrandaki önceki veriler güncel olmayabilir; durumu yeniden kontrol et.";
      if (!byId("theta-results").querySelector(".theta-result-card")) byId("theta-results").replaceChildren(element("p", "İnceleme kayıtları okunamadı; kayıt sayısı ve sonuçlar bilinmiyor.", "theta-empty"));
    }
    if (results[1].status === "fulfilled") renderSources(results[1].value);
    else {
      sources = [];
      const select = byId("theta-source");
      select.replaceChildren(element("option", "UOA kayıtları okunamadı"));
      select.disabled = true;
      byId("theta-source-submit").disabled = true;
      byId("theta-source-help").textContent = "Karar defteri şu anda okunamadı. Bu, kayıt olmadığı anlamına gelmez.";
    }
    button.disabled = false;
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
})();
