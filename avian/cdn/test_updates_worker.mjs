import assert from 'node:assert/strict';
import {timingSafeEqual, webcrypto} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {createUpdatesWorker, makeUpdatesObject} from './updates_worker.mjs';

const token = 'synthetic_private_notification_token_12345';
const origin = 'https://birds.example';
const date = n => `2024-05-01T00:${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}Z`;

class Socket {
  readyState = 1;
  bufferedAmount = undefined;
  messages = [];
  closed = [];
  attachment = null;
  events = [];
  serializeAttachment(value) { this.events.push('attachment'); this.attachment = structuredClone(value); }
  deserializeAttachment() { return structuredClone(this.attachment); }
  accept() { throw new Error('ordinary socket acceptance prevents hibernation'); }
  send(value) {
    if (this.failSend) throw new Error('synthetic socket failure');
    this.messages.push(value);
  }
  close(code, reason) { this.readyState = 3; this.closed.push({code, reason}); }
}

function fixture(extra = {}) {
  const sockets = [], names = [], comparisons = [];
  const sql = {
    row: null, writes: 0,
    exec(query, ...args) {
      if (query.startsWith('CREATE TABLE')) return {};
      if (query.startsWith('SELECT')) return {toArray: () => this.row ? [{...this.row}] : []};
      assert.match(query, /^INSERT INTO current_signal/);
      if (this.failWrite) throw new Error('synthetic storage failure');
      this.writes++;
      this.row = {generation: args[0], as_of: args[1], as_of_ms: args[2]};
      return {};
    },
  };
  const ctx = {
    storage: {sql},
    getWebSockets: () => sockets.filter(ws => ws.readyState !== 3),
    acceptWebSocket: ws => { ws.events.push('hibernating-accept'); sockets.push(ws); },
    setAlarm: () => { throw new Error('notifications must not schedule alarms'); },
    waitUntil: () => { throw new Error('notifications must not pin object residency'); },
  };
  class Base { constructor(state) { this.ctx = state; } }
  const cryptoRuntime = {subtle: {
    digest: webcrypto.subtle.digest.bind(webcrypto.subtle),
    timingSafeEqual(a, b) { comparisons.push([a.byteLength, b.byteLength]); return timingSafeEqual(Buffer.from(a), Buffer.from(b)); },
  }};
  const Hub = makeUpdatesObject(Base, {cryptoRuntime,
    createPair: () => [{side: 'client'}, new Socket()],
    upgradeResponse: webSocket => ({status: 101, webSocket}),
  });
  const env = {UPDATES_ALLOWED_ORIGINS: JSON.stringify([origin]), UPDATES_PUBLISH_TOKEN: token,
               UPDATES_MAX_CLIENTS: '256', ...extra};
  let hub = new Hub(ctx, env);
  env.UPDATES = {getByName(name) { names.push(name); return {fetch: request => hub.fetch(request)}; }};
  const worker = createUpdatesWorker({cryptoRuntime});
  const request = (path, options = {}) => new Request('https://updates.example' + path, options);
  return {sockets, sql, ctx, names, env, comparisons, Hub, get hub() { return hub; },
    wake() { hub = new Hub(ctx, env); return hub; },
    call: (path, options) => worker.fetch(request(path, options), env),
    live: (headers = {}) => worker.fetch(request('/live', {headers: {Origin: origin, Upgrade: 'websocket', ...headers}}), env),
    notify: (value, headers = {}) => worker.fetch(request('/notify', {
      method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token, ...headers},
      body: JSON.stringify(value),
    }), env),
  };
}

test('public connection requires exact configured HTTPS origin and WebSocket upgrade', async () => {
  const f = fixture();
  for (const Origin of ['', 'null', 'https://other.example', origin + '/', 'https://birds.example.evil']) {
    assert.equal((await f.live({Origin})).status, 403);
  }
  assert.equal((await f.live({Upgrade: ''})).status, 426);
  assert.equal(f.names.length, 0);
  assert.equal((await f.live()).status, 101);
  assert.deepEqual(f.names, ['current']);
  assert.equal(f.sockets.length, 1);
  assert.deepEqual(f.sockets[0].attachment, {bytes_sent: 0});
  assert.deepEqual(f.sockets[0].events, ['hibernating-accept', 'attachment']);
});

test('invalid settings fail closed without namespace routing or public secret exposure', async () => {
  for (const extra of [{UPDATES_ALLOWED_ORIGINS: '*'}, {UPDATES_ALLOWED_ORIGINS: '[]'},
    {UPDATES_ALLOWED_ORIGINS: '["http://birds.example"]'},
    {UPDATES_ALLOWED_ORIGINS: '["https://user:secret@birds.example"]'},
    {UPDATES_PUBLISH_TOKEN: ''}, {UPDATES_MAX_CLIENTS: '257'}, {UPDATES_MAX_CLIENTS: 2}]) {
    const f = fixture(extra), response = await f.live();
    assert.equal(response.status, 503);
    assert.equal(f.names.length, 0);
    assert.doesNotMatch(await response.text(), /synthetic_private|user:secret/);
  }
});

test('private notify authenticates with fixed-size constant-time comparisons', async () => {
  const f = fixture();
  assert.equal((await f.notify({generation: 'one', as_of: date(1)}, {Authorization: ''})).status, 401);
  assert.equal((await f.notify({generation: 'one', as_of: date(1)}, {Authorization: 'Bearer ' + 'x'.repeat(32)})).status, 401);
  assert.equal(f.names.length, 0);
  assert.equal((await f.notify({generation: 'one', as_of: date(1)})).status, 200);
  assert.ok(f.comparisons.length >= 3);
  assert.ok(f.comparisons.every(value => value[0] === 32 && value[1] === 32));
  assert.deepEqual(Object.keys(f.sql.row).sort(), ['as_of', 'as_of_ms', 'generation']);
});

test('authentication cannot downgrade when timingSafeEqual is unavailable', async () => {
  const f = fixture();
  const worker = createUpdatesWorker({cryptoRuntime: webcrypto});
  const request = new Request('https://updates.example/notify', {method: 'POST',
    headers: {Authorization: 'Bearer ' + token}, body: '{}'});
  assert.equal((await worker.fetch(request, f.env)).status, 503);
  assert.equal(f.names.length, 0);
});

test('only exact paths and methods route; no CORS preflight endpoint is exposed', async () => {
  const f = fixture();
  for (const path of ['/other', '/live/', '/live?', '/notify?', '/live?token=x', '/notify?x=1']) {
    assert.equal((await f.call(path)).status, 404);
  }
  assert.equal((await f.call('/live', {method: 'POST'})).status, 405);
  assert.equal((await f.call('/notify', {method: 'GET'})).status, 405);
  const preflight = await f.call('/notify', {method: 'OPTIONS'});
  assert.equal(preflight.status, 405);
  assert.equal(preflight.headers.get('Access-Control-Allow-Origin'), null);
  assert.equal(f.names.length, 0);
});

test('notify broadcasts only the exact update hint and stores one signal', async () => {
  const f = fixture();
  await f.live(); await f.live();
  const next = {generation: 'one', as_of: date(1)};
  assert.deepEqual(await (await f.notify(next)).json(), {status: 'updated', ...next, delivered: 2});
  for (const socket of f.sockets) assert.deepEqual(JSON.parse(socket.messages[0]), {type: 'updated', ...next});
  assert.equal(f.sql.writes, 1);
  assert.equal((await f.notify({generation: 'two', as_of: date(2)})).status, 200);
  assert.equal(f.sql.row.generation, 'two');
  assert.equal(f.sql.writes, 2);
});

test('duplicate generations and stale or equal instants create no writes or fanout', async () => {
  const f = fixture();
  await f.live();
  await f.notify({generation: 'one', as_of: date(2)});
  for (const value of [{generation: 'one', as_of: date(3)}, {generation: 'old', as_of: date(1)},
    {generation: 'equal', as_of: '2024-05-01T01:00:02+01:00'}]) {
    assert.deepEqual(await (await f.notify(value)).json(), {status: 'ignored', ...value});
  }
  assert.equal(f.sql.writes, 1);
  assert.equal(f.sockets[0].messages.length, 1);
});

test('hibernation reconstructs persisted signal and preserves socket byte attachment', async () => {
  const f = fixture();
  await f.live();
  await f.notify({generation: 'one', as_of: date(1)});
  const original = f.sockets[0], bytes = original.attachment.bytes_sent;
  f.wake();
  await f.live();
  assert.equal(f.sql.writes, 1);
  assert.equal(f.sockets[1].messages.length, 1);
  assert.equal(original.attachment.bytes_sent, bytes);
  await f.notify({generation: 'two', as_of: date(2)});
  assert.equal(original.messages.length, 2);
  assert.ok(original.attachment.bytes_sent > bytes);
});

test('notification payload requires bounded exact fields, safe generation and real aware date', async () => {
  const f = fixture();
  for (const value of [null, [], {generation: 'one'}, {generation: '../key', as_of: date(1)},
    {generation: 'bad.dot', as_of: date(1)}, {generation: 'x'.repeat(81), as_of: date(1)},
    {generation: 'one', as_of: '2024-05-01T00:00:00'},
    {generation: 'one', as_of: '2024-02-30T00:00:00Z'},
    {generation: 'one', as_of: '2024-05-01T24:00:00Z'},
    {generation: 'one', as_of: date(1), url: 'https://media.example/secret'}]) {
    assert.equal((await f.notify(value)).status, 400);
  }
  assert.equal((await f.notify({generation: 'one', as_of: date(1)}, {'Content-Type': 'text/plain'})).status, 400);
  assert.equal((await f.notify({generation: 'one', as_of: date(1)}, {'Content-Length': '1025'})).status, 413);
  assert.equal(f.sql.writes, 0);
  assert.equal((await f.notify({generation: '_valid_gen-1', as_of: '2024-02-29T00:00:00.123456+00:00'})).status, 200);
});

test('chunked oversized and malformed UTF-8 bodies are rejected without persistence', async () => {
  const f = fixture();
  let canceled = false;
  const stream = new ReadableStream({start(controller) { controller.enqueue(new Uint8Array(1025)); },
    cancel() { canceled = true; }});
  const headers = {'Content-Type': 'application/json', Authorization: 'Bearer ' + token};
  const request = new Request('https://updates.example/notify', {method: 'POST', headers, body: stream, duplex: 'half'});
  assert.equal((await f.hub.fetch(request)).status, 413);
  assert.equal(canceled, true);
  const invalid = new Request('https://updates.example/notify', {method: 'POST', headers, body: new Uint8Array([255])});
  assert.equal((await f.hub.fetch(invalid)).status, 400);
  assert.equal(f.sql.writes, 0);
});

test('socket capacity declines new connections and disconnect/error handlers do not publish', async () => {
  const f = fixture({UPDATES_MAX_CLIENTS: '2'});
  await f.live(); await f.live();
  assert.equal((await f.live()).status, 503);
  assert.equal(f.sockets.length, 2);
  f.hub.webSocketClose(f.sockets[0], 1000, 'private arbitrary client text');
  assert.equal(f.sockets[0].closed[0].reason, 'closed');
  assert.equal((await f.live()).status, 101);
  f.hub.webSocketError(f.sockets[1]);
  assert.equal(f.sockets[1].closed[0].code, 1011);
  assert.equal(f.sql.writes, 0);
});

test('application messages close clients and cannot broadcast or mutate storage', async () => {
  const f = fixture(); await f.live();
  f.hub.webSocketMessage(f.sockets[0], 'publish arbitrary message');
  assert.equal(f.sockets[0].closed[0].code, 1008);
  assert.equal(f.sql.writes, 0);
  assert.equal(f.sockets[0].messages.length, 0);
});

test('failed or visibly slow socket is closed while healthy fanout continues', async () => {
  const f = fixture(); await f.live(); await f.live(); await f.live();
  f.sockets[0].bufferedAmount = 4097;
  f.sockets[1].failSend = true;
  assert.deepEqual(await (await f.notify({generation: 'one', as_of: date(1)})).json(),
                   {status: 'updated', generation: 'one', as_of: date(1), delivered: 1});
  assert.equal(f.sockets[0].closed[0].code, 1013);
  assert.equal(f.sockets[1].closed[0].code, 1011);
  assert.equal(f.sockets[2].messages.length, 1);
});

test('bounded socket lifetime remains enforced without bufferedAmount across hibernation', async () => {
  const f = fixture(); await f.live();
  for (let n = 1; n <= 100; n++) {
    await f.notify({generation: 'g'.repeat(70) + n, as_of: date(n)});
    if (n % 4 === 0) f.wake();
  }
  const socket = f.sockets[0];
  const sent = socket.messages.reduce((sum, raw) => sum + Buffer.byteLength(raw), 0);
  assert.ok(sent > 0 && sent <= 8192);
  assert.equal(socket.closed[0].code, 1013);
  assert.equal(f.sql.row.generation, 'g'.repeat(70) + 100);
});

test('failed SQLite write prevents broadcast; corrupt durable signal fails closed', async () => {
  const f = fixture(); await f.live();
  f.sql.failWrite = true;
  assert.equal((await f.notify({generation: 'one', as_of: date(1)})).status, 503);
  assert.equal(f.sockets[0].messages.length, 0);
  assert.equal(f.sql.row, null);
  f.sql.failWrite = false;
  f.sql.row = {generation: 'old', as_of: date(1), as_of_ms: 0};
  assert.equal((await f.live()).status, 503);
  assert.equal(f.sockets.length, 1);
});

test('normal updates use no CDN/media fetch, browser token, alarms or timer', async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('notification layer attempted a network fetch'); };
  try {
    const f = fixture();
    assert.equal((await f.live()).status, 101);
    assert.equal((await f.notify({generation: 'one', as_of: date(1)})).status, 200);
    assert.doesNotMatch(f.sockets[0].messages[0], /synthetic_private|https:|audio|image/);
  } finally { globalThis.fetch = oldFetch; }
  const source = await readFile(new URL('./updates_worker.mjs', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /setInterval\(|setTimeout\(|setAlarm\(/);
});
