"""Upstream TLS policy: no external network; real handshakes use MemoryBIO."""
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from unittest import mock

from transmission_announce_relay import config, relay


class ContextTests(unittest.TestCase):
    def test_default_preserves_system_policy_and_verification(self):
        original = ssl.create_default_context()
        context = relay.upstream_tls_context()
        self.assertEqual(context.minimum_version, original.minimum_version)
        self.assertEqual(context.maximum_version, original.maximum_version)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_explicit_ceiling_keeps_minimum_and_verification(self):
        for label, ceiling in relay.TLS_MAX_VERSIONS.items():
            with self.subTest(version=label):
                original = ssl.create_default_context()
                original.minimum_version = ssl.TLSVersion.TLSv1_2
                original.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
                with mock.patch.object(relay.ssl, 'create_default_context', return_value=original):
                    context = relay.upstream_tls_context(label)
                self.assertIs(context, original)
                self.assertEqual(context.maximum_version, ceiling)
                self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)

    def test_does_not_raise_a_stricter_system_ceiling(self):
        context = ssl.create_default_context()
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        with mock.patch.object(relay.ssl, 'create_default_context', return_value=context):
            self.assertEqual(relay.upstream_tls_context('1.3').maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_rejects_ceiling_below_system_minimum(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        with mock.patch.object(relay.ssl, 'create_default_context', return_value=context):
            with self.assertRaisesRegex(ValueError, '^tls_max_version_below_system_minimum$'):
                relay.upstream_tls_context('1.2')
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_3)

    def test_rejects_unsupported_values_without_echoing_them(self):
        for value in ('1.0', '1.1', 'SSLv3', '', 'PRIVATE_VALUE'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '^invalid_tls_max_version$'):
                    relay.upstream_tls_context(value)

    def test_all_routes_share_the_policy_and_health_reports_requested_cap(self):
        raw = {'routes': {'example': {'upstream': 'https://tracker.example.org/SECRET/announce'},
                          'other': {'upstream': 'https://other.example.org/announce'}}}
        settings = config.validate(raw)
        settings['listen']['port'] = 0
        server = relay.Relay(settings, mock.Mock(), tls_max_version='1.2')
        try:
            first = server.routes['example'].tls
            self.assertIs(first, server.routes['other'].tls)
            self.assertEqual(first.maximum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(server.health()['upstream_tls_max_version'], '1.2')
            self.assertNotIn('SECRET', json.dumps(server.health()))
        finally:
            server.server_close()


class CliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'relay.json')
        with open(self.path, 'w') as handle:
            json.dump({'routes': {'example': {'upstream': 'https://tracker.example.org/SECRET/announce'}}}, handle)
        os.chmod(self.path, 0o600)

    def invoke(self, flags, agent_env=False, check=True):
        out, err = io.StringIO(), io.StringIO()
        args = ['--config', self.path] + (['--check'] if check else []) + flags
        with mock.patch.dict(os.environ, {'AGENT_SESSION': '1' if agent_env else '0'}), \
                mock.patch('sys.stdout', out), mock.patch('sys.stderr', err), \
                mock.patch('builtins.input', side_effect=AssertionError('must not prompt')):
            code = relay.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_check_default_and_caps_are_pure_json(self):
        for version in (None, '1.2', '1.3'):
            with self.subTest(version=version):
                args = ['--json'] + ([] if version is None else ['--tls-max-version', version])
                code, out, err = self.invoke(args)
                self.assertEqual((code, err), (0, ''))
                result = json.loads(out)
                self.assertEqual(result['event'], 'config_ok')
                self.assertEqual(result['upstream_tls_max_version'], version)
                self.assertNotIn('SECRET', out)
                self.assertNotIn(self.path, out)

    def test_human_agent_and_environment_modes(self):
        for flags, env, agent in (([], False, False), (['--agent'], False, True), ([], True, True)):
            with self.subTest(flags=flags, env=env):
                code, out, err = self.invoke(flags + ['--tls-max-version', '1.2'], agent_env=env)
                self.assertEqual((code, err), (0, ''))
                self.assertNotIn('\x1b', out)
                self.assertNotIn('SECRET', out)
                self.assertIn('upstream_tls_max_version="1.2"' if agent else 'upstream_tls_max_version: 1.2', out)
                if agent:
                    self.assertNotIn(' | ', out)
                    self.assertIn('\t', out)

    def test_invalid_value_is_a_safe_mode_aware_stderr_error(self):
        for value in ('1.0', '1.1', 'PRIVATE_VALUE', ''):
            code, out, err = self.invoke(['--json', '--tls-max-version=' + value])
            self.assertEqual((code, out), (1, ''))
            self.assertEqual(json.loads(err)['error'], 'invalid_tls_max_version')
            self.assertNotIn('PRIVATE_VALUE', err)
        for flags, env in (([], False), (['--agent'], False), ([], True)):
            code, out, err = self.invoke(flags + ['--tls-max-version', 'PRIVATE_VALUE'], agent_env=env)
            self.assertEqual((code, out), (1, ''))
            self.assertIn('invalid_tls_max_version', err)
            self.assertNotIn('PRIVATE_VALUE', err)

    def test_check_rejects_incompatible_system_minimum(self):
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        with mock.patch.object(relay.ssl, 'create_default_context', return_value=context):
            code, out, err = self.invoke(['--json', '--tls-max-version', '1.2'])
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(json.loads(err)['error'], 'tls_max_version_below_system_minimum')

    def test_start_passes_flag_to_server_and_reports_it(self):
        with mock.patch.object(relay, 'Relay') as factory, mock.patch.object(relay, 'notify_ready'):
            factory.return_value.server_address = ('127.0.0.1', 19053)
            code, out, err = self.invoke(['--json', '--tls-max-version', '1.2'], check=False)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(factory.call_args[1]['tls_max_version'], '1.2')
        self.assertEqual(json.loads(out)['upstream_tls_max_version'], '1.2')
        factory.return_value.server_close.assert_called_once()

    def test_conflicting_modes_and_missing_value_fail_without_prompting(self):
        for flags in (['--agent', '--json'], ['--tls-max-version']):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as caught:
                self.invoke(flags)
            self.assertEqual(caught.exception.code, 2)


@unittest.skipUnless(shutil.which('openssl'), 'openssl executable needed for temporary test certificate')
class HandshakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = os.path.join(cls.directory.name, 'cert.pem')
        cls.key = os.path.join(cls.directory.name, 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=tracker.example.org', '-addext', 'subjectAltName=DNS:tracker.example.org',
                        '-keyout', cls.key, '-out', cls.cert],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20)

    def negotiate(self, client_context, hostname='tracker.example.org', server_min=ssl.TLSVersion.TLSv1_2):
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = server_min
        server_context.load_cert_chain(self.cert, self.key)
        server_context.set_alpn_protocols(['http/1.1'])
        ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
        client = client_context.wrap_bio(ci, co, server_side=False, server_hostname=hostname)
        server = server_context.wrap_bio(si, so, server_side=True)
        done = [False, False]
        for _ in range(20):
            for index, peer, outbound, inbound in ((0, client, co, si), (1, server, so, ci)):
                if not done[index]:
                    try:
                        peer.do_handshake()
                        done[index] = True
                    except ssl.SSLWantReadError:
                        pass
                data = outbound.read()
                if data:
                    inbound.write(data)
            if all(done):
                return client.version(), client.selected_alpn_protocol()
        self.fail('TLS handshake did not converge')

    def trusted(self, ceiling):
        context = relay.upstream_tls_context(ceiling)
        context.load_verify_locations(cafile=self.cert)
        return context

    def test_tls12_cap_negotiates_verified_tls12(self):
        self.assertEqual(self.negotiate(self.trusted('1.2')), ('TLSv1.2', 'http/1.1'))

    @unittest.skipUnless(ssl.HAS_TLSv1_3, 'OpenSSL lacks TLS 1.3')
    def test_default_and_tls13_ceiling_can_negotiate_tls13(self):
        for ceiling in (None, '1.3'):
            self.assertEqual(self.negotiate(self.trusted(ceiling), server_min=ssl.TLSVersion.TLSv1_3)[0], 'TLSv1.3')

    @unittest.skipUnless(ssl.HAS_TLSv1_3, 'OpenSSL lacks TLS 1.3')
    def test_tls12_cap_cannot_negotiate_with_tls13_only_server(self):
        with self.assertRaises(ssl.SSLError):
            self.negotiate(self.trusted('1.2'), server_min=ssl.TLSVersion.TLSv1_3)

    def test_cap_does_not_disable_hostname_or_ca_verification(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.negotiate(self.trusted('1.2'), hostname='wrong.example.org')
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.negotiate(relay.upstream_tls_context('1.2'))


if __name__ == '__main__':
    unittest.main()
