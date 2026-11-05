'use strict';

// Real production script, isolated browser capabilities. No external packages,
// real timers, sockets, audio devices, permission prompts or notifications.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../webapp/static/live-alerts.js'), 'utf8');
const PREF = 'uoa.live-alerts.pref.v1';
const EPOCH = Date.parse('2026-11-04T12:00:00Z');
const copy = value => value === undefined ? undefined : structuredClone(value);
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  emit(event) { return (this.listeners.get(event.type) || []).map(fn => fn.call(this, event)); }
  dispatchEvent(event) { this.emit(event); return true; }
}

class Element extends Events {
  constructor(id) {
    super(); this.id = id; this.disabled = false; this.hidden = id === 'signal-toast';
    this.textContent = ''; this.value = 'all'; this.checked = true; this.dataset = {}; this.attributes = {};
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
}

function signal(number, overrides = {}) {
  return {key: number.toString(16).padStart(64, '0'), event_type: 'entry', instrument: 'long_call',
    ticker: 'SYNTH' + number, label: 'Synthetic plan', underlying_ref: 100,
    underlying_stop: 95, underlying_target: 110, expires_at: new Date(EPOCH + 600000).toISOString(), ...overrides};
}

class Browser {
  constructor({permission = 'granted', pref, maxSeen = 20} = {}) {
    this.now = 0; this.permission = permission; this.maxSeen = maxSeen; this.tabs = new Set();
    this.values = new Map(); if (pref) this.values.set(PREF, JSON.stringify(pref));
    this.seen = {}; this.transactions = []; this.active = null; this.paused = false;
    this.failOpen = false; this.failCommit = false; this.storageBroken = false; this.failPrefWrite = false;
    this.opens = 0; this.notifications = []; this.permissionRequests = []; this.permissionGate = null;
    this.signals = []; this.feedStatus = 'current'; this.fetches = []; this.writes = [];
  }
  feed() { return {status: this.feedStatus, detail: 'Synthetic stored feed', exit_status: 'current',
    server_time: new Date(EPOCH + this.now).toISOString(), signals: copy(this.signals)}; }
  writePref(pref, origin = null) {
    const value = typeof pref === 'string' ? pref : JSON.stringify(pref);
    const previous = this.values.get(PREF);
    this.values.set(PREF, value); this.writes.push(JSON.parse(value));
    if (previous === value) return; // Browsers do not dispatch unchanged values.
    for (const tab of this.tabs) if (tab !== origin && !tab.closed) {
      queueMicrotask(() => { if (!tab.closed) tab.window.emit({type: 'storage', key: PREF, newValue: value}); });
    }
  }
  pref() { return JSON.parse(this.values.get(PREF) || '{}'); }
  pump() {
    if (this.active || this.paused || !this.transactions.length) return;
    const tx = this.active = this.transactions.shift();
    queueMicrotask(() => {
      let staged = copy(this.seen);
      try {
        tx.request.result = copy(this.seen);
        tx.stage = value => { staged = copy(value); };
        if (tx.request.onsuccess) tx.request.onsuccess();
      } catch (error) { tx.error = error; tx.aborted = true; }
      queueMicrotask(() => {
        if (this.failCommit) { tx.error = new Error('synthetic commit failure'); tx.aborted = true; }
        if (tx.aborted) {
          if (tx.onabort) tx.onabort();
          else if (tx.onerror) tx.onerror();
        } else {
          this.seen = staged;
          if (tx.oncomplete) tx.oncomplete();
        }
        this.active = null; this.pump();
      });
    });
  }
  database() {
    const hub = this;
    return {createObjectStore() {}, close() {}, transaction(name, mode) {
      assert.equal(name, 'seen'); assert.equal(mode, 'readwrite');
      const tx = {request: {}, aborted: false, stage: null, error: null,
        abort() { this.aborted = true; this.error = new Error('synthetic abort'); },
        objectStore() { return {
          get(key) { assert.equal(key, 'keys'); return tx.request; },
          put(value, key) { assert.equal(key, 'keys'); tx.stage(value); }
        }; }};
      hub.transactions.push(tx); queueMicrotask(() => hub.pump()); return tx;
    }};
  }
  releaseTransactions() { this.paused = false; this.pump(); }
  advance(milliseconds) {
    this.now += milliseconds;
    for (const tab of this.tabs) {
      if (tab.closed) continue;
      for (const [id, timer] of [...tab.timers]) if (timer.at <= this.now) {
        if (!timer.interval) tab.timers.delete(id);
        else timer.at = this.now + timer.interval;
        // Polls are invoked explicitly; expiry timers still run at original time.
        if (timer.interval !== 30000) timer.fn();
      }
    }
  }
  tab({supported = true, locks = false} = {}) {
    const hub = this, tab = {window: new Events(), document: new Events(), elements: {}, timers: new Map(),
      nextTimer: 0, gesture: false, notifications: [], navigation: [], closed: false, audioStarts: 0, fetchGate: null, audioGate: null};
    this.tabs.add(tab);
    for (const id of ['live-alert-controls', 'alerts-toggle', 'alerts-test', 'alerts-scope', 'alerts-status',
      'alerts-exits', 'signal-toast', 'signal-message', 'signal-dismiss', 'signal-refresh',
      'alerts-desktop', 'alerts-desktop-test', 'alerts-channel-status']) tab.elements[id] = new Element(id);
    tab.elements['live-alert-controls'].dataset = {pollSeconds: '30', seenTtl: '86400', maxSeen: String(this.maxSeen)};
    tab.document.getElementById = id => tab.elements[id] || null;
    tab.document.visibilityState = 'visible';
    tab.window.focus = () => {};
    tab.window.location = {pathname: '/', assign(url) { tab.navigation.push(url); }};
    tab.window.isSecureContext = true;
    class AudioContext {
      constructor() { this.state = 'suspended'; this.currentTime = 0; this.destination = {}; }
      async resume() { if (tab.audioGate) await tab.audioGate.promise; this.state = 'running'; }
      createOscillator() { return {frequency: {}, connect() {}, start() { tab.audioStarts++; }, stop() {}}; }
      createGain() { return {connect() {}, gain: {setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {}}}; }
    }
    tab.window.AudioContext = AudioContext;
    function Notification(title, options) {
      assert.equal(hub.permission, 'granted', 'notification requires granted permission');
      this.title = title; this.options = options; this.createdAt = hub.now; this.closed = false;
      this.close = () => { this.closed = true; };
      tab.notifications.push(this); hub.notifications.push(this);
    }
    Object.defineProperty(Notification, 'permission', {get() { return hub.permission; }});
    Notification.requestPermission = function () {
      hub.permissionRequests.push({tab, gesture: tab.gesture});
      assert.equal(tab.gesture, true, 'permission may only be requested synchronously during a click');
      return (hub.permissionGate ? hub.permissionGate.promise : Promise.resolve('granted')).then(value => { hub.permission = value; return value; });
    };
    if (supported) tab.window.Notification = Notification;
    const navigator = locks ? {locks: {request(_name, fn) { return Promise.resolve().then(fn); }}} : {};
    const context = {window: tab.window, document: tab.document, navigator,
      localStorage: {
        getItem(key) { if (hub.storageBroken) throw new Error('synthetic storage failure'); return hub.values.get(key) ?? null; },
        setItem(key, value) { if (hub.storageBroken || hub.failPrefWrite) throw new Error('synthetic storage failure'); assert.equal(key, PREF); hub.writePref(value, tab); }
      },
      indexedDB: {open() {
        hub.opens++; const request = {};
        queueMicrotask(() => {
          if (hub.failOpen) { request.error = new Error('synthetic open failure'); if (request.onerror) request.onerror(); }
          else { request.result = hub.database(); if (request.onsuccess) request.onsuccess(); }
        }); return request;
      }},
      async fetch(url, options) {
        assert.equal(url, '/api/acilis/uyarilar');
        assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
        hub.fetches.push({tab, url}); const value = hub.feed();
        if (tab.fetchGate) await tab.fetchGate.promise;
        return {ok: true, async json() { return copy(value); }};
      },
      performance: {now() { return hub.now; }}, AbortSignal: {timeout() { return {}; }},
      Event: class Event { constructor(type) { this.type = type; } }, console,
      setTimeout(fn, delay) { const id = ++tab.nextTimer; tab.timers.set(id, {fn, at: hub.now + delay, interval: 0}); return id; },
      clearTimeout(id) { tab.timers.delete(id); },
      setInterval(fn, delay) { const id = ++tab.nextTimer; tab.timers.set(id, {fn, at: hub.now + delay, interval: delay}); return id; },
      clearInterval(id) { tab.timers.delete(id); }
    };
    if (supported) context.Notification = Notification;
    tab.context = vm.createContext(context);
    tab.click = id => {
      if (tab.elements[id].disabled) return Promise.resolve();
      tab.gesture = true;
      let results;
      try { results = tab.elements[id].emit({type: 'click'}); } finally { tab.gesture = false; }
      return Promise.all(results).then(settle);
    };
    tab.change = async (value, id = 'alerts-scope') => {
      tab.elements[id][id === 'alerts-scope' ? 'value' : 'checked'] = value;
      await Promise.all(tab.elements[id].emit({type: 'change'})); await settle();
    };
    tab.poll = async () => {
      const timer = [...tab.timers.values()].find(timer => timer.interval === 30000);
      assert.ok(timer, 'existing 30-second poll is preserved'); await timer.fn(); await settle();
    };
    tab.enabled = () => tab.elements['alerts-toggle'].getAttribute('aria-pressed') === 'true';
    tab.text = () => tab.elements['alerts-status'].textContent + ' ' + tab.elements['alerts-channel-status'].textContent;
    tab.close = () => { tab.closed = true; tab.timers.clear(); };
    vm.runInContext(source, tab.context, {filename: 'live-alerts.js'});
    return tab;
  }
}

test('new browsers default to all supported directions and retain an existing CALL-only choice', async () => {
  const fresh = new Browser(), a = fresh.tab(); await settle();
  assert.equal(a.elements['alerts-scope'].value, 'all'); assert.equal(fresh.permissionRequests.length, 0);
  const old = new Browser({pref: {enabled: false, scope: 'calls', exits: false}}), b = old.tab(); await settle();
  assert.equal(b.elements['alerts-scope'].value, 'calls'); assert.equal(b.elements['alerts-exits'].checked, false);
  assert.equal(old.fetches.length, 0); assert.equal(old.permissionRequests.length, 0);
});

test('sound enable baselines existing plans without asking for desktop permission', async () => {
  const hub = new Browser({permission: 'default'}), tab = hub.tab(); hub.signals = [signal(1)];
  await tab.click('alerts-toggle'); await tab.poll();
  assert.equal(tab.enabled(), true); assert.equal(tab.elements['signal-toast'].hidden, true);
  assert.equal(hub.notifications.length, 0); assert.equal(hub.permissionRequests.length, 0);
  assert.ok(Object.hasOwn(hub.seen, signal(1).key));
});

test('one fresh plan is delivered once across polls and a page reload', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(2)]; await tab.poll(); await tab.poll();
  assert.equal(hub.notifications.length, 1); assert.equal(tab.elements['signal-toast'].hidden, false);
  tab.close(); hub.tab(); await settle();
  assert.equal(hub.notifications.length, 1, 'reload retains committed seen identities');
});

test('two tabs atomically claim a fresh identity without Web Locks', async () => {
  const hub = new Browser(), a = hub.tab(), b = hub.tab();
  await a.click('alerts-toggle'); await settle(); assert.equal(b.enabled(), true);
  hub.signals = [signal(3)]; await Promise.all([a.poll(), b.poll()]);
  assert.equal(hub.notifications.length, 1); assert.equal(Object.keys(hub.seen).length, 1);
});

test('scope changes baseline existing stock plans and only later fresh plans alert', async () => {
  const hub = new Browser({pref: {enabled: false, scope: 'calls'}}), tab = hub.tab();
  hub.signals = [signal(4, {instrument: 'stock'})]; await tab.click('alerts-toggle');
  await tab.poll(); assert.equal(hub.notifications.length, 0);
  await tab.change('all'); await tab.poll(); assert.equal(hub.notifications.length, 0);
  hub.signals.push(signal(5, {instrument: 'stock'})); await tab.poll(); assert.equal(hub.notifications.length, 1);
});

test('desktop permission starts during the explicit click and enable baselines history', async () => {
  const hub = new Browser({permission: 'default'}), tab = hub.tab(); hub.signals = [signal(6)];
  hub.permissionGate = deferred(); const enabling = tab.click('alerts-desktop');
  assert.equal(hub.permissionRequests.length, 1); assert.equal(hub.permissionRequests[0].gesture, true);
  hub.permissionGate.resolve('granted'); await enabling; await tab.poll();
  assert.equal(tab.enabled(), true); assert.equal(hub.notifications.length, 0);
  assert.match(tab.text(), /PC|masaüst|bildirim/i);
});

test('granting desktop permission on an enabled tab never rebaselines away a fresh plan', async () => {
  const hub = new Browser({permission: 'default'}), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(7)]; const before = hub.fetches.length;
  await tab.click('alerts-desktop'); assert.equal(hub.fetches.length, before);
  await tab.poll(); assert.equal(hub.notifications.length, 1);
});

test('desktop test is visibly synthetic, never arms alerts or asks for permission', async () => {
  for (const permission of ['default', 'denied', 'granted']) {
    const hub = new Browser({permission}), tab = hub.tab(); await tab.click('alerts-desktop-test');
    assert.equal(hub.permissionRequests.length, 0); assert.equal(hub.fetches.length, 0);
    assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, permission === 'granted' ? 1 : 0);
    if (permission === 'granted') {
      const note = hub.notifications[0]; assert.match(note.title + ' ' + note.options.body, /test|deneme/i);
      assert.match(tab.text(), /tarayıcı|gönder|ilet|çağrı/i);
      hub.advance(5001); assert.equal(note.closed, true, 'synthetic notification expires after five seconds');
    }
  }
});

test('denied and unsupported desktops give guidance without prompting', async () => {
  const denied = new Browser({permission: 'denied'}), a = denied.tab(); await a.click('alerts-desktop');
  assert.equal(denied.permissionRequests.length, 0); assert.equal(denied.notifications.length, 0);
  assert.match(a.text(), /ayar|engell|redd/i);
  const unsupported = new Browser(), b = unsupported.tab({supported: false}); await b.click('alerts-desktop');
  await b.click('alerts-desktop-test'); assert.equal(unsupported.notifications.length, 0);
  assert.match(b.text(), /desteklen|desteklem|kullanılam/i);
});

test('another tab disabling during enable baseline cannot be undone by its late response', async () => {
  const hub = new Browser(), tab = hub.tab(); tab.fetchGate = deferred();
  const pending = tab.click('alerts-toggle'); await settle(); assert.equal(hub.fetches.length, 1);
  hub.writePref({enabled: false, scope: 'all', exits: true}); await settle();
  tab.fetchGate.resolve(); await pending;
  assert.equal(tab.enabled(), false); assert.equal(hub.pref().enabled, false); assert.equal(hub.notifications.length, 0);
});

test('another tab disabling during scope baseline cannot be undone', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  tab.fetchGate = deferred(); const pending = tab.change('calls'); await settle();
  hub.writePref({enabled: false, scope: 'calls', exits: true}); await settle();
  tab.fetchGate.resolve(); await pending;
  assert.equal(tab.enabled(), false); assert.equal(hub.pref().enabled, false); assert.equal(hub.notifications.length, 0);
});

test('a late permission grant cannot undo another tab disabling alerts', async () => {
  const hub = new Browser({permission: 'default'}), tab = hub.tab(); hub.permissionGate = deferred();
  const pending = tab.click('alerts-desktop'); await settle();
  hub.writePref({enabled: false, scope: 'all', exits: true}); await settle();
  hub.permissionGate.resolve('granted'); await pending;
  assert.equal(tab.enabled(), false); assert.equal(hub.pref().enabled, false); assert.equal(hub.notifications.length, 0);
});

test('delayed IndexedDB claims cannot extend the original server expiry', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(8, {expires_at: new Date(EPOCH + 10000).toISOString()})];
  hub.paused = true; const pending = tab.poll(); await settle(); assert.equal(hub.transactions.length, 1);
  hub.advance(4000); hub.releaseTransactions(); await pending; assert.equal(hub.notifications.length, 1);
  hub.advance(6001); assert.equal(tab.elements['signal-toast'].hidden, true);
  assert.equal(hub.notifications[0].closed, true);
});

test('signals expiring during a claim produce neither banner, sound nor desktop delivery', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle'); const sounds = tab.audioStarts;
  hub.signals = [signal(9, {expires_at: new Date(EPOCH + 1000).toISOString()})];
  hub.paused = true; const pending = tab.poll(); await settle(); hub.advance(1001);
  hub.releaseTransactions(); await pending;
  assert.equal(hub.notifications.length, 0); assert.equal(tab.elements['signal-toast'].hidden, true);
  assert.equal(tab.audioStarts, sounds);
});

test('withdrawal retires a previously displayed notification and banner', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(10)]; await tab.poll(); assert.equal(hub.notifications.length, 1);
  hub.signals = []; await tab.poll();
  assert.equal(hub.notifications[0].closed, true); assert.equal(tab.elements['signal-toast'].hidden, true);
});

test('native notification and banner actions open the canonical desk from every private screen', async () => {
  for (const pathname of ['/haberler', '/', '/acilis']) {
    const hub = new Browser(), tab = hub.tab(); tab.window.location.pathname = pathname;
    let refreshes = 0; tab.document.addEventListener('uoa:refresh-request', () => { refreshes++; });
    await tab.click('alerts-toggle'); hub.signals = [signal(22)]; await tab.poll();
    const note = hub.notifications[0]; assert.ok(note); note.onclick();
    assert.equal(note.closed, true); await tab.click('signal-refresh');
    assert.equal(tab.elements['signal-toast'].hidden, true);
    if (pathname === '/haberler') {
      assert.deepEqual(tab.navigation, ['/acilis', '/acilis']); assert.equal(refreshes, 0);
    } else {
      assert.deepEqual(tab.navigation, []); assert.equal(refreshes, 2);
    }
  }
});

test('capacity overflow fails visibly closed instead of replaying evicted live identities', async () => {
  const hub = new Browser({maxSeen: 2}), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(11), signal(12)]; await tab.poll(); const before = copy(hub.seen);
  hub.signals.push(signal(13)); await tab.poll(); await tab.poll();
  assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, 1);
  assert.deepEqual(hub.seen, before, 'overflow aborts without evicting still-live identities');
  assert.match(tab.text(), /kapasite|sınır|kayıt|tekrar/i);
});

test('failed first IndexedDB open can recover only through explicit enable and a new baseline', async () => {
  const hub = new Browser(), tab = hub.tab(); hub.failOpen = true; hub.signals = [signal(14)];
  await tab.click('alerts-toggle'); assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, 0);
  hub.failOpen = false; await tab.poll(); assert.equal(hub.notifications.length, 0);
  await tab.click('alerts-toggle'); assert.equal(tab.enabled(), true); assert.ok(hub.opens >= 2);
  await tab.poll(); assert.equal(hub.notifications.length, 0);
  hub.signals.push(signal(15)); await tab.poll(); assert.equal(hub.notifications.length, 1);
});

test('failed seen commit rolls back and recovery baselines rather than replaying plans', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(16)]; hub.failCommit = true; await tab.poll();
  assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, 0);
  assert.equal(Object.hasOwn(hub.seen, signal(16).key), false);
  hub.failCommit = false; await tab.click('alerts-toggle'); await tab.poll();
  assert.equal(tab.enabled(), true); assert.equal(hub.notifications.length, 0);
  hub.signals.push(signal(17)); await tab.poll(); assert.equal(hub.notifications.length, 1);
});

test('failed enable preference persistence leaves alerts off and explicit retry baselines history', async () => {
  const hub = new Browser(), tab = hub.tab(); hub.signals = [signal(18)]; hub.failPrefWrite = true;
  await tab.click('alerts-toggle');
  assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, 0);
  assert.match(tab.text(), /kayıt|depolama|tarayıcı/i);
  hub.failPrefWrite = false; await tab.click('alerts-toggle'); await tab.poll();
  assert.equal(tab.enabled(), true); assert.equal(hub.notifications.length, 0);
  hub.signals.push(signal(19)); await tab.poll(); assert.equal(hub.notifications.length, 1);
});

test('failed scope preference persistence stays visibly disabled until a fresh explicit baseline', async () => {
  const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
  hub.signals = [signal(20)]; hub.failPrefWrite = true; await tab.change('calls');
  assert.equal(tab.enabled(), false); assert.equal(hub.notifications.length, 0);
  assert.match(tab.text(), /kayıt|depolama|tarayıcı/i);
  hub.failPrefWrite = false; await tab.click('alerts-toggle'); await tab.poll();
  assert.equal(tab.enabled(), true); assert.equal(hub.notifications.length, 0);
  hub.signals.push(signal(21)); await tab.poll(); assert.equal(hub.notifications.length, 1);
});

for (const failure of ['open', 'commit']) {
  test(`previously enabled ${failure} failure survives reload without automatically replaying unseen plans`, async () => {
    const hub = new Browser({pref: {enabled: true, scope: 'all', exits: true}});
    hub.signals = [signal(23)];
    hub.failOpen = failure === 'open'; hub.failCommit = failure === 'commit';
    const failed = hub.tab(); await settle();
    assert.equal(failed.enabled(), false); assert.equal(hub.pref().enabled, false);
    assert.equal(hub.notifications.length, 0); assert.equal(Object.hasOwn(hub.seen, signal(23).key), false);
    failed.close(); hub.failOpen = false; hub.failCommit = false;
    const previousFetches = hub.fetches.length, reloaded = hub.tab(); await settle();
    assert.equal(reloaded.enabled(), false); assert.equal(hub.fetches.length, previousFetches);
    assert.equal(hub.notifications.length, 0, 'healthy storage must not revive a failed opt-in on reload');
    await reloaded.click('alerts-toggle'); await reloaded.poll();
    assert.equal(reloaded.enabled(), true); assert.equal(hub.notifications.length, 0);
    assert.ok(Object.hasOwn(hub.seen, signal(23).key), 'explicit enable records an existing-plan baseline');
    hub.signals.push(signal(24)); await reloaded.poll(); assert.equal(hub.notifications.length, 1);
  });
}

function put(number, instrument = 'long_put', overrides = {}) {
  return signal(number, {instrument, label: 'PUT AL · Düşüş tezi', underlying_stop: 105, underlying_target: 90, ...overrides});
}
for (const instrument of ['long_put', 'bear_put_debit']) {
  test(`fresh ${instrument} entry delivers once and stays deduplicated on reload`, async () => {
    const hub = new Browser(), tab = hub.tab(); await tab.click('alerts-toggle');
    hub.signals = [put(41, instrument)]; await tab.poll(); await tab.poll();
    assert.equal(hub.notifications.length, 1); assert.match(hub.notifications[0].title, /PUT/);
    tab.close(); hub.tab(); await settle(); assert.equal(hub.notifications.length, 1);
  });
}
for (const [scope, expected] of [['all',[51,52,53,54,55]],['up',[51,52,53]],['calls',[52,53]],['puts',[54,55]]]) {
  test(`${scope} scope admits exactly its directional instruments`, async () => {
    const hub=new Browser({pref:{enabled:false,scope}}), tab=hub.tab(); await tab.click('alerts-toggle');
    hub.signals=[signal(51,{instrument:'stock'}),signal(52),signal(53,{instrument:'bull_call_debit'}),put(54),put(55,'bear_put_debit')];
    await tab.poll();
    assert.deepEqual(Object.keys(hub.seen).sort(), expected.map(n=>signal(n).key).sort());
    assert.equal(hub.notifications.length,1);
  });
}
test('invalid and expired PUT entries stay silent',async()=>{
 const hub=new Browser(),tab=hub.tab();await tab.click('alerts-toggle');
 hub.signals=[put(61,'long_put',{underlying_stop:95}),put(62,'bear_put_debit',{underlying_target:110}),put(63,'long_put',{expires_at:new Date(EPOCH-1).toISOString()})];
 await tab.poll();assert.equal(hub.notifications.length,0);assert.equal(tab.elements['signal-toast'].hidden,true);
});
test('PUT exit reviews remain independent of CALL-only entry scope',async()=>{
 const hub=new Browser({pref:{enabled:false,scope:'calls',exits:true}}),tab=hub.tab();await tab.click('alerts-toggle');
 hub.signals=[put(64),put(65,'long_put',{event_type:'exit',message:'Existing PUT position review'})];await tab.poll();
 assert.equal(hub.notifications.length,1);assert.ok(Object.hasOwn(hub.seen,put(65).key));assert.ok(!Object.hasOwn(hub.seen,put(64).key));
});
test('scope expansion baselines existing PUT plans before accepting later fresh ones',async()=>{
 const hub=new Browser({pref:{enabled:false,scope:'calls'}}),tab=hub.tab();hub.signals=[put(66)];await tab.click('alerts-toggle');
 await tab.change('puts');await tab.poll();assert.equal(hub.notifications.length,0);
 hub.signals.push(put(67));await tab.poll();assert.equal(hub.notifications.length,1);
 assert.equal(hub.pref().scope,'puts');tab.close();const other=hub.tab();await settle();assert.equal(other.elements['alerts-scope'].value,'puts');assert.equal(hub.notifications.length,1);
});
test('legacy enabled all scope baselines PUTs without swallowing fresh upward signals or prompting',async()=>{
 const hub=new Browser({pref:{enabled:true,scope:'all',exits:true}});hub.signals=[put(71),signal(72)];const tab=hub.tab();await settle();await settle();
 assert.equal(hub.pref().scopeVersion,2);assert.equal(hub.permissionRequests.length,0);
 assert.ok(Object.hasOwn(hub.seen,put(71).key));assert.equal(hub.notifications.length,1);assert.match(hub.notifications[0].title,/SYNTH72/);
 hub.signals.push(put(73));await tab.poll();assert.equal(hub.notifications.length,2);
});
test('legacy migration cannot undo another tab disabling alerts during durable baseline',async()=>{
 const hub=new Browser({pref:{enabled:true,scope:'all',exits:true}});hub.paused=true;hub.signals=[put(74)];const tab=hub.tab();await settle();
 hub.writePref({enabled:false,scope:'all',exits:true});await settle();hub.paused=false;hub.pump();await settle();await settle();
 assert.equal(tab.enabled(),false);assert.equal(hub.pref().enabled,false);assert.equal(hub.notifications.length,0);
});
test('unknown persisted or current scopes cannot broaden entry delivery',async()=>{
 const hub=new Browser({pref:{enabled:true,scope:'unknown'}}),tab=hub.tab();await settle();assert.equal(tab.enabled(),false);assert.equal(hub.fetches.length,0);
 await tab.click('alerts-toggle');await tab.change('unknown');hub.signals=[put(75),signal(76)];await tab.poll();assert.equal(hub.notifications.length,0);
});

 test('legacy migration leaves fresh PUT exits available to the normal exit channel',async()=>{
  const hub=new Browser({pref:{enabled:true,scope:'all',exits:true}});hub.signals=[put(81),put(82,'long_put',{event_type:'exit',message:'Existing PUT exit review'})];hub.tab();await settle();await settle();
  assert.equal(hub.notifications.length,1);assert.match(hub.notifications[0].options.body,/Existing PUT exit review/);assert.ok(Object.hasOwn(hub.seen,put(81).key));
 });
 test('parallel legacy migrations never notify the existing PUT or duplicate a fresh CALL',async()=>{
  const hub=new Browser({pref:{enabled:true,scope:'all',exits:true}});hub.signals=[put(83),signal(84)];hub.tab();hub.tab();await settle();await settle();
  assert.equal(hub.notifications.length,1);assert.match(hub.notifications[0].title,/SYNTH84/);
 });
