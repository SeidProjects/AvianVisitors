// Optional update hints only. The publisher must notify after the current CDN
// index has passed its complete PUT/GET verification. This Worker reads no CDN
// objects, media, credentials from visitors, or detection data.
// Runtime API references:
// https://developers.cloudflare.com/durable-objects/examples/websocket-hibernation-server/
// https://developers.cloudflare.com/workers/examples/protect-against-timing-attacks/
const MAX_BODY = 1024;
const MAX_CLIENTS = 256;
const MAX_SENT_BYTES = 8192;
const encoder = new TextEncoder();

function reply(status, value) {
  return new Response(JSON.stringify(value), {status, headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'no-store',
  }});
}

function settings(env) {
  const raw = env.UPDATES_ALLOWED_ORIGINS;
  if (typeof raw !== 'string' || raw.length > 4096) throw new Error('configuration');
  const origins = JSON.parse(raw);
  if (!Array.isArray(origins) || !origins.length || origins.length > 16 || origins.some(value => {
    if (typeof value !== 'string') return true;
    try {
      const url = new URL(value);
      return url.protocol !== 'https:' || url.origin !== value || url.username || url.password;
    } catch { return true; }
  })) throw new Error('configuration');
  const secret = env.UPDATES_PUBLISH_TOKEN;
  if (typeof secret !== 'string' || !/^[A-Za-z0-9_-]{32,256}$/.test(secret)) throw new Error('configuration');
  const maximum = env.UPDATES_MAX_CLIENTS ?? String(MAX_CLIENTS);
  if (typeof maximum !== 'string' || !/^[1-9][0-9]{0,2}$/.test(maximum) || Number(maximum) > MAX_CLIENTS) {
    throw new Error('configuration');
  }
  return {origins: new Set(origins), secret, maximum: Number(maximum)};
}

async function authorized(request, secret, cryptoRuntime) {
  const header = request.headers.get('Authorization') ?? '';
  const match = /^Bearer ([A-Za-z0-9_-]{32,256})$/.exec(header);
  if (!match) return false;
  // Workers provides this constant-time extension; never fall back to string
  // equality if it is missing. Fixed-size digests avoid unequal-length buffers.
  const subtle = cryptoRuntime?.subtle;
  if (typeof subtle?.timingSafeEqual !== 'function') throw new Error('configuration');
  const [actual, expected] = await Promise.all([
    subtle.digest('SHA-256', encoder.encode(match[1])),
    subtle.digest('SHA-256', encoder.encode(secret)),
  ]);
  return subtle.timingSafeEqual(actual, expected);
}

async function admission(request, env, cryptoRuntime) {
  const url = new URL(request.url);
  if (url.protocol !== 'https:' || url.username || url.password ||
      request.url.includes('?') || request.url.includes('#') ||
      !['/live', '/notify'].includes(url.pathname)) {
    return {response: reply(404, {error: 'not-found'})};
  }
  if ((url.pathname === '/live' && request.method !== 'GET') ||
      (url.pathname === '/notify' && request.method !== 'POST')) {
    return {response: reply(405, {error: 'method'})};
  }
  const config = settings(env);
  if (url.pathname === '/live') {
    if (!config.origins.has(request.headers.get('Origin'))) return {response: reply(403, {error: 'origin'})};
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') {
      return {response: reply(426, {error: 'websocket-required'})};
    }
  } else if (!await authorized(request, config.secret, cryptoRuntime)) {
    return {response: reply(401, {error: 'unauthorized'})};
  }
  return {config, path: url.pathname};
}

function signal(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).sort().join(',') !== 'as_of,generation' ||
      typeof value.generation !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(value.generation) ||
      typeof value.as_of !== 'string') throw new Error('payload');
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value.as_of);
  if (!match) throw new Error('payload');
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
  if (year < 1000 || month < 1 || month > 12 || day < 1 ||
      day > new Date(Date.UTC(year, month, 0)).getUTCDate() ||
      hour > 23 || minute > 59 || second > 59 ||
      (match[7] !== 'Z' && (Number(match[7].slice(1, 3)) > 23 || Number(match[7].slice(4)) > 59))) {
    throw new Error('payload');
  }
  const milliseconds = Date.parse(value.as_of);
  if (!Number.isFinite(milliseconds)) throw new Error('payload');
  return {generation: value.generation, as_of: value.as_of, milliseconds};
}

async function readSignal(request) {
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(request.headers.get('Content-Type') ?? '')) throw new Error('payload');
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^\d{1,8}$/.test(length) || Number(length) > MAX_BODY)) throw new RangeError('body');
  if (!request.body) throw new Error('payload');
  const reader = request.body.getReader();
  let size = 0;
  const parts = [];
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY) throw new RangeError('body');
      parts.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
  return signal(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)));
}

function close(ws, code, reason) { try { ws.close(code, reason); } catch {} }

function sendHint(ws, value) {
  if (ws.readyState !== 1) return false;
  const raw = JSON.stringify({type: 'updated', generation: value.generation, as_of: value.as_of});
  const size = encoder.encode(raw).byteLength;
  try {
    const saved = ws.deserializeAttachment();
    if (!saved || !Number.isSafeInteger(saved.bytes_sent) || saved.bytes_sent < 0 ||
        saved.bytes_sent + size > MAX_SENT_BYTES ||
        (Number.isFinite(ws.bufferedAmount) && ws.bufferedAmount > 4096)) {
      close(ws, 1013, 'reconnect');
      return false;
    }
    ws.serializeAttachment({bytes_sent: saved.bytes_sent + size});
    ws.send(raw);
    return true;
  } catch { close(ws, 1011, 'notification-unavailable'); return false; }
}

export function createUpdatesWorker({cryptoRuntime = globalThis.crypto} = {}) {
  return {
    async fetch(request, env) {
      try {
        const admitted = await admission(request, env, cryptoRuntime);
        if (admitted.response) return admitted.response;
        // One named instance, never a user-supplied shard or namespace name.
        return await env.UPDATES.getByName('current').fetch(request);
      } catch { return reply(503, {error: 'notifications-unavailable'}); }
    },
  };
}

// Factory isolates cloudflare:workers and upgrade responses from Node tests.
export function makeUpdatesObject(DurableObject, {
  cryptoRuntime = globalThis.crypto,
  createPair = () => Object.values(new WebSocketPair()),
  upgradeResponse = client => new Response(null, {status: 101, webSocket: client}),
} = {}) {
  return class UpdatesHub extends DurableObject {
    constructor(ctx, env) {
      super(ctx, env);
      this.updatesEnv = env;
      this.sql = ctx.storage.sql;
      this.sql.exec('CREATE TABLE IF NOT EXISTS current_signal (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT NOT NULL, as_of TEXT NOT NULL, as_of_ms INTEGER NOT NULL)');
    }

    lastSignal() {
      const rows = this.sql.exec('SELECT generation,as_of,as_of_ms FROM current_signal WHERE singleton=1').toArray();
      if (!rows.length) return null;
      const result = signal({generation: rows[0].generation, as_of: rows[0].as_of});
      if (result.milliseconds !== rows[0].as_of_ms) throw new Error('stored-signal');
      return result;
    }

    async fetch(request) {
      try {
        const admitted = await admission(request, this.updatesEnv, cryptoRuntime);
        if (admitted.response) return admitted.response;
        if (admitted.path === '/live') {
          if (this.ctx.getWebSockets().length >= admitted.config.maximum) return reply(503, {error: 'socket-capacity'});
          const last = this.lastSignal();
          const [client, server] = createPair();
          this.ctx.acceptWebSocket(server); // Hibernation API; never server.accept().
          server.serializeAttachment({bytes_sent: 0});
          if (last) sendHint(server, last);
          return upgradeResponse(client);
        }
        let next;
        try { next = await readSignal(request); }
        catch (error) { return reply(error instanceof RangeError ? 413 : 400, {error: 'payload'}); }
        const last = this.lastSignal();
        if (last && (last.generation === next.generation || next.milliseconds <= last.milliseconds)) {
          return reply(200, {status: 'ignored', generation: next.generation, as_of: next.as_of});
        }
        // Synchronous SQLite read/write and fanout have no intervening await.
        // Workers' storage output gates hold outgoing messages until commit.
        this.sql.exec('INSERT INTO current_signal VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET generation=excluded.generation,as_of=excluded.as_of,as_of_ms=excluded.as_of_ms',
                      next.generation, next.as_of, next.milliseconds);
        let delivered = 0;
        for (const ws of this.ctx.getWebSockets()) if (sendHint(ws, next)) delivered++;
        return reply(200, {status: 'updated', generation: next.generation, as_of: next.as_of, delivered});
      } catch { return reply(503, {error: 'notifications-unavailable'}); }
    }

    webSocketMessage(ws) { close(ws, 1008, 'receive-only'); }
    webSocketClose(ws) { close(ws, 1000, 'closed'); }
    webSocketError(ws) { close(ws, 1011, 'notification-unavailable'); }
  };
}
