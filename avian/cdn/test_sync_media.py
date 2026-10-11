import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sync_media as sync


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.now += value


class Events:
    def __init__(self):
        self.callbacks = {}

    def register(self, event, callback, unique_id):
        self.callbacks[unique_id] = callback

    def unregister(self, event, unique_id):
        self.callbacks.pop(unique_id)

    def emit(self):
        for callback in list(self.callbacks.values()):
            callback()


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}, 'ResponseMetadata': {'HTTPStatusCode': 404}}


class FakeS3:
    """Synthetic storage with actual shared before-send callbacks; no sockets."""
    def __init__(self):
        self.meta = SimpleNamespace(events=Events())
        self.objects = {}
        self.calls = []
        self.fail_after_put = self.fail_get = False
        self.on_put = None
        self.bad_mime = self.bad_sha = False

    def put_object(self, **request):
        self.meta.events.emit()
        self.calls.append(('PUT', request['Key']))
        if request.get('IfNoneMatch') == '*' and request['Key'] in self.objects:
            error = Exception('synthetic existing immutable object')
            error.response = {'Error': {'Code': 'PreconditionFailed'}, 'ResponseMetadata': {'HTTPStatusCode': 412}}
            raise error
        body = request['Body']
        pieces = []
        while True:
            raw = body.read(65536)
            if not raw:
                break
            pieces.append(raw)
        payload = b''.join(pieces)
        if len(payload) != request['ContentLength']:
            raise AssertionError('invalid uploaded length')
        self.objects[request['Key']] = (payload, request['ContentType'])
        if self.on_put:
            self.on_put()
        if self.fail_after_put:
            self.fail_after_put = False
            raise TimeoutError('synthetic uncertain upload')
        return {}

    def get_object(self, **request):
        self.meta.events.emit()
        self.calls.append(('GET', request['Key']))
        if self.fail_get:
            self.fail_get = False
            raise TimeoutError('synthetic GET timeout')
        if request['Key'] not in self.objects:
            raise Missing()
        raw, mime = self.objects[request['Key']]
        if self.bad_sha:
            raw = bytes([raw[0] ^ 1]) + raw[1:]
        return {'ContentLength': len(raw), 'ContentType': 'text/plain' if self.bad_mime else mime,
                'Body': io.BytesIO(raw)}

    def close(self):
        pass


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.audio, self.images, self.state = [self.root / name for name in ('audio', 'images', 'state')]
        for root in (self.audio, self.images, self.state):
            root.mkdir(mode=0o700)
        self.config = sync.Config('https://s3.example.net', 'synthetic-media', 'https://media.example.net',
                                  self.audio, self.images, self.state)
        self.ledger = sync.open_ledger(self.config, outer_lock_held=True)
        self.addCleanup(self.ledger.close)
        self.client = FakeS3()

    def source(self, name='clip.mp3', payload=b'synthetic-audio', kind='audio'):
        path = (self.audio if kind == 'audio' else self.images) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def guard(self, config=None):
        clock = Clock()
        guard = sync.SyncGuard(config or self.config, clock=clock, sleep=clock.sleep,
                               resources=lambda: (3 * sync.GIB, sync.GIB))
        guard.attach(self.client)
        self.addCleanup(guard.detach)
        return guard

    def batch(self, config=None):
        guard = self.guard(config)
        try:
            return sync.run_batch(config or self.config, self.client, self.ledger, guard, outer_lock_held=True)
        finally:
            guard.detach()

    def receipts(self):
        return list(sync.iter_verified_receipts(self.config, self.ledger, outer_lock_held=True))

    def test_audio_and_all_image_types_get_verified_before_association(self):
        self.source()
        for number, extension in enumerate(('png', 'jpg', 'jpeg', 'webp')):
            self.source('nested/image-' + str(number) + '.' + extension, b'image-' + str(number).encode(), 'images')
        result = self.batch()
        self.assertEqual(result['verified'], 5)
        self.assertEqual(result['attempts'], 15)
        receipts = self.receipts()
        self.assertEqual(len(receipts), 5)
        for row in receipts:
            raw = (self.audio if row['kind'] == 'audio' else self.images).joinpath(row['relative_path']).read_bytes()
            self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertTrue(row['url'].startswith('https://media.example.net/' + row['key']))
            self.assertEqual(row['root_label'], 'audio' if row['kind'] == 'audio' else 'images')
            self.assertRegex(row['key'], r'^(audio|images)/[a-f0-9]{2}/[a-f0-9]{64}\.(mp3|png|jpg|webp)$')

    def test_unchanged_files_use_no_hash_reads_or_network(self):
        self.source()
        self.batch()
        count = len(self.client.calls)
        with patch.object(sync.os, 'read', side_effect=AssertionError('unchanged source rehashed')):
            result = self.batch()
        self.assertEqual(result['selected_files'], 0)
        self.assertEqual(len(self.client.calls), count)

    def test_unknown_preexisting_immutable_object_is_verified_without_overwrite(self):
        payload = b'existing-immutable-object'
        self.source(payload=payload)
        sha = hashlib.sha256(payload).hexdigest()
        key = 'audio/' + sha[:2] + '/' + sha + '.mp3'
        self.client.objects[key] = (payload, 'audio/mpeg')
        result = self.batch()
        self.assertEqual(result['uploaded'], 0)
        self.assertEqual(result['verified'], 1)
        self.assertEqual(self.client.calls, [('GET', key)])
        self.assertEqual(self.receipts()[0]['key'], key)

    def test_unknown_corrupt_cloud_key_is_not_overwritten_or_associated(self):
        payload = b'correct-object-content'
        self.source(payload=payload)
        sha = hashlib.sha256(payload).hexdigest()
        key = 'audio/' + sha[:2] + '/' + sha + '.mp3'
        self.client.objects[key] = (b'wrong-object-content!!', 'audio/mpeg')
        result = self.batch()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.client.calls, [('GET', key)])
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_objects').fetchone()[0], 'pending')

    def test_remote_cap_counts_uncertain_objects_before_send_and_distinct_keys_once(self):
        payload = b'first-content'
        self.source('first.mp3', payload)
        self.client.fail_after_put = True
        config = dataclasses.replace(self.config, max_remote_media_bytes=len(payload))
        result = self.batch(config)
        self.assertEqual(result['remote_tracked_bytes'], len(payload))
        self.assertEqual(result['object_pending'], 1)
        self.source('alias.mp3', payload)
        result = self.batch(config)
        self.assertEqual(result['remote_tracked_bytes'], len(payload))
        self.assertEqual(result['objects'], 1)
        self.assertEqual(len([call for call in self.client.calls if call[0] == 'PUT']), 1)
        self.assertEqual(len(self.receipts()), 2)
        self.source('other.mp3', b'different')
        prior_calls = len(self.client.calls)
        with self.assertRaises(sync.BudgetStop): self.batch(config)
        self.assertEqual(len(self.client.calls), prior_calls)
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM media_sync_objects').fetchone()[0], 1)

    def test_reserved_remote_baseline_is_permanent_and_checked_before_cloud_send(self):
        state = self.root / 'reserved-state'; state.mkdir(mode=0o700)
        config = dataclasses.replace(self.config, state_dir=state, max_remote_media_bytes=20,
                                     remote_reserved_bytes=15)
        ledger = sync.open_ledger(config, outer_lock_held=True)
        self.addCleanup(ledger.close)
        self.source(payload=b'sixsix')
        guard = self.guard(config)
        with self.assertRaises(sync.BudgetStop):
            sync.run_batch(config, self.client, ledger, guard, outer_lock_held=True)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(ledger.execute('SELECT COUNT(*) FROM media_sync_objects').fetchone()[0], 0)
        with self.assertRaises(sync.ConfigError):
            sync.open_ledger(dataclasses.replace(config, remote_reserved_bytes=0), outer_lock_held=True)
        self.assertEqual(config.remote_media_bytes, 20)

    def test_multiple_unrelated_image_roots_keep_private_identity_and_fixed_public_keys(self):
        first, second, state = [self.root / name for name in ('art-a', 'art-b', 'multi-state')]
        for path in (first, second, state): path.mkdir(mode=0o700)
        (first / 'pose.png').write_bytes(b'first-art')
        (second / 'pose.png').write_bytes(b'second-art')
        config = dataclasses.replace(self.config, images_root=None, state_dir=state,
                                     image_roots={'one': first, 'two': second})
        ledger = sync.open_ledger(config, outer_lock_held=True); self.addCleanup(ledger.close)
        result = sync.run_batch(config, self.client, ledger, self.guard(config), outer_lock_held=True)
        self.assertEqual(result['verified'], 2)
        rows = list(sync.iter_verified_receipts(config, ledger, outer_lock_held=True))
        self.assertEqual({(row['kind'], row['root_label'], row['relative_path']) for row in rows},
                         {('image', 'one', 'pose.png'), ('image', 'two', 'pose.png')})
        self.assertEqual(len({row['key'] for row in rows}), 2)
        for row in rows:
            self.assertTrue(row['key'].startswith('images/'))
            self.assertNotIn(row['root_label'] + '/', row['key'])
        changed = dataclasses.replace(config, image_roots={'one': second, 'two': first})
        with self.assertRaises(sync.ConfigError): sync.open_ledger(changed, outer_lock_held=True)
        with self.assertRaises(sync.ConfigError):
            dataclasses.replace(config, image_roots={'one': first, 'two': first})
        with self.assertRaises(sync.ConfigError):
            dataclasses.replace(config, image_roots={'audio': first})

    def test_changed_file_creates_new_key_and_keeps_old_immutable_receipt(self):
        path = self.source()
        self.batch()
        old_key = self.receipts()[0]['key']
        path.write_bytes(b'changed-content')
        self.batch()
        new_key = self.receipts()[0]['key']
        self.assertNotEqual(old_key, new_key)
        self.assertIn(old_key, self.client.objects)
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM media_sync_objects').fetchone()[0], 2)

    def test_uncertain_put_survives_and_retry_gets_before_second_put(self):
        self.source()
        self.client.fail_after_put = True
        first = self.batch()
        self.assertEqual(first['failed'], 1)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_objects').fetchone()[0], 'pending')
        second = self.batch()
        self.assertEqual(second['verified'], 1)
        self.assertEqual([call[0] for call in self.client.calls], ['GET', 'PUT', 'GET'])

    def test_get_timeout_preserves_uncertainty_and_next_run_recovers(self):
        self.source()
        # The first GET checks absence; fail only the verification after PUT.
        self.client.on_put = lambda: setattr(self.client, 'fail_get', True)
        self.assertEqual(self.batch()['failed'], 1)
        self.assertEqual(self.receipts(), [])
        self.client.on_put = None
        self.assertEqual(self.batch()['verified'], 1)
        self.assertEqual([call[0] for call in self.client.calls], ['GET', 'PUT', 'GET', 'GET'])

    def test_get_sha_or_mime_failure_never_associates(self):
        self.source()
        self.client.bad_sha = True
        self.assertEqual(self.batch()['failed'], 1)
        self.assertEqual(self.receipts(), [])
        self.client.bad_sha = False
        self.client.bad_mime = True
        self.assertEqual(self.batch()['failed'], 1)
        self.assertEqual(self.receipts(), [])
        self.client.bad_mime = False
        self.assertEqual(self.batch()['verified'], 1)
        self.assertEqual(len([call for call in self.client.calls if call[0] == 'PUT']), 1)

    def test_changed_source_during_send_keeps_cloud_object_but_no_current_association(self):
        path = self.source()
        self.client.on_put = lambda: path.write_bytes(b'new-source-version')
        self.assertEqual(self.batch()['failed'], 1)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(len(self.client.objects), 1)
        self.client.on_put = None
        self.assertEqual(self.batch()['verified'], 1)

    def test_source_growth_during_hash_reads_only_captured_bytes_and_sends_nothing(self):
        path = self.source()
        original_read, sizes = sync.os.read, []
        def grow(fd, size):
            raw = original_read(fd, size)
            sizes.append(len(raw))
            with path.open('ab') as handle:
                handle.write(b'not-selected')
            return raw
        with patch.object(sync.os, 'read', side_effect=grow):
            result = self.batch()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(sum(sizes), len(b'synthetic-audio'))
        self.assertEqual(self.client.calls, [])

    def test_shared_attempt_limit_stops_fatally_with_uncertain_receipt_intact(self):
        self.source()
        guard = self.guard()
        self.client.on_put = lambda: setattr(guard, 'attempts', 150)
        with self.assertRaises(sync.BudgetStop):
            sync.run_batch(self.config, self.client, self.ledger, guard, outer_lock_held=True,
                           reserve_attempts=0, reserve_seconds=0)
        self.assertEqual(guard.attempts, 150)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_objects').fetchone()[0], 'pending')

    def test_exact_404_only_and_no_such_bucket_never_authorize_put(self):
        for status, code in ((403, 'NoSuchKey'), (404, 'NoSuchBucket'), (500, '404')):
            error = Exception('synthetic')
            error.response = {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}
            self.assertFalse(sync._not_found(error))
        self.assertTrue(sync._not_found(Missing()))

    def test_catalog_source_directory_and_object_caps_stop_before_more_cloud_writes(self):
        self.source('first.mp3')
        self.source('second.mp3', b'different')
        config = dataclasses.replace(self.config, max_sources=1)
        with self.assertRaises(sync.BudgetStop): self.batch(config)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM media_sync_sources').fetchone()[0], 0)
        config = dataclasses.replace(self.config, max_objects=1)
        with self.assertRaises(sync.BudgetStop): self.batch(config)
        self.assertEqual(len([call for call in self.client.calls if call[0] == 'PUT']), 1)
        nested = self.source('child/file.mp3', b'child')
        config = dataclasses.replace(self.config, max_directories=2)
        with self.assertRaises(sync.BudgetStop): self.batch(config)
        self.assertTrue(nested.exists())

    def test_local_state_cap_accounts_files_journal_and_unlinked_spool_reservation(self):
        self.source()
        config = dataclasses.replace(self.config, local_state_bytes=sync.MIB, scan_entries=4)
        retained = self.state / 'retained-proof.bin'
        retained.write_bytes(b'p' * (900 * 1024))
        with self.assertRaises(sync.BudgetStop): self.batch(config)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(retained.stat().st_size, 900 * 1024)
        retained.unlink()
        with self.assertRaises(sync.BudgetStop):
            sync.check_state_budget(config, self.ledger, additional_bytes=sync.MIB)

    def test_nested_bind_mounts_are_not_followed_even_on_same_device(self):
        self.source('mounted/clip.mp3')
        with patch.object(sync, '_mount_points', return_value={str(self.audio / 'mounted')}):
            result = self.batch()
        self.assertGreaterEqual(result['unsafe'], 1)
        self.assertEqual(self.client.calls, [])

    def test_media_request_and_time_margins_preserve_shared_publisher_budget(self):
        self.source()
        guard = self.guard()
        guard.attempts = 116
        result = sync.run_batch(self.config, self.client, self.ledger, guard, outer_lock_held=True)
        self.assertIn('paused', result)
        self.assertEqual(guard.attempts, 116)
        self.assertEqual(guard.phase_attempt_limit, 150)
        self.assertEqual(self.client.calls, [])
        guard.clock.now = 90
        result = sync.run_batch(self.config, self.client, self.ledger, guard, outer_lock_held=True)
        self.assertIn('paused', result)
        self.assertEqual(guard.phase_seconds, 120)
        guard.before_request('GET', 'data/current-index.json')
        self.client.meta.events.emit()
        self.assertEqual(guard.attempts, 117)

    def test_receipt_predicate_runs_before_stat_and_shared_guard_is_checked(self):
        self.source('wanted.mp3')
        self.source('unwanted.mp3', b'other')
        self.batch()
        guard = self.guard()
        with patch.object(sync, '_directory', wraps=sync._directory) as directory:
            rows = list(sync.iter_verified_receipts(self.config, self.ledger, outer_lock_held=True,
                                                    guard=guard, predicate=lambda kind, relative: relative == 'wanted.mp3'))
        self.assertEqual([row['relative_path'] for row in rows], ['wanted.mp3'])
        self.assertEqual(directory.call_count, 1)
        guard.clock.now = 120
        with self.assertRaises(sync.BudgetStop):
            list(sync.iter_verified_receipts(self.config, self.ledger, outer_lock_held=True, guard=guard))

    def test_corrupt_current_receipt_shape_is_not_returned_as_public_media(self):
        self.source()
        self.batch()
        self.ledger.execute('UPDATE media_sync_objects SET verified_at=NULL')
        self.ledger.commit()
        self.assertEqual(self.receipts(), [])
        self.ledger.execute('UPDATE media_sync_objects SET verified_at=1')
        self.ledger.execute('UPDATE media_sync_sources SET verified_at=NULL')
        self.ledger.commit()
        self.assertEqual(self.receipts(), [])

    def test_elapsed_disk_and_memory_stops_make_no_cloud_calls(self):
        self.source()
        guard = self.guard()
        guard.clock.now = 120
        with self.assertRaises(sync.BudgetStop):
            sync.run_batch(self.config, self.client, self.ledger, guard, outer_lock_held=True)
        for resources in ((sync.MIB, sync.GIB), (3 * sync.GIB, sync.MIB)):
            guard.clock.now = 0
            guard.resources = lambda: resources
            with self.assertRaises(sync.BudgetStop): guard.check()
        self.assertEqual(self.client.calls, [])

    def test_selected_file_and_byte_budgets_are_joint_and_oversize_never_read(self):
        config = dataclasses.replace(self.config, batch_files=2, batch_bytes=7, file_bytes=4)
        for number in range(5): self.source(str(number) + '.mp3', b'1234')
        self.source('oversize.mp3', b'12345')
        result = self.batch(config)
        self.assertEqual(result['selected_files'], 1)
        self.assertEqual(result['selected_bytes'], 4)
        self.assertEqual(result['pending'], 4)
        self.assertEqual(len(self.client.objects), 1)

    def test_incremental_cursor_survives_batches_and_every_historical_file_progresses(self):
        config = dataclasses.replace(self.config, scan_entries=4, batch_files=3)
        for number in range(36): self.source('history/' + str(number) + '.mp3', str(number).encode())
        scanned = []
        for _ in range(24):
            result = self.batch(config)
            scanned.append(result['scanned'])
        self.assertEqual(len(self.receipts()), 36)
        self.assertTrue(all(value <= 4 for value in scanned))
        self.assertEqual(len([call for call in self.client.calls if call[0] == 'PUT']), 36)

    def test_missing_local_file_is_not_current_but_cloud_receipt_is_retained(self):
        path = self.source()
        self.batch()
        path.unlink()
        self.assertEqual(self.receipts(), [])
        self.assertEqual(len(self.client.objects), 1)
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_objects').fetchone()[0], 'verified')

    def test_retained_missing_oldest_queue_rows_are_tombstoned_without_starving_history(self):
        for number in range(36):
            path = self.source('%02d.mp3' % number, ('content-%02d' % number).encode())
            os.utime(path, (1000 + number, 1000 + number))
        guard = self.guard()
        sync.discover(self.config, self.ledger, guard, outer_lock_held=True)
        for number in range(8): (self.audio / ('%02d.mp3' % number)).unlink()
        # Discovery is fixed here to isolate failures of catalogued queue rows.
        def fixed_discovery(config, ledger, shared, **kwargs):
            shared.source_mounts = set()
            return dict(scanned=0, queued=0, unsafe=0)
        with patch.object(sync, 'discover', side_effect=fixed_discovery):
            for _ in range(4): self.batch()
        statuses = dict(self.ledger.execute('SELECT path,status FROM media_sync_sources'))
        self.assertTrue(all(statuses['%02d.mp3' % number] == 'missing' for number in range(8)))
        self.assertEqual(statuses['08.mp3'], 'verified')
        self.assertEqual(statuses['35.mp3'], 'verified')
        self.assertEqual(sync.catalog_status(self.config, self.ledger)['source_missing'], 8)
        self.assertTrue((self.audio / '08.mp3').exists())
        self.assertFalse(any(call[0] == 'DELETE' for call in self.client.calls))
        restored = self.source('00.mp3', b'new-recording-after-retention')
        self.batch()
        self.assertEqual(dict(self.ledger.execute('SELECT path,status FROM media_sync_sources'))['00.mp3'], 'verified')
        self.assertTrue(restored.exists())

    def test_source_removed_after_upload_preserves_immutable_receipt_without_current_association(self):
        path = self.source()
        self.client.on_put = path.unlink
        result = self.batch()
        self.assertEqual(result['source_missing'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.ledger.execute('SELECT COUNT(*) FROM media_sync_objects').fetchone()[0], 1)
        self.assertEqual(len(self.client.objects), 1)
        self.assertEqual([call[0] for call in self.client.calls], ['GET', 'PUT', 'GET'])

    def test_missing_source_cannot_tombstone_when_root_identity_has_changed(self):
        path = self.source()
        sync.discover(self.config, self.ledger, self.guard(), outer_lock_held=True)
        row = self.ledger.execute('SELECT * FROM media_sync_sources').fetchone()
        path.unlink()
        old = self.root / 'old-audio'; self.audio.rename(old); self.audio.mkdir()
        with self.assertRaises(sync.ConfigError):
            sync._mark_source_missing(self.config, self.ledger, row, self.guard())
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_sources').fetchone()[0], 'pending')

    def test_cloud_transport_file_error_never_marks_present_source_missing(self):
        path = self.source()
        with patch.object(self.client, 'get_object', side_effect=FileNotFoundError('synthetic transport error')):
            result = self.batch()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['source_missing'], 0)
        self.assertEqual(self.ledger.execute('SELECT status FROM media_sync_sources').fetchone()[0], 'pending')
        self.assertTrue(path.exists())

    def test_symlinks_hardlinks_special_and_empty_files_are_skipped_without_reads(self):
        outside = self.root / 'outside.mp3'
        outside.write_bytes(b'private-outside')
        (self.audio / 'symlink.mp3').symlink_to(outside)
        os.link(outside, self.audio / 'hardlink.mp3')
        os.mkfifo(self.audio / 'pipe.mp3')
        self.source('empty.mp3', b'')
        with patch.object(sync.os, 'read', side_effect=AssertionError('unsafe source read')):
            result = self.batch()
        self.assertGreaterEqual(result['unsafe'], 3)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(outside.read_bytes(), b'private-outside')

    def test_target_root_identity_and_shared_lock_guard_requirements_fail_closed(self):
        with self.assertRaises(sync.ConfigError):
            sync.run_batch(self.config, self.client, self.ledger, self.guard(), outer_lock_held=False)
        unbound_guard = sync.SyncGuard(self.config, resources=lambda: 3 * sync.GIB)
        with self.assertRaises(sync.ConfigError):
            sync.run_batch(self.config, self.client, self.ledger, unbound_guard, outer_lock_held=True)
        changed = dataclasses.replace(self.config, bucket='another-bucket')
        with self.assertRaises(sync.ConfigError):
            sync.run_batch(changed, self.client, self.ledger, self.guard(changed), outer_lock_held=True)
        renamed = self.root / 'old-audio'
        self.audio.rename(renamed)
        self.audio.mkdir()
        with self.assertRaises(sync.ConfigError):
            sync.run_batch(self.config, self.client, self.ledger, self.guard(), outer_lock_held=True)

    def test_payload_reader_rewinds_exact_bytes_and_counts_rereads_in_rate(self):
        guard = self.guard()
        raw = b'x' * 524288
        reader = guard.wrap_body(io.BytesIO(raw))
        self.assertEqual(reader.read(), raw)
        reader.seek(0)
        buffer = bytearray(len(raw))
        self.assertEqual(reader.readinto(buffer), len(raw))
        self.assertEqual(bytes(buffer), raw)
        self.assertEqual(guard.released_bytes, 2 * len(raw))
        self.assertEqual(guard.clock.now, 2)

    def test_dry_scan_reads_no_bytes_and_creates_no_state(self):
        self.source()
        state = self.root / 'absent-state'
        config = dataclasses.replace(self.config, state_dir=state)
        with patch.object(sync.os, 'read', side_effect=AssertionError('dry scan read bytes')):
            result = sync.dry_scan(config)
        self.assertEqual(result['eligible'], 1)
        self.assertFalse(state.exists())

    def test_config_tri_state_private_credentials_and_unsafe_urls(self):
        env = {'AVIAN_SYNC_ENDPOINT_URL': self.config.endpoint_url,
               'AVIAN_SYNC_BUCKET': self.config.bucket, 'AVIAN_SYNC_PUBLIC_ORIGIN': self.config.public_origin,
               'AVIAN_SYNC_AUDIO_ROOT': str(self.audio), 'AVIAN_SYNC_IMAGE_ROOT': str(self.images),
               'AVIAN_SYNC_STATE_DIR': str(self.state), 'AVIAN_SYNC_ACCESS_KEY_ID': 'synthetic-id',
               'AVIAN_SYNC_SECRET_ACCESS_KEY': 'synthetic-secret'}
        self.assertIsNone(sync.Config.from_env({}))
        self.assertIsNone(sync.Config.from_env({'AVIAN_SYNC_ENABLED': '0', 'AVIAN_SYNC_BUCKET': 'partial'}))
        self.assertEqual(sync.Config.from_env(env), self.config)
        with self.assertRaises(sync.ConfigError): sync.Config.from_env({'AVIAN_SYNC_BUCKET': 'partial'})
        with self.assertRaises(sync.ConfigError): sync.Config.from_env({'AVIAN_SYNC_ENABLED': '1'})
        incomplete = dict(env); incomplete.pop('AVIAN_SYNC_SECRET_ACCESS_KEY')
        with self.assertRaises(sync.ConfigError): sync.Config.from_env(incomplete)
        self.assertEqual(sync.Config.from_env(incomplete, require_credentials=False), self.config)
        self.assertNotIn('synthetic-secret', repr(self.config))
        multiple = dict(env); multiple.pop('AVIAN_SYNC_IMAGE_ROOT')
        multiple['AVIAN_SYNC_IMAGE_ROOTS'] = json.dumps({'art': str(self.images)})
        self.assertEqual(sync.Config.from_env(multiple).roots, {'audio': self.audio, 'art': self.images})
        multiple['AVIAN_SYNC_MAX_REMOTE_MEDIA_BYTES'] = '1234'
        multiple['AVIAN_SYNC_REMOTE_RESERVED_BYTES'] = '100'
        parsed = sync.Config.from_env(multiple)
        self.assertEqual((parsed.max_remote_media_bytes, parsed.remote_reserved_bytes), (1234, 100))
        for raw in ('{}', '[]', 'null', '{invalid', json.dumps({'audio': str(self.images)})):
            broken = dict(multiple); broken['AVIAN_SYNC_IMAGE_ROOTS'] = raw
            with self.assertRaises(sync.ConfigError): sync.Config.from_env(broken)
        conflicting = dict(multiple, AVIAN_SYNC_IMAGE_ROOT=str(self.images))
        with self.assertRaises(sync.ConfigError): sync.Config.from_env(conflicting)
        partial = dict(multiple); partial.pop('AVIAN_SYNC_STATE_DIR')
        with self.assertRaises(sync.ConfigError): sync.Config.from_env(partial)
        self.assertIsNone(sync.Config.from_env(dict(partial, AVIAN_SYNC_ENABLED='0')))
        for url in ('http://media.example.net', 'https://secret@media.example.net',
                    'https://media.example.net/path', 'https://media.example.net/?secret=x'):
            with self.assertRaises(sync.ConfigError): dataclasses.replace(self.config, public_origin=url)

    def test_private_env_is_literal_bounded_and_rejects_readable_or_linked_files(self):
        path = self.root / 'private.env'
        path.write_text('AVIAN_SYNC_SECRET_ACCESS_KEY="$(never-executed)"\nAVIAN_CDN_ENABLED=1\n')
        path.chmod(0o600)
        self.assertEqual(sync.load_env_file(path), {'AVIAN_SYNC_SECRET_ACCESS_KEY': '$(never-executed)'})
        path.chmod(0o644)
        with self.assertRaises(sync.ConfigError): sync.load_env_file(path)
        alias = self.root / 'env-alias'
        alias.symlink_to(path)
        with self.assertRaises(sync.ConfigError): sync.load_env_file(alias)

    def test_client_factory_requires_explicit_credentials_and_disables_sdk_retries(self):
        captured = {}
        def make(**kwargs):
            captured.update(kwargs)
            return kwargs
        session = SimpleNamespace(client=lambda service, **kwargs: make(**kwargs))
        modules = {'boto3': SimpleNamespace(session=SimpleNamespace(Session=lambda: session)),
                   'botocore.config': SimpleNamespace(Config=lambda **kwargs: kwargs)}
        with patch.dict(sys.modules, modules):
            with self.assertRaises(sync.ConfigError): sync.make_client(self.config, credentials={})
            sync.make_client(self.config, credentials={'AVIAN_SYNC_ACCESS_KEY_ID': 'synthetic-id',
                                                       'AVIAN_SYNC_SECRET_ACCESS_KEY': 'synthetic-secret'})
        self.assertEqual(captured['aws_access_key_id'], 'synthetic-id')
        self.assertEqual(captured['config']['retries']['total_max_attempts'], 1)
        self.assertEqual(captured['config']['max_pool_connections'], 1)
        self.assertEqual(os.environ['AWS_EC2_METADATA_DISABLED'], 'true')


if __name__ == '__main__':
    unittest.main()
