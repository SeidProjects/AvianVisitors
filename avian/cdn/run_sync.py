#!/usr/bin/env python3
"""Optional direct recorder-to-CDN media sync and rolling frontend cache.

One process, state lock, durable ledger and SDK attempt guard cover each cycle.
All eligible configured media is queued, independently of the frontend's
display windows. Exported data exposes only verified public media identities.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

import export_snapshots as exporter
from notify_updates import NotifyConfig, notify_current
from publish_cache import CacheHold, CurrentCachePublisher
import sync_media as media_sync

MIB = 1024 * 1024


class CacheGuard:
    """Join the SDK guard while admitting cache SQLite writes to the same quota."""
    def __init__(self, sync, ledger, shared):
        self.sync, self.ledger, self.shared = sync, ledger, shared

    def check(self):
        self.shared.check()
        media_sync.check_state_budget(self.sync, self.ledger)
        self.shared.check()

    def wrap_body(self, body):
        return self.shared.wrap_body(body)


def _path(value, label):
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_file():
        raise ValueError(label + ' must be an absolute unlinked regular file')
    return path


def _boolean(environment, name, default=False):
    value = environment.get('AVIAN_SYNC_' + name)
    if value is None:
        return default
    if value not in ('0', '1'):
        raise ValueError('invalid boolean sync setting: ' + name)
    return value == '1'


@dataclass(frozen=True)
class FrontendConfig:
    db_path: Path
    timezone: str
    image_map_file: object = None
    wiki_cache_file: object = None
    cache_prefix: str = 'data'
    cleanup_current_cache: bool = False
    fix_last_hour: bool = False
    seasonality: bool = False

    @classmethod
    def from_env(cls, environment, sync):
        path = _path(environment.get('AVIAN_SYNC_DB_PATH', ''), 'detections database')
        if sync.state_dir == path.parent or sync.state_dir in path.parents:
            raise ValueError('publisher state must be separate from the detections database')
        timezone = environment.get('AVIAN_SYNC_TIMEZONE')
        if not timezone:
            raise ValueError('the recorder timezone is required')
        ZoneInfo(timezone)
        optional = {}
        for name in ('IMAGE_MAP_FILE', 'WIKI_CACHE_FILE'):
            if environment.get('AVIAN_SYNC_' + name):
                optional[name.lower()] = _path(environment['AVIAN_SYNC_' + name], name)
        prefix = environment.get('AVIAN_SYNC_CACHE_PREFIX', 'data')
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', prefix):
            raise ValueError('invalid frontend cache prefix')
        return cls(path, timezone, cache_prefix=prefix,
                   cleanup_current_cache=_boolean(environment, 'CLEANUP_CURRENT_CACHE'),
                   fix_last_hour=_boolean(environment, 'FIX_LAST_HOUR'),
                   seasonality=_boolean(environment, 'SEASONALITY'), **optional)

    def cache_config(self, sync):
        return dict(endpoint_url=sync.endpoint_url, bucket=sync.bucket,
                    media_origin=sync.public_origin, cache_prefix=self.cache_prefix,
                    single_publisher=True, cleanup_current_cache=self.cleanup_current_cache,
                    auxiliary_keys=['descriptions.json'] if self.wiki_cache_file else [])


def read_json(path, limit, guard):
    """Bounded unlinked operator input; never include its private fields verbatim."""
    guard.check()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('operator JSON exceeds file/byte bounds')
        raw = stream.read(limit + 1)
        guard.check()
        after = os.fstat(stream.fileno())
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(raw) > limit or signature(before) != signature(after):
            raise ValueError('operator JSON changed while reading')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate operator JSON key')
            result[key] = value
        return result
    result = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('operator JSON must be an object')
    return result


def source_reference(value, root_label, allowed_roots=None):
    allowed = allowed_roots or {root_label}
    if not isinstance(value, dict) or value.get('root', root_label) not in allowed:
        raise ValueError('source reference has an invalid root')
    relative = value.get('path')
    if (not isinstance(relative, str) or not relative or
            PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts or
            str(PurePosixPath(relative)) != relative):
        raise ValueError('source reference requires a safe relative path')
    return relative


def validate_mapping(value, image_roots=None):
    if set(value) - {'images', 'recordings'}:
        raise ValueError('mapping accepts only images and recordings')
    for name in ('images', 'recordings'):
        if not isinstance(value.get(name, {}), dict):
            raise ValueError('source mapping must contain object maps')
    for sci, poses in value.get('images', {}).items():
        if not isinstance(sci, str) or not sci or not isinstance(poses, dict) or set(poses) - {'pose1', 'pose2'}:
            raise ValueError('invalid scientific name/image pose mapping')
        for reference in poses.values():
            source_reference(reference, 'images', image_roots)
    for name, reference in value.get('recordings', {}).items():
        if not isinstance(name, str) or not name:
            raise ValueError('invalid recording mapping name')
        source_reference(reference, 'audio')
    return value


def snapshot_requirements(responses):
    wanted, latest = set(), {}
    for name, response in responses.items():
        if name.startswith('species/'):
            for row in response['detections']:
                if row.get('file'):
                    wanted.add(row['file'])
            latest[response['sci']] = [row['file'] for row in response['detections'] if row.get('file')]
        elif name.startswith('recent/'):
            wanted.update(row['top_file'] for row in response['species'] if row.get('top_file'))
    return wanted, latest


def media_manifest(receipts, responses, mapping, generation):
    """Bind current verified receipts; ambiguous filenames require an explicit map."""
    wanted, latest_candidates = snapshot_requirements(responses)
    audio, artwork = {}, {}
    for receipt in receipts:
        entry = {key: receipt[key] for key in ('url', 'sha256', 'bytes')}
        label, relative = receipt['root_label'], receipt['relative_path']
        if label == 'audio':
            names = {relative, PurePosixPath(relative).name} & wanted
            for name in names:
                reference = mapping.get('recordings', {}).get(name)
                if reference and source_reference(reference, 'audio') != relative:
                    continue
                previous = audio.get(name)
                if previous and previous != entry:
                    raise ValueError('ambiguous recording name requires an explicit source mapping')
                audio[name] = entry
        else:
            artwork[(label, relative)] = entry
    images = {}
    for sci, poses in mapping.get('images', {}).items():
        for pose, reference in poses.items():
            label = reference.get('root', 'images')
            entry = artwork.get((label, source_reference(reference, 'images', {label})))
            if entry:
                entry = dict(entry)
                if reference.get('source') in ('illustrations', 'cutouts', 'generated'):
                    entry['source'] = reference['source']
                images.setdefault(sci, {})[pose] = entry
    latest = {}
    for sci, candidates in latest_candidates.items():
        name = next((name for name in candidates if name in audio), None)
        if name:
            latest[sci] = {'file': name}
    return dict(schema_version=1, generation=generation, recordings=audio,
                latest_recording=latest, images=images)


def apply_media(responses, manifest):
    for name, response in responses.items():
        response['snapshot']['media_manifest_generation'] = manifest['generation']
        if name == 'lifelist' or name.startswith('recent/'):
            for row in response['species']:
                row.update(exporter.species_media(manifest, row['sci']))
                if 'top_file' in row:
                    row['top_media'] = exporter.recording_media(manifest, row['top_file'])
        elif name.startswith('species/'):
            response.update(exporter.species_media(manifest, response['sci']))
            for row in response['detections']:
                row['media'] = exporter.recording_media(manifest, row['file'])


def wiki_payload(value):
    """Public description fields only; never export arbitrary local cache metadata."""
    species = value.get('species')
    if not isinstance(species, dict) or len(species) > 1000:
        raise ValueError('description input requires a bounded species map')
    public = {}
    for sci, entry in species.items():
        if not isinstance(sci, str) or not 1 <= len(sci) <= 200 or not isinstance(entry, dict):
            raise ValueError('invalid description entry')
        extract = entry.get('extract')
        if not isinstance(extract, str) or len(extract.encode('utf-8')) > 32768:
            raise ValueError('invalid description extract')
        output = {'extract': extract}
        title = entry.get('title')
        if isinstance(title, str) and len(title) <= 500:
            output['title'] = title
        url = entry.get('url')
        if isinstance(url, str):
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
                    parsed.query or parsed.fragment or len(url) > 2048):
                raise ValueError('unsafe public description link')
            output['url'] = url
        public[sci] = output
    raw = exporter._json_bytes({'schema_version': 1, 'species': public})
    if len(raw) > MIB:
        raise ValueError('public descriptions exceed 1 MiB')
    return raw


def cleanup_candidates(sync, ledger):
    """Remove only inode-bound, publisher-owned temporary exports after a crash."""
    with ledger:
        ledger.execute('CREATE TABLE IF NOT EXISTS cdn_export_temporary(name TEXT PRIMARY KEY,dev INTEGER,ino INTEGER,uid INTEGER)')
    rows = ledger.execute('SELECT name,dev,ino,uid FROM cdn_export_temporary').fetchall()
    if len(rows) > 1:
        raise ValueError('temporary export inventory exceeds the single-candidate bound')
    mounts = media_sync._mount_points()
    for name, dev, ino, uid in rows:
        if not re.fullmatch(r'\.candidate-[A-Za-z0-9_-]{1,64}', name):
            raise ValueError('invalid owned temporary export name')
        root = sync.state_dir / name
        if root.exists() or root.is_symlink():
            info = root.lstat()
            if (not stat.S_ISDIR(info.st_mode) or str(root) in mounts or os.path.ismount(root) or
                    (info.st_dev, info.st_ino, info.st_uid) != (dev, ino, uid)):
                raise ValueError('owned temporary export identity changed')
            count, total = 0, 0
            for directory, names, files in os.walk(root, followlinks=False):
                for child in names + files:
                    count += 1
                    full = Path(directory) / child
                    value = full.lstat()
                    if (count > 2048 or value.st_dev != dev or value.st_uid != uid or
                            str(full) in mounts or os.path.ismount(full) or
                            not (stat.S_ISDIR(value.st_mode) or
                                 stat.S_ISREG(value.st_mode) and value.st_nlink == 1)):
                        raise ValueError('unsafe owned temporary export tree')
                    total += max(value.st_size, getattr(value, 'st_blocks', 0) * 512)
                    if total > 8 * MIB:
                        raise ValueError('owned temporary export exceeds cleanup byte bound')
            fresh = root.lstat()
            if (fresh.st_dev, fresh.st_ino, fresh.st_uid) != (dev, ino, uid):
                raise ValueError('temporary export changed during inspection')
            shutil.rmtree(root)
        with ledger:
            ledger.execute('DELETE FROM cdn_export_temporary WHERE name=?', (name,))


@contextmanager
def candidate_output(sync, ledger):
    cleanup_candidates(sync, ledger)
    root = Path(tempfile.mkdtemp(prefix='.candidate-', dir=sync.state_dir))
    info = root.lstat()
    try:
        with ledger:
            ledger.execute('INSERT INTO cdn_export_temporary VALUES(?,?,?,?)',
                           (root.name, info.st_dev, info.st_ino, info.st_uid))
    except Exception:
        root.rmdir()
        raise
    try:
        yield root
    finally:
        # Frozen cloud publication bytes live in the one pending SQLite record.
        # An interrupted local export can be discarded and recaptured next time.
        cleanup_candidates(sync, ledger)


def prepare_frontend(sync, config, ledger, guard, publisher, *, instant=None):
    guard.check()
    mapping = validate_mapping(read_json(config.image_map_file, MIB, guard), set(sync.roots) - {'audio'}) if config.image_map_file else {}
    instant = instant or dt.datetime.now(dt.timezone.utc)
    generation = uuid.uuid4().hex
    db = sqlite3.connect(config.db_path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    # Interrupt a long read-only query when the caller's shared guard expires.
    def progress():
        try:
            guard.check()
            return 0
        except media_sync.BudgetStop:
            return 1
    db.set_progress_handler(progress, 10000)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(detections)')}
        if not exporter.REQUIRED_COLUMNS <= columns:
            raise ValueError('unsupported detections database schema')
        responses, paths, metadata = exporter.collect_snapshot(db, instant, ZoneInfo(config.timezone),
                                                              generation, fix_last_hour=config.fix_last_hour,
                                                              seasonality=config.seasonality, guard=guard)
    finally:
        db.rollback()
        db.close()
    wanted, _ = snapshot_requirements(responses)
    artwork_paths = {source_reference(ref, 'images', set(sync.roots) - {'audio'}) for poses in mapping.get('images', {}).values()
                     for ref in poses.values()}
    def eligible(kind, relative):
        return (kind == 'audio' and (relative in wanted or PurePosixPath(relative).name in wanted) or
                kind == 'image' and relative in artwork_paths)
    receipts = media_sync.iter_verified_receipts(sync, ledger, outer_lock_held=True,
                                                predicate=eligible, guard=guard)
    manifest = media_manifest(receipts, responses, mapping, generation)
    apply_media(responses, manifest)
    metadata['media_manifest_generation'] = generation
    guard.check()
    # Reserve the local exporter tree plus the frozen SQLite body growth before
    # either write. The common state lock prevents overlapping admissions.
    media_sync.check_state_budget(sync, ledger, guard, additional_bytes=12 * MIB)
    # The owned temporary directory exists only until its exact bytes are frozen.
    # No local history or snapshot backup directory accumulates between cycles.
    with candidate_output(sync, ledger) as temporary:
        exporter.publish_local(temporary, generation, responses, paths, metadata,
                               max_output_bytes=8 * MIB, min_free_disk_bytes=sync.min_free_bytes,
                               max_candidate_bytes=4 * MIB)
        guard.check()
        publisher.prepare(temporary)
    return generation


def run_cycle(sync, config, client, ledger, guard):
    """Caller holds the single media state lock; all calls share its SDK guard."""
    cleanup_candidates(sync, ledger)
    publisher = CurrentCachePublisher(client, ledger, config.cache_config(sync), CacheGuard(sync, ledger, guard), outer_lock_held=True)
    before = publisher.status()
    if before['pending_generation']:
        # Finish exact frozen bytes before new uploads/export, preventing starvation.
        publisher.publish()
        return dict(status='cache-resumed', cache=publisher.status(), attempts=guard.attempts)
    if before.get('auxiliary_pending_keys'):
        for key in before['auxiliary_pending_keys']:
            publisher.resume_auxiliary(key)
        return dict(status='descriptions-resumed', cache=publisher.status(), attempts=guard.attempts)
    if before['current_generation']:
        publisher.cleanup()
        if config.wiki_cache_file:
            media_sync.check_state_budget(sync, ledger, guard, additional_bytes=MIB)
            publisher.publish_auxiliary('descriptions.json', wiki_payload(read_json(config.wiki_cache_file, MIB, guard)))
    result = media_sync.run_batch(sync, client, ledger, guard, outer_lock_held=True)
    prepare_frontend(sync, config, ledger, guard, publisher)
    publisher.publish()
    if config.wiki_cache_file and not before['current_generation']:
        media_sync.check_state_budget(sync, ledger, guard, additional_bytes=MIB)
        publisher.publish_auxiliary('descriptions.json', wiki_payload(read_json(config.wiki_cache_file, MIB, guard)))
    return dict(status='complete', media=result, cache=publisher.status(), attempts=guard.attempts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    parser.add_argument('--dry-run', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--loop', action='store_true')
    parser.add_argument('--adopt-current-cache', action='store_true',
                        help='explicitly verify/adopt existing current cache under exclusive authority')
    args = parser.parse_args(argv)
    if args.adopt_current_cache and (args.loop or args.dry_run):
        parser.error('cache adoption is a separate one-time admission')
    environment = media_sync.load_env_file(args.env_file) if args.env_file else {}
    environment.update(os.environ)
    try:
        sync = media_sync.Config.from_env(environment, require_credentials=not args.dry_run)
        if sync is None:
            print(json.dumps({'status': 'disabled'}))
            return 0
        config = FrontendConfig.from_env(environment, sync)
        notifications = NotifyConfig.from_env(environment)
        if args.dry_run:
            print(json.dumps(dict(media_sync.dry_scan(sync), status='read-only')))
            return 0
        client = media_sync.make_client(sync, credentials=environment)
    except (ValueError, OSError):
        parser.exit(1, 'sync configuration failed; check private server settings\n')
    try:
        while True:
            delay = sync.interval_seconds
            try:
                with media_sync.state_lock(sync):
                    ledger = media_sync.open_ledger(sync, outer_lock_held=True)
                    guard = media_sync.SyncGuard(sync)
                    try:
                        guard.attach(client)
                        if args.adopt_current_cache:
                            media_sync.check_state_budget(sync, ledger, guard, additional_bytes=8 * MIB)
                            publisher = CurrentCachePublisher(client, ledger, config.cache_config(sync), CacheGuard(sync, ledger, guard), outer_lock_held=True)
                            status = publisher.status()
                            if status['pending_generation']:
                                raise CacheHold('finish the frozen candidate before adoption')
                            if status['current_generation']:
                                publisher.recover()
                            else:
                                publisher.adopt_current(allow_adoption=True)
                            if config.wiki_cache_file:
                                key = config.cache_prefix + '/descriptions.json'
                                if ledger.execute('SELECT 1 FROM cdn_cache_aux WHERE key=?', (key,)).fetchone() is None:
                                    publisher.adopt_auxiliary('descriptions.json', allow_adoption=True)
                            result = dict(status='current-cache-adopted', cache=publisher.status(), attempts=guard.attempts)
                        else:
                            result = run_cycle(sync, config, client, ledger, guard)
                            result['updates'] = notify_current(sync, ledger, guard, notifications)
                            result['attempts'] = guard.attempts
                        print(json.dumps(result, sort_keys=True), flush=True)
                    finally:
                        guard.detach()
                        ledger.close()
            except Exception as error:
                # No arbitrary SDK errors/headers, private paths or values in logs.
                print(json.dumps({'status': 'held', 'reason': type(error).__name__}), flush=True)
                if not args.loop:
                    return 1
                delay = sync.backoff_seconds
            if not args.loop:
                return 0
            time.sleep(delay)
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
