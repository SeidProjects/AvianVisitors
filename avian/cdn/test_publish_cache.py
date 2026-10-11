import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import export_snapshots
from publish_cache import CacheHold, CurrentCachePublisher, MIB, publish_current


class S3Error(Exception):
    def __init__(self, status, code):
        self.response = {'ResponseMetadata': {'HTTPStatusCode': status}, 'Error': {'Code': code}}


class BudgetStop(RuntimeError):
    pass


class Guard:
    def __init__(self):
        self.sends = 0
        self.limit = 10000
        self.wrapped = 0

    def check(self):
        pass

    def before_send(self):
        if self.sends >= self.limit:
            raise BudgetStop('shared actual SDK attempt allowance exhausted')
        self.sends += 1

    def wrap_body(self, body):
        self.wrapped += 1
        return body


class S3:
    """No network: each mock SDK send uses the caller's one shared guard hook."""
    def __init__(self, guard):
        self.guard, self.store, self.calls = guard, {}, []
        self.fail_after_put = None
        self.fail_after_delete = None
        self.keep_deleted = False
        self.head_error = None
        self.corrupt_get = None
        self.after_get = None
        self.before_put = None
        self.cache_controls = {}

    def call(self, method, key):
        self.guard.before_send()
        self.calls.append((method, key))

    def get_object(self, Bucket, Key):
        self.call('GET', Key)
        if Key not in self.store:
            raise S3Error(404, 'NoSuchKey')
        raw = self.store[Key]
        if self.corrupt_get == Key:
            raw = bytes([raw[0] ^ 1]) + raw[1:]
        if self.after_get:
            self.after_get(Key)
        return {'Body': io.BytesIO(raw), 'ContentLength': len(raw)}

    def put_object(self, Bucket, Key, Body, ContentLength, ContentType, CacheControl, **options):
        self.call('PUT', Key)
        if options.get('IfNoneMatch') == '*' and Key in self.store:
            raise S3Error(412, 'PreconditionFailed')
        if self.before_put:
            self.before_put(Key)
        raw = Body.read()
        assert len(raw) == ContentLength
        self.store[Key] = raw
        self.cache_controls[Key] = CacheControl
        if self.fail_after_put == Key:
            self.fail_after_put = None
            raise RuntimeError('PUT succeeded but reply was lost')
        return {}

    def delete_object(self, Bucket, Key):
        self.call('DELETE', Key)
        if not self.keep_deleted:
            self.store.pop(Key, None)
        if self.fail_after_delete == Key:
            self.fail_after_delete = None
            raise RuntimeError('DELETE succeeded but reply was lost')
        return {}

    def head_object(self, Bucket, Key):
        self.call('HEAD', Key)
        if self.head_error:
            raise self.head_error
        if Key not in self.store:
            raise S3Error(404, 'NotFound')
        return {'ContentLength': len(self.store[Key])}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source.sqlite'
        self.writer = sqlite3.connect(self.source)
        self.writer.execute('CREATE TABLE detections(Date TEXT,Time TEXT,Sci_Name TEXT,Com_Name TEXT,Confidence REAL,File_Name TEXT)')
        self.add('Synthetic species', '08:00:00', 'bird’s-clip.mp3')
        self.add('Second species', '08:05:00', 'second.mp3')
        self.output = self.root / 'candidate'
        self.state = self.root / 'state.sqlite'
        self.db = sqlite3.connect(self.state)
        self.guard = Guard()
        self.s3 = S3(self.guard)
        self.config = {'bucket': 'public-cache', 'endpoint_url': 'https://storage.example',
                       'single_publisher': True, 'cleanup_current_cache': True,
                       'auxiliary_keys': ['descriptions.json']}
        self.pub = self.publisher()
        self.serial = 0

    def tearDown(self):
        self.writer.close()
        self.db.close()
        self.temp.cleanup()

    def add(self, sci, time, filename):
        self.writer.execute('INSERT INTO detections VALUES(?,?,?,?,?,?)',
                            ('2024-05-01', time, sci, 'Synthetic bird', .8, filename))
        self.writer.commit()

    def publisher(self):
        return CurrentCachePublisher(self.s3, self.db, self.config, self.guard, outer_lock_held=True)

    def export(self):
        self.serial += 1
        return export_snapshots.export(self.source, self.output,
                                       instant=dt.datetime.fromisoformat('2024-05-01T13:00:00+00:00'),
                                       generation='synthetic-' + str(self.serial))

    def prepopulate(self, index):
        for path in [*index['objects'], 'generations/' + index['generation'] + '/manifest.json', 'snapshot-index.json']:
            self.s3.store['data/' + path] = (self.output / path).read_bytes()

    def new_connection(self):
        self.db.close()
        self.db = sqlite3.connect(self.state)
        self.pub = self.publisher()

    def test_actual_exporter_graph_puts_pointer_last_and_verifies_full_gets(self):
        index = self.export()
        def admitted(key):
            pending = self.db.execute("SELECT phase FROM cdn_cache_graph WHERE slot='pending'").fetchone()
            self.assertIsNotNone(pending)
            if key == 'data/snapshot-index.json':
                self.assertEqual(pending[0], 'pointer')
            else:
                self.assertEqual(self.db.execute('SELECT status FROM cdn_cache_objects WHERE key=?', (key,)).fetchone()[0], 'putting')
        self.s3.before_put = admitted
        result = self.pub.publish(self.output)
        self.assertEqual(result, index)
        puts = [key for method, key in self.s3.calls if method == 'PUT']
        self.assertEqual(puts[-1], 'data/snapshot-index.json')
        self.assertEqual(puts[-2], 'data/generations/synthetic-1/manifest.json')
        for key in puts:
            at = self.s3.calls.index(('PUT', key))
            self.assertIn(('GET', key), self.s3.calls[at + 1:])
        self.assertEqual(self.guard.sends, len(self.s3.calls))
        self.assertEqual(self.guard.wrapped, len(puts))
        self.assertEqual(self.pub.status()['frozen_candidate_bytes'], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cdn_cache_graph').fetchone()[0], 1)

    def test_new_generation_reuses_unchanged_data_and_keeps_only_current_manifest(self):
        one = self.export()
        self.pub.publish(self.output)
        self.s3.calls.clear()
        two = self.export()
        self.pub.publish(self.output)
        self.assertEqual(one['objects'], two['objects'])
        puts = [key for method, key in self.s3.calls if method == 'PUT']
        self.assertEqual(puts, ['data/generations/synthetic-2/manifest.json', 'data/snapshot-index.json'])
        self.assertNotIn('data/generations/synthetic-1/manifest.json', self.s3.store)
        self.assertEqual(set(self.s3.store), {'data/' + path for path in two['objects']} | {
            'data/generations/synthetic-2/manifest.json', 'data/snapshot-index.json'})

    def test_frozen_candidate_resumes_after_data_put_reply_loss_without_source_directory(self):
        index = self.export()
        key = 'data/' + min(index['objects'])
        self.s3.fail_after_put = key
        with self.assertRaisesRegex(RuntimeError, 'reply was lost'):
            self.pub.publish(self.output)
        self.assertEqual(self.pub._row(key)[2], 'putting')
        shutil.rmtree(self.output)
        self.new_connection()
        self.pub.publish()
        self.assertEqual(self.pub.status()['current_generation'], index['generation'])
        self.assertEqual(self.s3.calls.count(('PUT', key)), 1)

    def test_pointer_reply_loss_recovers_new_selected_graph_before_cleanup(self):
        one = self.export()
        self.pub.publish(self.output)
        two = self.export()
        self.s3.fail_after_put = 'data/snapshot-index.json'
        with self.assertRaises(RuntimeError):
            self.pub.publish(self.output)
        self.assertEqual(self.pub.status()['current_generation'], one['generation'])
        self.assertEqual(self.pub.status()['pending_generation'], two['generation'])
        self.new_connection()
        before = self.s3.calls.count(('PUT', 'data/snapshot-index.json'))
        self.assertEqual(self.pub.recover(), 'selected')
        self.assertEqual(self.pub.status()['current_generation'], two['generation'])
        self.pub.cleanup()
        self.assertEqual(self.s3.calls.count(('PUT', 'data/snapshot-index.json')), before)
        self.assertNotIn('data/generations/' + one['generation'] + '/manifest.json', self.s3.store)

    def test_recovery_of_lost_pointer_reply_rechecks_all_referenced_bytes(self):
        index = self.export()
        self.s3.fail_after_put = 'data/snapshot-index.json'
        with self.assertRaises(RuntimeError):
            self.pub.publish(self.output)
        self.s3.corrupt_get = 'data/' + index['routes']['stats']
        with self.assertRaises(CacheHold):
            self.pub.recover()
        self.assertIsNone(self.pub.status()['current_generation'])
        self.assertEqual(self.pub.status()['pending_generation'], index['generation'])
        self.assertFalse(any(method == 'DELETE' for method, key in self.s3.calls))

    def test_foreign_or_missing_selected_pointer_never_gets_overwritten(self):
        self.export()
        self.s3.store['data/snapshot-index.json'] = b'{"foreign":true}'
        with self.assertRaises(CacheHold):
            self.pub.publish(self.output)
        self.assertFalse(any(method == 'PUT' for method, key in self.s3.calls))
        self.s3.store.clear()
        self.pub.publish(self.output)
        self.s3.store.pop('data/snapshot-index.json')
        self.s3.calls.clear()
        with self.assertRaises(CacheHold):
            self.pub.publish(self.output)
        self.assertFalse(any(method in ('PUT', 'DELETE') for method, key in self.s3.calls))

    def test_explicit_adoption_verifies_current_graph_without_adopting_old_unknown_keys(self):
        index = self.export()
        self.prepopulate(index)
        self.s3.store['data/objects/' + 'f' * 64 + '.json'] = b'{"unknown":true}'
        with self.assertRaises(ValueError):
            self.pub.adopt_current()
        self.pub.adopt_current(allow_adoption=True)
        self.assertEqual(self.pub.status()['current_generation'], index['generation'])
        self.assertEqual(self.pub.status()['tracked_keys'], len(index['objects']) + 1)
        self.pub.cleanup()
        self.assertIn('data/objects/' + 'f' * 64 + '.json', self.s3.store)
        self.assertFalse(any(method != 'GET' for method, key in self.s3.calls))

    def test_bad_adoption_bytes_or_changed_pointer_leave_no_claimed_state(self):
        index = self.export()
        self.prepopulate(index)
        self.s3.corrupt_get = 'data/' + index['routes']['stats']
        with self.assertRaises(CacheHold):
            self.pub.adopt_current(allow_adoption=True)
        self.assertEqual(self.pub.status()['tracked_keys'], 0)
        self.s3.corrupt_get = None
        count = 0
        def changed(key):
            nonlocal count
            if key == 'data/snapshot-index.json':
                count += 1
                if count == 1:
                    self.s3.store[key] = b'{"changed":true}'
        self.s3.after_get = changed
        with self.assertRaises(CacheHold):
            self.pub.adopt_current(allow_adoption=True)
        self.assertIsNone(self.pub.status()['current_generation'])

    def test_cleanup_never_targets_media_unknown_keys_or_current_inflight_closure(self):
        self.export()
        self.pub.publish(self.output)
        self.add('Synthetic species', '08:30:00', 'new.mp3')
        self.export()
        self.pub.prepare(self.output)
        protected = set(self.pub._graph('current')['identities']) | set(self.pub._graph('pending')['identities'])
        self.s3.store.update({'audio/a.mp3': b'a', 'images/b.png': b'b', 'data/operator.json': b'{}'})
        self.s3.calls.clear()
        self.pub.cleanup()
        self.assertFalse(any(method == 'DELETE' and key in protected for method, key in self.s3.calls))
        self.assertTrue({'audio/a.mp3', 'images/b.png', 'data/operator.json'} <= set(self.s3.store))

    def test_delete_reply_loss_keeps_quarantined_proof_until_head404_recovery(self):
        one = self.export()
        self.pub.publish(self.output)
        self.config['cleanup_current_cache'] = False
        self.pub = self.publisher()
        self.export()
        self.pub.publish(self.output)
        self.config['cleanup_current_cache'] = True
        self.pub = self.publisher()
        key = 'data/generations/' + one['generation'] + '/manifest.json'
        self.s3.fail_after_delete = key
        with self.assertRaises(RuntimeError):
            self.pub.cleanup()
        self.assertEqual(self.pub._row(key)[2], 'deleting')
        self.new_connection()
        self.pub.cleanup()
        self.assertIsNone(self.pub._row(key))
        self.assertEqual(self.s3.calls.count(('DELETE', key)), 1)

    def test_delete_denial_or_still_present_does_not_release_proof(self):
        one = self.export()
        self.pub.publish(self.output)
        self.config['cleanup_current_cache'] = False
        self.pub = self.publisher()
        self.export()
        self.pub.publish(self.output)
        self.config['cleanup_current_cache'] = True
        self.pub = self.publisher()
        key = 'data/generations/' + one['generation'] + '/manifest.json'
        self.s3.keep_deleted = True
        with self.assertRaises(CacheHold):
            self.pub.cleanup()
        self.assertEqual(self.pub._row(key)[2], 'deleting')
        self.s3.head_error = S3Error(403, 'AccessDenied')
        with self.assertRaises(S3Error):
            self.pub.cleanup()
        self.assertEqual(self.pub._row(key)[2], 'deleting')

    def test_deleted_data_referenced_again_requires_new_full_put_get_proof(self):
        one = self.export()
        self.pub.publish(self.output)
        first_stats = 'data/' + one['routes']['stats']
        self.add('Synthetic species', '08:30:00', 'new.mp3')
        self.export()
        self.config['max_cleanup_keys'] = 100
        self.pub = self.publisher()
        self.pub.publish(self.output)
        self.assertNotIn(first_stats, self.s3.store)
        self.writer.execute("DELETE FROM detections WHERE File_Name='new.mp3'")
        self.writer.commit()
        self.export()
        self.s3.calls.clear()
        self.pub.publish(self.output)
        self.assertIn(('PUT', first_stats), self.s3.calls)
        self.assertIn(('GET', first_stats), self.s3.calls)

    def test_unknown_matching_data_can_be_used_without_claiming_cleanup_ownership(self):
        one = self.export()
        key = 'data/' + one['routes']['stats']
        self.s3.store[key] = (self.output / one['routes']['stats']).read_bytes()
        self.pub.publish(self.output)
        self.assertEqual(self.pub._row(key)[3], 0)
        self.add('Synthetic species', '08:30:00', 'new.mp3')
        self.export()
        self.config['max_cleanup_keys'] = 100
        self.pub = self.publisher()
        self.pub.publish(self.output)
        self.assertIn(key, self.s3.store)
        self.assertNotIn(('DELETE', key), self.s3.calls)
        self.assertIsNone(self.pub._row(key))

    def test_disabled_cleanup_has_explicit_tracked_capacity_hold(self):
        self.config.update(cleanup_current_cache=False, max_owned_keys=12)
        self.pub = self.publisher()
        one = self.export()
        self.pub.publish(self.output)
        self.add('Synthetic species', '08:30:00', 'new.mp3')
        self.export()
        before = dict(self.s3.store)
        with self.assertRaises(CacheHold):
            self.pub.prepare(self.output)
        self.assertEqual(self.s3.store, before)
        self.assertEqual(self.pub.status()['current_generation'], one['generation'])

    def test_candidate_hash_manifest_link_and_byte_limit_fail_before_cloud_writes(self):
        index = self.export()
        path = self.output / index['routes']['stats']
        original = path.read_bytes()
        path.write_bytes(original + b'x')
        with self.assertRaises(ValueError):
            self.pub.prepare(self.output)
        path.write_bytes(original)
        manifest = self.output / 'generations' / index['generation'] / 'manifest.json'
        manifest.write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.pub.prepare(self.output)
        manifest.write_bytes((self.output / 'snapshot-index.json').read_bytes())
        path.unlink()
        target = self.root / 'outside.json'
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.pub.prepare(self.output)
        self.assertEqual(self.s3.calls, [])
        self.assertEqual(self.pub.status()['frozen_candidate_bytes'], 0)

    def test_shared_sdk_budget_stop_leaves_frozen_candidate_and_previous_pointer(self):
        one = self.export()
        self.pub.publish(self.output)
        self.add('Synthetic species', '08:30:00', 'new.mp3')
        two = self.export()
        self.guard.limit = self.guard.sends + 4
        with self.assertRaises(BudgetStop):
            self.pub.publish(self.output)
        self.assertEqual(self.pub.status()['current_generation'], one['generation'])
        self.assertEqual(self.pub.status()['pending_generation'], two['generation'])
        self.assertEqual(json.loads(self.s3.store['data/snapshot-index.json'])['generation'], one['generation'])

    def test_prepared_not_uploaded_graph_reserves_its_full_cache_byte_union(self):
        index = self.export()
        self.pub.prepare(self.output)
        raw = (self.output / 'snapshot-index.json').read_bytes()
        expected = sum(value['bytes'] for value in index['objects'].values()) + 2 * len(raw)
        status = self.pub.status()
        self.assertEqual(status['tracked_cache_bytes'], expected)
        self.assertEqual(status['reserved_unwritten_keys'], len(index['objects']) + 1)
        self.assertEqual(status['proof_keys'], 0)
        self.assertEqual(self.s3.calls, [])

    def test_owned_backlog_is_drained_before_new_candidate_capacity_admission(self):
        self.config.update(cleanup_current_cache=False, max_owned_keys=13)
        self.pub = self.publisher()
        self.export()
        self.pub.publish(self.output)
        self.export()
        self.pub.publish(self.output)
        self.config['cleanup_current_cache'] = True
        self.pub = self.publisher()
        self.export()
        self.pub.publish(self.output)
        self.assertEqual(self.pub.status()['pending_generation'], None)
        self.assertEqual(self.pub.status()['current_generation'], 'synthetic-3')
        self.assertEqual(len([key for key in self.s3.store if '/generations/' in key]), 1)

    def test_target_durability_lock_boolean_and_namespace_validation(self):
        for change in ({'endpoint_url': 'http://storage.example'}, {'cache_prefix': 'audio'},
                       {'cache_prefix': '../data'}, {'cleanup_current_cache': 'yes'},
                       {'max_cache_bytes': True}, {'single_publisher': False}):
            with self.assertRaises(ValueError):
                CurrentCachePublisher(self.s3, self.db, {**self.config, **change}, self.guard, outer_lock_held=True)
        with self.assertRaises(ValueError):
            CurrentCachePublisher(self.s3, self.db, self.config, self.guard, outer_lock_held=False)
        with self.assertRaises(CacheHold):
            CurrentCachePublisher(self.s3, self.db, {**self.config, 'bucket': 'another-cache'}, self.guard, outer_lock_held=True)
        temporary = sqlite3.connect(':memory:')
        try:
            with self.assertRaises(ValueError):
                CurrentCachePublisher(self.s3, temporary, self.config, self.guard, outer_lock_held=True)
        finally:
            temporary.close()

    def test_auxiliary_allowlist_current_value_verification_and_snapshot_cleanup_protection(self):
        raw = b'{"schema_version":1,"species":{}}'
        self.assertEqual(self.pub.publish_auxiliary('descriptions.json', raw), hashlib.sha256(raw).hexdigest())
        self.assertNotIn('immutable', self.s3.cache_controls['data/descriptions.json'])
        before = list(self.s3.calls)
        self.pub.publish_auxiliary('descriptions.json', raw)
        self.assertEqual(self.s3.calls, before)
        with self.assertRaises(ValueError):
            self.pub.publish_auxiliary('../audio/clip.mp3', raw)
        with self.assertRaises(ValueError):
            self.pub.publish_auxiliary('descriptions.json', b'x' * (MIB + 1))
        self.export()
        self.pub.publish(self.output)
        self.export()
        self.pub.publish(self.output)
        self.assertEqual(self.s3.store['data/descriptions.json'], raw)
        self.assertNotIn(('DELETE', 'data/descriptions.json'), self.s3.calls)

    def test_auxiliary_reply_loss_recovers_only_prepared_value_without_history(self):
        raw = b'{"species":{}}'
        key = 'data/descriptions.json'
        self.s3.fail_after_put = key
        with self.assertRaises(RuntimeError):
            self.pub.publish_auxiliary('descriptions.json', raw)
        self.new_connection()
        self.pub.publish_auxiliary('descriptions.json', raw)
        self.assertEqual(self.s3.calls.count(('PUT', key)), 1)
        row = self.db.execute('SELECT current,pending FROM cdn_cache_aux').fetchone()
        self.assertEqual(row, (raw, None))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cdn_cache_aux').fetchone()[0], 1)

    def test_auxiliary_existing_unknown_requires_explicit_actual_get_adoption(self):
        self.s3.store['data/descriptions.json'] = b'{"species":{}}'
        with self.assertRaises(CacheHold):
            self.pub.publish_auxiliary('descriptions.json', b'{"species":{"Synthetic species":{}}}')
        self.pub.adopt_auxiliary('descriptions.json', allow_adoption=True)
        self.pub.publish_auxiliary('descriptions.json', b'{"species":{"Synthetic species":{}}}')
        self.assertIsNone(self.db.execute('SELECT pending FROM cdn_cache_aux').fetchone()[0])

    def test_auxiliary_frozen_byte_proof_is_durable_and_checked_before_recovery(self):
        raw = b'{"species":{}}'
        self.s3.fail_after_put = 'data/descriptions.json'
        with self.assertRaises(RuntimeError):
            self.pub.publish_auxiliary('descriptions.json', raw)
        row = self.db.execute('SELECT pending_sha,pending_size FROM cdn_cache_aux').fetchone()
        self.assertEqual(row, (hashlib.sha256(raw).hexdigest(), len(raw)))
        with self.db:
            self.db.execute("UPDATE cdn_cache_aux SET pending=x'7b7d'")
        before = list(self.s3.calls)
        with self.assertRaises(CacheHold):
            self.pub.publish_auxiliary('descriptions.json', raw)
        self.assertEqual(self.s3.calls, before)

    def test_auxiliary_resume_uses_frozen_value_and_exposes_pending_keys(self):
        self.assertFalse(self.pub.resume_auxiliary('descriptions.json'))
        raw = b'{"species":{}}'
        self.s3.fail_after_put = 'data/descriptions.json'
        with self.assertRaises(RuntimeError):
            self.pub.publish_auxiliary('descriptions.json', raw)
        self.assertEqual(self.pub.status()['auxiliary_pending_keys'], ['descriptions.json'])
        self.new_connection()
        self.assertTrue(self.pub.resume_auxiliary('descriptions.json'))
        self.assertEqual(self.pub.status()['auxiliary_pending_keys'], [])
        self.assertEqual(self.s3.store['data/descriptions.json'], raw)
        self.assertFalse(self.pub.resume_auxiliary('descriptions.json'))


if __name__ == '__main__':
    unittest.main()
