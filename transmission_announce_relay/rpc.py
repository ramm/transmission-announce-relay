"""Minimal Transmission RPC client (session-id negotiation, optional basic auth)."""
import base64
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = 'http://127.0.0.1:9091/transmission/rpc'
TORRENT_FIELDS = ['id', 'hashString', 'name', 'status', 'percentDone', 'trackerList', 'trackerStats',
                  'error', 'errorString']
STATUS_NAMES = {0: 'paused', 1: 'check-wait', 2: 'checking', 3: 'download-wait',
                4: 'downloading', 5: 'seed-wait', 6: 'seeding'}


class RpcError(RuntimeError):
    pass


class Client:
    def __init__(self, url=None, user=None, password=None, timeout=30):
        self.url = url or os.environ.get('TR_RPC_URL') or DEFAULT_URL
        user = user if user is not None else os.environ.get('TR_RPC_USER')
        password = password if password is not None else os.environ.get('TR_RPC_PASSWORD')
        self.auth = None
        if user is not None:
            token = base64.b64encode('{}:{}'.format(user, password or '').encode()).decode()
            self.auth = 'Basic ' + token
        self.timeout = timeout
        self.session_id = None
        # Never let an ambient proxy see RPC traffic.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, arguments=None):
        payload = json.dumps({'method': method, 'arguments': arguments or {}}).encode()
        for _ in range(2):
            request = urllib.request.Request(self.url, data=payload, method='POST',
                                             headers={'Content-Type': 'application/json'})
            if self.session_id:
                request.add_header('X-Transmission-Session-Id', self.session_id)
            if self.auth:
                request.add_header('Authorization', self.auth)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode())
                    break
            except urllib.error.HTTPError as error:
                if error.code == 409 and error.headers.get('X-Transmission-Session-Id'):
                    self.session_id = error.headers['X-Transmission-Session-Id']
                    continue
                raise RpcError('rpc_http_{}'.format(error.code))
        else:
            raise RpcError('rpc_session_negotiation_failed')
        if result.get('result') != 'success':
            raise RpcError('rpc_failed')
        return result.get('arguments', {})

    def torrents(self, ids=None, fields=None):
        arguments = {'fields': fields or TORRENT_FIELDS}
        if ids is not None:
            arguments['ids'] = list(ids)
        return self.call('torrent-get', arguments)['torrents']

    def torrent(self, hash_string, fields=None):
        rows = self.torrents([hash_string], fields)
        if len(rows) != 1:
            raise RpcError('torrent_not_found')
        return rows[0]

    def set_tracker_list(self, hash_string, tracker_list):
        self.call('torrent-set', {'ids': [hash_string], 'trackerList': tracker_list})

    def reannounce(self, hash_string):
        self.call('torrent-reannounce', {'ids': [hash_string]})
