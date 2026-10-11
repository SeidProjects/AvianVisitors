import fs from 'node:fs';
import vm from 'node:vm';
import {createHash, webcrypto} from 'node:crypto';
import assert from 'node:assert/strict';
import test from 'node:test';

const app = fs.readFileSync(new URL('../frontend/apt.js', import.meta.url), 'utf8');
const moduleSource = fs.readFileSync(new URL('../frontend/seasonality.js', import.meta.url), 'utf8');
const begin = app.indexOf('  // Optional CDN snapshots.');
const end = app.indexOf('  // ---- Sliding pill helper', begin);
assert(begin >= 0 && end > begin, 'actual verified loader boundaries exist');
const loaderSource = app.slice(begin, end);
const origin = 'https://cdn.example.net';
const indexUrl = origin + '/data/snapshot-index.json';
const sci = 'Example bird';
const encode = value => Buffer.from(JSON.stringify(value), 'utf8');
const drain = async () => { await new Promise(resolve => setImmediate(resolve)); };

function response(raw, status = 200) {
  let offset = 0;
  return {ok: status >= 200 && status < 300, status, headers: {get: () => String(raw.length)},
    body: {getReader: () => ({read() {
      if (offset >= raw.length) return Promise.resolve({done: true});
      const value = new Uint8Array(raw.subarray(offset, offset + 97)); offset += value.length;
      return Promise.resolve({done: false, value});
    }, cancel: () => Promise.resolve()})}};
}
function generation(id = 'g1', count = 1, minute = 0, schema = 3, mutateAggregate = null) {
  const objects = new Map();
  const index = {schema_version: schema, generation: id,
    published_at: `2026-10-11T15:${String(minute).padStart(2, '0')}:00Z`,
    as_of: `2026-10-11T11:${String(minute).padStart(2, '0')}:00-04:00`, timezone: 'America/New_York',
    compatibility: {last_hour: 'php_today_only'}, media_manifest_generation: 'media-' + id,
    routes: {}, objects: {}};
  const store = payload => {
    const raw = encode(payload), hash = createHash('sha256').update(raw).digest('hex');
    const path = 'objects/' + hash + '.json';
    objects.set(new URL(path, indexUrl).href, raw);
    index.objects[path] = {sha256: hash, bytes: raw.length}; return path;
  };
  const bird = {sci, com: 'Example Bird', n: count, latest_media: {available: false, url: null}, media_images: {}};
  index.routes.stats = store({totals: {detections: count, species: 1}, today: {}, last_hour: {}, week: {}, started: '2026-10-10'});
  index.routes.lifelist = store({species: [bird]});
  index.routes.timeseries = {'30': store({days: 30, daily: [{date: '2026-10-10', detections: count, species: 1}], by_hour: []})};
  index.routes.firstseen = {'10': store({species: [{sci, total: count}]})};
  index.routes.recent = {};
  for (const hours of [1, 12, 24, 168, 1000000]) index.routes.recent[hours] = store({hours, species: [bird]});
  index.routes.species = {[sci]: store({sci, summary: {com: bird.com, total: count}, detections: []})};
  if (schema === 3) {
    index.capabilities = ['seasonality-v1'];
    const aggregate = {format: 'seasonality-v1', start_date: '2025-10-12', end_date: '2026-10-11',
      coverage: {basis: 'detections_do_not_establish_uptime', default: 'unknown', partial_dates: ['2026-10-11'], first_observation_date: '2026-10-10'},
      species: [{sci, com: 'Example Bird', total: count, detected_days: 1, counts: [[363, count]]}],
      totals: {detections: count}, excluded_invalid_rows: 0};
    if (mutateAggregate) mutateAggregate(aggregate);
    index.routes.seasonality = store(aggregate);
  }
  return {index, objects};
}
function runtime(first = generation()) {
  const state = {index: first.index, objects: new Map(first.objects), statuses: new Map(), intercept: null};
  const calls = [], elements = new Map(), warnings = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent: '', hidden: false, attributes: {},
      hasAttribute: () => false, getAttribute(name) { return this.attributes[name]; },
      setAttribute(name, value) {this.attributes[name] = String(value);}, removeAttribute(name) {delete this.attributes[name];},
      style: {}, classList: {add() {}, remove() {}, toggle() {}}});
    return elements.get(id);
  };
  const window = {AVIAN_PUBLIC_STATIC_MODE: true, AVIAN_MEDIA_ORIGIN: origin, AVIAN_SNAPSHOT_INDEX_URL: indexUrl};
  const context = vm.createContext({window, URL, TextDecoder, TextEncoder, AbortController, Uint8Array, crypto: webcrypto,
    location: {origin: 'https://birds.example.org', href: 'https://birds.example.org/', hash: ''},
    document: {documentElement: element('document-root'), body: element('body'), getElementById: element},
    console: {warn(...values) {warnings.push(values);}},
    setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    DATA: {stats: null, lifelist: null, timeseries: null, firstseen: null, recent: null},
    currentHours: 24, SPECIES_CACHE: {},
    recomputeDerived() {}, renderTimeIndependent() {}, renderCollageFromData() {}, syncPill() {},
    fetch(url, options) {
      calls.push({url, options});
      if (state.intercept) {const result = state.intercept(url, options); if (result) return result;}
      const raw = url === indexUrl ? encode(state.index) : state.objects.get(url);
      return Promise.resolve(response(raw || Buffer.alloc(0), state.statuses.get(url) || (raw ? 200 : 404)));
    }});
  vm.runInContext(moduleSource, context);
  vm.runInContext(loaderSource, context);
  return {context, state, calls, warnings};
}
const run = (runtime, source) => vm.runInContext(source, runtime.context);
const install = (runtime, next) => {
  runtime.state.index = next.index;
  for (const [url, bytes] of next.objects) runtime.state.objects.set(url, bytes);
};
const basePage = runtime => run(runtime, 'loadSnapshots(snapshotPageRequests(24)).then(function(parts) { installSnapshotPage(parts, false, 24); return parts; })');
const annualUrl = index => new URL(index.routes.seasonality, indexUrl).href;
const assertCdnOnly = runtime => {
  assert(runtime.calls.every(call => call.url.startsWith(origin + '/data/')));
  assert(runtime.calls.every(call => call.options.credentials === 'omit' && call.options.redirect === 'error'));
};

test('schema two keeps its existing page and rejects an annual route without a recorder fallback', async () => {
  const first = generation('old', 1, 0, 2), r = runtime(first);
  await basePage(r);
  assert.equal(r.context.DATA.stats.snapshot.schema_version, 2);
  await assert.rejects(run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true})'), /seasonality/);
  assert.equal(r.calls.length, 6);
  assertCdnOnly(r);
});
test('schema three verifies the base page before lazily reading one annual aggregate', async () => {
  const first = generation(), r = runtime(first);
  await basePage(r);
  assert(!r.calls.some(call => call.url === annualUrl(first.index)));
  const parts = await run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true})');
  assert.equal(parts[0].species[0].total, 1);
  assert.equal(parts[0].snapshot.generation, 'g1');
  assert.equal(r.calls.filter(call => call.url === annualUrl(first.index)).length, 1);
  assertCdnOnly(r);
});
test('a corrupt annual body fails checksum verification and does not select its index', async () => {
  const first = generation(), r = runtime(first), url = annualUrl(first.index);
  r.state.objects.set(url, Buffer.from(r.state.objects.get(url).toString().replace('"total":1', '"total":2')));
  await assert.rejects(run(r, 'loadSnapshots([{action:"seasonality"}])'), /checksum/);
  assert.equal(run(r, 'snapshotCurrentIndex'), null);
  assertCdnOnly(r);
});
test('checksummed malformed calendar, totals and duplicate offsets cannot be adopted', async () => {
  const cases = [payload => {payload.end_date = '2026-10-10';},
    payload => {payload.species[0].total = 2;},
    payload => {payload.species[0].counts = [[363, 1], [363, 1]]; payload.species[0].total = 2; payload.totals.detections = 2;},
    payload => {payload.species[0].counts = [[365, 1]];},
    payload => {payload.species[0].counts = [[363, 200000]]; payload.species[0].total = 200000; payload.totals.detections = 200000; payload.excluded_invalid_rows = 1;},
    payload => {payload.species[0].file = '/private/recordings/source.mp3';},
    payload => {payload.coverage.source_url = 'http://recorder.example.local/api';}];
  for (const mutate of cases) {
    const r = runtime(generation('malformed', 1, 0, 3, mutate));
    await assert.rejects(run(r, 'loadSnapshots([{action:"seasonality"}])'));
    assert.equal(run(r, 'snapshotCurrentIndex'), null);
    assertCdnOnly(r);
  }
});
test('retired pinned annual metadata recovers once with the complete new base page', async () => {
  const first = generation(), next = generation('g2', 2, 1), r = runtime(first);
  await basePage(r); install(r, next);
  r.state.statuses.set(annualUrl(first.index), 404);
  const pointerBefore = r.calls.filter(call => call.url === indexUrl).length;
  const parts = await run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true})');
  assert.equal(parts[0].snapshot.generation, 'g2');
  assert.equal(parts[0].species[0].total, 2);
  assert.equal(r.context.DATA.stats.snapshot.generation, 'g2');
  assert.equal(r.context.DATA.recent.snapshot.generation, 'g2');
  assert.equal(r.context.DATA.stats.totals.detections, 2);
  assert.equal(r.calls.filter(call => call.url === indexUrl).length, pointerBefore + 1);
  assert.equal(r.calls.filter(call => call.url === annualUrl(first.index)).length, 1);
  assert.equal(r.calls.filter(call => call.url === annualUrl(next.index)).length, 1);
  assertCdnOnly(r);
});
test('a second retired annual object stops recovery and retains the previous complete page', async () => {
  const first = generation(), next = generation('g2', 2, 1), r = runtime(first);
  await basePage(r); install(r, next);
  r.state.statuses.set(annualUrl(first.index), 404); r.state.statuses.set(annualUrl(next.index), 410);
  const pointerBefore = r.calls.filter(call => call.url === indexUrl).length;
  await assert.rejects(run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true})'));
  assert.equal(run(r, 'snapshotCurrentIndex.generation'), 'g1');
  assert.equal(r.context.DATA.stats.snapshot.generation, 'g1');
  assert.equal(r.calls.filter(call => call.url === indexUrl).length, pointerBefore + 1);
  assertCdnOnly(r);
});
test('an invalidated selection cannot accept a late annual response', async () => {
  const first = generation(), r = runtime(first);
  await basePage(r);
  let release;
  r.context.acceptSelection = true;
  r.state.intercept = url => url === annualUrl(first.index) ? new Promise(resolve => {release = resolve;}) : null;
  const pending = run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true, accept:function() { return acceptSelection; }})');
  const rejected = assert.rejects(pending, error => error.obsolete === true);
  await drain(); assert.equal(typeof release, 'function');
  r.context.acceptSelection = false;
  release(response(first.objects.get(annualUrl(first.index))));
  await rejected;
  assert.equal(run(r, 'snapshotCurrentIndex.generation'), 'g1');
  assertCdnOnly(r);
});
test('a late aggregate from the previous generation cannot overwrite a newer adopted page', async () => {
  const first = generation(), next = generation('g2', 2, 1), r = runtime(first);
  await basePage(r);
  let release;
  r.state.intercept = url => url === annualUrl(first.index) ? new Promise(resolve => {release = resolve;}) : null;
  const pending = run(r, 'loadSnapshots([{action:"seasonality"}], {pinCurrent:true})');
  const rejected = assert.rejects(pending, error => error.obsolete === true);
  await drain(); assert.equal(typeof release, 'function');
  install(r, next); await basePage(r);
  release(response(first.objects.get(annualUrl(first.index))));
  await rejected;
  assert.equal(run(r, 'snapshotCurrentIndex.generation'), 'g2');
  assert.equal(r.context.DATA.stats.totals.detections, 2);
  assertCdnOnly(r);
});
