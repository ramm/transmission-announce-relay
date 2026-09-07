"""Loopback announce relay daemon. Python 3.8+; standard library only.

Transmission -> http://127.0.0.1:19053/r/<route>/announce?... -> fresh verified
HTTPS connection to the route's upstream tracker -> response bytes returned
unchanged. Scrape (``/r/<route>/scrape``) is mapped when the upstream path
contains ``announce``.
"""
import argparse
import http.client
import http.server
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse

from . import config as config_module
from . import recovery
from .dispatch import Aborted, Dispatcher, QueueExpired
from .output import Output, resolve_mode

MAX_RESPONSE = 1024 * 1024
MAX_QUERY = 8192
RECOVERY_POLICY = 'paced-hedge-requeue-v4'


def bdecode(data):
    def read(pos, depth=0):
        if depth > 24 or pos >= len(data):
            raise ValueError('invalid_bencode')
        ch = data[pos:pos + 1]
        if ch == b'i':
            end = data.index(b'e', pos + 1)
            raw = data[pos + 1:end]
            if len(raw) > 20 or not re.fullmatch(b'-?[0-9]+', raw):
                raise ValueError('invalid_integer')
            return int(raw), end + 1
        if ch in (b'l', b'd'):
            result = [] if ch == b'l' else {}
            pos += 1
            while data[pos:pos + 1] != b'e':
                key, pos = read(pos, depth + 1)
                if ch == b'l':
                    result.append(key)
                else:
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError('invalid_key')
                    value, pos = read(pos, depth + 1)
                    result[key] = value
            return result, pos + 1
        end = data.index(b':', pos)
        raw = data[pos:end]
        if len(raw) > 8 or not raw.isdigit():
            raise ValueError('invalid_length')
        length = int(raw)
        start = end + 1
        if start + length > len(data):
            raise ValueError('short_string')
        return data[start:start + length], start + length
    value, end = read(0)
    if end != len(data):
        raise ValueError('trailing_data')
    return value


def bencoded_rejection(body):
    """A bencoded dictionary with a failure reason is a final tracker answer."""
    if not body.startswith(b'd'):
        return False
    try:
        parsed = bdecode(body)
    except (ValueError, IndexError, OverflowError, RecursionError):
        return False
    return isinstance(parsed, dict) and b'failure reason' in parsed


def classify(status, body, kind):
    result = {'http_status': status, 'response_bytes': len(body)}
    if status != 200:
        result['outcome'] = 'upstream_http_error'
    elif kind == 'scrape':
        result['outcome'] = 'scrape_ok' if body.startswith(b'd') else 'unexpected_body'
    else:
        try:
            value = bdecode(body)
            if not isinstance(value, dict):
                raise ValueError('not_dictionary')
            if value.get(b'failure reason'):
                result['outcome'] = 'tracker_rejected'
            elif isinstance(value.get(b'interval'), int) and value[b'interval'] > 0:
                result['outcome'] = 'tracker_ok'
                result['interval_seconds'] = value[b'interval']
                if isinstance(value.get(b'min interval'), int):
                    result['min_interval_seconds'] = value[b'min interval']
                result['has_warning'] = bool(value.get(b'warning message'))
            else:
                result['outcome'] = 'unexpected_body'
        except (ValueError, IndexError, OverflowError, RecursionError):
            result['outcome'] = 'unexpected_body'
    return result


class Route:
    def __init__(self, name, spec, tls, output):
        self.name = name
        self.spec = spec
        self.tls = tls
        self.stopping = threading.Event()
        # Indirection so tests (and subclasses) can replace ``fetch`` on the instance.
        self.dispatcher = Dispatcher(lambda *args, **kwargs: self.fetch(*args, **kwargs), bencoded_rejection,
                                     lambda body: classify(200, body, 'announce')['outcome'] == 'tracker_ok',
                                     self.stopping)
        self.allowed = None if spec['info_hashes'] is None else {bytes.fromhex(h) for h in spec['info_hashes']}
        self.lock = threading.Lock()
        self.counts = {}
        self.last = None

    def close(self):
        self.stopping.set()
        self.dispatcher.close()

    def record(self, payload):
        with self.lock:
            key = payload['kind'] + ':' + payload['outcome']
            self.counts[key] = self.counts.get(key, 0) + 1
            self.last = payload

    def stats(self):
        with self.lock:
            counts = dict(self.counts)
            last = self.last
        result = {'upstream_host': self.spec['host'], 'counts': counts, 'last': last,
                  'allowlisted_info_hashes': None if self.allowed is None else len(self.allowed)}
        result.update(self.dispatcher.stats())
        return result

    def fetch(self, target, user_agent, deadline=None, on_headers=None, handle=None):
        """One direct TLS connection; no redirects, proxy environment, or pooling.

        Fail fast: connect within CONNECT_TIMEOUT, headers within HEADERS_TIMEOUT
        of the socket start, body within BODY_TIMEOUT after headers, all capped by
        ``deadline``. ``on_headers`` may raise Aborted when another socket already
        won; ``handle`` lets the dispatcher shut this socket down.
        """
        started = time.monotonic()
        deadline = min(deadline or float('inf'), started + recovery.ATTEMPT_BUDGET)
        headers_deadline = min(deadline, started + recovery.HEADERS_TIMEOUT)

        def remaining(until):
            left = until - time.monotonic()
            if handle is not None and handle.aborted:
                raise Aborted()
            if left <= 0:
                raise TimeoutError('upstream_deadline')
            return left

        connection = http.client.HTTPSConnection(
            self.spec['host'], self.spec['port'], context=self.tls,
            timeout=min(recovery.CONNECT_TIMEOUT, remaining(headers_deadline)))
        if handle is not None:
            with handle.lock:
                handle.connection = connection
            if handle.aborted:
                connection.close()
                raise Aborted()
        response = None
        try:
            connection.connect()
            connection.sock.settimeout(remaining(headers_deadline))
            connection.request('GET', target, headers={
                'User-Agent': user_agent, 'Accept': '*/*',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            connection.sock.settimeout(remaining(headers_deadline))
            response = connection.getresponse()
            if on_headers is not None:
                on_headers()
            body_deadline = min(deadline, time.monotonic() + recovery.BODY_TIMEOUT)
            headers = recovery.ResponseHeaders(response.getheaders())
            body = bytearray()
            while not response.isclosed():
                raw_socket = response.fp.raw._sock  # Connection: close can detach connection.sock
                raw_socket.settimeout(remaining(body_deadline))
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise ValueError('upstream_response_too_large')
            if response.length is not None and response.length > 0:
                raise http.client.IncompleteRead(bytes(body), response.length)
            return response.status, headers, bytes(body)
        finally:
            if response is not None:
                response.close()
            connection.close()


class Relay(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config, output):
        self.config = config
        self.output = output
        self.started = int(time.time())
        tls = ssl.create_default_context()
        tls.set_alpn_protocols(['http/1.1'])
        self.routes = {name: Route(name, spec, tls, output) for name, spec in config['routes'].items()}
        super().__init__((config['listen']['host'], config['listen']['port']), Handler)

    def server_close(self):
        super().server_close()
        for route in self.routes.values():
            route.close()

    def health(self):
        return {'status': 'ready', 'started': self.started, 'recovery_policy': RECOVERY_POLICY,
                'upstream_http': '1.1', 'fresh_connection_per_request': True,
                'max_attempts': recovery.MAX_ATTEMPTS,
                'announce_deadline_seconds': recovery.ANNOUNCE_DEADLINE,
                'routes': {name: route.stats() for name, route in self.routes.items()}}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'transmission-announce-relay'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass  # Never log paths, query strings, passkeys, or peer identifiers.

    def send_error(self, code, message=None, explain=None):
        self.respond(code, b'request rejected\n', {})

    def respond(self, code, body, headers):
        self.close_connection = True
        try:
            self.send_response(code)
            self.send_header('Connection', 'close')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            for name, value in headers.items():
                if name.lower() in ('content-type', 'content-encoding'):
                    self.send_header(name, value)
            for value in getattr(headers, 'retry_after_values', ()):
                self.send_header('Retry-After', value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0':
            return self.respond(400, b'body not accepted\n', {})
        if self.path == '/healthz':
            return self.respond(200, json.dumps(self.server.health(), sort_keys=True).encode(),
                                {'Content-Type': 'application/json'})
        if not self.path.startswith('/r/') or any(ord(c) < 33 or ord(c) > 126 for c in self.path):
            return self.respond(404, b'not found\n', {})
        path, sep, query = self.path.partition('?')
        parts = path.split('/')
        if len(parts) != 4 or parts[3] not in ('announce', 'scrape') or len(query) > MAX_QUERY or '#' in query:
            return self.respond(404, b'not found\n', {})
        route = self.server.routes.get(parts[2])
        if route is None:
            return self.respond(404, b'not found\n', {})
        kind = parts[3]
        upstream_path = route.spec['path']
        if kind == 'scrape':
            if not route.spec.get('scrape', True) or 'announce' not in upstream_path:
                return self.respond(404, b'scrape not available\n', {})
            upstream_path = upstream_path[::-1].replace('announce'[::-1], 'scrape'[::-1], 1)[::-1]
        metadata = {}
        if kind == 'announce':
            hashes = [urllib.parse.unquote_to_bytes(value.replace('+', ' '))
                      for name, _, value in (field.partition('=') for field in query.split('&'))
                      if urllib.parse.unquote_to_bytes(name) == b'info_hash']
            if len(hashes) != 1:
                return self.respond(400, b'one info_hash required\n', {})
            if route.allowed is not None and hashes[0] not in route.allowed:
                return self.respond(403, b'torrent not allowlisted\n', {})
            events = [urllib.parse.unquote_to_bytes(field.partition('=')[2]) for field in query.split('&')
                      if field.partition('=')[0] == 'event']
            event = events[0] if len(events) == 1 else b''
            metadata['announce_event'] = event.decode() if event in (b'started', b'stopped', b'completed') else 'update'
        # The original encoded query is forwarded byte-for-byte; binary hashes are never re-encoded.
        target = upstream_path + (sep + query if sep else '')
        started = time.monotonic()
        user_agent = self.headers.get('User-Agent', 'transmission-announce-relay/1')
        if len(user_agent) > 256 or any(ord(c) < 32 or ord(c) > 126 for c in user_agent):
            user_agent = 'transmission-announce-relay/1'
        base = {'event': 'request', 'route': route.name, 'kind': kind, 'time': int(time.time())}
        base.update(metadata)
        try:
            status, headers, body, queue_wait_ms, attempts, coalesced = route.dispatcher.submit(
                target, user_agent, priority=1 if kind == 'scrape' else 0)
            record = classify(status, body, kind)
            record.update(base, elapsed_ms=round((time.monotonic() - started) * 1000),
                          queue_wait_ms=queue_wait_ms, upstream_attempts=attempts, coalesced=coalesced)
            route.record(record)
            self.server.output.emit(record)
            self.respond(status, body, headers)
        except (queue.Full, QueueExpired) as exc:
            record = dict(base, outcome='queue_full' if isinstance(exc, queue.Full) else 'queue_expired',
                          elapsed_ms=round((time.monotonic() - started) * 1000))
            route.record(record)
            self.server.output.emit(record)
            self.respond(503, b'relay queue unavailable\n', {})
        except Exception as exc:
            # Exception strings can contain the upstream URL; only the type is logged.
            record = dict(base, outcome='network_error', error_type=type(exc).__name__,
                          elapsed_ms=round((time.monotonic() - started) * 1000))
            route.record(record)
            self.server.output.emit(record)
            self.respond(502, b'upstream request failed\n', {})


def notify_ready():
    """systemd Type=notify readiness, when NOTIFY_SOCKET is set."""
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(b'READY=1')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='transmission-announce-relay', description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True, help='private JSON config (chmod 600)')
    parser.add_argument('--check', action='store_true', help='validate the config and exit')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--agent', action='store_true')
    modes.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    output = Output(resolve_mode(args.agent, args.json))
    server = None
    try:
        config = config_module.load(args.config)
        if args.check:
            output.emit({'event': 'config_ok', 'routes': sorted(config['routes']),
                         'listen': '{}:{}'.format(config['listen']['host'], config['listen']['port'])})
            return 0
        server = Relay(config, output)
        output.emit({'event': 'ready', 'listen': '{}:{}'.format(*server.server_address[:2]),
                     'routes': sorted(config['routes'])})
        notify_ready()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = str(exc)
        safe = message if message and all(c.islower() or c == '_' for c in message) else 'relay_start_failed'
        output.emit({'error': safe, 'error_type': type(exc).__name__}, error=True)
        return 1
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
