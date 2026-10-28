/* Withdraw by server clock plus monotonic elapsed time; GET never creates advice. */
(function () {
  'use strict';
  let root = document.getElementById('opening-desk');
  if (!root) return;
  let rendered = performance.now(), server = Date.parse(root.dataset.serverNow);
  const cadence = Number(root.dataset.refreshSeconds) * 1000;
  function retire() {
    const now = server + performance.now() - rendered;
    document.querySelectorAll('[data-opening-expiry]').forEach(function (card) {
      const deadline = Date.parse(card.dataset.openingExpiry);
      if (!Number.isFinite(deadline) || !Number.isFinite(now) || deadline > now || card.dataset.openingAction === 'WAIT') return;
      if (card.hasAttribute('data-opening-buys')) {
        card.textContent = 'BEKLE · Planın tazelik süresi doldu; güncel kayıt bekle.';
        return;
      }
      if (card.hasAttribute('data-opening-exit')) {
        card.textContent = 'ÇIKIŞI İNCELE · Taze fiyat bekle. Güncel pozisyon sayfasını aç.';
        return;
      }
      card.dataset.openingAction = 'WAIT';
      card.querySelector('.decision-badge').textContent = 'BEKLE';
      card.querySelector('[data-opening-action-text]').textContent = 'Planın tazelik süresi doldu; güncel kayıt bekle.';
    });
  }
  async function refresh() {
    if (document.hidden) return;
    const active = document.activeElement;
    if (active && active.matches('input, select, textarea')) return;
    if (document.getElementById('opening-page')) {
      if (!location.search) location.reload();
      return;
    }
    try {
      const response = await fetch('/', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12000)});
      if (!response.ok) return;
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updated = page.getElementById('opening-desk');
      if (!updated) return;
      root.replaceWith(updated); root = updated;
      server = Date.parse(root.dataset.serverNow); rendered = performance.now(); retire();
    } catch (_) { retire(); }
  }
  setInterval(retire, 1000);
  if (Number.isFinite(cadence) && cadence > 0) setInterval(refresh, cadence);
  document.addEventListener('visibilitychange', function () { retire(); if (!document.hidden) refresh(); });
  document.addEventListener('uoa:refresh-request', refresh);
  retire();
})();
