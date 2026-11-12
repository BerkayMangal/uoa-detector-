/* One-use fragment handoff. No credential input, analytics, persistent storage or automatic redemption. */
(() => {
  "use strict";
  const root = document.getElementById("theta-pairing");
  if (!root) return;
  const automatic = root.dataset.automaticMode === "true";
  let token = window.location.hash.slice(1);
  window.history.replaceState(null, "", window.location.pathname);
  const button = document.getElementById("theta-pairing-download");
  const status = document.getElementById("theta-pairing-status");
  const title = document.getElementById("theta-pairing-title");
  const detail = document.getElementById("theta-pairing-detail");
  const renew = document.getElementById("theta-pairing-renew");
  const command = document.getElementById("theta-pairing-command");
  const copy = document.getElementById("theta-pairing-copy");
  const fail = (message) => {
    status.dataset.error = "true";
    status.textContent = message;
    renew.className = "";
    button.disabled = true;
    token = "";
  };
  if (!/^[a-f0-9]{64}$/.test(token)) {
    title.textContent = "Geçerli bağlantı gerekli";
    detail.textContent = "Sana verilen indirme bağlantısını aç veya UOA’dan yeni bir bağlantı hazırla.";
    fail("Bu sayfada kullanılabilir indirme bağlantısı yok.");
    return;
  }
  button.disabled = false;
  button.addEventListener("click", async () => {
    if (button.disabled || !token) return;
    button.disabled = true;
    status.dataset.error = "false";
    status.textContent = "Bağlantı dosyası hazırlanıyor…";
    try {
      const response = await fetch(automatic ? "/api/theta/device/redeem" : "/api/theta/pairing/redeem", {
        method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
        headers: {"Content-Type": "application/json", Accept: "application/octet-stream"},
        body: JSON.stringify({pairing_token: token}),
      });
      token = "";
      if (!response.ok) {
        if ([400, 404, 409, 410].includes(response.status)) {
          fail("Bu bağlantı kullanılmış, süresi dolmuş veya geçerli değil. UOA’dan yeni bağlantı hazırla.");
        } else {
          fail("İndirme doğrulanamadı. Dosya inmediyse UOA’dan yeni bağlantı hazırla.");
        }
        return;
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const pattern = automatic
        ? /^attachment;\s*filename=(?:"(uoa-theta-otomatik-[a-f0-9]{12}\.py)"|(uoa-theta-otomatik-[a-f0-9]{12}\.py))$/
        : /^attachment;\s*filename=(?:"(uoa-theta-hazir-[a-f0-9]{12}\.py)"|(uoa-theta-hazir-[a-f0-9]{12}\.py))$/;
      const attachment = pattern.exec(disposition.trim());
      if (!attachment) { fail("İndirme dosyasının adı doğrulanamadı. UOA’dan yeni bağlantı hazırla."); return; }
      const filename = attachment[1] || attachment[2];
      const blob = await response.blob();
      if (!blob.size) { fail("Boş bir yanıt alındı. UOA’dan yeni bağlantı hazırla."); return; }
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = filename;
      link.rel = "noreferrer";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
      command.textContent = "python3 ~/Downloads/" + filename + (automatic ? " --install" : " --watch");
      copy.disabled = false;
      button.textContent = "İndirme başlatıldı";
      status.textContent = automatic
        ? "İndirme başlatıldı; henüz bağlantı kurulmadı. Dosya İndirilenler’e geldikten sonra aşağıdaki kurulum komutunu Mac Terminal’de bir kez çalıştır."
        : "Dosya İndirilenler klasörüne geldikten sonra aşağıdaki komutu Mac Terminal’de çalıştır.";
    } catch (_) {
      fail("İndirme doğrulanamadı. Dosya inmediyse UOA’dan yeni bağlantı hazırla; şifre girmen gerekmiyor.");
    }
  });
  copy.addEventListener("click", async () => {
    if (copy.disabled) return;
    try {
      await navigator.clipboard.writeText(command.textContent);
      copy.textContent = "Kopyalandı";
    } catch (_) {
      status.textContent = "Komutu yukarıdan seçip kopyalayarak Mac Terminal’e yapıştır.";
    }
  });
})();
