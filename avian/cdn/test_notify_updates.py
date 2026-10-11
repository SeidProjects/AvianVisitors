import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import export_snapshots
import notify_updates as notify
from publish_cache import CurrentCachePublisher
import sync_media as sync
from test_publish_cache import S3
from test_sync_media import Clock


class Response:
    def __init__(self, body=b'{}', status=200):
        self.status, self.body = status, io.BytesIO(body)
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.body.close()


class Opener:
    """One synthetic HTTP operation, never a socket or credential lookup."""
    def __init__(self, response=None, fail=False, before=None):
        self.response = response
        self.fail, self.before, self.calls = fail, before, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.before:
            self.before()
        if self.fail:
            raise TimeoutError('synthetic response loss containing private values')
        if self.response is None:
            self.response = Response(json.dumps(dict(json.loads(request.data), status='updated')).encode())
        return self.response


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        audio, images, state = [self.root / name for name in ('audio', 'images', 'state')]
        for path in (audio, images, state): path.mkdir(mode=0o700)
        self.sync = sync.Config('https://storage.example.net', 'synthetic-media', 'https://media.example.net',
                                audio, images, state)
        self.db = sync.open_ledger(self.sync, outer_lock_held=True); self.addCleanup(self.db.close)
        self.clock = Clock()
        self.guard = sync.SyncGuard(self.sync, clock=self.clock, sleep=self.clock.sleep,
                                   resources=lambda: (3 * sync.GIB, sync.GIB))
        self.s3 = S3(self.guard)
        self.publisher = CurrentCachePublisher(self.s3, self.db,
            dict(endpoint_url=self.sync.endpoint_url, bucket=self.sync.bucket,
                 single_publisher=True, cleanup_current_cache=True), self.guard, outer_lock_held=True)
        self.source = self.root / 'source.sqlite'
        writer = sqlite3.connect(self.source)
        try:
            writer.execute('CREATE TABLE detections(Date TEXT,Time TEXT,Sci_Name TEXT,Com_Name TEXT,Confidence REAL,File_Name TEXT)')
            writer.execute('INSERT INTO detections VALUES(?,?,?,?,?,?)',
                           ('2024-05-01', '08:00:00', 'Synthetic species', 'Synthetic bird', .8, 'clip.mp3'))
            writer.commit()
        finally:
            writer.close()
        self.config = notify.NotifyConfig.from_env({'AVIAN_SYNC_NOTIFY_URL': 'https://updates.example.net/notify',
                                                    'AVIAN_SYNC_NOTIFY_TOKEN': 'synthetic_' + 'x' * 32})
        self.sequence = 0

    def select(self, generation=None):
        self.sequence += 1
        candidate = self.root / ('candidate-' + str(self.sequence))
        export_snapshots.export(self.source, candidate,
            instant=dt.datetime.fromisoformat('2024-05-01T13:00:00+00:00'),
            generation=generation or ('synthetic-' + str(self.sequence)))
        self.publisher.publish(candidate)
        return json.loads(bytes(self.db.execute("SELECT raw FROM cdn_cache_graph WHERE slot='current'").fetchone()[0]))

    def send(self, opener, config=None):
        return notify.notify_current(self.sync, self.db, self.guard,
                                     config or self.config, opener=opener)

    def delivery(self):
        return self.db.execute('SELECT generation,as_of,status FROM cdn_update_delivery WHERE id=1').fetchone()

    def test_unconfigured_is_disabled_without_ledger_queries_or_http(self):
        class NeverLedger:
            def execute(self, *args): raise AssertionError('disabled notification queried state')
        opener = Opener()
        self.assertIsNone(notify.NotifyConfig.from_env({}))
        self.assertEqual(notify.notify_current(self.sync, NeverLedger(), self.guard, None, opener=opener), 'disabled')
        self.assertEqual(opener.calls, [])

    def test_partial_unsafe_config_and_secret_repr_are_rejected_or_hidden(self):
        token = self.config.token
        for env in ({'AVIAN_SYNC_NOTIFY_URL': self.config.url}, {'AVIAN_SYNC_NOTIFY_TOKEN': token},
                    {'AVIAN_SYNC_NOTIFY_URL': self.config.url, 'AVIAN_SYNC_NOTIFY_TOKEN': 'short'}):
            with self.assertRaises(ValueError): notify.NotifyConfig.from_env(env)
        for url in ('http://updates.example.net/notify', 'https://user@updates.example.net/notify',
                    'https://updates.example.net:444/notify', 'https://updates.example.net/other',
                    'https://updates.example.net/notify?token=secret', 'https://updates.example.net/notify#secret',
                    'https://updates.example.net/notify?', 'https://updates.example.net/notify#'):
            with self.assertRaises(ValueError):
                notify.NotifyConfig.from_env({'AVIAN_SYNC_NOTIFY_URL': url, 'AVIAN_SYNC_NOTIFY_TOKEN': token})
        self.assertNotIn(token, repr(self.config))
        self.assertNotIn(self.config.url, repr(self.config))

    def test_no_current_and_actual_frozen_pending_graph_never_notify(self):
        opener = Opener()
        self.assertEqual(self.send(opener), 'unavailable')
        candidate = self.root / 'pending-candidate'
        export_snapshots.export(self.source, candidate,
            instant=dt.datetime.fromisoformat('2024-05-01T13:00:00+00:00'), generation='pending-only')
        self.publisher.prepare(candidate)
        self.assertIsNotNone(self.db.execute("SELECT raw FROM cdn_cache_graph WHERE slot='pending'").fetchone())
        self.assertEqual(self.send(opener), 'unavailable')
        self.assertEqual(opener.calls, [])

    def test_current_is_delivered_after_durable_pending_with_exact_one_attempt(self):
        index = self.select()
        before = self.guard.attempts
        opener = Opener(before=lambda: self.assertEqual(self.delivery(),
                                                        (index['generation'], index['as_of'], 'pending')))
        self.assertEqual(self.send(opener), 'delivered')
        self.assertEqual(self.guard.attempts, before + 1)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual((request.full_url, request.get_method(), timeout), (self.config.url, 'POST', 3))
        self.assertEqual(json.loads(request.data), {'generation': index['generation'], 'as_of': index['as_of']})
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.config.token)
        self.assertEqual(opener.response.read_sizes, [1025])
        self.assertTrue(opener.response.body.closed)
        self.assertEqual(self.delivery()[2], 'delivered')

    def test_unchanged_acknowledged_generation_has_no_second_http_attempt(self):
        self.select(); self.assertEqual(self.send(Opener()), 'delivered')
        before = self.guard.attempts; opener = Opener()
        self.assertEqual(self.send(opener), 'unchanged')
        self.assertEqual(self.guard.attempts, before)
        self.assertEqual(opener.calls, [])

    def test_endpoint_or_token_rotation_renotifies_current_with_only_hashed_identity(self):
        self.select(); self.assertEqual(self.send(Opener()), 'delivered')
        original = self.db.execute('SELECT identity FROM cdn_update_delivery').fetchone()[0]
        for config in (notify.NotifyConfig('https://other.example.net/notify', self.config.token),
                       notify.NotifyConfig('https://other.example.net/notify', 'rotated_' + 'y' * 32)):
            opener = Opener(); self.assertEqual(self.send(opener, config), 'delivered')
            identity = self.db.execute('SELECT identity FROM cdn_update_delivery').fetchone()[0]
            self.assertNotEqual(identity, original); self.assertRegex(identity, r'^[a-f0-9]{64}$')
            self.assertNotIn(config.token, str(self.db.execute('SELECT * FROM cdn_update_delivery').fetchall()))
            self.assertEqual(len(opener.calls), 1); original = identity

    def test_pending_reply_loss_survives_reopen_and_retries_latest_once(self):
        index = self.select(); opener = Opener(fail=True)
        self.assertEqual(self.send(opener), 'deferred')
        self.assertEqual(self.delivery(), (index['generation'], index['as_of'], 'pending'))
        old = self.db; self.db = sync.open_ledger(self.sync, outer_lock_held=True)
        self.addCleanup(self.db.close); old.close()
        retry = Opener(); before = self.guard.attempts
        self.assertEqual(self.send(retry), 'delivered')
        self.assertEqual(self.guard.attempts, before + 1)
        self.assertEqual(len(retry.calls), 1)

    def test_coalescing_new_selected_generation_replaces_pending_without_history(self):
        first = self.select('older'); self.assertEqual(self.send(Opener(fail=True)), 'deferred')
        newer = self.select('newer'); opener = Opener()
        self.assertEqual(self.send(opener), 'delivered')
        self.assertEqual(json.loads(opener.calls[0][0].data)['generation'], 'newer')
        self.assertNotEqual(first['generation'], newer['generation'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cdn_update_delivery').fetchone()[0], 1)

    def test_existing_current_never_announces_different_frozen_pending_generation(self):
        index = self.select('current')
        candidate = self.root / 'new-pending'
        export_snapshots.export(self.source, candidate,
            instant=dt.datetime.fromisoformat('2024-05-01T13:00:00+00:00'), generation='pending')
        self.publisher.prepare(candidate)
        opener = Opener(); self.assertEqual(self.send(opener), 'delivered')
        self.assertEqual(json.loads(opener.calls[0][0].data)['generation'], index['generation'])

    def test_invalid_selected_phase_generation_or_clock_never_sends(self):
        index = self.select()
        for values, phase in ((dict(index), 'pointer'), (dict(index, generation='../invalid'), 'selected'),
                              (dict(index, as_of='2024-05-01T13:00:00'), 'selected'),
                              (dict(index, as_of='bad-clock'), 'selected')):
            with self.db:
                self.db.execute("UPDATE cdn_cache_graph SET raw=?,phase=? WHERE slot='current'",
                                (json.dumps(values).encode(), phase))
            opener = Opener()
            with self.assertRaises(ValueError): self.send(opener)
            self.assertEqual(opener.calls, [])

    def test_request_time_disk_and_memory_holds_defer_without_http_or_cache_rollback(self):
        self.select(); selected = self.db.execute("SELECT raw,phase FROM cdn_cache_graph WHERE slot='current'").fetchone()
        before = self.guard.attempts
        self.guard.attempts = 150
        opener = Opener(); self.assertEqual(self.send(opener), 'deferred')
        self.assertEqual(opener.calls, []); self.assertEqual(self.guard.attempts, 150)
        self.guard.attempts = before
        self.clock.now = 120
        self.assertEqual(self.send(Opener()), 'deferred')
        self.clock.now = 0
        for resources in ((sync.MIB, sync.GIB), (3 * sync.GIB, sync.MIB)):
            self.guard.resources = lambda: resources; self.guard.next_resource_check = 0
            held = Opener(); self.assertEqual(self.send(held), 'deferred'); self.assertEqual(held.calls, [])
        self.assertEqual(self.db.execute("SELECT raw,phase FROM cdn_cache_graph WHERE slot='current'").fetchone(), selected)

    def test_local_state_cap_defers_before_any_optional_send(self):
        self.select()
        opener = Opener()
        with patch.object(sync, 'check_state_budget', side_effect=sync.BudgetStop('synthetic state cap')):
            self.assertEqual(self.send(opener), 'deferred')
        self.assertEqual(opener.calls, [])

    def test_bad_status_and_oversize_reply_remain_pending_with_no_retries(self):
        self.select()
        for response in (Response(status=500), Response(b'x' * 1025), Response(status=302)):
            opener = Opener(response=response); before = self.guard.attempts
            self.assertEqual(self.send(opener), 'deferred')
            self.assertEqual(self.guard.attempts, before + 1)
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(self.delivery()[2], 'pending')
            self.assertTrue(response.body.closed)

    def test_acknowledgment_must_echo_generation_clock_and_supported_status(self):
        index = self.select()
        for value in ([], {}, dict(generation=index['generation'], as_of=index['as_of'], status='bad'),
                      dict(generation='other', as_of=index['as_of'], status='updated'),
                      dict(generation=index['generation'], as_of='other', status='updated')):
            opener = Opener(Response(json.dumps(value).encode()))
            self.assertEqual(self.send(opener), 'deferred')
            self.assertEqual(self.delivery()[2], 'pending')
        for body in (b'<html>wrong route</html>', b'not-json'):
            self.assertEqual(self.send(Opener(Response(body))), 'deferred')
        self.assertEqual(self.send(Opener(Response(status=204))), 'deferred')
        ignored = json.dumps(dict(generation=index['generation'], as_of=index['as_of'], status='ignored')).encode()
        self.assertEqual(self.send(Opener(Response(ignored))), 'delivered')

    def test_redirect_handler_refuses_other_host_and_default_opener_uses_it(self):
        with self.assertRaises(ValueError):
            notify.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other.example.net/notify')
        self.select(); opener = Opener()
        with patch.object(notify.urllib.request, 'build_opener', return_value=opener) as make:
            self.assertEqual(notify.notify_current(self.sync, self.db, self.guard, self.config), 'delivered')
        self.assertIsInstance(make.call_args.args[0], notify.NoRedirect)
        self.assertEqual(len(opener.calls), 1)

    def test_private_error_values_never_appear_in_return_or_output(self):
        self.select(); opener = Opener(fail=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            status = self.send(opener)
        self.assertEqual(status, 'deferred')
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), '')
        self.assertNotIn(self.config.token, status)
        self.assertNotIn(self.config.url, status)


if __name__ == '__main__':
    unittest.main()
