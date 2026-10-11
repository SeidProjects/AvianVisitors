import fs from 'node:fs';
import vm from 'node:vm';
import { createHash, webcrypto } from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';

const draft = fs.readFileSync(new URL('../frontend/apt.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
const css = fs.readFileSync(new URL('../frontend/public-static.css', import.meta.url), 'utf8');
const section = (from, to) => {
  const a = draft.indexOf(from), b = draft.indexOf(to, a + from.length);
  assert(a >= 0 && b > a, 'test source anchors exist');
  return draft.slice(a, b);
};
const helpers = section('  // Optional CDN snapshots.', '  // ---- Sliding pill helper');
const base = 'https://cdn.example.net';
const indexUrl = base + '/data/snapshot-index.json';
const sci = 'Calypte anna';
const latestFile = "Anna's_Hummingbird-0.61-2026-10-11-birdnet-11-03-07.mp3";
const rowFile = 'Mésange_à_tête_noire-0.97-2026-10-10-birdnet-08-02-19.mp3';
const latestUrl = base + '/audio/latest/' + encodeURIComponent(latestFile);
const rowUrl = base + '/audio/older/' + encodeURIComponent(rowFile);
const imageUrl = base + '/images/g1/calypte-anna.png';
const encode = value => Buffer.from(JSON.stringify(value), 'utf8');
const buffer = value => value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
function response(raw, ok = true, chunkSize = 97) {
  let offset = 0;
  return { ok, status: ok ? 200 : 503, headers: { get: () => String(raw.length) },
    body: { getReader() { return { read() {
      if (offset >= raw.length) return Promise.resolve({ done: true });
      const value = new Uint8Array(raw.subarray(offset, offset + chunkSize)); offset += value.length;
      return Promise.resolve({ done: false, value });
    }, cancel: () => Promise.resolve() }; } },
    arrayBuffer: () => Promise.resolve(buffer(raw)), json: () => Promise.resolve(JSON.parse(raw)) };
}
function httpError(status) {
  return Object.assign(response(Buffer.alloc(0), false), { status });
}
const unescape = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
const drain = async () => { await new Promise(resolve => setImmediate(resolve)); };
const settle = async () => { for (let i = 0; i < 10; i++) await drain(); };

class Element {
  constructor() {
    this.attrs = {}; this.dataset = {}; this.style = { setProperty() {} }; this.listeners = {};
    this.children = []; this.maps = {}; this.textContent = ''; this._html = '';
    this.clientWidth = 1000; this.clientHeight = 700; this.width = 600; this.height = 120;
    const classes = new Set();
    this.classList = { add(...v) { v.forEach(x => classes.add(x)); }, remove(...v) { v.forEach(x => classes.delete(x)); },
      toggle(v) { classes.has(v) ? classes.delete(v) : classes.add(v); }, contains(v) { return classes.has(v); } };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(k, f) { this.listeners[k] = f; }
  removeEventListener() {}
  querySelector(k) { return this.maps[k] || null; }
  querySelectorAll(k) { return this.maps[k] || []; }
  appendChild(e) { this.children.push(e); this.firstChild = this.children[0]; }
  removeChild(e) { this.children = this.children.filter(child => child !== e); this.firstChild = this.children[0]; }
  insertBefore(e) { this.appendChild(e); }
  insertAdjacentHTML(_, s) { this._html = s + this._html; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 700 }; }
  contains() { return true; }
  focus() {}
  getContext() { return { fillRect() {}, clearRect() {}, getImageData: () => ({ data: [] }), putImageData() {} }; }
  set src(value) { this.setAttribute('src', value); }
  get src() { return this.getAttribute('src') || ''; }
  set innerHTML(s) { this._html = s; this.children = []; }
  get innerHTML() { return this._html; }
}
function generation(id = 'g1', count = 1, minute = 0) {
  const objects = new Map();
  const index = { schema_version: 2, generation: id, published_at: `2026-10-11T15:${String(minute).padStart(2, '0')}:00Z`,
    as_of: `2026-10-11T11:${String(minute).padStart(2, '0')}:00-04:00`, timezone: 'America/New_York',
    compatibility: { last_hour: 'php_today_only' }, media_manifest_generation: 'media-' + id, routes: {}, objects: {} };
  const store = value => {
    const raw = encode(value), hash = createHash('sha256').update(raw).digest('hex'), route = 'objects/' + hash + '.json';
    objects.set(new URL(route, indexUrl).href, raw); index.objects[route] = { sha256: hash, bytes: raw.length }; return route;
  };
  const bird = { sci, com: "Anna's Hummingbird", n: count, best_file: rowFile,
    latest_media: { available: true, file: latestFile, url: latestUrl },
    media_images: { pose1: { available: true, url: imageUrl.replace('/g1/', '/' + id + '/') } } };
  index.routes.stats = store({ totals: { detections: count, species: 1 }, today: {}, last_hour: {}, week: {}, started: '2026-10-11' });
  index.routes.lifelist = store({ species: [bird] });
  index.routes.timeseries = { '30': store({ days: 30, daily: [], by_hour: [] }) };
  index.routes.firstseen = { '10': store({ species: [{ sci, total: count }] }) };
  index.routes.recent = {};
  for (const hours of [1, 12, 24, 168, 1000000]) index.routes.recent[String(hours)] = store({ hours, species: [bird] });
  index.routes.species = { [sci]: store({ sci, summary: { total: count, com: bird.com }, latest_media: bird.latest_media,
    media_images: bird.media_images, detections: [{ file: latestFile, d: '2026-10-11', t: '11:03:07', conf: .61,
      media: { available: true, url: latestUrl } }, { file: rowFile, d: '2026-10-10', t: '08:02:19', conf: .97,
      media: { available: true, url: rowUrl } }] }) };
  return { index, objects };
}
function runtime(settings = {}, fullScript = false) {
  const first = generation(), state = { index: first.index, objects: first.objects, failures: new Set(), intercept: null };
  const calls = [], audios = [], timers = [], warnings = [], elements = new Map(), documentListeners = {};
  const element = id => elements.has(id) ? elements.get(id) : (elements.set(id, new Element()), elements.get(id));
  const grid = element('atlasGrid');
  Object.defineProperty(grid, 'innerHTML', { get() { return this._html; }, set(value) {
    this._html = value; this.maps['[data-action="play"]'] = [];
    for (const match of value.matchAll(/<article class="bird-card" data-sci="([^"]*)" data-audio="([^"]*)">/g)) {
      const card = new Element(), button = new Element(), strip = new Element();
      card.dataset = { sci: unescape(match[1]), audio: unescape(match[2]) };
      card.maps['.spectro-wrap'] = strip; card.maps['[data-action="play"]'] = button; button.closest = () => card;
      this.maps['[data-action="play"]'].push(button);
    }
  } });
  const p1 = new Element(), p2 = new Element(); p1.dataset.pose = '1'; p2.dataset.pose = '2';
  element('modalPoseToggle').maps['button'] = [p1, p2]; element('modalPoseToggle').maps['button[data-pose="1"]'] = p1;
  const window = { AVIAN_PUBLIC_STATIC_MODE: true, AVIAN_MEDIA_ORIGIN: base, AVIAN_SNAPSHOT_INDEX_URL: indexUrl,
    __atlasCardPose: '2', ...settings, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };
  const documentRoot = new Element();
  if (settings.testConfigRequired) documentRoot.setAttribute('data-avian-config-required', '');
  const context = vm.createContext({ window,
    location: { origin: 'https://birds.example.org', href: 'https://birds.example.org/', hash: settings.testHash || '' },
    URL, AbortController, TextDecoder, Uint8Array, atob, WebSocket: settings.testWebSocket,
    crypto: webcrypto, console: { warn(...v) { warnings.push(v); } },
    localStorage: { getItem: () => null, setItem() {} }, performance: { now: () => 0 },
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    getComputedStyle: () => ({ getPropertyValue: () => '#efe8d8' }),
    setTimeout(fn, delay) { const timer = { fn, delay }; timers.push(timer); return timer; },
    clearTimeout(timer) { if (timer) timer.cleared = true; }, setInterval: () => 1, clearInterval() {},
    fetch(url, opts) {
      calls.push({ url, opts }); if (state.intercept) { const result = state.intercept(url, opts); if (result) return result; }
      const raw = url === indexUrl ? encode(state.index) : state.objects.get(url), ok = !!raw && !state.failures.has(url);
      return Promise.resolve(response(raw || Buffer.alloc(0), ok));
    },
    document: { getElementById: element, createElement: () => new Element(), contains: () => true,
      querySelector: selector => element(selector), querySelectorAll: () => [],
      addEventListener(name, fn) { (documentListeners[name] ||= []).push(fn); },
      body: new Element(), documentElement: documentRoot, hidden: false },
    DATA: { stats: null, lifelist: null, timeseries: null, firstseen: null, recent: null }, currentHours: 24,
    DIMS: { 'calypte-anna-2': [560, 398], 'calypte-anna': [560, 400] },
    SKETCH_VERSION: 'cache-old', IMG_VERSION: 'cache-old', ICON_PLAY: 'PLAY', ICON_PAUSE: 'PAUSE',
    SPECIES_CACHE: {}, WIKI_CACHE: {}, _decodedCache: {}, atlasGridEl: null,
    slugify: s => s.toLowerCase().replace(/ /g, '-'), fmtNK: String, fmtN: String, windowLabel: () => 'today',
    wikiUrl: () => 'https://en.wikipedia.org/wiki/Calypte_anna', ebirdUrl: () => 'https://ebird.org/',
    audioClaim() {}, audioRelease() {}, playAtlasEntrance() {}, getSpecCtx: () => null, paintSpectrogram() {},
    morphModalOpen() {}, stopModalAudio() {}, syncPill() {}, rarityLabel: () => 'common', fmtRecTime: () => '11:03', fmtDateLine: () => 'today',
    renderTimeIndependent() {}, renderCollageFromData() {}, renderWindowDependent() {}, recomputeDerived() {},
    Audio: class { constructor(url) { this.url = url; this.listeners = {}; audios.push(this); }
      addEventListener(k, f) { this.listeners[k] = f; } pause() {} load() {} removeAttribute() {}
      play() { if (this.listeners.playing) this.listeners.playing(); return Promise.resolve(); } }
  });
  // Full boot exercises automatic requests, not the expensive visualization loops.
  if (fullScript && !settings.testSuccessfulBoot) state.failures.add(indexUrl);
  if (settings.testConfigScript) vm.runInContext(settings.testConfigScript, context);
  vm.runInContext(fullScript ? draft : helpers, context);
  return { context, state, calls, audios, timers, warnings, element, grid, p1, p2, documentListeners };
}
const run = (r, code) => vm.runInContext(code, r.context);
const install = (r, g) => { r.state.index = g.index; for (const [url, raw] of g.objects) r.state.objects.set(url, raw); };
function wireRefresh(r) {
  vm.runInContext(section('  function fetchJson(', '  function backfillDaily('), r.context);
  vm.runInContext(section('  function refreshRecent(', '  // Kick off the initial fetch.'), r.context);
}
function wireModal(r) {
  vm.runInContext(section('  function sketchSrc(', '  function closeDetailModal('), r.context);
}
function makeRow(file, url) {
  const row = new Element(), button = new Element(), strip = new Element(), loading = new Element();
  row.dataset = { file, audio: url }; row.maps['.rec-spectro'] = strip; strip.maps['.rec-spectro-loading'] = loading;
  button.closest = () => row; return { row, button, strip, loading };
}

test('actual complete script startup in static mode requests only the CDN index, with no manifest, menu, config, wiki, audio or Pi', async () => {
  const r = runtime({}, true); await drain();
  assert.deepEqual(r.calls.map(c => c.url), [indexUrl]); assert.equal(r.audios.length, 0);
  assert.equal(r.element('liveAudioBtn').disabled, true);
  assert.equal(r.element('liveStatus').textContent, 'Live audio is unavailable.');
  assert.equal(r.element('snapshotFreshness').textContent, 'Bird data is unavailable.');
  r.element('unlockForm').listeners.submit({ preventDefault() {} }); await drain();
  assert.equal(r.calls.length, 1);
});
test('missing image markup has no empty source and scoped styles keep unavailable art invisible without collapsing card layout', () => {
  const images = [...html.matchAll(/<img\b[^>]*>/g)].map(m => m[0]);
  assert(images.length > 0); assert(images.every(tag => !/\bsrc\s*=\s*(["'])\s*\1/.test(tag)));
  const modal = images.find(tag => /\bid="modalImg"/.test(tag));
  assert(modal && /\bhidden\b/.test(modal)); assert(!/\bsrc\s*=/.test(modal));
  const hiddenRule = css.match(/([^{}]+)\{\s*visibility:\s*hidden\s*;\s*\}/);
  assert(hiddenRule, 'unavailable images have a visibility rule');
  const selectors = hiddenRule[1].replace(/\/\*[\s\S]*?\*\//g, '').split(',').map(s => s.trim());
  assert.deepEqual(selectors, ['.bird-card .img-wrap img[hidden]', '.modal-img img[hidden]', '.gtile img[hidden]']);
  assert(!hiddenRule[0].includes('display:'), 'layout is preserved instead of collapsed');
  const r = runtime(), img = r.element('modalImg'); r.context.img = img; r.context.url = imageUrl;
  run(r, 'setMediaImage(img, url)'); assert.equal(img.src, imageUrl); assert.equal(img.hidden, false);
  run(r, 'setMediaImage(img, "")'); assert.equal(img.getAttribute('src'), undefined); assert.equal(img.src, ''); assert.equal(img.hidden, true);
  assert.equal(run(r, 'mediaImageAttrs("")'), ' hidden');
  assert(!run(r, 'mediaImageAttrs("")').includes('src='));
  assert.equal(r.calls.length, 0); assert.equal(r.audios.length, 0);
});
test('static mode missing/invalid mandatory settings fails closed in the actual script', async () => {
  for (const settings of [{ AVIAN_MEDIA_ORIGIN: '', AVIAN_SNAPSHOT_INDEX_URL: '' },
    { AVIAN_MEDIA_ORIGIN: base + '/wrong-path' }, { AVIAN_SNAPSHOT_INDEX_URL: 'https://other.example/index.json' }]) {
    const r = runtime(settings, true); await drain(); assert.equal(r.calls.length, 0); assert.equal(r.audios.length, 0);
    assert.equal(r.element('snapshotFreshness').textContent, 'Bird data is unavailable.');
  }
});
test('default actual startup retains origin metadata and automatic menu behavior; freshness stays hidden', async () => {
  const r = runtime({ AVIAN_PUBLIC_STATIC_MODE: false, AVIAN_MEDIA_ORIGIN: '', AVIAN_SNAPSHOT_INDEX_URL: '' }, true); await drain();
  assert(r.calls.some(c => c.url === './avian/api/menu.php'));
  assert.equal(r.calls.filter(c => c.url.includes('birdnet-api.php')).length, 5);
  assert.equal(r.element('snapshotFreshness').hidden, true);
});
test('complete verified generation installs only embedded images/latest; failed candidate retains matching media and data', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  assert.equal(r.context.DATA.stats.totals.detections, 1); assert.equal(r.context.MEDIA.generation, 'media-g1');
  assert.equal(run(r, 'birdImageSrc("Calypte anna", 1)'), imageUrl); assert.equal(run(r, 'birdImageSrc("Calypte anna", 2)'), '');
  assert.equal(run(r, 'recordingSrc("' + rowFile + '")'), '');
  const second = generation('g2', 2, 1); install(r, second);
  r.state.failures.add(new URL(second.index.routes.stats, indexUrl).href); await run(r, 'refreshAll(false)');
  assert.equal(r.context.DATA.stats.totals.detections, 1); assert.equal(r.context.MEDIA.generation, 'media-g1');
  assert.equal(run(r, 'birdImageSrc("Calypte anna", 1)'), imageUrl);
  assert.equal(r.context.window.__avianSnapshotStatus.state, 'retained');
  r.state.failures.clear(); await run(r, 'refreshAll(false)');
  assert.equal(r.context.MEDIA.generation, 'media-g2'); assert.equal(run(r, 'birdImageSrc("Calypte anna", 1)'), imageUrl.replace('/g1/', '/g2/'));
  assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});
test('actual atlas uses embedded latest audio, never older best_file/master mapping, and shows published pose 1', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  r.context.currentHours = 1000000;
  vm.runInContext(section('  function sketchSrc(', '  function openDetailModal('), r.context);
  vm.runInContext(section('  function renderAtlas(', '  function renderWindowDependent('), r.context);
  run(r, 'MEDIA.recordings = {}; renderAtlas(false)');
  assert(r.grid.innerHTML.includes(imageUrl)); assert(r.grid.innerHTML.includes(latestUrl.replace(/'/g, '&#39;')));
  assert(!r.grid.innerHTML.includes(rowUrl)); assert(!r.grid.innerHTML.includes('/avian/api/'));
  r.grid.maps['[data-action="play"]'][0].listeners.click(); assert.equal(r.audios[0].url, latestUrl);
  run(r, 'DATA.lifelist.species[0].latest_media = {available:false,url:"' + latestUrl + '"}; renderAtlas(false)');
  assert(r.grid.innerHTML.includes('disabled aria-label="recording unavailable"'));
  assert(r.grid.innerHTML.includes('<span>unavailable</span>'));
  r.grid.maps['[data-action="play"]'][0].listeners.click(); assert.equal(r.audios.length, 1);
});
test('static modal marks unpublished rows unavailable upfront without play buttons or pending waveforms; published row stays playable', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  r.context.SPECIES_CACHE[sci] = { snapshot: { generation: 'g1' }, summary: {}, detections: [
    { file: latestFile, media: { available: true, url: latestUrl } },
    { file: 'unpublished.mp3', media: { available: false, url: latestUrl } },
    { file: 'no-entry.mp3' },
    { file: 'wrong-origin.mp3', media: { available: true, url: 'https://other.example/clip.mp3' } }
  ] };
  run(r, 'openDetailModal("Calypte anna")'); await settle();
  const rows = [...r.element('modalRecordings').innerHTML.matchAll(/<li class="rec-row"[\s\S]*?<\/li>/g)].map(m => m[0]);
  assert.equal(rows.length, 4); assert(rows[0].includes('<button class="play"'));
  assert(rows[0].includes('loading spectrogram...')); assert(!rows[0].includes('Recording unavailable'));
  for (const row of rows.slice(1)) {
    assert(row.includes('Recording unavailable'));
    assert(!row.includes('<button')); assert(!row.includes('rec-spectro')); assert(!row.includes('loading spectrogram'));
    assert(row.includes('data-audio=""'));
  }
  assert.equal(r.audios.length, 0); assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});
test('actual modal renders escaped embedded row URLs, uses cached wiki, and does no HEAD/global manifest/Pi requests', async () => {
  const r = runtime({ AVIAN_WIKI_INDEX_URL: base + '/descriptions.json' }); wireRefresh(r); await run(r, 'refreshAll(false)');
  r.state.objects.set(base + '/descriptions.json', encode({ schema_version: 1, species: { [sci]: { extract: 'Cached hummingbird description.' } } }));
  wireModal(r); run(r, 'openDetailModal("Calypte anna")'); await settle();
  const list = r.element('modalRecordings').innerHTML;
  assert(list.includes('Anna&#39;s_Hummingbird'), list); assert(list.includes(rowFile));
  const urls = [...list.matchAll(/data-audio="([^"]*)"/g)].map(m => unescape(m[1]));
  assert.deepEqual(urls, [latestUrl, rowUrl]); assert.equal(r.element('modalDesc').textContent, 'Cached hummingbird description.');
  assert.equal(r.element('modalImg').src, imageUrl); assert.equal(r.p2.getAttribute('data-unavailable'), 'true');
  assert(r.calls.every(c => c.url.startsWith(base + '/'))); assert(!r.calls.some(c => c.opts?.method === 'HEAD'));
});
test('actual modal row playback and waveform use validated embedded URLs without global recording entries', async () => {
  const r = runtime(); r.context.getSpecCtx = () => ({ decodeAudioData: () => Promise.resolve({ decoded: true }) });
  r.state.objects.set(rowUrl, Buffer.from('audio')); r.state.objects.set(latestUrl, Buffer.from('audio'));
  vm.runInContext(section('  function ensureSpectroImage(', '  // Per-recording row interactions'), r.context);
  Object.assign(r.context, { modalAudio: null, modalRecBtn: null, stopModalAudio() {}, pauseModalAudio() {}, startCursorLoop() {}, stopCursorLoop() {} });
  vm.runInContext(section("  document.getElementById('modalRecordings').addEventListener('click'", '  // Scrub by clicking / dragging'), r.context);
  const row = makeRow(rowFile, rowUrl), latest = makeRow(latestFile, latestUrl);
  for (const item of [row, latest]) {
    r.element('modalRecordings').listeners.click({ target: { closest: s => s === '.play' ? item.button : null } }); await drain();
  }
  assert.deepEqual(r.audios.map(a => a.url), [rowUrl, latestUrl]);
  assert.deepEqual(r.calls.map(c => c.url), [rowUrl, latestUrl]);
  assert.equal(Object.keys(r.context.MEDIA.recordings).length, 0);
  assert(r.context._decodedCache[rowUrl]); assert(r.calls.every(c => c.opts.redirect === 'error' && c.opts.credentials === 'omit'));
  for (const url of ['', 'https://other.example/clip.mp3', './avian/api/recording.php']) {
    const absent = makeRow('missing.mp3', url); r.context.row = absent.row; run(r, 'ensureSpectroImage(row)');
    r.element('modalRecordings').listeners.click({ target: { closest: s => s === '.play' ? absent.button : null } });
    assert.equal(absent.loading.textContent, 'recording unavailable'); assert.equal(absent.button.getAttribute('aria-label'), 'recording unavailable');
  }
  assert.equal(r.audios.length, 2); assert.equal(r.calls.length, 2);
});
test('freshness exposes captured snapshot time, delayed retained/stale data, unavailable state, and hides in origin mode', () => {
  const r = runtime();
  r.context.snapshotCurrentIndex = { timezone: 'America/New_York' };
  r.context.window.__avianSnapshotStatus = { state: 'fresh', as_of: new Date().toISOString() }; run(r, 'renderSnapshotFreshness()');
  assert.match(r.element('snapshotFreshness').textContent, /^Updated /); assert.equal(r.element('snapshotFreshness').hidden, false);
  r.context.window.__avianSnapshotStatus.state = 'retained'; run(r, 'renderSnapshotFreshness()');
  assert.match(r.element('snapshotFreshness').textContent, /^Updates delayed · data from /);
  r.context.window.__avianSnapshotStatus = { state: 'fresh', as_of: '2020-01-01T00:00:00Z' }; run(r, 'renderSnapshotFreshness()');
  assert.equal(r.element('snapshotFreshness').getAttribute('data-state'), 'stale');
  r.context.window.__avianSnapshotStatus = { state: 'unavailable', as_of: null }; run(r, 'renderSnapshotFreshness()');
  assert.equal(r.element('snapshotFreshness').textContent, 'Bird data is unavailable.');
  assert(html.includes('role="status" aria-live="polite" hidden')); assert(html.includes('./avian/frontend/public-static.css'));
});
test('cached wiki is bounded/lazy/same CDN and missing or invalid entries make no Pi request', async () => {
  const r = runtime({ AVIAN_WIKI_INDEX_URL: base + '/descriptions.json' });
  r.state.objects.set(base + '/descriptions.json', encode({ schema_version: 1, species: { [sci]: { extract: 'cached' } } }));
  assert.equal(r.calls.length, 0);
  const [a, b] = await Promise.all([run(r, 'loadPublicWiki("Calypte anna")'), run(r, 'loadPublicWiki("Calypte anna")')]);
  assert.equal(a.extract, 'cached'); assert.equal(b.extract, 'cached'); assert.equal(r.calls.length, 1);
  assert.equal((await run(r, 'loadPublicWiki("Unknown bird")')).extract, ''); assert.equal(r.calls.length, 1);
  for (const setting of ['', 'http://example.test/wiki.json', 'https://other.example/wiki.json']) {
    r.context.window.AVIAN_WIKI_INDEX_URL = setting; assert.equal((await run(r, 'loadPublicWiki("Calypte anna")')).extract, '');
  }
  assert.equal(r.calls.length, 1); assert(r.calls[0].opts.credentials === 'omit');
});
test('static admin routes and legacy configuration functions only expose explicit HTTPS private link', async () => {
  for (const section of ['system', 'logs', 'tools', 'settings']) {
    const boot = runtime({ testHash: '#admin=' + section }, true); await drain();
    assert.equal(boot.calls.length, 1); assert(boot.element('adminBody').innerHTML.includes('Administration is unavailable here.'));
    assert(!boot.element('adminBody').innerHTML.includes('restart'));
  }
  const r = runtime({ AVIAN_PRIVATE_ADMIN_URL: 'https://private.example/#admin=system', testHash: '#admin=system' }, true); await drain();
  const isolated = runtime({ AVIAN_PRIVATE_ADMIN_URL: 'https://private.example/#admin=system' });
  Object.assign(isolated.context, { adminEl: isolated.element('admin'), adminTitle: isolated.element('adminTitle'), adminBody: isolated.element('adminBody') });
  run(isolated, 'renderPublicAdministration()');
  assert(isolated.element('adminBody').innerHTML.includes('https://private.example/#admin=system'));
  assert(!isolated.element('adminBody').innerHTML.includes('restart')); assert.equal(isolated.calls.length, 0);
  assert.equal(run(isolated, 'publicHttpsUrl("https://user:token@private.example/")'), '');
  assert.equal(run(isolated, 'publicHttpsUrl("http://private.example/")'), '');
  vm.runInContext(section('  function loadSettings(', '  function settingsToggle('), isolated.context);
  vm.runInContext(section('  function saveSettings(', '  // ---- Hash routing'), isolated.context);
  vm.runInContext(section('  function adminApi(', '  function openAdmin('), isolated.context);
  run(isolated, 'loadSettings(); saveSettings()');
  await assert.rejects(run(isolated, 'adminApi("./avian/api/birdnet-status.php?action=restart")'), /private administration only/);
  assert.equal(isolated.calls.length, 0);
  assert.equal(r.calls.length, 1); assert(r.element('dd-items').innerHTML.includes('https://private.example/#admin=system'));
});
test('configured live audio uses explicit user click; unset remains unavailable and default stream stays intact', async () => {
  const url = 'https://stream.example/live';
  const r = runtime({ AVIAN_LIVE_STREAM_URL: url }, true); await drain(); assert.equal(r.audios.length, 0);
  r.element('liveAudioBtn').listeners.click({ stopPropagation() {} }); await drain();
  assert.equal(r.audios[0].url, url); assert.equal(r.audios[0].crossOrigin, 'anonymous');
  assert.equal(r.element('liveStatus').textContent, 'live audio');
  const origin = runtime({ AVIAN_PUBLIC_STATIC_MODE: false, AVIAN_SNAPSHOT_INDEX_URL: '' });
  assert.match(run(origin, 'liveStreamSrc()'), /^\/stream\?t=\d+$/);
});
test('snapshot chart calendar follows captured snapshot date across historical dates and UTC midnight', () => {
  const r = runtime();
  vm.runInContext(section('  function backfillDaily(', '  function recomputeDerived('), r.context);
  r.context.DATA.timeseries = { snapshot: { as_of: '2020-12-31T23:59:00-08:00' } };
  const values = run(r, 'backfillDaily([{date:"2020-12-30",detections:4,species:1},{date:"2020-12-31",detections:7,species:2},{date:"2021-01-01",detections:99,species:9}], 2)');
  assert.deepEqual(Array.from(values, v => v.detections), [4, 7]);
});
test('late detail from replaced generation cannot replace recordings or mutate adopted media', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  let complete;
  const detailUrl = new URL(r.state.index.routes.species[sci], indexUrl).href;
  r.state.intercept = url => url === detailUrl ? new Promise(resolve => { complete = resolve; }) : null;
  run(r, 'openDetailModal("Calypte anna")'); await drain();
  install(r, generation('g2', 2, 1)); await run(r, 'refreshAll(false)');
  const raw = r.state.objects.get(detailUrl); complete(response(raw)); await settle();
  assert.equal(r.context.MEDIA.generation, 'media-g2'); assert.equal(r.context.SPECIES_CACHE[sci], undefined);
  assert(r.element('modalRecordings').innerHTML.includes('Bird data updated. Reopen this bird'), r.element('modalRecordings').innerHTML);
});

const fullRequests = [ { action: 'stats' }, { action: 'lifelist' }, { action: 'timeseries', days: 30 },
  { action: 'firstseen', limit: 10 }, { action: 'recent', hours: 24 } ];
function snapshotRuntime() { const r = runtime(); r.context.requests = fullRequests; return r; }

test('one captured index hydrates a complete batch; unchanged immutable payloads are reused', async () => {
  const r = snapshotRuntime(), parts = await run(r, 'loadSnapshots(requests)');
  assert.equal(parts.length, 5); assert(parts.every(part => part.snapshot.generation === 'g1'));
  assert(r.calls.every(call => call.opts.redirect === 'error' && call.opts.credentials === 'omit'));
  const before = r.calls.filter(call => call.url !== indexUrl).length;
  // Image paths are generation-independent for this immutable-reuse fixture.
  const next = JSON.parse(JSON.stringify(r.state.index)); next.generation = 'g2';
  next.published_at = '2026-10-11T15:01:00Z'; next.as_of = '2026-10-11T11:01:00-04:00';
  r.state.index = next;
  const updated = await run(r, 'loadSnapshots(requests)');
  assert.equal(updated[0].snapshot.generation, 'g2');
  assert.equal(r.calls.filter(call => call.url !== indexUrl).length, before);
});
test('first partial generation failure adopts no data; corrupt candidate later retains complete old data', async () => {
  const r = snapshotRuntime(), url = new URL(r.state.index.routes.stats, indexUrl).href;
  r.state.failures.add(url); await assert.rejects(run(r, 'loadSnapshots(requests)'), /snapshot HTTP/);
  assert.equal(r.context.snapshotCurrentIndex, null);
  r.state.failures.clear(); await run(r, 'loadSnapshots(requests)');
  const next = generation('g2', 2, 1); install(r, next);
  const target = new URL(next.index.routes.stats, indexUrl).href;
  const good = r.state.objects.get(target), corrupt = Buffer.from(good); corrupt[1] ^= 1;
  r.state.objects.set(target, corrupt);
  const retained = await run(r, 'loadSnapshots(requests)');
  assert(retained.every(part => part.snapshot.generation === 'g1'));
  assert.match(r.context.window.__avianSnapshotStatus.error, /checksum mismatch/);
  r.state.objects.set(target, good); const recovered = await run(r, 'loadSnapshots(requests)');
  assert(recovered.every(part => part.snapshot.generation === 'g2'));
});
test('same-generation index mutation and older pointer cannot replace an adopted generation', async () => {
  const r = snapshotRuntime(); await run(r, 'loadSnapshots(requests)');
  const original = r.state.index, changed = JSON.parse(JSON.stringify(original));
  changed.objects[changed.routes.stats].bytes += 1; r.state.index = changed;
  await run(r, 'loadSnapshots(requests)'); assert.match(r.context.window.__avianSnapshotStatus.error, /generation changed/);
  install(r, generation('g2', 2, 1)); await run(r, 'loadSnapshots(requests)');
  r.state.index = original; const retained = await run(r, 'loadSnapshots(requests)');
  assert(retained.every(part => part.snapshot.generation === 'g2'));
});
test('unsupported and duplicate parameters reject before any private request; unpublished windows do not forward', async () => {
  const r = snapshotRuntime(); wireRefresh(r); await run(r, 'loadSnapshots(requests)');
  const before = r.calls.length;
  await assert.rejects(run(r, "fetchJson('./avian/api/birdnet-api.php?action=restart')"), /unsupported/);
  await assert.rejects(run(r, "fetchJson('./avian/api/birdnet-api.php?action=stats&action=stats')"), /duplicate/);
  await assert.rejects(run(r, "fetchJson('./avian/api/config.php')"), /private API/);
  assert.equal(r.calls.length, before);
  r.context.requests = [{ action: 'recent', hours: 2 }];
  await assert.rejects(run(r, 'loadSnapshots(requests)'), /route not published/);
  assert.equal(r.calls.at(-1).url, indexUrl);
});
test('species details pin displayed generation and invalidate cache when a complete update is adopted', async () => {
  const r = snapshotRuntime(); await run(r, 'loadSnapshots(requests)');
  r.context.SPECIES_CACHE[sci] = { old: true }; install(r, generation('g2', 2, 1));
  r.context.detailRequest = [{ action: 'species', sci }];
  const old = await run(r, 'loadSnapshots(detailRequest, {pinCurrent:true})');
  assert.equal(old[0].snapshot.generation, 'g1');
  await run(r, 'loadSnapshots(requests)'); assert.equal(Object.keys(r.context.SPECIES_CACHE).length, 0);
});
test('bounded streaming rejects declared and chunked oversized bodies without full-buffer reads', async () => {
  const r = snapshotRuntime(); let reads = 0, cancelled = false;
  r.state.intercept = () => Promise.resolve({ ok: true, headers: { get: () => '1000' },
    body: { getReader() { throw new Error('must reject declared length first'); } },
    arrayBuffer() { throw new Error('unbounded read forbidden'); } });
  await assert.rejects(run(r, 'snapshotFetchBytes("https://cdn.example.net/huge", 100, "no-cache")'), /too large/);
  r.state.intercept = () => Promise.resolve({ ok: true, headers: { get: () => null },
    body: { getReader() { return { read() { reads++; return Promise.resolve({ done: false, value: new Uint8Array(60) }); },
      cancel() { cancelled = true; return Promise.resolve(); } }; } },
    arrayBuffer() { throw new Error('unbounded read forbidden'); } });
  await assert.rejects(run(r, 'snapshotFetchBytes("https://cdn.example.net/chunked", 100, "no-cache")'), /too large/);
  assert.equal(reads, 2); assert.equal(cancelled, true);
});
test('pointer deadline retains previous complete generation and late completion does not change it', async () => {
  const r = snapshotRuntime(); await run(r, 'loadSnapshots(requests)'); let complete;
  r.state.intercept = url => url === indexUrl ? new Promise(resolve => { complete = resolve; }) : null;
  const pending = run(r, 'loadSnapshots(requests)'); r.timers.find(timer => !timer.cleared).fn();
  const retained = await pending; assert(retained.every(part => part.snapshot.generation === 'g1'));
  complete(response(encode(generation('g2', 2, 1).index))); await settle();
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g1');
});
test('CSP-safe HTML config requirement precedes the tracked config and application; attribution is retained', () => {
  const required = html.indexOf('<html lang="en" data-avian-config-required>');
  const config = html.indexOf('src="./avian/frontend/public-config.js"');
  const app = html.indexOf('src="./apt.js"');
  assert(required >= 0 && config > required && app > config);
  assert(html.includes('https://theodore.net')); assert(html.includes('https://github.com/Twarner491/AvianVisitors'));
});
test('absent globals use legacy startup while partial inferred configuration fails closed', async () => {
  const legacy = runtime({ AVIAN_PUBLIC_STATIC_MODE: undefined, AVIAN_MEDIA_ORIGIN: undefined, AVIAN_SNAPSHOT_INDEX_URL: undefined }, true);
  await settle(); assert.equal(legacy.calls.filter(call => call.url.includes('birdnet-api.php')).length, 5);
  assert(legacy.calls.some(call => call.url.includes('menu.php')));
  const partial = runtime({ AVIAN_PUBLIC_STATIC_MODE: undefined, AVIAN_SNAPSHOT_INDEX_URL: '' }, true);
  await settle(); assert.equal(partial.calls.length, 0); assert.equal(partial.audios.length, 0);
});

test('required HTML config missing or corrupt fails closed even when inline scripts never ran', async () => {
  for (const flag of [undefined, 'false', null]) {
    const r = runtime({ testConfigRequired: true, AVIAN_PUBLIC_STATIC_MODE: flag,
      AVIAN_MEDIA_ORIGIN: undefined, AVIAN_SNAPSHOT_INDEX_URL: undefined }, true);
    await settle(); assert.equal(r.calls.length, 0); assert.equal(r.audios.length, 0);
    assert.equal(r.element('snapshotFreshness').textContent, 'Bird data is unavailable.');
  }
  const loadedDefault = runtime({ testConfigRequired: true, AVIAN_PUBLIC_STATIC_MODE: false,
    AVIAN_MEDIA_ORIGIN: '', AVIAN_SNAPSHOT_INDEX_URL: '' }, true);
  await settle(); assert.equal(loadedDefault.calls.filter(call => call.url.includes('birdnet-api.php')).length, 5);
});

test('actual successful full application startup renders CDN cards and performs no private bootstrap requests', async () => {
  const r = runtime({ testSuccessfulBoot: true }, true); await settle();
  assert(r.calls.length > 1); assert(r.calls.every(call => call.url.startsWith(base + '/data/')));
  assert(r.grid.innerHTML.includes(latestUrl.replace(/'/g, '&#39;')));
  assert(r.grid.innerHTML.includes(imageUrl)); assert.equal(r.audios.length, 0);
  assert.equal(r.context.window.__avianSnapshotStatus.state, 'fresh');
  assert(!r.warnings.some(warning => warning.some(value => value && value.name === 'TypeError')), 'mocked startup has no unexpected type errors');
});
test('concurrent older candidate cannot overwrite a later complete generation', async () => {
  const r = snapshotRuntime(); await run(r, 'loadSnapshots(requests)');
  const next = generation('g2', 2, 1); install(r, next);
  const slowUrl = new URL(next.index.routes.lifelist, indexUrl).href; let complete;
  r.state.intercept = url => url === slowUrl ? new Promise(resolve => { complete = resolve; }) : null;
  const old = run(r, 'loadSnapshots(requests)'); await drain();
  install(r, generation('g3', 3, 2)); await run(r, 'loadSnapshots(requests)');
  complete(response(r.state.objects.get(slowUrl))); await assert.rejects(old, /obsolete snapshot refresh/);
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g3');
});

test('hash-valid malformed action payload is rejected before index, data or media adoption', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  const prior = r.context.snapshotCurrentIndex, priorData = r.context.DATA.stats, priorMedia = r.context.MEDIA;
  for (const [action, malformed] of [
    ['lifelist', { species: 'invalid' }], ['recent', { hours: 12, species: [] }],
    ['timeseries', { days: 30, daily: [], by_hour: 'invalid' }],
    ['firstseen', { species: [null] }], ['stats', { totals: null, today: {}, last_hour: {}, week: {} }]
  ]) {
    const next = generation('g2', 2, 1), raw = encode(malformed), hash = createHash('sha256').update(raw).digest('hex');
    const route = 'objects/' + hash + '.json'; next.index.objects[route] = { sha256: hash, bytes: raw.length };
    next.objects.set(new URL(route, indexUrl).href, raw);
    if (action === 'timeseries') next.index.routes.timeseries['30'] = route;
    else if (action === 'recent') next.index.routes.recent['24'] = route;
    else if (action === 'firstseen') next.index.routes.firstseen['10'] = route;
    else next.index.routes[action] = route;
    install(r, next); await run(r, 'refreshAll(false)');
    assert.equal(r.context.snapshotCurrentIndex, prior); assert.equal(r.context.DATA.stats.snapshot.generation, 'g1');
    assert.deepEqual(r.context.DATA.stats.totals, priorData.totals);
    assert.equal(r.context.MEDIA.generation, priorMedia.generation); assert.equal(r.context.MEDIA.images[sci].pose1.url, imageUrl);
    assert.match(r.context.window.__avianSnapshotStatus.error, /invalid snapshot payload/);
  }
});
test('detail species identity must match the requested route before rendering', async () => {
  const r = snapshotRuntime(), wrong = { sci: 'Corvus brachyrhynchos', summary: {}, detections: [] };
  const raw = encode(wrong), hash = createHash('sha256').update(raw).digest('hex'), route = 'objects/' + hash + '.json';
  r.state.index.routes.species[sci] = route; r.state.index.objects[route] = { sha256: hash, bytes: raw.length };
  r.state.objects.set(new URL(route, indexUrl).href, raw); await run(r, 'loadSnapshots(requests)');
  r.context.detailRequest = [{ action: 'species', sci }];
  await assert.rejects(run(r, 'loadSnapshots(detailRequest, {pinCurrent:true})'), /invalid snapshot payload/);
});

test('actual tracked default config loads under the required marker and preserves local behavior', async () => {
  const config = fs.readFileSync(new URL('../frontend/public-config.js', import.meta.url), 'utf8');
  const r = runtime({ testConfigRequired: true, testConfigScript: config }, true); await settle();
  assert.equal(r.context.window.AVIAN_PUBLIC_STATIC_MODE, false);
  assert.equal(r.calls.filter(call => call.url.includes('birdnet-api.php')).length, 5);
  assert(r.calls.some(call => call.url.includes('menu.php')));
  assert.equal(r.element('snapshotFreshness').hidden, true);
});

test('retired pinned modal metadata refreshes one coherent page and detail batch from the current CDN index', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href;
  install(r, generation('g2', 2, 1)); r.state.objects.delete(oldDetail);
  r.state.intercept = url => url === oldDetail ? Promise.resolve(httpError(404)) : null;
  const before = r.calls.length;
  run(r, 'openDetailModal("Calypte anna")'); await settle();
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g2');
  for (const part of Object.values(r.context.DATA)) assert.equal(part.snapshot.generation, 'g2');
  assert.equal(r.context.MEDIA.generation, 'media-g2');
  assert.equal(r.context.SPECIES_CACHE[sci].snapshot.generation, 'g2');
  assert.equal(r.element('modalAllTime').textContent, '2'); assert.equal(r.element('modalWindow').textContent, '2');
  assert.equal(r.element('modalImg').src, imageUrl.replace('/g1/', '/g2/'));
  assert(r.element('modalRecordings').innerHTML.includes('data-audio="' + rowUrl + '"'));
  assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 1);
  assert.equal(r.calls.slice(before).filter(c => c.url === oldDetail).length, 1);
  assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('still-missing current metadata rejects after one 410 recovery without a loop or Pi fallback', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  const missing = new URL(r.state.index.routes.species[sci], indexUrl).href, prior = r.context.DATA.stats;
  r.context.detailRequest = [{ action: 'species', sci }];
  r.state.intercept = url => url === missing ? Promise.resolve(httpError(410)) : null;
  const before = r.calls.length;
  await assert.rejects(run(r, 'loadSnapshots(detailRequest, {pinCurrent:true})'), error => error.status === 410 && error.retiredMetadata);
  assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 1);
  assert.equal(r.calls.slice(before).filter(c => c.url === missing).length, 2);
  assert.equal(r.context.DATA.stats, prior); assert.equal(r.context.snapshotCurrentIndex.generation, 'g1');
  assert.equal(r.context.MEDIA.generation, 'media-g1'); assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('a newly fetched pointer with a retired object gets one current-index retry before complete adoption', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  const next = generation('g2', 2, 1), current = generation('g3', 3, 2); install(r, next);
  const retired = new URL(next.index.routes.stats, indexUrl).href;
  r.state.intercept = url => {
    if (url !== retired) return null;
    install(r, current); return Promise.resolve(httpError(404));
  };
  const before = r.calls.length; await run(r, 'refreshAll(false)');
  assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 2);
  assert.equal(r.calls.slice(before).filter(c => c.url === retired).length, 1);
  assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g3'));
  assert.equal(r.context.MEDIA.generation, 'media-g3'); assert.equal(r.context.snapshotCurrentIndex.generation, 'g3');
  assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('obsolete accepted-request token rejects a retired response before refreshing the index', async () => {
  const r = snapshotRuntime(); await run(r, 'loadSnapshots(requests)');
  const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href; let complete;
  r.context.detailRequest = [{ action: 'species', sci }]; r.context.acceptDetail = true;
  r.state.intercept = url => url === oldDetail ? new Promise(resolve => { complete = resolve; }) : null;
  const before = r.calls.length;
  const pending = run(r, 'loadSnapshots(detailRequest, {pinCurrent:true,accept:function(){return acceptDetail;}})');
  await drain(); r.context.acceptDetail = false;
  const rejected = assert.rejects(pending, error => error.obsolete === true);
  complete(httpError(404)); await rejected;
  assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 0);
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g1');
});

test('closing the actual modal during retired-data recovery prevents page, media and detail adoption', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  vm.runInContext(section('  function closeDetailModal(', '  // Shared-element morph:'), r.context);
  Object.assign(r.context, { stopModalAudio() {}, morphModalClose(_card, _source, done) { done(); } });
  const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href;
  const next = generation('g2', 2, 1); install(r, next);
  const freshDetail = new URL(next.index.routes.species[sci], indexUrl).href; let complete;
  r.state.intercept = url => url === oldDetail ? Promise.resolve(httpError(404)) :
    url === freshDetail ? new Promise(resolve => { complete = resolve; }) : null;
  run(r, 'openDetailModal("Calypte anna")'); await settle(); assert.equal(typeof complete, 'function');
  run(r, 'closeDetailModal()'); complete(response(r.state.objects.get(freshDetail))); await settle();
  assert.equal(r.element('detail-modal').getAttribute('aria-hidden'), 'true');
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g1'); assert.equal(r.context.MEDIA.generation, 'media-g1');
  assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g1'));
  assert.equal(r.context.SPECIES_CACHE[sci], undefined);
});

test('reopening the same bird supersedes its old modal token and permits only the current coherent recovery', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href; let complete;
  install(r, generation('g2', 2, 1));
  r.state.intercept = url => url === oldDetail ? new Promise(resolve => { complete = resolve; }) : null;
  const before = r.calls.length;
  run(r, 'openDetailModal("Calypte anna")'); await drain();
  run(r, 'openDetailModal("Calypte anna")'); complete(httpError(404)); await settle();
  assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 1);
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g2'); assert.equal(r.context.SPECIES_CACHE[sci].snapshot.generation, 'g2');
  assert.equal(r.element('modalAllTime').textContent, '2');
  assert(!r.element('modalRecordings').innerHTML.includes('Failed to load'));
});

test('retired recovery still rejects checksum or species-shape failures without adopting or falling back to Pi', async () => {
  for (const badKind of ['checksum', 'shape']) {
    const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
    const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href;
    const next = generation('g2', 2, 1);
    if (badKind === 'shape') {
      const raw = encode({ sci: 'Wrong bird', summary: {}, detections: [] }), hash = createHash('sha256').update(raw).digest('hex');
      const route = 'objects/' + hash + '.json'; next.index.routes.species[sci] = route;
      next.index.objects[route] = { sha256: hash, bytes: raw.length }; next.objects.set(new URL(route, indexUrl).href, raw);
    } else {
      const target = new URL(next.index.routes.species[sci], indexUrl).href, raw = Buffer.from(next.objects.get(target));
      raw[1] ^= 1; next.objects.set(target, raw);
    }
    install(r, next); r.context.detailRequest = [{ action: 'species', sci }];
    r.state.intercept = url => url === oldDetail ? Promise.resolve(httpError(404)) : null;
    const before = r.calls.length;
    await assert.rejects(run(r, 'loadSnapshots(detailRequest, {pinCurrent:true})'), /checksum mismatch|invalid snapshot payload/);
    assert.equal(r.calls.slice(before).filter(c => c.url === indexUrl).length, 1);
    assert.equal(r.context.snapshotCurrentIndex.generation, 'g1'); assert.equal(r.context.MEDIA.generation, 'media-g1');
    assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g1'));
    assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
  }
});

test('transient current-index failure during retired recovery retains complete cached old page parts', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  const next = generation('g2', 2, 1); install(r, next);
  const retired = new URL(next.index.routes.stats, indexUrl).href; let pointers = 0;
  r.state.intercept = url => url === retired ? Promise.resolve(httpError(404)) :
    url === indexUrl && ++pointers === 2 ? Promise.resolve(httpError(503)) : null;
  await run(r, 'refreshAll(false)');
  assert.equal(pointers, 2); assert.equal(r.context.snapshotCurrentIndex.generation, 'g1');
  assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g1'));
  assert.equal(r.context.MEDIA.generation, 'media-g1'); assert.equal(r.context.window.__avianSnapshotStatus.state, 'retained');
  assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('retired retry cannot overwrite a later adopted generation', async () => {
  const r = runtime(); wireRefresh(r); await run(r, 'refreshAll(false)');
  const oldDetail = new URL(r.state.index.routes.species[sci], indexUrl).href;
  const next = generation('g2', 2, 1); install(r, next);
  const freshDetail = new URL(next.index.routes.species[sci], indexUrl).href; let complete;
  r.context.detailRequest = [{ action: 'species', sci }];
  r.state.intercept = url => url === oldDetail ? Promise.resolve(httpError(404)) :
    url === freshDetail ? new Promise(resolve => { complete = resolve; }) : null;
  const pending = run(r, 'loadSnapshots(detailRequest, {pinCurrent:true})'); await settle();
  assert.equal(typeof complete, 'function'); install(r, generation('g3', 3, 2)); await run(r, 'refreshAll(false)');
  const rejected = assert.rejects(pending, error => error.obsolete === true);
  complete(response(r.state.objects.get(freshDetail))); await rejected;
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g3'); assert.equal(r.context.MEDIA.generation, 'media-g3');
  assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g3'));
});

// Synthetic notification transport: no Worker, credential or network required.
const updatesUrl = 'wss://updates.example.org/live';
function fakeSockets() {
  const sockets = [];
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.closes = 0; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    message(value) { this.onmessage?.({ data: typeof value === 'string' ? value : JSON.stringify(value) }); }
    close() { this.closes++; this.readyState = 3; this.onclose?.(); }
    fail() { this.onerror?.(); }
  }
  return { Socket, sockets };
}
function wireUpdates(r) {
  vm.runInContext(section('  // Optional update signals.', '  // ---- Menu dropdown ----'), r.context);
}
function timer(r, delay) {
  const selected = r.timers.find(t => !t.cleared && !t.fired && t.delay === delay);
  assert(selected, 'expected scheduled timer ' + delay); return selected;
}
function fire(t) { assert(!t.cleared && !t.fired); t.fired = true; t.fn(); }
const hint = (generation = 'g2', as_of = '2026-10-11T15:01:00Z') => ({ type: 'updated', generation, as_of });
function visibility(r, hidden) {
  r.context.document.hidden = hidden;
  for (const fn of r.documentListeners.visibilitychange || []) fn();
}

test('optional updates make no socket without a URL, in local mode, with invalid settings or without browser support', async () => {
  for (const settings of [{}, { AVIAN_UPDATES_URL: updatesUrl, AVIAN_PUBLIC_STATIC_MODE: false },
    { AVIAN_UPDATES_URL: 'ws://updates.example.org/live' }, { AVIAN_UPDATES_URL: 'wss://user:secret@updates.example.org/live' },
    { AVIAN_UPDATES_URL: updatesUrl + '?token=secret' }, { AVIAN_UPDATES_URL: updatesUrl + '#' },
    { AVIAN_UPDATES_URL: ' wss://updates.example.org/live' },
    { AVIAN_UPDATES_URL: updatesUrl, AVIAN_MEDIA_ORIGIN: '' }]) {
    const { Socket, sockets } = fakeSockets(); const r = runtime({ testWebSocket: Socket, ...settings });
    wireUpdates(r); assert.equal(sockets.length, 0); assert.equal(r.calls.length, 0);
  }
  const r = runtime({ AVIAN_UPDATES_URL: updatesUrl }); wireUpdates(r); assert.equal(r.context.updatesSocket, null);
  const { Socket, sockets } = fakeSockets(); const full = runtime({ testWebSocket: Socket }, true);
  await settle(); assert.equal(sockets.length, 0); assert.deepEqual(full.calls.map(c => c.url), [indexUrl]);
});

test('update bursts coalesce into one two-second CDN refresh and never adopt a hinted generation', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'updatesRefreshCount=0; refreshAll=function(){updatesRefreshCount++;return Promise.resolve();}'); wireUpdates(r);
  assert.equal(sockets[0].url, updatesUrl); sockets[0].open();
  for (let n = 0; n < 20; n++) sockets[0].message(hint('generation-' + n, '2026-10-11T15:01:00Z'));
  assert.equal(r.timers.filter(t => t.delay === 2000 && !t.cleared && !t.fired).length, 1);
  fire(timer(r, 2000)); await settle(); assert.equal(r.context.updatesRefreshCount, 1);
  assert.equal(r.context.snapshotCurrentIndex, null); assert.equal(r.calls.length, 0);
});

test('stale, unchanged, oversized, malformed and non-aware update hints are ignored', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  r.context.snapshotCurrentIndex = generation().index;
  run(r, 'updatesRefreshCount=0; refreshAll=function(){updatesRefreshCount++;return Promise.resolve();}'); wireUpdates(r);
  sockets[0].open(); fire(timer(r, 2000)); await settle();
  for (const value of [hint('g1'), hint('older', '2020-01-01T00:00:00Z'), hint('same-date', '2026-10-11T15:00:00Z'),
    hint('../unsafe'), hint('g2', '2026-10-11T15:01:00'), hint('g2', 'invalid'),
    { ...hint(), url: 'https://untrusted.example/object' }, { type: 'unrelated' }, 'not-json', ' '.repeat(1025),
    { ...hint(), generation: 'é' }, { ...hint(), as_of: [] }]) sockets[0].message(value);
  sockets[0].onmessage({ data: new Uint8Array(100) });
  assert.equal(r.context.updatesLastHint, null); assert.equal(r.context.updatesRefreshCount, 1);
  assert(!r.timers.some(t => t.delay === 2000 && !t.cleared && !t.fired));
});

test('update hint generations match the publisher format including leading underscore and hyphen', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'updatesRefreshCount=0;refreshAll=function(){updatesRefreshCount++;return Promise.resolve();}'); wireUpdates(r);
  sockets[0].open(); fire(timer(r, 2000)); await settle();
  for (const value of ['', 'with.dot', 'a'.repeat(81), '../unsafe']) sockets[0].message(hint(value));
  assert.equal(r.context.updatesLastHint, null); assert.equal(r.context.updatesRefreshCount, 1);
  for (const value of ['_generation', '-generation', 'a'.repeat(80)]) {
    sockets[0].message(hint(value)); assert.equal(r.context.updatesLastHint.generation, value);
  }
  fire(timer(r, 2000)); await settle(); assert.equal(r.context.updatesRefreshCount, 2);
  assert.equal(r.context.snapshotCurrentIndex, null); assert.equal(r.calls.length, 0);
});

test('signal refresh verifies the CDN generation and ignores message generation as publication authority', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  wireRefresh(r); await run(r, 'refreshAll(false)'); wireUpdates(r); sockets[0].open();
  install(r, generation('g2', 2, 1)); sockets[0].message(hint('untrusted-generation', '2026-10-11T16:00:00Z'));
  fire(timer(r, 2000)); await settle(); assert.equal(r.context.snapshotCurrentIndex.generation, 'g2');
  assert(Object.values(r.context.DATA).every(part => part.snapshot.generation === 'g2'));
  assert.equal(r.context.MEDIA.generation, 'media-g2'); assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
  const bad = generation('g3', 3, 2), url = new URL(bad.index.routes.lifelist, indexUrl).href;
  const raw = Buffer.from(bad.objects.get(url)); raw[1] ^= 1; bad.objects.set(url, raw); install(r, bad);
  sockets[0].message(hint('another-hint', '2026-10-11T17:00:00Z')); fire(timer(r, 2000)); await settle();
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g2'); assert.equal(r.context.MEDIA.generation, 'media-g2');
  assert.equal(r.context.window.__avianSnapshotStatus.state, 'retained');
});

test('failed or flapping sockets reconnect with exponential jitter capped at sixty seconds; polling remains usable', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'Math.random=function(){return 0.5;}'); wireUpdates(r); const delays = [];
  for (let n = 0; n < 9; n++) {
    const socket = sockets.at(-1); socket.open(); socket.fail(); socket.onclose();
    const retry = r.context.updatesReconnectTimer; delays.push(retry.delay); assert(retry.delay >= 1000 && retry.delay <= 60000);
    fire(retry); assert.equal(sockets.length, n + 2);
  }
  assert.deepEqual(delays, [1125, 2250, 4500, 9000, 18000, 36000, 60000, 60000, 60000]);
  assert.equal(r.calls.length, 0); wireRefresh(r); await run(r, 'refreshAll(false)');
  assert.equal(r.context.DATA.stats.totals.detections, 1); assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('hidden pages disconnect and cancel retries; visible pages reconnect and refresh the CDN', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'updatesRefreshCount=0;refreshAll=function(){updatesRefreshCount++;return Promise.resolve();}'); wireUpdates(r);
  sockets[0].open(); const queued = timer(r, 2000); visibility(r, true);
  assert.equal(sockets[0].readyState, 3); assert(queued.cleared); assert.equal(r.context.updatesReconnectTimer, null);
  assert.equal(r.context.updatesSocket, null); sockets[0].message(hint()); assert.equal(r.context.updatesLastHint, null);
  visibility(r, false); assert.equal(sockets.length, 2); fire(timer(r, 2000)); await settle();
  assert.equal(r.context.updatesRefreshCount, 1); assert.equal(r.calls.length, 0);
});

test('notifications received during a refresh queue one further refresh without parallel work', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'updatesRefreshCount=0;refreshAll=function(){updatesRefreshCount++;return new Promise(function(resolve){finishUpdates=resolve;});}');
  wireUpdates(r); sockets[0].open(); fire(timer(r, 2000)); await settle();
  for (let n = 0; n < 10; n++) sockets[0].message(hint('new-' + n));
  assert.equal(r.context.updatesRefreshCount, 1); assert.equal(r.context.updatesRefreshTimer, null);
  run(r, 'finishUpdates()'); await settle(); fire(timer(r, 2000)); await settle();
  assert.equal(r.context.updatesRefreshCount, 2); run(r, 'finishUpdates()'); await settle();
});

test('signal refresh reloads an open modal coherently without repeating its entrance animation', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  run(r, 'modalEntrances=0;audioStops=0;morphModalOpen=function(){modalEntrances++;};stopModalAudio=function(){audioStops++;};openDetailModal("Calypte anna")');
  await settle(); wireUpdates(r); sockets[0].open(); install(r, generation('g2', 2, 1));
  sockets[0].message(hint()); fire(timer(r, 2000)); await settle();
  assert.equal(r.context.modalEntrances, 1); assert.equal(r.context.audioStops, 1);
  assert.equal(r.element('modalAllTime').textContent, '2'); assert.equal(r.element('modalWindow').textContent, '2');
  assert.equal(r.context.SPECIES_CACHE[sci].snapshot.generation, 'g2');
  assert.equal(r.element('modalImg').src, imageUrl.replace('/g1/', '/g2/'));
  assert(r.calls.every(c => c.url.startsWith(base + '/data/')));
});

test('closing a modal while a signal refresh is pending never reopens or writes that closed modal', async () => {
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  wireRefresh(r); await run(r, 'refreshAll(false)'); wireModal(r);
  vm.runInContext(section('  function closeDetailModal(', '  // Shared-element morph:'), r.context);
  r.context.morphModalClose = (_card, _source, done) => done();
  run(r, 'openDetailModal("Calypte anna")'); await settle(); wireUpdates(r); sockets[0].open();
  const next = generation('g2', 2, 1); install(r, next);
  const slow = new URL(next.index.routes.lifelist, indexUrl).href; let complete;
  r.state.intercept = url => url === slow ? new Promise(resolve => { complete = resolve; }) : null;
  fire(timer(r, 2000)); await settle(); run(r, 'closeDetailModal()');
  complete(response(r.state.objects.get(slow))); await settle();
  assert.equal(r.context.snapshotCurrentIndex.generation, 'g2');
  assert.equal(r.element('detail-modal').getAttribute('aria-hidden'), 'true');
  assert.equal(r.element('modalAllTime').textContent, '1'); assert.equal(r.context.SPECIES_CACHE[sci], undefined);
});

test('socket construction and connection deadlines back off once; obsolete callbacks cannot schedule extra work', () => {
  class Unavailable { constructor() { throw new Error('synthetic transport unavailable'); } }
  const absent = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Unavailable });
  run(absent, 'Math.random=function(){return 0;}'); wireUpdates(absent);
  assert.equal(absent.context.updatesReconnectTimer.delay, 1000); assert.equal(absent.calls.length, 0);
  const { Socket, sockets } = fakeSockets(); const r = runtime({ AVIAN_UPDATES_URL: updatesUrl, testWebSocket: Socket });
  run(r, 'Math.random=function(){return 0;}'); wireUpdates(r); const old = sockets[0];
  fire(timer(r, 10000)); const retry = r.context.updatesReconnectTimer;
  assert.equal(old.readyState, 3); assert.equal(retry.delay, 1000);
  old.open(); old.message(hint()); old.onclose(); assert.equal(r.context.updatesReconnectTimer, retry);
  assert.equal(r.context.updatesRefreshTimer, null); fire(retry); sockets[1].open();
  fire(timer(r, 30000)); assert.equal(r.context.updatesReconnectAttempt, 0);
});
