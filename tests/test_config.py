import json
import os
import tempfile
import unittest

from transmission_announce_relay import config

GOOD = {'listen': {'host': '127.0.0.1', 'port': 19053},
        'routes': {'example': {'upstream': 'https://tracker.example.org:8443/PASSKEY/announce'}}}


class ConfigTests(unittest.TestCase):
    def test_validate_good_config(self):
        result = config.validate(GOOD)
        route = result['routes']['example']
        self.assertEqual((route['host'], route['port'], route['path']),
                         ('tracker.example.org', 8443, '/PASSKEY/announce'))
        self.assertIsNone(route['info_hashes'])
        self.assertEqual(config.local_announce_url(19053, 'example'),
                         'http://127.0.0.1:19053/r/example/announce')

    def test_defaults_and_allowlist(self):
        result = config.validate({'routes': {'x': {'upstream': 'https://t.example.org/announce',
                                                    'info_hashes': ['A' * 40, 'a' * 40, 'b' * 40]}}})
        self.assertEqual(result['listen'], {'host': '127.0.0.1', 'port': config.DEFAULT_PORT})
        self.assertEqual(result['routes']['x']['info_hashes'], ['a' * 40, 'b' * 40])
        self.assertEqual(result['routes']['x']['port'], 443)

    def test_rejects_unsafe_or_malformed(self):
        cases = [
            ({}, 'routes_required'),
            ({'routes': {}}, 'routes_required'),
            ({'listen': {'host': '0.0.0.0'}, 'routes': GOOD['routes']}, 'listen_host_must_be_loopback'),
            ({'listen': {'port': 80}, 'routes': GOOD['routes']}, 'invalid_listen_port'),
            ({'routes': {'bad name': {'upstream': 'https://t.example.org/announce'}}}, 'invalid_route_name'),
            ({'routes': {'x': {}}}, 'route_upstream_required'),
            ({'routes': {'x': {'upstream': 'http://t.example.org/announce'}}}, 'route_upstream_must_be_clean_https_url'),
            ({'routes': {'x': {'upstream': 'https://t.example.org/announce?x=1'}}}, 'route_upstream_must_be_clean_https_url'),
            ({'routes': {'x': {'upstream': 'https://u:p@t.example.org/announce'}}}, 'route_upstream_must_be_clean_https_url'),
            ({'routes': {'x': {'upstream': 'https://t.example.org/announce', 'info_hashes': ['zz']}}}, 'invalid_info_hashes'),
            ({'routes': GOOD['routes'], 'extra': 1}, 'unknown_config_keys'),
            ({'routes': {'x': {'upstream': 'https://t.example.org/announce', 'scrape': 'no'}}}, 'invalid_scrape_flag'),
            ({'routes': {'x': {'upstream': 'https://t.example.org/announce', 'bogus': 1}}}, 'unknown_route_keys'),
        ]
        for raw, code in cases:
            with self.assertRaisesRegex(config.ConfigError, code):
                config.validate(raw)

    def test_load_requires_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'relay.json')
            with open(path, 'w') as handle:
                json.dump(GOOD, handle)
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(config.ConfigError, 'config_permissions_must_be_private'):
                config.load(path)
            os.chmod(path, 0o600)
            self.assertEqual(sorted(config.load(path)['routes']), ['example'])


if __name__ == '__main__':
    unittest.main()
