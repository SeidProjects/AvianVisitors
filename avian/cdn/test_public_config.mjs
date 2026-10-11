import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import test from 'node:test';

const generator = join(dirname(fileURLToPath(import.meta.url)), 'generate_public_config.py');
const names = ['AVIAN_PUBLIC_STATIC_MODE', 'AVIAN_MEDIA_ORIGIN',
  'AVIAN_SNAPSHOT_INDEX_URL', 'AVIAN_WIKI_INDEX_URL'];
const python = process.env.PYTHON || 'python3';

function generated(environment, inspect, previous = '') {
  const directory = mkdtempSync(join(tmpdir(), 'public-config-test-'));
  const output = join(directory, 'public-config.js');
  if (previous) writeFileSync(output, previous);
  try {
    // Pass only synthetic fixture inputs to the subprocess, not a real env dump.
    const result = spawnSync(python, [generator, '--output', output], {
      env: environment, encoding: 'utf8', timeout: 10000,
    });
    assert.equal(result.error, undefined);
    inspect(result, () => readFileSync(output, 'utf8'));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

function evaluate(source, existing = {}) {
  const window = { ...existing };
  vm.runInNewContext(source, { window }, { timeout: 1000 });
  assert.deepEqual(Object.keys(window).sort(), [...names].sort());
  return window;
}

test('default/private-only inputs execute as cleared legacy settings', () => {
  generated({ PRIVATE_API_KEY: 'synthetic-private-sentinel' }, (result, read) => {
    assert.equal(result.status, 0);
    const source = read();
    assert(!source.includes('synthetic-private-sentinel'));
    const window = evaluate(source);
    assert.equal(window.AVIAN_PUBLIC_STATIC_MODE, false);
    assert(names.slice(1).every(name => window[name] === ''));
  });
});

test('explicit disable overwrites stale CDN globals and ignores stale URLs', () => {
  generated({ AVIAN_CDN_ENABLED: '0', AVIAN_MEDIA_ORIGIN: 'http://obsolete.example.org',
    AVIAN_SNAPSHOT_INDEX_URL: 'invalid-stale-setting' }, (result, read) => {
    assert.equal(result.status, 0);
    const window = evaluate(read(), Object.fromEntries(names.map(name => [name, 'stale'])));
    assert.equal(window.AVIAN_PUBLIC_STATIC_MODE, false);
    assert(names.slice(1).every(name => window[name] === ''));
  });
});

test('complete public URLs auto-enable and use equal browser origins', () => {
  generated({ AVIAN_MEDIA_ORIGIN: 'https://MEDIA.EXAMPLE.NET:443/',
    AVIAN_SNAPSHOT_INDEX_URL: 'https://media.example.net/data/snapshot-index.json',
    AVIAN_WIKI_INDEX_URL: 'https://media.example.net:443/data/descriptions.json',
    PRIVATE_API_KEY: 'synthetic-private-sentinel', UNKNOWN_SETTING: 'synthetic-unknown' },
  (result, read) => {
    assert.equal(result.status, 0);
    const source = read();
    assert(!source.includes('synthetic-private-sentinel') && !source.includes('synthetic-unknown'));
    const window = evaluate(source);
    assert.equal(window.AVIAN_PUBLIC_STATIC_MODE, true);
    assert.equal(window.AVIAN_MEDIA_ORIGIN, 'https://media.example.net');
    for (const name of names.slice(2)) {
      assert.equal(new URL(window[name]).origin, window.AVIAN_MEDIA_ORIGIN);
    }
    assert.notEqual(new URL('https://birds.example.org').origin, window.AVIAN_MEDIA_ORIGIN);
  });
});

test('partial configuration exits unsuccessfully and preserves prior browser artifact', () => {
  generated({ AVIAN_CDN_ENABLED: '1', AVIAN_MEDIA_ORIGIN: 'https://media.example.net',
    PRIVATE_API_KEY: 'synthetic-private-sentinel' }, (result, read) => {
    assert.equal(result.status, 2);
    assert(result.stderr.includes('requires both'));
    assert(!`${result.stdout}${result.stderr}`.includes('synthetic-private-sentinel'));
    assert.equal(read(), 'previous-release-bytes');
  }, 'previous-release-bytes');
});
