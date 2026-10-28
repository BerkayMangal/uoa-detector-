/* Opt-in page-open alerts. No service worker, vendor request or broker action. */
(function () {
  'use strict';
  const controls = document.getElementById('live-alert-controls');
  if (!controls) return;
  const toggle = document.getElementById('alerts-toggle'), test = document.getElementById('alerts-test');
  const scope = document.getElementById('alerts-scope'), status = document.getElementById('alerts-status');
  const exitChoice = document.getElementById('alerts-exits');
  const toast = document.getElementById('signal-toast'), message = document.getElementById('signal-message');
  const PREF = 'uoa.live-alerts.pref.v1';
  const pollSeconds = Number(controls.dataset.pollSeconds);
  const retention = Number(controls.dataset.seenTtl) * 1000, maxSeen = Number(controls.dataset.maxSeen);
  let enabled = false, generation = 0, polling = false, audio = null, deliveryIssue = '', toastDeadline = 0;
  const notes = new Map();
  let toastKeys = [];
  function retire(keep) {
    if (!keep || toastKeys.some(function (key) { return !keep.has(key); })) toast.hidden = true;
    notes.forEach(function (item, note) {
      if (!keep || item.keys.some(function (key) { return !keep.has(key); })) { note.close(); notes.delete(note); }
    });
  }
  function say(text) { status.textContent = text; }
  function readPref() {
    const p = JSON.parse(localStorage.getItem(PREF) || '{}');
    return {enabled: p.enabled === true, scope: p.scope === 'all' ? 'all' : 'calls', exits: p.exits !== false};
  }
  function paint() {
    toggle.textContent = enabled ? 'Uyarıları kapat' : 'Uyarıları aç';
    toggle.setAttribute('aria-pressed', String(enabled));
  }
  function persist() { localStorage.setItem(PREF, JSON.stringify({enabled: enabled, scope: scope.value, exits: exitChoice ? exitChoice.checked : true})); }
  function failStorage() {
    enabled = false; generation++; paint(); retire();
    say('Tarayıcı kayıt alanı kullanılamıyor. Tekrarları önlemek için uyarılar kapatıldı; site verilerine izin verip yeniden aç.');
  }
  function lock(fn) {
    return navigator.locks ? navigator.locks.request('uoa.live-alerts.v1', fn) : Promise.resolve().then(fn);
  }
  let database;
  function seenDatabase() {
    if (!database) database = new Promise(function (resolve, reject) {
      const request = indexedDB.open('uoa-live-alerts-v1', 1);
      request.onupgradeneeded = function () { request.result.createObjectStore('seen'); };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
      request.onblocked = function () { reject(new Error('browser storage blocked')); };
    });
    return database;
  }
  async function claim(signals, serverTime) {
    const db = await seenDatabase();
    // IndexedDB read/write transactions serialize across renderer processes.
    // A Web Lock alone cannot flush another renderer's localStorage cache.
    return new Promise(function (resolve, reject) {
      const transaction = db.transaction('seen', 'readwrite'), store = transaction.objectStore('seen');
      const request = store.get('keys'), fresh = [], now = Date.parse(serverTime);
      request.onsuccess = function () {
        const stored = request.result || {};
        if (Array.isArray(stored) || typeof stored !== 'object') { transaction.abort(); return; }
        const seen = Object.fromEntries(Object.entries(stored).filter(function (pair) {
          return /^[a-f0-9]{64}$/.test(pair[0]) && Number.isFinite(pair[1]) && pair[1] >= now - retention;
        }));
        signals.forEach(function (signal) {
          if (!Object.prototype.hasOwnProperty.call(seen, signal.key)) fresh.push(signal);
          seen[signal.key] = now;
        });
        store.put(Object.fromEntries(Object.entries(seen).sort(function (a, b) { return b[1] - a[1]; }).slice(0, maxSeen)), 'keys');
      };
      transaction.oncomplete = function () { resolve(fresh); };
      transaction.onerror = transaction.onabort = function () { reject(transaction.error || new Error('seen transaction failed')); };
    });
  }
  async function unlockAudio() {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) throw new Error('audio unsupported');
    if (!audio || audio.state === 'closed') audio = new Audio();
    await audio.resume();
    if (audio.state !== 'running') throw new Error('audio not running');
  }
  function ding() {
    if (!audio || audio.state !== 'running') return false;
    try {
      [523.25, 783.99].forEach(function (frequency, index) {
        const tone = audio.createOscillator(), gain = audio.createGain();
        const at = audio.currentTime + index * 0.16;
        tone.type = 'sine'; tone.frequency.value = frequency;
        gain.gain.setValueAtTime(0, at); gain.gain.linearRampToValueAtTime(0.12, at + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.001, at + 0.25);
        tone.connect(gain); gain.connect(audio.destination); tone.start(at); tone.stop(at + 0.27);
      });
      return true;
    } catch (_) { return false; }
  }
  function channelText() {
    let text = audio && audio.state === 'running' ? 'Ses hazır.' : 'Ses için bu sekmede “Sesi dene”ye bas.';
    if (!('Notification' in window)) text += ' Tarayıcı bildirimi desteklenmiyor; sayfa uyarısı açık.';
    else if (Notification.permission !== 'granted') text += ' Tarayıcı bildirim izni yok; sayfa uyarısı açık.';
    if (!navigator.locks) text += ' Tekrarlayan uyarıları önlemek için bu tarayıcıda tek sekme kullan.';
    return text + (deliveryIssue ? ' ' + deliveryIssue : '');
  }
  async function load() {
    const began = performance.now();
    const response = await fetch(controls.dataset.feedUrl || '/api/canli/uyarilar', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('feed unavailable');
    const data = await response.json();
    if (!Number.isFinite(Date.parse(data.server_time)) || !Array.isArray(data.signals)) throw new Error('invalid feed');
    data.receivedAt = performance.now();
    data.elapsed = data.receivedAt - began;
    return data;
  }
  function valid(signal, data) {
    const exit = signal && signal.event_type === 'exit';
    return data.status === 'current' && signal && /^[a-f0-9]{64}$/.test(signal.key)
      && (exit ? ['stock', 'long_call', 'bull_call_debit', 'long_put', 'bear_put_debit'] : ['stock', 'long_call', 'bull_call_debit']).includes(signal.instrument)
      && (exit ? !exitChoice || exitChoice.checked : scope.value === 'all' || signal.instrument !== 'stock')
      && typeof signal.ticker === 'string' && typeof signal.label === 'string'
      && (exit ? typeof signal.message === 'string' && signal.message.length > 0 : [signal.underlying_ref, signal.underlying_stop, signal.underlying_target].every(function (n) { return Number.isFinite(n) && n > 0; }))
      && Date.parse(signal.expires_at) - Date.parse(data.server_time) > data.elapsed + performance.now() - data.receivedAt;
  }
  function show(signals, data) {
    const live = signals.filter(function (signal) { return valid(signal, data); });
    if (!live.length || !enabled) return;
    const labels = live.map(function (s) { return s.ticker + ' · ' + s.label; });
    const first = live[0];
    const money = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'});
    const body = first.event_type === 'exit' ? first.message : first.ticker + ' · Deneysel plan · Dayanak ref ' + money.format(first.underlying_ref)
      + ' · stop ' + money.format(first.underlying_stop) + ' · hedef ' + money.format(first.underlying_target) + '. Güncel planı kontrol et.';
    message.textContent = labels.join(', ') + ' — ' + body;
    toastKeys = live.map(function (s) { return s.key; });
    toastDeadline = performance.now() + Math.min.apply(null, live.map(function (s) { return Date.parse(s.expires_at) - Date.parse(data.server_time); })) - data.elapsed;
    toast.hidden = false;
    setTimeout(expireToast, Math.max(0, toastDeadline - performance.now()));
    if (!ding()) deliveryIssue = 'Bu sekmede ses çalınamadı; “Sesi dene” ile yeniden etkinleştir.';
    if ('Notification' in window && Notification.permission === 'granted') {
      try {
        const note = new Notification('UOA · ' + labels.join(', '), {body: body, tag: 'uoa-' + first.key});
        notes.set(note, {deadline: toastDeadline, keys: toastKeys});
        note.onclick = function () { window.focus(); document.dispatchEvent(new Event('uoa:refresh-request')); note.close(); };
        setTimeout(function () { note.close(); notes.delete(note); }, Math.max(0, toastDeadline - performance.now()));
      } catch (_) { deliveryIssue += ' Sistem bildirimi açılamadı; sayfa uyarısı gösterildi.'; }
    }
  }
  async function poll() {
    if (!enabled || polling) return;
    polling = true;
    const armed = generation;
    try {
      const data = await load();
      if (!enabled || armed !== generation) return;
      if (data.status !== 'current') { retire(); say(data.detail + ' ' + channelText()); return; }
      retire(new Set(data.signals.filter(function (s) { return valid(s, data); }).map(function (s) { return s.key; })));
      await lock(async function () {
        if (!enabled || armed !== generation) return;
        try { if (!readPref().enabled) return; } catch (_) { failStorage(); return; }
        let fresh;
        try { fresh = await claim(data.signals.filter(function (s) { return valid(s, data); }), data.server_time); }
        catch (_) { failStorage(); return; }
        if (enabled && armed === generation) show(fresh, data);
      });
      if (enabled) say('Uyarılar açık · ' + data.detail + (data.exit_status === 'unavailable' ? ' Çıkış uyarıları doğrulanamıyor.' : '') + ' ' + channelText());
    } catch (_) {
      if (enabled && armed === generation) { retire(); say('Uyarı bağlantısı kurulamadı; güncel sinyal doğrulanamıyor. Yeniden denenecek.'); }
    } finally { polling = false; }
  }
  async function baseline() {
    const data = await load();
    if (!['current', 'not_live', 'no_scan', 'stale'].includes(data.status)) throw new Error('no usable feed');
    await lock(async function () { await claim(data.signals.filter(function (s) { return valid(s, data); }), data.server_time); });
    return data;
  }
  toggle.addEventListener('click', async function () {
    if (enabled) {
      enabled = false; generation++; retire();
      try { persist(); } catch (_) { failStorage(); return; }
      paint(); say('Uyarılar kapalı.'); return;
    }
    toggle.disabled = true; deliveryIssue = ''; say('Uyarı bağlantısı kuruluyor…');
    // Both permission and audio activation are initiated directly by the click.
    const sound = unlockAudio().catch(function () {});
    if ('Notification' in window && Notification.permission === 'default') {
      try { Promise.resolve(Notification.requestPermission()).catch(function () {}); }
      catch (_) { deliveryIssue = 'Sistem bildirim izni istenemedi; sayfa uyarısı kullanılacak.'; }
    }
    try {
      await sound;
      const data = await baseline();
      enabled = true; generation++; persist(); paint();
      say('Uyarılar açık. Mevcut öneriler başlangıç olarak alındı; sonraki yeni öneriler bildirilecek. ' + data.detail + ' ' + channelText());
    } catch (_) { enabled = false; paint(); say('Uyarılar açılamadı. Bağlantıyı ve tarayıcı kayıt iznini kontrol edip tekrar dene.'); }
    finally { toggle.disabled = false; }
  });
  test.addEventListener('click', async function () {
    try { await unlockAudio(); if (!ding()) throw new Error('sound failed'); deliveryIssue = ''; say('Test sesi çalındı. ' + (enabled ? channelText() : 'Sinyal izlemek için uyarıları aç.')); }
    catch (_) { say('Ses etkinleştirilemedi. Tarayıcının site sesine izin verdiğini kontrol et.'); }
  });
  async function changeScope() {
    generation++; retire();
    if (!enabled) { try { persist(); } catch (_) { failStorage(); } return; }
    scope.disabled = true; enabled = false; paint();
    try {
      await baseline(); enabled = true; persist(); paint();
      say('Kapsam değişti. Mevcut öneriler başlangıç olarak alındı. ' + channelText());
    } catch (_) { try { persist(); } catch (_) {} say('Kapsam kaydedilemedi; uyarıları yeniden aç.'); }
    finally { scope.disabled = false; }
  }
  scope.addEventListener('change', changeScope);
  if (exitChoice) exitChoice.addEventListener('change', changeScope);
  document.getElementById('signal-dismiss').addEventListener('click', function () { toast.hidden = true; });
  document.getElementById('signal-refresh').addEventListener('click', function () {
    document.dispatchEvent(new Event('uoa:refresh-request')); toast.hidden = true;
  });
  window.addEventListener('storage', function (event) {
    if (event.key !== PREF) return;
    try {
      const p = readPref(); enabled = p.enabled; scope.value = p.scope; generation++; paint();
      if (exitChoice) exitChoice.checked = p.exits;
      retire();
      say(enabled ? 'Uyarılar açık. ' + channelText() : 'Uyarılar diğer sekmeden kapatıldı.');
    } catch (_) { failStorage(); }
  });
  function expireToast() {
    if (performance.now() >= toastDeadline) toast.hidden = true;
    notes.forEach(function (item, note) { if (performance.now() >= item.deadline) { note.close(); notes.delete(note); } });
  }
  document.addEventListener('visibilitychange', function () { expireToast(); if (document.visibilityState === 'visible') poll(); });
  setInterval(expireToast, 1000);
  setInterval(poll, pollSeconds * 1000);
  try {
    const p = readPref(); enabled = p.enabled; scope.value = p.scope; paint();
    if (exitChoice) exitChoice.checked = p.exits;
    if (enabled) { say('Uyarı bağlantısı kuruluyor. ' + channelText()); poll(); }
  } catch (_) { failStorage(); }
})();
