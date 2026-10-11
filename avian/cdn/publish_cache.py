"""Publish one coherent current frontend cache, never an archive of snapshots.

The caller supplies a durable file-backed SQLite connection, one exclusive
publisher authority/outer lock, an S3 client, and its shared resource/SDK guard.
No lock, SDK retry configuration, credentials, timer or cloud connection is
created here. The caller's SDK before-send hook counts actual attempts including
retries. Optional guard.wrap_body limits
payload release. Those hooks do not imply a wire-rate or provider spending cap.

CurrentCachePublisher.prepare freezes at most one candidate in SQLite before
cloud writes, so an owned temporary exporter directory can then be removed.
publish() resumes those frozen bytes; recover() resolves a lost pointer reply.
Explicit adopt_current(allow_adoption=True) performs actual GET verification of
an existing current graph during a single-authority handover. It claims only
that closure; unknown earlier cache objects are never adopted or swept.

Cleanup requires config['cleanup_current_cache'] is True. It reclaims only
ledger-owned frontend objects/manifests outside current and in-flight closures.
The mutable index, audio, images and unknown bucket keys are never deleted.
Disabling cleanup retains old remote metadata until explicit cleanup; tracked
byte/key limits hold further publication rather than growing state indefinitely.
"""
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

MIB = 1024 * 1024
OBJECT = re.compile(r'objects/([a-f0-9]{64})\.json')
GENERATION = re.compile(r'[A-Za-z0-9_-]{1,80}')
IMMUTABLE = 'public, max-age=31536000, immutable'
POINTER_CACHE = 'public, max-age=30, must-revalidate'


class CacheHold(ValueError):
    """Preserve the selected graph; operator evidence or recovery is required."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError('cache limit must be an exact positive integer')
    return value


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _missing(error):
    response = getattr(error, 'response', {})
    return (response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404 and
            str(response.get('Error', {}).get('Code')) in ('404', 'NoSuchKey', 'NotFound'))


def _config(value):
    result = dict(value)
    endpoint = urlsplit(result.get('endpoint_url', ''))
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or
            endpoint.password or endpoint.path not in ('', '/') or endpoint.query or endpoint.fragment):
        raise ValueError('endpoint_url must be a clean HTTPS endpoint')
    bucket = result.get('bucket')
    if not isinstance(bucket, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{1,251}[A-Za-z0-9]', bucket):
        raise ValueError('invalid cache bucket')
    prefix = result.setdefault('cache_prefix', 'data')
    if (not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', prefix) or
            any(part in ('audio', 'images') for part in prefix.split('/'))):
        raise ValueError('cache_prefix must be a dedicated frontend metadata namespace')
    if result.get('single_publisher') is not True:
        raise ValueError('exclusive single-publisher authority is required')
    result.setdefault('cleanup_current_cache', False)
    if type(result['cleanup_current_cache']) is not bool:
        raise ValueError('cleanup_current_cache must be an explicit boolean')
    auxiliary = result.setdefault('auxiliary_keys', [])
    if (not isinstance(auxiliary, list) or len(auxiliary) > 8 or
            any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}\.json', key) or
                key == 'snapshot-index.json' for key in auxiliary) or len(set(auxiliary)) != len(auxiliary)):
        raise ValueError('auxiliary keys require at most eight explicit stable JSON names')
    for name, default in (('max_index_bytes', MIB), ('max_object_bytes', 2 * MIB),
                          ('max_candidate_bytes', 4 * MIB), ('max_objects', 512),
                          ('max_owned_keys', 1026), ('max_cache_bytes', 8 * MIB),
                          ('max_cleanup_keys', 8)):
        result[name] = _integer(result.get(name, default))
    return result


def _index(raw, config):
    if len(raw) > config['max_index_bytes']:
        raise ValueError('cache index exceeds byte limit')
    index = _json(raw)
    if (not isinstance(index, dict) or type(index.get('schema_version')) is not int or
            index['schema_version'] not in (2, 3) or not isinstance(index.get('generation'), str) or
            not GENERATION.fullmatch(index['generation'])):
        raise ValueError('invalid cache index')
    try:
        zone = ZoneInfo(index['timezone'])
        instant = dt.datetime.fromisoformat(index['as_of'].replace('Z', '+00:00'))
        published = dt.datetime.fromisoformat(index['published_at'].replace('Z', '+00:00'))
        if (instant.utcoffset() is None or published.utcoffset() is None or
                instant.utcoffset() != instant.astimezone(zone).utcoffset()):
            raise ValueError('invalid cache timezone/clock')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('missing cache timezone/clock') from error
    objects, routes = index.get('objects'), index.get('routes')
    actions = {'stats', 'lifelist', 'recent', 'timeseries', 'firstseen', 'species'}
    if index['schema_version'] == 3:
        if index.get('capabilities') != ['seasonality-v1']:
            raise ValueError('unsupported cache capabilities')
        actions.add('seasonality')
    elif index.get('capabilities') not in (None, []):
        raise ValueError('unsupported cache capabilities')
    if (not isinstance(objects, dict) or not 1 <= len(objects) <= config['max_objects'] or
            not isinstance(routes, dict) or
            set(routes) != actions):
        raise ValueError('invalid cache object/route inventory')
    for action, keys in (('recent', {'1', '12', '24', '168', '1000000'}),
                         ('timeseries', {'30'}), ('firstseen', {'10'})):
        if not isinstance(routes[action], dict) or set(routes[action]) != keys:
            raise ValueError('invalid cache route windows')
    if not isinstance(routes['species'], dict):
        raise ValueError('invalid cache species routes')
    referenced = [routes['stats'], routes['lifelist']]
    for action in ('recent', 'timeseries', 'firstseen', 'species'):
        referenced.extend(routes[action].values())
    if index['schema_version'] == 3:
        referenced.append(routes['seasonality'])
    if any(not isinstance(path, str) for path in referenced) or set(referenced) != set(objects):
        raise ValueError('cache routes must reference exactly the captured objects')
    identities, total = {}, 2 * len(raw)
    for path, descriptor in objects.items():
        match = OBJECT.fullmatch(path)
        if (not match or not isinstance(descriptor, dict) or set(descriptor) != {'sha256', 'bytes'} or
                descriptor['sha256'] != match[1] or type(descriptor['bytes']) is not int or
                not 2 <= descriptor['bytes'] <= config['max_object_bytes']):
            raise ValueError('invalid content-addressed cache identity')
        identities[path] = (descriptor['sha256'], descriptor['bytes'])
        total += descriptor['bytes']
    if total > config['max_candidate_bytes']:
        raise ValueError('complete cache candidate exceeds byte limit')
    identities['generations/' + index['generation'] + '/manifest.json'] = (_sha(raw), len(raw))
    return index, identities


def _file(root, name, limit, check):
    path = root / name
    if path.resolve(strict=True) != path:
        raise ValueError('linked/aliased cache candidate path')
    check()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('invalid candidate file type/size')
        body = bytearray()
        while len(body) <= limit:
            check()
            chunk = stream.read(min(65536, limit + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        after = os.fstat(stream.fileno())
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(body) > limit or signature(before) != signature(after):
            raise ValueError('candidate file changed or exceeded limit')
    return bytes(body)


class CurrentCachePublisher:
    def __init__(self, client, ledger, config, guard, *, outer_lock_held):
        if outer_lock_held is not True:
            raise ValueError('caller must hold the existing publisher lock')
        self.client, self.db, self.config, self.guard = client, ledger, _config(config), guard
        if ledger.in_transaction:
            raise ValueError('cache publication requires committed caller state')
        files = [row[2] for row in ledger.execute('PRAGMA database_list') if row[1] == 'main']
        if len(files) != 1 or not files[0]:
            raise ValueError('durable file-backed shared SQLite state is required')
        if ledger.execute('PRAGMA synchronous').fetchone()[0] not in (2, 3):
            raise ValueError('SQLite FULL durability is required')
        self.prefix = self.config['cache_prefix'] + '/'
        self.pointer = self.prefix + 'snapshot-index.json'
        identity = json.dumps([self.config['endpoint_url'], self.config['bucket'], self.prefix], separators=(',', ':'))
        self._check()
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS cdn_cache_binding(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT NOT NULL)')
            bound = self.db.execute('SELECT identity FROM cdn_cache_binding WHERE id=1').fetchone()
            if bound is not None and bound != (identity,):
                raise CacheHold('cache state belongs to a different target')
            self.db.execute('INSERT OR IGNORE INTO cdn_cache_binding VALUES(1,?)', (identity,))
            self.db.execute("CREATE TABLE IF NOT EXISTS cdn_cache_graph(slot TEXT PRIMARY KEY CHECK(slot IN ('current','pending')),raw BLOB NOT NULL,phase TEXT NOT NULL)")
            self.db.execute('CREATE TABLE IF NOT EXISTS cdn_cache_pending(key TEXT PRIMARY KEY,body BLOB NOT NULL)')
            self.db.execute("CREATE TABLE IF NOT EXISTS cdn_cache_objects(key TEXT PRIMARY KEY,sha TEXT NOT NULL,size INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('putting','verified','deleting')),owned INTEGER NOT NULL CHECK(owned IN (0,1)))")
            self.db.execute('CREATE TABLE IF NOT EXISTS cdn_cache_aux(key TEXT PRIMARY KEY,current BLOB,pending BLOB,phase TEXT NOT NULL,current_sha TEXT,current_size INTEGER,pending_sha TEXT,pending_size INTEGER)')
        self.identity = identity

    def _check(self):
        self.guard.check()

    def _request(self, method, key, **kwargs):
        self._check()
        return getattr(self.client, method)(Bucket=self.config['bucket'], Key=key, **kwargs)

    def _get(self, key, limit, missing_ok=False):
        try:
            response = self._request('get_object', key)
        except Exception as error:
            if missing_ok and _missing(error):
                return None
            raise
        stream = response['Body']
        try:
            length = response.get('ContentLength')
            if length is not None and (type(length) is not int or not 0 <= length <= limit):
                raise CacheHold('cloud cache body exceeds captured bound')
            raw = bytearray()
            while len(raw) <= limit:
                self._check()
                chunk = stream.read(min(65536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > limit or (length is not None and len(raw) != length):
                raise CacheHold('cloud cache body length differs')
            return bytes(raw)
        finally:
            stream.close()

    def _put(self, key, raw, mutable=False, create_only=False):
        body = io.BytesIO(raw)
        wrapper = getattr(self.guard, 'wrap_body', None)
        wrapped = wrapper(body) if wrapper else body
        try:
            options = {} if mutable and not create_only else {'IfNoneMatch': '*'}
            self._request('put_object', key, Body=wrapped, ContentLength=len(raw),
                          ContentType='application/json; charset=utf-8',
                          CacheControl=POINTER_CACHE if mutable else IMMUTABLE, **options)
        finally:
            body.close()

    def _graph(self, slot):
        row = self.db.execute('SELECT raw,phase FROM cdn_cache_graph WHERE slot=?', (slot,)).fetchone()
        if row is None:
            return None
        raw = bytes(row[0])
        index, identities = _index(raw, self.config)
        return {'raw': raw, 'phase': row[1], 'index': index,
                'identities': {self.prefix + key: value for key, value in identities.items()}}

    def _row(self, key):
        return self.db.execute('SELECT sha,size,status,owned FROM cdn_cache_objects WHERE key=?', (key,)).fetchone()

    def _record(self, key, raw, status, owned):
        expected = (_sha(raw), len(raw))
        row = self._row(key)
        if row is not None and row[:2] != expected:
            raise CacheHold('immutable cache key conflicts with durable identity')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cdn_cache_objects VALUES(?,?,?,?,?)', (key, *expected, status, owned))

    def _capacity(self, proposed=None, pointer_bytes=0):
        rows = {key: size for key, size in self.db.execute('SELECT key,size FROM cdn_cache_objects')}
        graphs = [g for g in (self._graph('current'), self._graph('pending')) if g]
        for graph in graphs:
            for key, identity in graph['identities'].items():
                rows.setdefault(key, identity[1])
        for key, identity in (proposed or {}).items():
            old = self._row(key)
            if old is not None and old[:2] != identity:
                raise CacheHold('immutable cache key conflicts with durable identity')
            rows[key] = identity[1]
        # The mutable pointer also occupies one captured index, overwritten in place.
        pointer_bytes = max([pointer_bytes] + [len(g['raw']) for g in graphs])
        auxiliary_bytes = sum(max(len(current or b''), len(pending or b''))
                              for current, pending in self.db.execute('SELECT current,pending FROM cdn_cache_aux'))
        if len(rows) > self.config['max_owned_keys'] or sum(rows.values()) + pointer_bytes + auxiliary_bytes > self.config['max_cache_bytes']:
            raise CacheHold('tracked frontend cache key/byte allowance exhausted')

    def prepare(self, candidate_dir):
        root = Path(candidate_dir)
        if not root.is_absolute():
            root = Path.cwd() / root
        if root.resolve(strict=True) != root:
            raise ValueError('candidate root must be canonical and unlinked')
        raw = _file(root, 'snapshot-index.json', self.config['max_index_bytes'], self._check)
        index, identities = _index(raw, self.config)
        bodies = {}
        for name, (sha, size) in identities.items():
            body = _file(root, name, size, self._check)
            if len(body) != size or _sha(body) != sha:
                raise ValueError('candidate bytes/hash differ from captured index')
            if OBJECT.fullmatch(name):
                value = _json(body)
                if not isinstance(value, dict) or 'snapshot' in value or 'as_of' in value:
                    raise ValueError('candidate data must use stable schema-2 payloads')
            elif body != raw:
                raise ValueError('candidate manifest differs from captured index')
            bodies[self.prefix + name] = body
        pending = self._graph('pending')
        if pending:
            if pending['raw'] != raw:
                raise CacheHold('finish the frozen in-flight candidate before preparing another')
            return index
        current = self._graph('current')
        if current and current['raw'] == raw:
            return index
        proposed = {self.prefix + key: identity for key, identity in identities.items()}
        if any(self._row(key) and self._row(key)[2] == 'deleting' for key in proposed):
            raise CacheHold('resolve uncertain deletion before referencing that cache key')
        self._capacity(proposed, len(raw))
        with self.db:
            if self.db.execute('SELECT COUNT(*) FROM cdn_cache_pending').fetchone()[0]:
                raise CacheHold('unbound pending payloads require investigation')
            self.db.execute("INSERT INTO cdn_cache_graph VALUES('pending',?,'prepared')", (raw,))
            self.db.executemany('INSERT INTO cdn_cache_pending VALUES(?,?)', bodies.items())
        return index

    def _frozen(self, graph):
        bodies = {key: bytes(body) for key, body in self.db.execute('SELECT key,body FROM cdn_cache_pending')}
        if set(bodies) != set(graph['identities']):
            raise CacheHold('in-flight payload inventory differs from frozen graph')
        for key, (sha, size) in graph['identities'].items():
            if len(bodies[key]) != size or _sha(bodies[key]) != sha:
                raise CacheHold('frozen candidate byte proof differs')
        return bodies

    def _ensure(self, key, raw):
        row = self._row(key)
        if row is not None:
            if row[:2] != (_sha(raw), len(raw)) or row[2] == 'deleting':
                raise CacheHold('cache proof conflicts or deletion remains uncertain')
            if row[2] == 'verified':
                return
        existing = self._get(key, len(raw), missing_ok=True)
        if existing is not None:
            if existing != raw:
                raise CacheHold('existing immutable cloud cache bytes conflict')
            self._record(key, raw, 'verified', row[3] if row else 0)
            return
        # Ownership and uncertain bytes are committed before a conditional PUT.
        self._record(key, raw, 'putting', 1)
        self._put(key, raw)
        if self._get(key, len(raw)) != raw:
            raise CacheHold('uploaded cache bytes failed full GET verification')
        self._record(key, raw, 'verified', 1)

    def _promote(self, pending):
        for key, identity in pending['identities'].items():
            row = self._row(key)
            if row is None or row[:2] != identity or row[2] != 'verified':
                raise CacheHold('current graph lacks a verified data proof')
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cdn_cache_graph VALUES('current',?,'selected')", (pending['raw'],))
            self.db.execute("DELETE FROM cdn_cache_graph WHERE slot='pending'")
            self.db.execute('DELETE FROM cdn_cache_pending')

    def recover(self):
        """Resolve pointer uncertainty; never overwrite a missing/foreign pointer."""
        current, pending = self._graph('current'), self._graph('pending')
        remote = self._get(self.pointer, self.config['max_index_bytes'], missing_ok=True)
        if pending and remote == pending['raw']:
            if pending['phase'] != 'pointer':
                raise CacheHold('unexpected pointer selects a candidate with no durable PUT admission')
            bodies = self._frozen(pending)
            for key, raw in bodies.items():
                row = self._row(key)
                if row is None or row[2] == 'deleting' or self._get(key, len(raw)) != raw:
                    raise CacheHold('selected in-flight graph failed recovery GET verification')
                self._record(key, raw, 'verified', row[3])
            self._promote(pending)
            return 'selected'
        if current and remote == current['raw']:
            return 'pending' if pending else 'selected'
        if current is None and remote is None:
            return 'pending' if pending else 'empty'
        raise CacheHold('cloud pointer is missing or outside the durable current/in-flight graph')

    def adopt_current(self, *, allow_adoption=False):
        """Explicit cold handover: verify and claim only the actual current closure."""
        if allow_adoption is not True:
            raise ValueError('operator must explicitly authorize current-cache adoption')
        if (self._graph('current') or self._graph('pending') or
                self.db.execute('SELECT COUNT(*) FROM cdn_cache_objects').fetchone()[0] or
                self.db.execute('SELECT COUNT(*) FROM cdn_cache_pending').fetchone()[0]):
            raise CacheHold('current-cache adoption requires empty helper state')
        raw = self._get(self.pointer, self.config['max_index_bytes'])
        index, identities = _index(raw, self.config)
        identities = {self.prefix + key: value for key, value in identities.items()}
        self._capacity(identities, len(raw))
        verified = []
        for key, (sha, size) in identities.items():
            body = self._get(key, size)
            if len(body) != size or _sha(body) != sha:
                raise CacheHold('adopted current data failed full GET verification')
            verified.append((key, sha, size, 'verified', 1))
        if self._get(self.pointer, self.config['max_index_bytes']) != raw:
            raise CacheHold('current pointer changed during adoption')
        with self.db:
            self.db.executemany('INSERT INTO cdn_cache_objects VALUES(?,?,?,?,?)', verified)
            self.db.execute("INSERT INTO cdn_cache_graph VALUES('current',?,'selected')", (raw,))
        return index

    def publish(self, candidate_dir=None):
        self.recover()
        self._capacity()
        if candidate_dir is not None:
            self.cleanup()  # Drain owned backlog before new allocation admission.
            self.prepare(candidate_dir)
        pending = self._graph('pending')
        if pending:
            bodies = self._frozen(pending)
            # Data first; generation manifest last; pointer after every full GET proof.
            ordered = sorted(bodies, key=lambda key: ('/generations/' in key, key))
            for key in ordered:
                self._ensure(key, bodies[key])
            self.recover()  # Fresh current-pointer check before the mutable PUT.
            if self._graph('pending') is None:
                self.cleanup()
                return self._graph('current')['index']
            with self.db:
                self.db.execute("UPDATE cdn_cache_graph SET phase='pointer' WHERE slot='pending'")
            self._put(self.pointer, pending['raw'], mutable=True)
            if self._get(self.pointer, self.config['max_index_bytes']) != pending['raw']:
                raise CacheHold('pointer PUT outcome differs; recovery is required')
            self._promote(pending)
        current = self._graph('current')
        if current is None:
            raise CacheHold('no prepared or selected current cache')
        self.cleanup()
        return current['index']

    def cleanup(self):
        if self.config['cleanup_current_cache'] is not True:
            return 0
        self.recover()  # Never sweep from stale local current-pointer knowledge.
        protected = set()
        for slot in ('current', 'pending'):
            graph = self._graph(slot)
            if graph:
                protected.update(graph['identities'])
        retired = 0
        rows = self.db.execute('SELECT key,sha,size,status,owned FROM cdn_cache_objects ORDER BY key').fetchall()
        for key, sha, size, status, owned in rows:
            self._check()
            if key in protected:
                continue
            if retired >= self.config['max_cleanup_keys']:
                break
            suffix = key[len(self.prefix):] if key.startswith(self.prefix) else ''
            if not (OBJECT.fullmatch(suffix) or re.fullmatch(r'generations/[A-Za-z0-9_-]{1,80}/manifest\.json', suffix)):
                raise CacheHold('ledger contains an unowned cache namespace key')
            if not owned:
                with self.db:
                    self.db.execute('DELETE FROM cdn_cache_objects WHERE key=?', (key,))
                retired += 1  # Forget proof; never delete an unknown pre-existing key.
                continue
            if status == 'deleting':
                try:
                    self._request('head_object', key)
                except Exception as error:
                    if not _missing(error):
                        raise
                    with self.db:
                        self.db.execute('DELETE FROM cdn_cache_objects WHERE key=?', (key,))
                    retired += 1
                    continue
            else:
                with self.db:
                    self.db.execute("UPDATE cdn_cache_objects SET status='deleting' WHERE key=?", (key,))
            self._request('delete_object', key)
            try:
                self._request('head_object', key)
            except Exception as error:
                if not _missing(error):
                    raise
                with self.db:
                    self.db.execute('DELETE FROM cdn_cache_objects WHERE key=?', (key,))
                retired += 1
            else:
                raise CacheHold('cache deletion is not yet confirmed absent; proof remains quarantined')
        return retired

    def status(self):
        """Local allocation evidence for the caller's joint resource admission."""
        current, pending = self._graph('current'), self._graph('pending')
        counts = {name: 0 for name in ('putting', 'verified', 'deleting')}
        sizes = {}
        for key, byte_count, state in self.db.execute('SELECT key,size,status FROM cdn_cache_objects'):
            counts[state] += 1
            sizes[key] = byte_count
        proof_keys = len(sizes)
        for graph in (current, pending):
            if graph:
                for key, identity in graph['identities'].items():
                    sizes.setdefault(key, identity[1])
        pointer_bytes = max([len(g['raw']) for g in (current, pending) if g] or [0])
        auxiliary_bytes = sum(max(len(current or b''), len(inflight or b''))
                              for current, inflight in self.db.execute('SELECT current,pending FROM cdn_cache_aux'))
        return {'current_generation': current['index']['generation'] if current else None,
                'pending_generation': pending['index']['generation'] if pending else None,
                'tracked_keys': len(sizes), 'proof_keys': proof_keys,
                'reserved_unwritten_keys': len(sizes) - proof_keys,
                'tracked_cache_bytes': sum(sizes.values()) + pointer_bytes + auxiliary_bytes,
                'auxiliary_cache_bytes': auxiliary_bytes,
                'auxiliary_pending_keys': [key[len(self.prefix):] for (key,) in self.db.execute('SELECT key FROM cdn_cache_aux WHERE pending IS NOT NULL ORDER BY key')],
                'frozen_candidate_bytes': self.db.execute('SELECT COALESCE(SUM(length(body)),0) FROM cdn_cache_pending').fetchone()[0],
                'object_states': counts, 'cleanup_enabled': self.config['cleanup_current_cache']}

    def _aux_key(self, relative_key):
        if relative_key not in self.config['auxiliary_keys']:
            raise ValueError('auxiliary key is outside the operator allowlist')
        return self.prefix + relative_key

    def _aux_row(self, key):
        row = self.db.execute('SELECT current,pending,phase,current_sha,current_size,pending_sha,pending_size FROM cdn_cache_aux WHERE key=?', (key,)).fetchone()
        if row:
            for body, sha, size in ((row[0], row[3], row[4]), (row[1], row[5], row[6])):
                if ((body is None and (sha is not None or size is not None)) or
                        (body is not None and (type(size) is not int or len(body) != size or _sha(body) != sha))):
                    raise CacheHold('durable auxiliary bytes/hash identity differs')
        return row[:3] if row else None

    def adopt_auxiliary(self, relative_key, *, allow_adoption=False):
        """Explicitly claim one existing stable JSON cache value by actual GET."""
        key = self._aux_key(relative_key)
        if allow_adoption is not True or self.db.execute('SELECT 1 FROM cdn_cache_aux WHERE key=?', (key,)).fetchone():
            raise CacheHold('auxiliary adoption requires explicit authorization and empty key state')
        raw = self._get(key, MIB)
        if not isinstance(_json(raw), dict):
            raise ValueError('auxiliary cache must be a JSON object')
        if self.db.execute('SELECT COUNT(*) FROM cdn_cache_aux').fetchone()[0] >= 8 or self.status()['tracked_cache_bytes'] + len(raw) > self.config['max_cache_bytes']:
            raise CacheHold('auxiliary cache exceeds tracked byte allowance')
        with self.db:
            self.db.execute("INSERT INTO cdn_cache_aux VALUES(?,?,NULL,'selected',?,?,NULL,NULL)", (key, raw, _sha(raw), len(raw)))
        return _sha(raw)

    def publish_auxiliary(self, relative_key, body):
        """One current stable JSON value plus bounded frozen PUT uncertainty.

        This cache is independent of the schema-2 snapshot. Browser freshness
        semantics for descriptions must not pretend it shares the snapshot clock.
        Snapshot cleanup never targets auxiliary keys.
        """
        key = self._aux_key(relative_key)
        if not isinstance(body, bytes) or len(body) > MIB or not isinstance(_json(body), dict):
            raise ValueError('auxiliary cache requires a JSON object of at most 1 MiB')
        row = self._aux_row(key)
        current, pending, phase = row if row else (None, None, 'selected')
        if row is None and self.db.execute('SELECT COUNT(*) FROM cdn_cache_aux').fetchone()[0] >= 8:
            raise CacheHold('auxiliary cache key allowance exhausted')
        current, pending = (bytes(current) if current is not None else None,
                            bytes(pending) if pending is not None else None)
        if pending is None and current == body:
            return _sha(body)
        remote = self._get(key, MIB, missing_ok=True)
        if pending is not None:
            if remote == pending and phase == 'pointer':
                with self.db:
                    self.db.execute("UPDATE cdn_cache_aux SET current=pending,current_sha=pending_sha,current_size=pending_size,pending=NULL,pending_sha=NULL,pending_size=NULL,phase='selected' WHERE key=?", (key,))
                if body != pending:
                    raise CacheHold('recovered auxiliary value; publish the newer value in a separate admission')
                return _sha(pending)
            if body != pending:
                raise CacheHold('finish the frozen auxiliary value before replacing it')
        elif remote != current:
            raise CacheHold('unknown stable auxiliary cache requires explicit adoption')
        if remote != current:
            raise CacheHold('auxiliary cloud value is outside durable selected/in-flight state')
        old_bytes = max(len(current or b''), len(pending or b''))
        if self.status()['tracked_cache_bytes'] - old_bytes + max(len(current or b''), len(body)) > self.config['max_cache_bytes']:
            raise CacheHold('auxiliary candidate exceeds tracked cache allocation')
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cdn_cache_aux VALUES(?,?,?,'pointer',?,?,?,?)", (key, current, body, _sha(current) if current is not None else None, len(current) if current is not None else None, _sha(body), len(body)))
        # Preserve a cold key against an uncoordinated existing object.
        self._put(key, body, mutable=True, create_only=current is None)
        if self._get(key, MIB) != body:
            raise CacheHold('auxiliary PUT requires recovery after failed GET verification')
        with self.db:
            self.db.execute("UPDATE cdn_cache_aux SET current=pending,current_sha=pending_sha,current_size=pending_size,pending=NULL,pending_sha=NULL,pending_size=NULL,phase='selected' WHERE key=?", (key,))
        return _sha(body)

    def resume_auxiliary(self, relative_key):
        """Resolve an admitted frozen value before reading newer operator input."""
        row = self._aux_row(self._aux_key(relative_key))
        if row is None or row[1] is None:
            return False
        self.publish_auxiliary(relative_key, bytes(row[1]))
        return True


def publish_current(candidate_dir, client, ledger, config, guard, *, outer_lock_held):
    """Publish/resume a candidate using only caller-supplied connections/authority."""
    return CurrentCachePublisher(client, ledger, config, guard,
                                 outer_lock_held=outer_lock_held).publish(candidate_dir)
