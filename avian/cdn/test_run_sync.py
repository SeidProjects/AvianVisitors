import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import publish_cache
import run_sync
import sync_media
from test_sync_media import Clock, FakeS3, Missing


class IntegratedS3(FakeS3):
    """Shared SDK events and synthetic conditional/cache operations, no sockets."""
    def __init__(self):
        super().__init__()
        self.lost_reply_key = None
        self.failed_read_key = None
        self.closed = 0

    def put_object(self, **request):
        if request.get('IfNoneMatch') == '*' and request['Key'] in self.objects:
            self.meta.events.emit()
            self.calls.append(('PUT', request['Key']))
            error = RuntimeError('synthetic conditional conflict')
            error.response = {'ResponseMetadata': {'HTTPStatusCode': 412}, 'Error': {'Code': 'PreconditionFailed'}}
            raise error
        result = super().put_object(**request)
        if request['Key'] == self.lost_reply_key:
            self.lost_reply_key = None
            raise TimeoutError('synthetic lost successful reply')
        return result

    def get_object(self, **request):
        result = super().get_object(**request)
        if request['Key'] == self.failed_read_key:
            self.failed_read_key = None
            result['Body'].close()
            raise TimeoutError('synthetic interrupted setup read')
        return result

    def head_object(self, **request):
        self.meta.events.emit()
        self.calls.append(('HEAD', request['Key']))
        if request['Key'] not in self.objects:
            raise Missing()
        return {'ContentLength': len(self.objects[request['Key']][0])}

    def delete_object(self, **request):
        self.meta.events.emit()
        self.calls.append(('DELETE', request['Key']))
        self.objects.pop(request['Key'], None)
        return {}

    def close(self):
        self.closed += 1


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.audio, self.images, self.state = [self.root / name for name in ('audio', 'images', 'state')]
        for path in (self.audio, self.images, self.state):
            path.mkdir(mode=0o700)
        self.source_db = self.root / 'observations.sqlite'
        self.writer = sqlite3.connect(self.source_db)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA journal_mode=WAL')
        self.writer.execute('CREATE TABLE detections(Date TEXT,Time TEXT,Sci_Name TEXT,Com_Name TEXT,Confidence REAL,File_Name TEXT)')
        self.sync = sync_media.Config('https://storage.example', 'synthetic-media', 'https://media.example',
                                      self.audio, self.images, self.state, min_free_bytes=16 * sync_media.MIB)
        self.front = run_sync.FrontendConfig(self.source_db, 'UTC', cleanup_current_cache=True)
        self.ledger = sync_media.open_ledger(self.sync, outer_lock_held=True)
        self.addCleanup(self.ledger.close)
        self.client = IntegratedS3()
        self.sci = 'Synthetic species'
        self.guards = []

    def source(self, name, raw, kind='audio'):
        path = (self.audio if kind == 'audio' else self.images) / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def detection(self, name='clip.mp3', number=0, sci=None, conf=.8):
        seconds = number % 86400
        stamp = '%02d:%02d:%02d' % (seconds // 3600, seconds // 60 % 60, seconds % 60)
        self.writer.execute('INSERT INTO detections VALUES(?,?,?,?,?,?)',
                            ('2024-05-01', stamp, sci or self.sci, 'Synthetic bird', conf, name))
        self.writer.commit()

    def guard(self):
        clock = Clock()
        guard = sync_media.SyncGuard(self.sync, clock=clock, sleep=clock.sleep,
                                     resources=lambda: (3 * sync_media.GIB, sync_media.GIB))
        guard.attach(self.client)
        self.guards.append(guard)
        return guard

    def cycle(self):
        with sync_media.state_lock(self.sync):
            guard = self.guard()
            try:
                self.assertEqual(len(self.client.meta.events.callbacks), 1)
                before = len(self.client.calls)
                result = run_sync.run_cycle(self.sync, self.front, self.client, self.ledger, guard)
                self.assertEqual(result['attempts'], len(self.client.calls) - before)
                self.assertEqual(len(self.client.meta.events.callbacks), 1)
                return result
            finally:
                guard.detach()
        self.assertEqual(len(self.client.meta.events.callbacks), 0)

    def cache(self):
        index = json.loads(self.client.objects['data/snapshot-index.json'][0])
        for path, identity in index['objects'].items():
            raw = self.client.objects['data/' + path][0]
            self.assertEqual(len(raw), identity['bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), identity['sha256'])
        manifest = 'data/generations/' + index['generation'] + '/manifest.json'
        self.assertEqual(self.client.objects[manifest][0], self.client.objects['data/snapshot-index.json'][0])
        return index

    def payload(self, index, action, key=None):
        path = index['routes'][action]
        if key is not None:
            path = path[str(key)]
        return json.loads(self.client.objects['data/' + path][0])

    def public_inputs(self, mapping=None, wiki=None):
        paths = {}
        for name, value in (('image_map_file', mapping), ('wiki_cache_file', wiki)):
            if value is not None:
                path = self.root / (name + '.json')
                path.write_text(json.dumps(value), encoding='utf-8')
                paths[name] = path
        self.front = dataclasses.replace(self.front, **paths)
        return paths

    def environment(self):
        return {'AVIAN_SYNC_ENABLED': '1', 'AVIAN_SYNC_ENDPOINT_URL': self.sync.endpoint_url,
                'AVIAN_SYNC_BUCKET': self.sync.bucket, 'AVIAN_SYNC_PUBLIC_ORIGIN': self.sync.public_origin,
                'AVIAN_SYNC_AUDIO_ROOT': str(self.audio), 'AVIAN_SYNC_IMAGE_ROOT': str(self.images),
                'AVIAN_SYNC_STATE_DIR': str(self.state), 'AVIAN_SYNC_DB_PATH': str(self.source_db),
                'AVIAN_SYNC_TIMEZONE': 'UTC', 'AVIAN_SYNC_ACCESS_KEY_ID': 'synthetic-id',
                'AVIAN_SYNC_SECRET_ACCESS_KEY': 'synthetic-not-a-real-credential'}

    def test_integrated_cycle_joins_one_guard_and_publishes_coherent_cache(self):
        self.source('bird’s.mp3', b'unicode audio')
        self.detection('bird’s.mp3')
        result = self.cycle()
        index = self.cache()
        row = self.payload(index, 'species', self.sci)['detections'][0]
        self.assertTrue(row['media']['available'])
        self.assertEqual(row['media']['sha256'], hashlib.sha256(b'unicode audio').hexdigest())
        self.assertEqual(result['media']['verified'], 1)
        self.assertEqual(result['cache']['pending_generation'], None)
        self.assertEqual(self.client.meta.events.callbacks, {})

    def test_verified_audio_and_mapped_art_stay_on_cdn_after_local_retention(self):
        audio = self.source('clip.mp3', b'previously verified audio')
        image = self.source('pose.png', b'previously verified artwork', 'images')
        self.detection('clip.mp3')
        self.public_inputs(mapping={'images': {self.sci: {'pose1': {'root': 'images', 'path': 'pose.png'}}}})
        self.cycle()
        initial = self.payload(self.cache(), 'species', self.sci)
        media_keys = {key for key in self.client.objects if key.startswith(('audio/', 'images/'))}
        audio.unlink(); image.unlink()
        before = len(self.client.calls)
        with patch.object(sync_media.os, 'read', side_effect=AssertionError('retained media rehash')):
            self.cycle()
        current = self.payload(self.cache(), 'species', self.sci)
        self.assertEqual(current['detections'][0]['media'], initial['detections'][0]['media'])
        self.assertTrue(current['detections'][0]['media']['available'])
        self.assertEqual(current['media_images'], initial['media_images'])
        self.assertTrue(current['media_images']['pose1']['available'])
        self.assertTrue(media_keys <= self.client.objects.keys())
        self.assertFalse(any(key.startswith(('audio/', 'images/')) for method, key in self.client.calls[before:]))

    def test_all_historical_audio_and_unmapped_images_progress_outside_modal500(self):
        for number in range(505):
            name = 'historical-%04d.mp3' % number
            self.source(name, ('audio-%04d' % number).encode())
            self.detection(name, number=number, conf=.99 if number == 504 else .5)
        self.source('unmapped.jpg', b'unmapped image', 'images')
        self.source('unmapped.webp', b'unmapped web image', 'images')
        for iteration in range(30):
            self.cycle()
            verified = self.ledger.execute("SELECT COUNT(*) FROM media_sync_sources WHERE status='verified'").fetchone()[0]
            if verified == 507:
                break
        self.assertEqual(verified, 507)
        index = self.cache()
        rows = self.payload(index, 'species', self.sci)['detections']
        self.assertEqual(len(rows), 500)
        self.assertNotIn('historical-0000.mp3', {row['file'] for row in rows})
        oldest_sha = hashlib.sha256(b'audio-0000').hexdigest()
        self.assertIn('audio/' + oldest_sha[:2] + '/' + oldest_sha + '.mp3', self.client.objects)
        self.assertEqual(len([key for key in self.client.objects if key.startswith('images/')]), 2)
        self.assertTrue(all(not key.startswith(('audio/', 'images/')) for method, key in self.client.calls if method == 'DELETE'))

    def test_unchanged_media_has_no_file_hash_reads_or_media_sdk_calls(self):
        self.source('clip.mp3', b'unchanged')
        self.detection()
        self.cycle()
        before = len(self.client.calls)
        with patch.object(sync_media.os, 'read', side_effect=AssertionError('unchanged media rehashed')):
            self.cycle()
        after = self.client.calls[before:]
        self.assertFalse(any(key.startswith(('audio/', 'images/')) for method, key in after))
        index = self.cache()
        self.assertTrue(self.payload(index, 'species', self.sci)['detections'][0]['media']['available'])

    def test_private_paths_mapping_extra_fields_and_wiki_metadata_never_become_public(self):
        self.source('nested/clip.mp3', b'audio')
        self.detection()
        self.source('pose.png', b'image', 'images')
        self.public_inputs(mapping={'images': {self.sci: {'pose1': {'root': 'images', 'path': 'pose.png',
                                                                 'source': 'illustrations', 'private': 'private-map-sentinel'}}}},
                           wiki={'schema_version': 1, 'local_path': 'private-root-sentinel',
                                 'species': {self.sci: {'extract': 'Synthetic bird description.', 'title': 'Synthetic bird',
                                                       'url': 'https://birds.example/description',
                                                       'private_path': 'private-wiki-sentinel'}}})
        self.cycle()
        index = self.cache()
        data = b'\n'.join(self.client.objects['data/' + path][0] for path in index['objects'])
        data += self.client.objects['data/descriptions.json'][0]
        for private in (str(self.root).encode(), b'private-map-sentinel', b'private-root-sentinel',
                        b'private-wiki-sentinel', b'nested/clip.mp3', b'pose.png', b'current_fingerprint'):
            self.assertNotIn(private, data)
        image = self.payload(index, 'lifelist')['species'][0]['media_images']['pose1']
        self.assertEqual(set(image), {'available', 'url', 'sha256', 'bytes', 'source'})
        wiki = json.loads(self.client.objects['data/descriptions.json'][0])
        self.assertEqual(set(wiki), {'schema_version', 'species'})
        self.assertEqual(set(wiki['species'][self.sci]), {'extract', 'title', 'url'})

    def test_basename_collision_holds_cache_until_explicit_operator_mapping(self):
        self.source('first/same.mp3', b'first')
        self.source('second/same.mp3', b'second')
        self.detection('same.mp3')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.cycle()
        self.assertNotIn('data/snapshot-index.json', self.client.objects)
        self.public_inputs(mapping={'recordings': {'same.mp3': {'root': 'audio', 'path': 'second/same.mp3'}}})
        self.cycle()
        index = self.cache()
        row = self.payload(index, 'species', self.sci)['detections'][0]
        self.assertEqual(row['media']['sha256'], hashlib.sha256(b'second').hexdigest())

    def test_frozen_snapshot_resumes_before_new_media_and_needs_no_temporary_candidate(self):
        self.source('clip.mp3', b'first')
        self.detection()
        self.client.lost_reply_key = 'data/snapshot-index.json'
        with self.assertRaises(TimeoutError):
            self.cycle()
        pending = self.ledger.execute("SELECT raw FROM cdn_cache_graph WHERE slot='pending'").fetchone()[0]
        self.assertFalse(list(self.state.glob('.candidate-*')))
        self.source('second.mp3', b'second')
        self.detection('second.mp3', number=1)
        before = len(self.client.calls)
        resumed = self.cycle()
        self.assertEqual(resumed['status'], 'cache-resumed')
        self.assertEqual(self.client.objects['data/snapshot-index.json'][0], pending)
        self.assertFalse(any(method == 'PUT' and key.startswith('audio/') for method, key in self.client.calls[before:]))
        self.cycle()
        index = self.cache()
        self.assertEqual(len(self.payload(index, 'species', self.sci)['detections']), 2)

    def test_current_only_cache_retirement_preserves_media_and_leaves_no_output_trees(self):
        self.source('clip.mp3', b'first')
        self.detection()
        self.cycle()
        first = self.cache()
        media_keys = {key for key in self.client.objects if key.startswith('audio/')}
        self.cycle()
        second = self.cache()
        self.assertNotEqual(first['generation'], second['generation'])
        self.assertNotIn('data/generations/' + first['generation'] + '/manifest.json', self.client.objects)
        self.assertEqual(len([key for key in self.client.objects if '/generations/' in key]), 1)
        self.assertTrue(media_keys <= set(self.client.objects))
        self.assertFalse(list(self.state.glob('.candidate-*')))
        self.assertFalse((self.state / 'snapshots').exists())
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM cdn_cache_graph').fetchone()[0], 1)

    def test_unavailable_media_keeps_db_rows_and_later_links_verified_source(self):
        self.detection()
        self.cycle()
        self.assertFalse(self.payload(self.cache(), 'species', self.sci)['detections'][0]['media']['available'])
        self.source('clip.mp3', b'arrived later')
        self.cycle()
        self.assertTrue(self.payload(self.cache(), 'species', self.sci)['detections'][0]['media']['available'])

    def test_auxiliary_reply_loss_has_startup_priority_over_new_work_and_newer_input(self):
        self.source('clip.mp3', b'first')
        self.detection()
        files = self.public_inputs(wiki={'species': {self.sci: {'extract': 'first description'}}})
        self.client.lost_reply_key = 'data/descriptions.json'
        with self.assertRaises(TimeoutError):
            self.cycle()
        frozen = self.ledger.execute('SELECT pending FROM cdn_cache_aux').fetchone()[0]
        files['wiki_cache_file'].write_text(json.dumps({'species': {self.sci: {'extract': 'newer description'}}}))
        self.source('second.mp3', b'second')
        self.detection('second.mp3', number=1)
        before = len(self.client.calls)
        result = self.cycle()
        self.assertEqual(self.client.objects['data/descriptions.json'][0], frozen)
        self.assertEqual(self.ledger.execute('SELECT pending FROM cdn_cache_aux').fetchone()[0], None)
        self.assertFalse(any(method == 'PUT' and key.startswith('audio/') for method, key in self.client.calls[before:]))
        self.cycle()
        self.assertIn(b'newer description', self.client.objects['data/descriptions.json'][0])

    def test_prepare_uses_readonly_single_transaction_during_concurrent_db_write(self):
        self.detection()
        original = run_sync.exporter._rows
        fired = False
        def interleaved(db, sql, values=None):
            nonlocal fired
            rows = original(db, sql, values)
            if not fired:
                fired = True
                self.detection('new.mp3', number=1, sci='Second species')
            return rows
        guard = self.guard()
        publisher = publish_cache.CurrentCachePublisher(self.client, self.ledger, self.front.cache_config(self.sync),
                                                        guard, outer_lock_held=True)
        try:
            with patch.object(run_sync.exporter, '_rows', side_effect=interleaved):
                run_sync.prepare_frontend(self.sync, self.front, self.ledger, guard, publisher,
                                          instant=dt.datetime.fromisoformat('2024-05-01T01:00:00+00:00'))
            publisher.publish()
        finally:
            guard.detach()
        self.assertTrue(fired)
        self.assertEqual(self.payload(self.cache(), 'stats')['totals']['detections'], 1)
        self.assertEqual(self.writer.execute('SELECT COUNT(*) FROM detections').fetchone()[0], 2)

    def test_optional_configuration_off_absent_partial_and_cleanup_optin(self):
        for environment in ({}, {'AVIAN_SYNC_ENABLED': '0', 'AVIAN_SYNC_BUCKET': 'incomplete'}):
            with patch.dict(os.environ, environment, clear=True), patch.object(run_sync.media_sync, 'make_client') as factory, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_sync.main(['--once']), 0)
                factory.assert_not_called()
        environment = self.environment()
        missing = dict(environment)
        del missing['AVIAN_SYNC_DB_PATH']
        with patch.dict(os.environ, missing, clear=True), patch.object(run_sync.media_sync, 'make_client') as factory, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_sync.main(['--once'])
            factory.assert_not_called()
        parsed = run_sync.FrontendConfig.from_env(environment, self.sync)
        self.assertFalse(parsed.cleanup_current_cache)
        self.assertTrue(run_sync.FrontendConfig.from_env({**environment, 'AVIAN_SYNC_CLEANUP_CURRENT_CACHE': '1'}, self.sync).cleanup_current_cache)

    def test_operator_mapping_and_description_validation_reject_unsafe_values(self):
        for mapping in ({'unexpected': {}}, {'recordings': {'clip.mp3': {'path': '../outside.mp3'}}},
                        {'images': {self.sci: {'pose3': {'path': 'pose.png'}}}}):
            with self.assertRaises(ValueError):
                run_sync.validate_mapping(mapping)
        for entry in ({'extract': 1}, {'extract': 'x', 'url': 'javascript:bad'},
                      {'extract': 'x', 'url': 'https://user:secret@birds.example/description'}):
            with self.assertRaises(ValueError):
                run_sync.wiki_payload({'species': {self.sci: entry}})

    def registered_temporary(self, name='.candidate-synthetic-crash'):
        run_sync.cleanup_candidates(self.sync, self.ledger)
        root = self.state / name
        root.mkdir(mode=0o700)
        info = root.stat()
        with self.ledger:
            self.ledger.execute('INSERT INTO cdn_export_temporary VALUES(?,?,?,?)',
                                (name, info.st_dev, info.st_ino, info.st_uid))
        return root

    def test_restart_removes_only_inode_registered_temporary_output(self):
        owned = self.registered_temporary()
        (owned / 'objects').mkdir(mode=0o700)
        (owned / 'objects' / ('a' * 64 + '.json')).write_bytes(b'{}')
        unrelated = self.state / '.candidate-not-registered'
        unrelated.mkdir(mode=0o700)
        (unrelated / 'operator.txt').write_text('keep this unrelated file')
        run_sync.cleanup_candidates(self.sync, self.ledger)
        self.assertFalse(owned.exists())
        self.assertEqual((unrelated / 'operator.txt').read_text(), 'keep this unrelated file')
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM cdn_export_temporary').fetchone()[0], 0)

    def test_unsafe_or_replaced_registered_temporary_holds_without_following_links(self):
        owned = self.registered_temporary()
        outside = self.root / 'operator-file.txt'
        outside.write_text('outside protected value')
        (owned / 'linked.txt').symlink_to(outside)
        with self.assertRaises(ValueError):
            run_sync.cleanup_candidates(self.sync, self.ledger)
        self.assertTrue(owned.exists())
        self.assertEqual(outside.read_text(), 'outside protected value')
        (owned / 'linked.txt').unlink()
        moved = self.state / 'retained-original'
        owned.rename(moved)
        owned.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            run_sync.cleanup_candidates(self.sync, self.ledger)
        self.assertTrue(moved.exists())
        self.assertTrue(owned.exists())

    def test_temporary_cleanup_rejects_same_device_nested_mount_before_removal(self):
        owned = self.registered_temporary()
        mounted = owned / 'same-device-mount'
        mounted.mkdir(mode=0o700)
        sentinel = mounted / 'external-owned-sentinel.txt'
        sentinel.write_text('external mounted content must remain')
        self.assertEqual(mounted.stat().st_dev, owned.stat().st_dev)
        with patch.object(sync_media, '_mount_points', return_value={str(mounted)}), \
                patch.object(sync_media, 'check_state_budget', side_effect=AssertionError('cleanup must release owned output without quota admission')):
            with self.assertRaises(ValueError):
                run_sync.cleanup_candidates(self.sync, self.ledger)
        self.assertTrue(owned.exists())
        self.assertEqual(sentinel.read_text(), 'external mounted content must remain')
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM cdn_export_temporary').fetchone()[0], 1)

    def test_local_state_admission_holds_before_snapshot_temporary_and_blob_growth(self):
        self.sync = dataclasses.replace(self.sync, local_state_bytes=sync_media.MIB)
        self.detection()
        guard = self.guard()
        publisher = publish_cache.CurrentCachePublisher(self.client, self.ledger, self.front.cache_config(self.sync),
                                                        guard, outer_lock_held=True)
        untouched = self.state / 'unrelated-allocated.bin'
        untouched.write_bytes(b'x' * sync_media.MIB)
        try:
            with self.assertRaises(sync_media.BudgetStop):
                run_sync.prepare_frontend(self.sync, self.front, self.ledger, guard, publisher)
        finally:
            guard.detach()
        self.assertFalse(list(self.state.glob('.candidate-*')))
        self.assertEqual(publisher.status()['frozen_candidate_bytes'], 0)
        self.assertNotIn('data/snapshot-index.json', self.client.objects)
        self.assertEqual(untouched.stat().st_size, sync_media.MIB)

    def test_multiple_image_roots_resolve_same_relative_name_by_explicit_root(self):
        second = self.root / 'other-art'
        second.mkdir(mode=0o700)
        (second / 'pose.png').write_bytes(b'second image root')
        self.source('pose.png', b'first image root', 'images')
        self.source('clip.mp3', b'first recording')
        self.detection()
        self.ledger.close()
        self.state = self.root / 'multi-root-state'
        self.state.mkdir(mode=0o700)
        self.sync = dataclasses.replace(self.sync, images_root=None,
                                       image_roots={'art_a': self.images, 'art_b': second},
                                       state_dir=self.state)
        self.ledger = sync_media.open_ledger(self.sync, outer_lock_held=True)
        self.addCleanup(self.ledger.close)
        self.public_inputs(mapping={'images': {self.sci: {
            'pose1': {'root': 'art_b', 'path': 'pose.png'},
            'pose2': {'root': 'art_a', 'path': 'pose.png'}}}})
        self.cycle()
        images = self.payload(self.cache(), 'lifelist')['species'][0]['media_images']
        self.assertEqual(images['pose1']['sha256'], hashlib.sha256(b'second image root').hexdigest())
        self.assertEqual(images['pose2']['sha256'], hashlib.sha256(b'first image root').hexdigest())
        self.assertEqual(self.ledger.execute("SELECT COUNT(*) FROM media_sync_sources WHERE status='verified'").fetchone()[0], 3)

    def test_cli_setup_retries_partial_auxiliary_adoption_with_one_lock_and_hook(self):
        self.detection()
        files = self.public_inputs(wiki={'species': {self.sci: {'extract': 'current description'}}})
        self.cycle()
        selected = self.client.objects['data/snapshot-index.json'][0]
        adoption_state = self.root / 'adoption-state'
        adoption_state.mkdir(mode=0o700)
        environment = self.environment()
        environment['AVIAN_SYNC_STATE_DIR'] = str(adoption_state)
        environment['AVIAN_SYNC_MIN_FREE_BYTES'] = str(16 * sync_media.MIB)
        environment['AVIAN_SYNC_WIKI_CACHE_FILE'] = str(files['wiki_cache_file'])
        real_guard, real_lock = sync_media.SyncGuard, sync_media.state_lock
        counts = {'locks': 0, 'guards': 0}
        def controlled_guard(config):
            counts['guards'] += 1
            clock = Clock()
            return real_guard(config, clock=clock, sleep=clock.sleep,
                              resources=lambda: (3 * sync_media.GIB, sync_media.GIB))
        @contextlib.contextmanager
        def counted_lock(config):
            counts['locks'] += 1
            with real_lock(config):
                yield
        self.client.failed_read_key = 'data/descriptions.json'
        before = len(self.client.calls)
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(sync_media, 'make_client', return_value=self.client), \
                patch.object(sync_media, 'SyncGuard', side_effect=controlled_guard), \
                patch.object(sync_media, 'state_lock', side_effect=counted_lock), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_sync.main(['--adopt-current-cache']), 1)
            self.assertEqual(self.client.meta.events.callbacks, {})
            self.assertEqual(run_sync.main(['--adopt-current-cache']), 0)
        self.assertEqual(counts, {'locks': 2, 'guards': 2})
        self.assertEqual(self.client.meta.events.callbacks, {})
        self.assertEqual(self.client.objects['data/snapshot-index.json'][0], selected)
        self.assertTrue(all(method == 'GET' for method, _ in self.client.calls[before:]))
        with contextlib.closing(sqlite3.connect(adoption_state / 'ledger.sqlite')) as adopted:
            self.assertEqual(adopted.execute('SELECT COUNT(*) FROM cdn_cache_graph').fetchone()[0], 1)
            self.assertEqual(adopted.execute('SELECT COUNT(*) FROM cdn_cache_aux').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
