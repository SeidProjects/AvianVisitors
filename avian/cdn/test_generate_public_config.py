import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import generate_public_config as config


class PublicConfigTests(unittest.TestCase):
    def setUp(self):
        self.required = dict(AVIAN_MEDIA_ORIGIN='https://media.example.net',
                             AVIAN_SNAPSHOT_INDEX_URL='https://media.example.net/data/snapshot-index.json')

    def test_default_and_private_only_environment_preserve_legacy(self):
        for environment in ({}, {'PRIVATE_API_KEY': 'do-not-publish', 'R2_SECRET': 'keep-private'}):
            resolved = config.public_config(environment)
            self.assertIs(resolved['AVIAN_PUBLIC_STATIC_MODE'], False)
            self.assertTrue(all(resolved[name] == '' for name in config.URL_NAMES))

    def test_both_public_required_urls_auto_enable_when_flag_absent(self):
        self.assertIs(config.public_config(self.required)['AVIAN_PUBLIC_STATIC_MODE'], True)

    def test_explicit_enabled_mode_requires_both_settings(self):
        for environment in ({'AVIAN_CDN_ENABLED': '1'},
                            {'AVIAN_CDN_ENABLED': '1', 'AVIAN_MEDIA_ORIGIN': self.required['AVIAN_MEDIA_ORIGIN']}):
            with self.assertRaisesRegex(ValueError, 'requires both'): config.public_config(environment)
        self.assertIs(config.public_config(dict(self.required, AVIAN_CDN_ENABLED='1'))['AVIAN_PUBLIC_STATIC_MODE'], True)

    def test_explicit_disabled_clears_urls_and_ignores_stale_invalid_public_settings(self):
        result = config.public_config(dict(self.required, AVIAN_CDN_ENABLED='0', AVIAN_WIKI_INDEX_URL='secret-stale-string'))
        self.assertEqual(result, dict(AVIAN_PUBLIC_STATIC_MODE=False, **{name: '' for name in config.URL_NAMES}))

    def test_any_partial_public_config_fails_clearly(self):
        for name in config.URL_NAMES:
            with self.assertRaisesRegex(ValueError, 'requires both'):
                config.public_config({name: 'https://media.example.net/example'})

    def test_flag_accepts_only_exact_zero_or_one(self):
        for value in ('', 'true', 'false', 'yes', '01', ' 1', True, 1):
            with self.assertRaisesRegex(ValueError, 'exactly 0 or 1'):
                config.public_config(dict(self.required, AVIAN_CDN_ENABLED=value))

    def test_https_clean_origin_and_same_origin_routes(self):
        for name, value in (
                ('AVIAN_MEDIA_ORIGIN', 'http://media.example.net'),
                ('AVIAN_MEDIA_ORIGIN', 'https://media.example.net/media'),
                ('AVIAN_MEDIA_ORIGIN', 'https://user:token@media.example.net'),
                ('AVIAN_MEDIA_ORIGIN', 'https://media.example.net?token=value'),
                ('AVIAN_MEDIA_ORIGIN', 'https://media.example.net#x'),
                ('AVIAN_SNAPSHOT_INDEX_URL', '/data/snapshot-index.json'),
                ('AVIAN_SNAPSHOT_INDEX_URL', 'https://other.example.org/data/snapshot-index.json'),
                ('AVIAN_SNAPSHOT_INDEX_URL', 'https://media.example.net/data?token=value'),
                ('AVIAN_WIKI_INDEX_URL', 'https://other.example.org/descriptions.json')):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                config.public_config(dict(self.required, **{name: value}))

    def test_parser_ambiguities_and_invalid_ports_fail(self):
        for value in (' https://media.example.net', 'https://media.example.net\n', 'https://media.example.net\\@other.example.org',
                      'https://media.example.net:bad', 'https://media.example.net:65536', 'https://media.example.net:',
                      'https://media%2eexample.net', 'https://[not-an-ip]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.public_config(dict(self.required, AVIAN_MEDIA_ORIGIN=value))

    def test_default_https_port_host_case_and_trailing_origin_slash_normalize(self):
        result = config.public_config(dict(self.required, AVIAN_MEDIA_ORIGIN='https://MEDIA.EXAMPLE.NET:443/',
                                           AVIAN_WIKI_INDEX_URL='https://media.example.net:443/descriptions.json'))
        self.assertEqual(result['AVIAN_MEDIA_ORIGIN'], 'https://media.example.net')
        self.assertEqual(result['AVIAN_WIKI_INDEX_URL'], 'https://media.example.net:443/descriptions.json')

    def test_public_js_allowlist_never_serializes_private_environment(self):
        environment = dict(self.required, AVIAN_CDN_ENABLED='1', PRIVATE_API_KEY='unique-secret-value',
                           R2_ACCESS_KEY_ID='private-access-value', AVIAN_PRIVATE_ADMIN_URL='https://private.example/?token=secret',
                           AVIAN_LIVE_STREAM_URL='https://private.example/?token=secret',
                           UNKNOWN_PUBLIC_SETTING='do-not-copy')
        body = config.render(config.public_config(environment))
        for secret in ('unique-secret-value', 'private-access-value', 'token=secret', 'PRIVATE_API_KEY', 'UNKNOWN_PUBLIC_SETTING'):
            self.assertNotIn(secret, body)
        assignments = [line for line in body.splitlines() if line.startswith('window.')]
        self.assertEqual(len(assignments), 5)
        self.assertEqual([line.split(' = ')[0][7:] for line in assignments], list(config.WINDOW_NAMES))
        self.assertIs(json.loads(assignments[0].split(' = ')[1][:-1]), True)

    def test_invalid_build_keeps_prior_artifact_and_exits_unsuccessfully_without_secret_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'public-config.js'
            output.write_text('previous verified release')
            with self.assertRaises(ValueError):
                config.generate(dict(self.required, AVIAN_CDN_ENABLED='1', AVIAN_MEDIA_ORIGIN='http://media.example.net'), output)
            self.assertEqual(output.read_text(), 'previous verified release')
            result = subprocess.run([sys.executable, str(Path(config.__file__)), '--output', str(output)],
                                    env={'AVIAN_CDN_ENABLED': '1', 'PRIVATE_API_KEY': 'unique-secret-value'},
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('requires both', result.stderr)
            self.assertNotIn('unique-secret-value', result.stderr + result.stdout)
            self.assertEqual(output.read_text(), 'previous verified release')

    def test_valid_build_writes_exact_generated_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'frontend' / 'public-config.js'
            body = config.generate(self.required, output)
            self.assertEqual(output.read_bytes(), body)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(output.parent.glob('.public-config.js.*')), [])

    def test_optional_updates_url_allows_a_separate_worker_origin(self):
        result = config.public_config(dict(self.required, AVIAN_UPDATES_URL='wss://updates.example.org/live'))
        self.assertEqual(result['AVIAN_UPDATES_URL'], 'wss://updates.example.org/live')
        self.assertEqual(config.public_config(self.required)['AVIAN_UPDATES_URL'], '')

    def test_updates_url_rejects_credentials_queries_fragments_and_parser_ambiguities(self):
        for value in ('ws://updates.example.org/live', 'https://updates.example.org/live',
                      'wss://user:secret@updates.example.org/live', 'wss://updates.example.org/live?token=x',
                      'wss://updates.example.org/live#x', 'wss://updates.example.org/live?',
                      'wss://updates.example.org/live#', ' wss://updates.example.org/live',
                      'wss://updates.example.org\\@other.example.org', 'wss://updates.example.org:65536',
                      'wss://updates.example.org:'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.public_config(dict(self.required, AVIAN_UPDATES_URL=value))

    def test_disabled_updates_setting_is_cleared_and_notification_token_never_serializes(self):
        result = config.public_config(dict(self.required, AVIAN_CDN_ENABLED='0', AVIAN_UPDATES_URL='invalid-secret'))
        self.assertEqual(result['AVIAN_UPDATES_URL'], '')
        body = config.render(config.public_config(dict(self.required, AVIAN_UPDATES_URL='wss://updates.example.org/live',
                            AVIAN_UPDATES_NOTIFY_TOKEN='unique-notification-secret')))
        self.assertNotIn('unique-notification-secret', body)
        self.assertNotIn('AVIAN_UPDATES_NOTIFY_TOKEN', body)


if __name__ == '__main__':
    unittest.main()
