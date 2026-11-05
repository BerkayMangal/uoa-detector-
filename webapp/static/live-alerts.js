/* Opt-in page-open alerts. No service worker, vendor request or broker action. */
(function () {
  'use strict';
  const controls = document.getElementById('live-alert-controls');
  if (!controls) return;
  const toggle = document.getElementById('alerts-toggle'), test = document.getElementById('alerts-test');
  const scope = document.getElementById('alerts-scope'), status = document.getElementById('alerts-status');
  const desktop = document.getElementById('alerts-desktop'), desktopTest = document.getElementById('alerts-desktop-test');
  const channels = document.getElementById('alerts-channel-status');
  const exitChoice = document.getElementById('alerts-exits');
  const toast = document.getElementById('signal-toast'), message = document.getElementById('signal-message');
  const PREF = 'uoa.live-alerts.pref.v1';
  const pollSeconds = Number(controls.dataset.pollSeconds);
  const retention = Number(controls.dataset.seenTtl) * 1000, maxSeen = Number(controls.dataset.maxSeen);
  let enabled = false, generation = 0, polling = false, audio = null, deliveryIssue = '', toastDeadline = 0, busy = false;
  const notes = new Map();
  let toastKeys = [];
  function retire(keep) {
    if (!keep || toastKeys.some(function (key) { return !keep.has(key); })) toast.hidden = true;
    notes.forEach(function (item, note) {
      if (!keep || item.keys.some(function (key) { return !keep.has(key); })) { note.close(); notes.delete(note); }
    });
  }
  function say(text) { status.textContent = text; }
  function knownScope(value) { return ['all', 'calls', 'puts', 'up'].includes(value); }
  function readPref() {
    const p = JSON.parse(localStorage.getItem(PREF) || '{}');
    return {enabled: p.enabled === true && (p.scope === undefined || knownScope(p.scope)), scope: knownScope(p.scope) ? p.scope : 'all', scopeVersion: p.scopeVersion === 2, exits: p.exits !== false};
  }
  function permission() {
    return 'Notification' in window && window.isSecureContext !== false ? Notification.permission : 'unsupported';
  }
  function paintChannels() {
    const p = permission();
    desktop.disabled = busy || p === 'unsupported' || p === 'denied' || (enabled && p === 'granted');
    desktopTest.disabled = p !== 'granted';
    desktop.textContent = p === 'granted' ? (enabled ? 'PC izni açık' : 'PC bildirimleriyle başlat') : 'PC bildirimlerini aç';
    let text = p === 'granted' ? 'PC bildirim izni açık.' : p === 'denied'
      ? 'PC bildirimi engelli. Tarayıcının bu siteye ait bildirim ayarlarından izin ver; tekrar izin istemiyoruz.'
      : p === 'unsupported' ? 'Bu tarayıcıda PC bildirimi kullanılamıyor; HTTPS ve destekleyen bir masaüstü tarayıcı gerekir.'
      : 'PC izni henüz verilmedi; “PC bildirimlerini aç” düğmesini kullan.';
    text += audio && audio.state === 'running' ? ' Ses hazır.' : ' Ses için bu sekmede “Sesi dene”ye bas.';
    text += enabled ? ' Sayfa uyarısı açık.' : ' Sinyal izleme kapalı.';
    channels.textContent = text + (deliveryIssue ? ' ' + deliveryIssue : '');
  }
  function paint() {
    toggle.textContent = enabled ? 'Uyarıları kapat' : 'Uyarıları aç';
    toggle.setAttribute('aria-pressed', String(enabled));
    toggle.disabled = busy;
    scope.disabled = busy;
    if (exitChoice) exitChoice.disabled = busy;
    paintChannels();
  }
  function persist() { localStorage.setItem(PREF, JSON.stringify({enabled: enabled, scope: scope.value, scopeVersion: 2, exits: exitChoice ? exitChoice.checked : true})); }
  function failStorage(error) {
    enabled = false; generation++; paint(); retire();
    // A recovered database after reload must not auto-arm an old preference
    // and notify plans which were never successfully baselined/claimed.
    try { persist(); } catch (_) {}
    if (error && error.code === 'seen_capacity') {
      say('Tekrarları önleyen kayıt sınırına ulaşıldı. Uyarılar kapatıldı; eski kayıtların süresi dolunca yeniden aç. Mevcut sinyaller tekrar bildirilmez.');
    } else {
      say('Tarayıcı kayıt alanı kullanılamıyor. Tekrarları önlemek için uyarılar kapatıldı; site verilerine izin verip yeniden aç.');
    }
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
    }).catch(function (error) { database = undefined; throw error; });
    return database;
  }
  async function claim(signals, serverTime) {
    const db = await seenDatabase();
    // IndexedDB read/write transactions serialize across renderer processes.
    // A Web Lock alone cannot flush another renderer's localStorage cache.
    return new Promise(function (resolve, reject) {
      const transaction = db.transaction('seen', 'readwrite'), store = transaction.objectStore('seen');
      const request = store.get('keys'), fresh = [], now = Date.parse(serverTime);
      let failure;
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
        // Never evict a retained key and rediscover it as a new live signal.
        if (Object.keys(seen).length > maxSeen) {
          failure = new Error('seen capacity reached'); failure.code = 'seen_capacity';
          transaction.abort(); return;
        }
        store.put(seen, 'keys');
      };
      transaction.oncomplete = function () { resolve(fresh); };
      transaction.onerror = transaction.onabort = function () { reject(failure || transaction.error || new Error('seen transaction failed')); };
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
    paintChannels();
    return permission() === 'granted' ? 'PC bildirimi için tarayıcı izni var; işletim sistemi teslimatı sınırlayabilir.' : 'PC kanalı kapalı; sayfa ve kullanılabilir ses izleniyor.';
  }
  async function load() {
    const began = performance.now();
    const response = await fetch('/api/acilis/uyarilar', {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12000)});
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
      && ['stock', 'long_call', 'bull_call_debit', 'long_put', 'bear_put_debit'].includes(signal.instrument)
      && (exit ? !exitChoice || exitChoice.checked : scope.value === 'all'
        || scope.value === 'calls' && ['long_call', 'bull_call_debit'].includes(signal.instrument)
        || scope.value === 'puts' && ['long_put', 'bear_put_debit'].includes(signal.instrument)
        || scope.value === 'up' && ['stock', 'long_call', 'bull_call_debit'].includes(signal.instrument))
      && typeof signal.ticker === 'string' && typeof signal.label === 'string'
      && (exit ? typeof signal.message === 'string' && signal.message.length > 0 : [signal.underlying_ref, signal.underlying_stop, signal.underlying_target].every(function (n) { return Number.isFinite(n) && n > 0; }))
      && (exit || (['long_put', 'bear_put_debit'].includes(signal.instrument)
        ? signal.underlying_target < signal.underlying_ref && signal.underlying_ref < signal.underlying_stop
        : signal.underlying_stop < signal.underlying_ref && signal.underlying_ref < signal.underlying_target))
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
    // Storage/Web Lock waits consume the original server lifetime too.
    toastDeadline = data.receivedAt + Math.min.apply(null, live.map(function (s) { return Date.parse(s.expires_at) - Date.parse(data.server_time); })) - data.elapsed;
    if (!Number.isFinite(toastDeadline) || performance.now() >= toastDeadline) return;
    toast.hidden = false;
    setTimeout(expireToast, Math.max(0, toastDeadline - performance.now()));
    if (!ding()) deliveryIssue = 'Bu sekmede ses çalınamadı; “Sesi dene” ile yeniden etkinleştir.';
    if (permission() === 'granted' && performance.now() < toastDeadline) {
      try {
        const note = new Notification('UOA · ' + labels.join(', '), {body: body, tag: 'uoa-' + first.key});
        notes.set(note, {deadline: toastDeadline, keys: toastKeys});
        note.onclick = function () { openDesk(); note.close(); };
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
        catch (error) { if (armed === generation) failStorage(error); return; }
        if (enabled && armed === generation) show(fresh, data);
      });
      if (enabled) say('Uyarılar açık · ' + data.detail + (data.exit_status === 'unavailable' ? ' Çıkış uyarıları doğrulanamıyor.' : '') + ' ' + channelText());
    } catch (_) {
      if (enabled && armed === generation) { retire(); say('Uyarı bağlantısı kurulamadı; güncel sinyal doğrulanamıyor. Yeniden denenecek.'); }
    } finally { polling = false; }
  }
  async function baseline(armed, putsOnly) {
    const data = await load();
    if (armed !== generation) return null;
    if (!['current', 'not_live', 'no_scan', 'stale'].includes(data.status)) throw new Error('no usable feed');
    await lock(async function () {
      if (armed === generation) await claim(data.signals.filter(function (s) { return valid(s, data) && (!putsOnly || s.event_type !== 'exit' && ['long_put', 'bear_put_debit'].includes(s.instrument)); }), data.server_time);
    });
    if (armed !== generation) return null;
    return data;
  }
  async function upgradeDirections() {
    const armed = ++generation;
    enabled = false; busy = true; retire(); paint();
    try {
      await baseline(armed, true);
      if (armed !== generation) return;
      enabled = true;
      try { persist(); } catch (error) { failStorage(error); return; }
      paint(); say('Tüm yönler izleniyor. Mevcut PUT planları başlangıç kaydına alındı; sonraki yeni planlar bildirilir.');
      poll();
    } catch (error) {
      if (armed === generation) failStorage(error);
    } finally { busy = false; paint(); }
  }
  async function enable(wantDesktop) {
    const armed = ++generation, wasEnabled = enabled;
    busy = true; deliveryIssue = ''; paint(); say('Uyarı bağlantısı kuruluyor…');
    // Audio and any permission request start on this explicit user gesture.
    const sound = unlockAudio().catch(function () {});
    let requested = Promise.resolve();
    if (wantDesktop && permission() === 'default') {
      try { requested = Promise.resolve(Notification.requestPermission()).then(function () {}, function () { deliveryIssue = 'PC izni istenemedi.'; }); }
      catch (_) { deliveryIssue = 'PC izni istenemedi; sayfa uyarısı kullanılacak.'; }
    }
    try {
      await sound;
      if (armed !== generation) return;
      await requested;
      if (armed !== generation) return;
      paintChannels();
      const data = wasEnabled ? null : await baseline(armed);
      if (armed !== generation) return;
      enabled = true;
      try { persist(); } catch (error) { failStorage(error); return; }
      generation++; paint();
      say('Uyarılar açık. ' + (data ? 'Mevcut öneriler başlangıç olarak alındı; sonraki yeni ve güncel sinyal bildirilecek. ' + data.detail + ' ' : '') + channelText());
    } catch (error) {
      if (armed !== generation) return;
      if (error && error.code === 'seen_capacity') failStorage(error);
      else { enabled = false; paint(); say('Uyarılar açılamadı. Bağlantıyı ve tarayıcı kayıt iznini kontrol edip tekrar dene.'); }
    } finally { busy = false; paint(); }
  }
  toggle.addEventListener('click', async function () {
    if (enabled) {
      enabled = false; generation++; retire();
      try { persist(); } catch (_) { failStorage(); return; }
      paint(); say('Uyarılar kapalı.'); return;
    }
    await enable(false);
  });
  desktop.addEventListener('click', async function () { await enable(true); });
  desktopTest.addEventListener('click', function () {
    if (permission() !== 'granted') { paintChannels(); say('PC denemesi için önce bildirim izni ver.'); return; }
    try {
      const note = new Notification('UOA · DENEME BİLDİRİMİ', {body: 'Bu sentetik bir bildirim denemesidir; işlem sinyali değildir.', tag: 'uoa-desktop-test'});
      const deadline = performance.now() + 5000;
      notes.set(note, {deadline: deadline, keys: []});
      note.onclick = function () { openDesk(); note.close(); };
      setTimeout(function () { note.close(); notes.delete(note); }, 5000);
      say('Deneme bildirimi tarayıcıya gönderildi. Ekranda görünmesi işletim sistemi ve rahatsız etmeyin ayarlarına bağlıdır.');
    } catch (_) { say('PC denemesi açılamadı. Tarayıcı ve işletim sistemi bildirim ayarlarını kontrol et.'); }
    paintChannels();
  });
  test.addEventListener('click', async function () {
    try { await unlockAudio(); if (!ding()) throw new Error('sound failed'); deliveryIssue = ''; say('Test sesi çalındı. ' + (enabled ? channelText() : 'Sinyal izlemek için uyarıları aç.')); }
    catch (_) { say('Ses etkinleştirilemedi. Tarayıcının site sesine izin verdiğini kontrol et.'); }
  });
  async function changeScope() {
    const armed = ++generation; retire();
    if (!enabled) { try { persist(); } catch (_) { failStorage(); } return; }
    scope.disabled = true; busy = true; enabled = false; paint();
    try {
      await baseline(armed);
      if (armed !== generation) return;
      enabled = true;
      try { persist(); } catch (error) { failStorage(error); return; }
      paint();
      say('Kapsam değişti. Mevcut öneriler başlangıç olarak alındı. ' + channelText());
    } catch (error) {
      if (armed !== generation) return;
      if (error && error.code === 'seen_capacity') failStorage(error);
      else {
        try { persist(); } catch (failure) { failStorage(failure); return; }
        say('Kapsam kaydedilemedi; uyarıları yeniden aç.');
      }
    } finally { scope.disabled = false; busy = false; paint(); }
  }
  scope.addEventListener('change', changeScope);
  if (exitChoice) exitChoice.addEventListener('change', changeScope);
  document.getElementById('signal-dismiss').addEventListener('click', function () { toast.hidden = true; });
  function openDesk() {
    window.focus();
    if (window.location.pathname !== '/' && window.location.pathname !== '/acilis') window.location.assign('/acilis');
    else document.dispatchEvent(new Event('uoa:refresh-request'));
  }
  document.getElementById('signal-refresh').addEventListener('click', function () {
    openDesk(); toast.hidden = true;
  });
  window.addEventListener('storage', function (event) {
    if (event.key !== PREF) return;
    try {
      const p = readPref(); enabled = p.enabled; scope.value = p.scope; generation++; paint();
      if (exitChoice) exitChoice.checked = p.exits;
      retire();
      if (enabled && p.scope === 'all' && !p.scopeVersion) upgradeDirections();
      else say(enabled ? 'Uyarılar açık. ' + channelText() : 'Uyarılar diğer sekmeden kapatıldı.');
    } catch (_) { failStorage(); }
  });
  function expireToast() {
    if (performance.now() >= toastDeadline) toast.hidden = true;
    notes.forEach(function (item, note) { if (performance.now() >= item.deadline) { note.close(); notes.delete(note); } });
  }
  document.addEventListener('visibilitychange', function () { expireToast(); paintChannels(); if (document.visibilityState === 'visible') poll(); });
  window.addEventListener('focus', paintChannels);
  setInterval(expireToast, 1000);
  setInterval(poll, pollSeconds * 1000);
  try {
    const p = readPref(); enabled = p.enabled; scope.value = p.scope; paint();
    if (exitChoice) exitChoice.checked = p.exits;
    if (enabled && p.scope === 'all' && !p.scopeVersion) upgradeDirections();
    else if (enabled) { say('Uyarı bağlantısı kuruluyor. ' + channelText()); poll(); }
  } catch (_) { failStorage(); }
})();
