/* Refresh the live section without replacing the alert controller/audio context. */
(function () {
  'use strict';
  let clean = function () {};
  function mount(root) {
    clean();
    const events = new AbortController();
    const on = {signal: events.signal};
    const search = root.querySelector('#stock-search');
    const buttons = root.querySelectorAll('[data-focus]');
    const rows = Array.from(root.querySelectorAll('[data-board-row]'));
    let focus = 'all', editing = false, refreshing = false;
    const renderedAt = Date.now();
    const deadline = renderedAt + Number(root.dataset.entryTtl) * 1000;
    const ageAtRender = Number(root.dataset.scanAge);
    const cadence = Number(root.dataset.reloadSeconds);
    function filter() {
      const term = search ? search.value.trim().toUpperCase() : '';
      let count = 0;
      rows.forEach(function (row) {
        row.hidden = !(row.dataset.boardRow.includes(term) && (focus === 'all' || (focus === 'hot' && row.dataset.hotRow === 'true') || (focus === 'lag' && row.dataset.lagRow === 'true')));
        if (!row.hidden) count++;
      });
      const label = root.querySelector('#stock-count'), empty = root.querySelector('#stock-empty');
      if (label) label.textContent = count + ' / ' + rows.length + ' hisse';
      if (empty) empty.hidden = count !== 0;
    }
    if (search) search.addEventListener('input', filter, on);
    buttons.forEach(function (button) { button.addEventListener('click', function () {
      focus = button.dataset.focus;
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b === button)); });
      filter();
    }, on); });
    root.querySelectorAll('form input, form select').forEach(function (input) {
      input.addEventListener('input', function () { editing = true; }, on);
      input.addEventListener('change', function () { editing = true; }, on);
    });
    function updateDataAge() {
      const age = ageAtRender + (Date.now() - renderedAt) / 1000;
      const label = root.querySelector('#scan-age');
      if (label) label.textContent = String(Math.floor(age / 60));
      root.querySelectorAll('[data-position-age]').forEach(function (row) {
        const age = Number(row.dataset.positionAge) + (Date.now() - renderedAt) / 1000;
        if (age > Number(root.dataset.priceMaxAge) && !row.dataset.ageWarned) {
          row.dataset.ageWarned = 'true';
          const note = document.createElement('p'); note.className = 'quiet-copy';
          note.textContent = 'Fiyat gözlemi eski; güncel fiyatı yeniden kontrol et.';
          row.querySelector('[data-position-status]').append(note);
        }
      });
      if (label && age > cadence * 3 && !root.querySelector('#client-age-warning')) {
        const warning = document.createElement('p');
        warning.id = 'client-age-warning'; warning.className = 'desk-notice'; warning.setAttribute('role', 'alert');
        warning.textContent = 'Veriler eskidi. Güncel fiyat ve kararlar için sayfayı yenile; yazdığın form korunmak için otomatik yenileme duraklatıldı.';
        (root.querySelector('[data-age-anchor]') || root.querySelector('.desk-heading')).after(warning);
      }
    }
    function withdrawExpiredEntries() {
      if (Date.now() < deadline) return;
      root.querySelectorAll('.plan-card[data-ready="READY"]').forEach(function (card) {
        if (card.dataset.clientWithdrawn || card.dataset.serverWithdrawn === 'true') return;
        card.dataset.clientWithdrawn = 'true'; card.dataset.tone = 'gray';
        card.querySelector('.decision-badge').textContent = 'GİRİŞ GERİ ÇEKİLDİ';
        card.querySelector('.plan-status').textContent = 'yeni tarama bekleniyor';
        const note = document.createElement('p'); note.className = 'desk-notice'; note.setAttribute('role', 'alert');
        note.textContent = 'Bu planın giriş süresi doldu. Yeni tarama için sayfayı yenile.';
        card.querySelector('.plan-title').after(note);
        card.querySelectorAll('form[action$="/paper"] button').forEach(function (button) { button.disabled = true; });
      });
      root.querySelectorAll('[data-verdict-tone="green"]').forEach(function (cell) {
        cell.dataset.verdictTone = 'gray'; cell.style.color = 'var(--muted)';
        cell.querySelector('span').textContent = 'BEKLE';
        const reason = cell.querySelector('div'); if (reason) reason.textContent = 'Giriş süresi doldu; yeni tarama bekleniyor.';
      });
      const ready = root.querySelector('#ready-count'); if (ready) ready.textContent = '0';
    }
    function canRefresh() { return !editing && focus === 'all' && !(search && search.value); }
    async function refresh() {
      if (refreshing || !canRefresh() || document.visibilityState !== 'visible') return;
      refreshing = true;
      try {
        const response = await fetch(window.location.href, {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
        if (!response.ok) throw new Error('refresh failed');
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        const next = html.getElementById('live-alpha');
        // The owner may have begun an edit while this request was in flight.
        if (next && canRefresh() && root.isConnected) {
          next.querySelectorAll('script').forEach(function (script) { script.remove(); });
          root.querySelectorAll('details[id]').forEach(function (fold) {
            const replacement = next.querySelector('#' + CSS.escape(fold.id));
            if (replacement) replacement.open = fold.open;
          });
          root.replaceWith(next); mount(next);
        }
      } catch (_) {
        // Keep the current form and the independent entry-expiry guard.
      } finally { refreshing = false; }
    }
    withdrawExpiredEntries();
    const guardTimer = setInterval(function () { withdrawExpiredEntries(); updateDataAge(); }, 1000);
    const refreshTimer = setInterval(refresh, cadence * 1000);
    document.addEventListener('visibilitychange', function () { withdrawExpiredEntries(); updateDataAge(); }, on);
    document.addEventListener('uoa:refresh-request', refresh, on);
    clean = function () { clearInterval(guardTimer); clearInterval(refreshTimer); events.abort(); };
  }
  const root = document.getElementById('live-alpha');
  if (root) mount(root);
})();
