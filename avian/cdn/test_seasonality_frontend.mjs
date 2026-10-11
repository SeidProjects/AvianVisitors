import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
const api = createRequire(import.meta.url)('../frontend/seasonality.js');

const path = 'objects/' + 'a'.repeat(64) + '.json';
function index(generation = 'test1') {
  return { schema_version: 3, capabilities: ['seasonality-v1'], generation,
    published_at: '2026-10-11T15:00:00Z', as_of: '2026-10-11T11:00:00-04:00', timezone: 'America/New_York',
    compatibility: { last_hour: 'rolling' }, routes: { stats: path, lifelist: path, seasonality: path,
      recent: Object.fromEntries(['1', '12', '24', '168', '1000000'].map(key => [key, path])),
      timeseries: { '30': path }, firstseen: { '10': path }, species: { 'Synthetic bird': path } },
    objects: { [path]: { sha256: 'a'.repeat(64), bytes: 100 } } };
}
function aggregate() {
  return { format: 'seasonality-v1', start_date: '2025-10-12', end_date: '2026-10-11',
    coverage: { basis: 'detections_do_not_establish_uptime', default: 'unknown', partial_dates: ['2026-10-11'], first_observation_date: '2026-06-21' },
    species: [{ sci: 'Synthetic bird', com: 'Example Bird', total: 5, detected_days: 2, counts: [[252, 3], [364, 2]] }], totals: { detections: 5 }, excluded_invalid_rows: 0 };
}
const validate = value => api.validatePayload(value, { action: 'seasonality' }, index());
const drain = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };

test('inclusive annual window clamps February 29 without discarding the leap day', () => {
  assert.equal(api.annualStart('2024-02-29'), '2023-03-01');
  assert.equal(api.calendarDays('2023-03-01', '2024-02-29').length, 366);
  assert.equal(api.annualStart('2025-02-28'), '2024-02-29');
  assert.equal(api.calendarDays('2024-02-29', '2025-02-28').length, 366);
  assert.equal(api.annualStart('2026-10-11'), '2025-10-12');
});
test('civil arithmetic stays on the correct day across both DST transitions', () => {
  assert.deepEqual(api.calendarDays('2026-03-07', '2026-03-10'), ['2026-03-07', '2026-03-08', '2026-03-09', '2026-03-10']);
  assert.deepEqual(api.calendarDays('2026-10-31', '2026-11-02'), ['2026-10-31', '2026-11-01', '2026-11-02']);
  assert.equal(api.addDays('2026-03-08', 1), '2026-03-09');
});
test('invalid and unbounded civil dates fail closed', () => {
  for (const value of ['2025-02-29', '2026-13-01', '2026-04-31', '2026-1-01', '0000-01-01']) assert.throws(() => api.civilDate(value));
  assert.throws(() => api.calendarDays('2025-01-01', '2026-10-11'));
  assert.throws(() => api.calendarDays('2026-10-12', '2026-10-11'));
});
test('recorder date validation works when the visitor is in another timezone', () => {
  const original = process.env.TZ;
  try { process.env.TZ = 'Pacific/Honolulu'; assert.equal(api.validateIndex(index()).as_of, '2026-10-11T11:00:00-04:00'); assert.equal(validate(aggregate()).end_date, '2026-10-11'); }
  finally { if (original === undefined) delete process.env.TZ; else process.env.TZ = original; }
});
test('clock strings must agree with the explicitly named recorder timezone', () => {
  const value = index(); value.as_of = '2026-10-11T11:00:00Z'; assert.throws(() => api.validateIndex(value));
  value.as_of = '2026-10-11T11:00:00-04:00'; value.timezone = 'Invalid/Timezone'; assert.throws(() => api.validateIndex(value));
});
test('schema and capability are explicit, with no day-recording route extension', () => {
  assert.equal(api.validateIndex(index()).schema_version, 3);
  const unknown = index(); unknown.capabilities.push('unexpected'); assert.throws(() => api.validateIndex(unknown));
  const extra = index(); extra.routes.recording_days = {}; assert.throws(() => api.validateIndex(extra));
  const legacy = index(); legacy.schema_version = 2; assert.throws(() => api.validateIndex(legacy));
  const numeric = index(); numeric.generation = 123; assert.throws(() => api.validateIndex(numeric));
  const published = index(); published.published_at = 10; assert.throws(() => api.validateIndex(published));
});
test('all references must exactly close over the hashed inventory', () => {
  const unreferenced = index(); unreferenced.objects['objects/' + 'b'.repeat(64) + '.json'] = { sha256: 'b'.repeat(64), bytes: 100 }; assert.throws(() => api.validateIndex(unreferenced));
  const mismatch = index(); mismatch.objects[path].sha256 = 'b'.repeat(64); assert.throws(() => api.validateIndex(mismatch));
  const traversal = index(); traversal.routes.seasonality = '../data.json'; assert.throws(() => api.validateIndex(traversal));
  const oversize = index(); oversize.objects[path].bytes = 2 * 1024 * 1024 + 1; assert.throws(() => api.validateIndex(oversize));
});
test('feature keeps the existing four MiB complete candidate ceiling', () => {
  const value = index(), other = 'objects/' + 'b'.repeat(64) + '.json'; value.objects[path].bytes = 2 * 1024 * 1024; value.objects[other] = { sha256: 'b'.repeat(64), bytes: 2 * 1024 * 1024 }; value.routes.seasonality = other;
  assert.throws(() => api.validateIndex(value));
});
test('daily data is complete and explicitly distinguishes unknown recorder uptime', () => {
  assert.equal(validate(aggregate()).totals.detections, 5);
  const value = aggregate(); value.coverage.default = 'covered'; assert.throws(() => validate(value));
  const historical = aggregate(); historical.coverage.first_observation_date = '2025-10-12'; assert.throws(() => validate(historical));
});
test('sparse counts reject duplicate, unsorted, zero and out-of-window entries', () => {
  for (const counts of [[[0, 1], [0, 2]], [[2, 1], [1, 2]], [[0, 0]], [[365, 1]], [[-1, 1]], [[1, 1.5]]]) assert.throws(() => api.denseCounts(counts, 365));
  assert.deepEqual(api.denseCounts([[0, 2], [2, 1]], 3), [2, 0, 1]);
});
test('daily and per-bird totals cannot silently disagree', () => {
  const total = aggregate(); total.totals.detections = 6; assert.throws(() => validate(total));
  const days = aggregate(); days.species[0].detected_days = 3; assert.throws(() => validate(days));
  const duplicate = aggregate(); duplicate.species.push({ ...duplicate.species[0] }); assert.throws(() => validate(duplicate));
  const future = aggregate(); future.end_date = '2026-10-12'; assert.throws(() => validate(future));
  const scanned = aggregate(); scanned.excluded_invalid_rows = 200000; assert.throws(() => validate(scanned));
});
test('an empty first year remains a valid unknown-coverage calendar', () => {
  const value = aggregate(); value.species = []; value.totals.detections = 0; value.coverage.first_observation_date = null;
  assert.equal(validate(value).species.length, 0);
});
test('ordering compares detected-day density before total occurrences', () => {
  const birds = [{ sci: 'b', com: 'Busy', total: 100, detected_days: 1, counts: [[0, 100]] }, { sci: 'a', com: 'Often', total: 2, detected_days: 2, counts: [[0, 1], [1, 1]] }];
  assert.deepEqual(api.rankedSpecies(birds, 365, 'density').map(bird => bird.sci), ['a', 'b']);
});
test('seasonal ordering uses the strongest 28-day period, with stable earliest ties', () => {
  const early = Array(365).fill(0); early[1] = 5;
  const later = Array(365).fill(0); later[200] = 5;
  assert(api.peakOffset(early) < api.peakOffset(later));
  assert.equal(api.peakOffset(Array(365).fill(1)), 13);
});
test('rapid generation changes keep one active fetch and only the newest queued fetch', async () => {
  const requests = [], values = [], failures = [];
  const task = api.latestTask(value => new Promise((resolve, reject) => requests.push({ value, resolve, reject })), value => values.push(value), error => failures.push(error));
  task.request('first'); await drain(); task.request('second'); task.request('third'); task.request('fourth');
  assert.equal(requests.length, 1); requests[0].resolve('obsolete'); await drain();
  assert.equal(requests.length, 2); assert.equal(requests[1].value, 'fourth'); assert.deepEqual(values, []);
  requests[1].resolve('current'); await drain(); assert.deepEqual(values, ['current']); assert.deepEqual(failures, []);
});
test('closed and destroyed views cannot receive pending response values or failures', async () => {
  let resolve, reject; const values = [], failures = [];
  const task = api.latestTask(() => new Promise((good, bad) => { resolve = good; reject = bad; }), value => values.push(value), error => failures.push(error));
  task.request('first'); await drain(); task.invalidate(); resolve('old'); await drain(); assert.deepEqual(values, []);
  task.request('second'); await drain(); task.destroy(); reject(new Error('old')); await drain(); assert.deepEqual(failures, []);
});

class Element {
  constructor(tag, doc) { this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {}; this.style = {}; this._text = ''; this.classList = { add: name => { this.className = (this.className || '') + ' ' + name; } }; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  get firstChild() { return this.children[0] || null; }
  set textContent(value) { this._text = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(key, value) { this.listeners[key] = value; }
  closest() { return null; }
  contains(element) { return element === this || this.all().includes(element); }
  querySelector(selector) { return this.children.flatMap(child => [child, ...child.all()]).find(child => selector === 'input[type="date"]' && child.tagName === 'INPUT' && child.type === 'date') || null; }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  getBoundingClientRect() { return { left: 0, width: 960 }; }
}
function document() { const doc = { defaultView: {}, createElement: tag => new Element(tag, doc), createElementNS: (_, tag) => new Element(tag, doc) }; return doc; }
test('renderer places names in text nodes and only shows trusted HTTPS bird artwork', async () => {
  const doc = document(), root = new Element('div', doc), value = aggregate(); value.species[0].com = '<img onerror="bad">';
  const current = index(), controller = api.create({ root, getIndex: () => current, loadSeasonality: async () => value, imageUrl: () => 'javascript:bad' });
  controller.setIndex(current); controller.show(); await drain();
  assert(root.textContent.includes('<img onerror="bad">')); assert(!root.all().some(element => element.tagName === 'IMG'));
  assert(root.all().some(element => element.tagName === 'SVG')); assert(root.textContent.includes('2 recorded'));
  controller.destroy(); assert.equal(root.children.length, 0);
});
test('refresh failure retains the prior complete calendar with its captured time', async () => {
  const doc = document(), root = new Element('div', doc); let current = index(), calls = 0;
  const controller = api.create({ root, getIndex: () => current, loadSeasonality: async () => { if (++calls > 1) throw new Error('offline'); return aggregate(); } });
  controller.setIndex(current); controller.show(); await drain(); current = index('new-generation'); controller.setIndex(current); await drain();
  assert(root.textContent.includes('Example Bird')); assert(root.textContent.includes('latest calendar could not be loaded')); assert(root.textContent.includes('2026-10-11T11:00:00-04:00'));
  controller.destroy();
});
test('only visible charts mount and disconnected observer callbacks cannot redraw old rows', async () => {
  const doc = document(), root = new Element('div', doc), observers = [], current = index();
  doc.defaultView.IntersectionObserver = class { constructor(callback) { this.callback = callback; this.hosts = []; observers.push(this); } observe(host) { this.hosts.push(host); } disconnect() {} };
  const value = aggregate(); value.species = Array.from({ length: 100 }, (_, position) => ({ ...value.species[0], sci: 'Example ' + position, com: 'Example ' + position })); value.totals.detections = 500;
  const controller = api.create({ root, getIndex: () => current, loadSeasonality: async () => value }); controller.setIndex(current); controller.show(); await drain();
  assert.equal(root.all().filter(element => element.tagName === 'SVG').length, 0);
  const first = observers[0]; first.callback(first.hosts.slice(0, 2).map(target => ({ target, isIntersecting: true })));
  assert.equal(root.all().filter(element => element.tagName === 'SVG').length, 2);
  first.callback([{ target: first.hosts[0], isIntersecting: false }]); assert.equal(root.all().filter(element => element.tagName === 'SVG').length, 1);
  controller.show(); const old = first.hosts[0]; first.callback([{ target: old, isIntersecting: true }]); assert.equal(old.children.length, 0);
  controller.destroy();
});
