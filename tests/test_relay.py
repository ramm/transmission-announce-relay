import concurrent.futures
import http.client
import io
import json
import socket
import threading
import time
import unittest
from unittest import mock

from transmission_announce_relay import config, recovery, relay

HASH = bytes(range(20))
BODY = b'd8:intervali1800e5:peers0:e'


def make_config(port=0):
    raw = {'listen': {'host': '127.0.0.1', 'port': 19053}, 'routes': {
        'example': {'upstream': 'https://tracker.example.org:8443/PASSKEY/announce'},
        'strict': {'upstream': 'https://tracker.example.org/announce', 'info_hashes': [HASH.hex()]},
        'noscrape': {'upstream': 'https://tracker.example.org/a/b'},
        'scrapeoff': {'upstream': 'https://tracker.example.org/announce', 'scrape': False}}}
    validated = config.validate(raw)
    validated['listen']['port'] = port
    return validated


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.output = mock.Mock()
        self.server = relay.Relay(make_config(), self.output)
        for route in self.server.routes.values():
            route.fetch = mock.Mock(return_value=(200, {'Content-Type': 'text/plain'}, BODY))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.query = 'info_hash=' + ''.join('%%%02x' % b for b in HASH) + '&peer_id=%00%ff%2B+&uploaded=123&event=started'
        self.timing = mock.patch.multiple(recovery, PACE=0.01, HEDGE_AFTER=0.15, REQUEUE_DELAY=0.05,
                                          MAX_REQUEUE_DELAY=0.5, HEADERS_TIMEOUT=1.0, BODY_TIMEOUT=1.0,
                                          ATTEMPT_BUDGET=2.0, HEDGED_ATTEMPT_BUDGET=2.15,
                                          ANNOUNCE_DEADLINE=6.0, CONNECT_TIMEOUT=0.5)
        self.timing.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.timing.stop()

    def get(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('GET', path, headers={'User-Agent': 'Transmission/4.0'})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_announce_is_forwarded_byte_for_byte_and_never_logged(self):
        status, headers, body = self.get('/r/example/announce?' + self.query)
        self.assertEqual((status, body), (200, BODY))
        self.assertEqual(headers['Connection'], 'close')
        self.server.routes['example'].fetch.assert_called_once_with(
            '/PASSKEY/announce?' + self.query, 'Transmission/4.0', deadline=mock.ANY, on_headers=mock.ANY, handle=mock.ANY)
        health = self.server.health()
        self.assertEqual(health['routes']['example']['counts']['announce:tracker_ok'], 1)
        for secret in ('PASSKEY', HASH.hex(), 'peer_id', 'tracker.example.org:8443/PASSKEY'):
            self.assertNotIn(secret, json.dumps(health).replace('tracker.example.org', ''))
            self.assertNotIn(secret, str(self.output.mock_calls))

    def test_scrape_maps_the_upstream_path_when_possible(self):
        self.get('/r/example/scrape?info_hash=abc')
        self.server.routes['example'].fetch.assert_called_once_with(
            '/PASSKEY/scrape?info_hash=abc', 'Transmission/4.0', deadline=mock.ANY, on_headers=mock.ANY, handle=mock.ANY)
        for name in ('noscrape', 'scrapeoff'):
            status, _, _ = self.get('/r/{}/scrape?info_hash=abc'.format(name))
            self.assertEqual(status, 404)
            self.server.routes[name].fetch.assert_not_called()

    def test_unknown_routes_and_bad_requests_never_forward(self):
        for path in ('/r/missing/announce?' + self.query, '/r/example/other?x=1', '/x', '/r/example/announce?info_hash=a&info_hash=b',
                     '/r/example/announce?peer_id=1', '/r/example/announce?' + 'a' * (relay.MAX_QUERY + 1)):
            status, _, _ = self.get(path)
            self.assertIn(status, (400, 404))
        for route in self.server.routes.values():
            route.fetch.assert_not_called()

    def test_allowlisted_route_rejects_other_hashes(self):
        other = 'info_hash=' + ''.join('%%%02x' % b for b in bytes(20))
        self.assertEqual(self.get('/r/strict/announce?' + other)[0], 403)
        self.assertEqual(self.get('/r/strict/announce?' + self.query)[0], 200)
        self.server.routes['strict'].fetch.assert_called_once()

    def test_health_is_sanitized(self):
        status, _, body = self.get('/healthz')
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health['recovery_policy'], relay.RECOVERY_POLICY)
        self.assertEqual(sorted(health['routes']), ['example', 'noscrape', 'scrapeoff', 'strict'])
        self.assertNotIn('PASSKEY', body.decode())

    def test_upstream_failure_is_safe_and_redirects_not_followed(self):
        route = self.server.routes['example']
        route.fetch.side_effect = RuntimeError('PASSKEY leak attempt')
        status, _, body = self.get('/r/example/announce?' + self.query)
        self.assertEqual(status, 502)
        self.assertNotIn(b'PASSKEY', body)
        self.assertNotIn('leak', str(self.output.mock_calls))
        route.fetch.reset_mock(side_effect=True)
        route.fetch.return_value = (302, {'Location': 'https://evil.example'}, b'redirect')
        status, headers, body = self.get('/r/example/announce?' + self.query)
        self.assertEqual((status, body), (302, b'redirect'))
        self.assertNotIn('Location', headers)
        route.fetch.assert_called_once()

    def test_tracker_rejection_and_client_errors_are_final(self):
        route = self.server.routes['example']
        for status, body in ((503, b'd14:failure reason7:blockede'), (403, b'nope'), (200, b'not bencode')):
            route.fetch.reset_mock()
            route.fetch.return_value = (status, {}, body)
            self.assertEqual(self.get('/r/example/announce?' + self.query)[0], status)
            route.fetch.assert_called_once()

    def test_server_error_is_requeued_once_and_stall_is_hedged(self):
        route = self.server.routes['example']
        route.fetch.side_effect = [(503, {'Retry-After': '0'}, b'busy'), (200, {}, BODY)]
        self.assertEqual(self.get('/r/example/announce?' + self.query)[2], BODY)
        self.assertEqual(route.fetch.call_count, 2)
        stats = self.server.health()['routes']['example']['transport']
        self.assertEqual((stats['requeued_http'], stats['recovered_groups']), (1, 1))
        release = threading.Event()
        calls = []

        def fetch(target, ua, deadline=None, on_headers=None, handle=None):
            calls.append(target)
            if len(calls) == 1:
                sock = mock.Mock()
                sock.shutdown.side_effect = lambda how: release.set()
                handle.connection = mock.Mock(sock=sock)
                release.wait(timeout=3)
                raise ConnectionResetError()
            on_headers()
            return 200, {}, BODY
        route.fetch = fetch
        self.assertEqual(self.get('/r/example/announce?' + self.query)[2], BODY)
        stats = self.server.health()['routes']['example']['transport']
        self.assertEqual((stats['hedges'], stats['hedge_wins'], stats['upstream_transport_errors']), (1, 1, 0))

    def test_routes_are_independent_and_paced_dispatch_does_not_block(self):
        entered = threading.Event()
        release = threading.Event()

        def slow(target, ua, **kwargs):
            if 'uploaded=123' in target:
                entered.set()
                release.wait(timeout=3)
            return 200, {}, BODY
        self.server.routes['example'].fetch = slow
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.get, '/r/example/announce?' + self.query)
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.get('/r/strict/announce?' + self.query)[0], 200)
            other = self.query.replace('uploaded=123', 'uploaded=456')
            self.assertEqual(pool.submit(self.get, '/r/example/announce?' + other).result(timeout=3)[0], 200)
            release.set()
            self.assertEqual(first.result(timeout=3)[0], 200)


class PriorityTests(unittest.TestCase):
    def test_announces_start_before_queued_scrapes(self):
        from transmission_announce_relay import dispatch
        order = []
        release = threading.Event()

        def fetch(target, ua, **kwargs):
            order.append(target)
            if target == '/first':
                release.wait(timeout=3)
            return 200, {}, BODY
        stopping = threading.Event()
        dispatcher = dispatch.Dispatcher(fetch, lambda body: False, lambda body: True, stopping)
        try:
            with mock.patch.object(recovery, 'PACE', 0.3), mock.patch.object(recovery, 'HEDGE_AFTER', 5.0), \
                    concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                first = pool.submit(dispatcher.submit, '/first', 'ua')
                while not order:
                    time.sleep(0.01)
                scrape = pool.submit(dispatcher.submit, '/scrape', 'ua', 1)
                time.sleep(0.05)
                announce = pool.submit(dispatcher.submit, '/announce', 'ua', 0)
                release.set()
                for future in (first, scrape, announce):
                    future.result(timeout=5)
            self.assertEqual(order, ['/first', '/announce', '/scrape'])
        finally:
            dispatcher.close()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.route = relay.Route('example', make_config()['routes']['example'], mock.Mock(), mock.Mock())
        self.addCleanup(self.route.close)

    def wire(self, raw):
        reader, writer = socket.socketpair()
        writer.sendall(raw)
        writer.close()
        response = http.client.HTTPResponse(reader)
        response.begin()
        connection = mock.Mock()
        connection.sock = reader
        connection.getresponse.return_value = response
        connection.close.side_effect = reader.close
        return connection

    def test_fetch_uses_a_fresh_connection_and_keeps_duplicate_retry_after(self):
        raw = b'HTTP/1.1 503 Busy\r\nRetry-After: 120\r\nRetry-After: 0\r\nContent-Length: 4\r\nConnection: close\r\n\r\nbusy'
        connection = self.wire(raw)
        with mock.patch('transmission_announce_relay.relay.http.client.HTTPSConnection', return_value=connection) as factory:
            status, headers, body = self.route.fetch('/PASSKEY/announce?x=1', 'UA')
        self.assertEqual((status, body), (503, b'busy'))
        self.assertEqual(headers.retry_after_values, ('120', '0'))
        factory.assert_called_once()
        self.assertEqual(factory.call_args[0][:2], ('tracker.example.org', 8443))
        connection.request.assert_called_once_with('GET', '/PASSKEY/announce?x=1', headers={
            'User-Agent': 'UA', 'Accept': '*/*', 'Accept-Encoding': 'identity', 'Connection': 'close'})
        connection.close.assert_called_once()

    def test_truncated_body_is_an_error_not_a_success(self):
        connection = self.wire(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc')
        with mock.patch('transmission_announce_relay.relay.http.client.HTTPSConnection', return_value=connection):
            with self.assertRaises(http.client.IncompleteRead):
                self.route.fetch('/PASSKEY/announce', 'UA')

    def test_no_late_announce_after_slow_connection_setup(self):
        connection = mock.Mock()
        with mock.patch('transmission_announce_relay.relay.http.client.HTTPSConnection', return_value=connection), \
                mock.patch('transmission_announce_relay.relay.time.monotonic', side_effect=[100, 100, 121]):
            with self.assertRaises(TimeoutError):
                self.route.fetch('/x', 'UA')
        connection.connect.assert_called_once()
        connection.request.assert_not_called()
        connection.close.assert_called_once()


class CliTests(unittest.TestCase):
    def test_check_validates_config_and_modes(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'relay.json')
            with open(path, 'w') as handle:
                json.dump({'routes': {'example': {'upstream': 'https://t.example.org/announce'}}}, handle)
            os.chmod(path, 0o600)
            with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(relay.main(['--config', path, '--check', '--json']), 0)
            self.assertEqual(json.loads(out.getvalue())['routes'], ['example'])
            os.chmod(path, 0o644)
            with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                self.assertEqual(relay.main(['--config', path, '--check', '--json']), 1)
            self.assertEqual(json.loads(err.getvalue())['error'], 'config_permissions_must_be_private')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr', new_callable=io.StringIO):
            relay.main(['--config', 'x', '--agent', '--json'])


if __name__ == '__main__':
    unittest.main()
