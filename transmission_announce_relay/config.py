"""Relay configuration: a private JSON file mapping route names to upstream announce URLs.

Example::

    {
      "listen": {"host": "127.0.0.1", "port": 19053},
      "routes": {
        "example": {"upstream": "https://tracker.example.org:8443/PASSKEY/announce"}
      }
    }

The passkey lives only here. Transmission's tracker list only ever sees
``http://127.0.0.1:19053/r/example/announce``.
"""
import json
import os
import re
import stat
import urllib.parse

ROUTE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
DEFAULT_PORT = 19053


class ConfigError(ValueError):
    pass


def load(path):
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError('config_permissions_must_be_private')  # chmod 600
    with open(path) as handle:
        return validate(json.load(handle))


def validate(raw):
    if not isinstance(raw, dict):
        raise ConfigError('config_must_be_object')
    listen = raw.get('listen', {})
    if not isinstance(listen, dict):
        raise ConfigError('invalid_listen')
    host = listen.get('host', '127.0.0.1')
    port = listen.get('port', DEFAULT_PORT)
    if host not in ('127.0.0.1', 'localhost'):  # IPv4 loopback only; the server socket is AF_INET
        # The relay carries passkeys on behalf of a local client; it is not a public proxy.
        raise ConfigError('listen_host_must_be_loopback')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ConfigError('invalid_listen_port')
    routes = raw.get('routes')
    if not isinstance(routes, dict) or not routes:
        raise ConfigError('routes_required')
    validated = {}
    for name, route in routes.items():
        if not isinstance(name, str) or not ROUTE_NAME.fullmatch(name):
            raise ConfigError('invalid_route_name')
        if not isinstance(route, dict) or not isinstance(route.get('upstream'), str):
            raise ConfigError('route_upstream_required')
        url = urllib.parse.urlsplit(route['upstream'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password or
                url.query or url.fragment or not url.path.startswith('/') or
                any(ord(c) < 33 or ord(c) > 126 for c in url.path)):
            raise ConfigError('route_upstream_must_be_clean_https_url')
        hashes = route.get('info_hashes')
        if hashes is not None:
            if (not isinstance(hashes, list) or not hashes or
                    any(not isinstance(h, str) or not re.fullmatch('[0-9a-fA-F]{40}', h) for h in hashes)):
                raise ConfigError('invalid_info_hashes')
            hashes = sorted({h.lower() for h in hashes})
        scrape = route.get('scrape', True)
        if type(scrape) is not bool:
            raise ConfigError('invalid_scrape_flag')
        unknown_route_keys = set(route) - {'upstream', 'info_hashes', 'scrape'}
        if unknown_route_keys:
            raise ConfigError('unknown_route_keys')
        validated[name] = {'upstream': route['upstream'], 'host': url.hostname,
                           'port': url.port or 443, 'path': url.path, 'info_hashes': hashes,
                           'scrape': scrape}
    log = raw.get('log')
    if log is not None and (not isinstance(log, dict) or not isinstance(log.get('path'), str)):
        raise ConfigError('invalid_log')
    unknown = set(raw) - {'listen', 'routes', 'log'}
    if unknown:
        raise ConfigError('unknown_config_keys')
    return {'listen': {'host': host, 'port': port}, 'routes': validated, 'log': log}


def local_announce_url(port, route, host='127.0.0.1'):
    return 'http://{}:{}/r/{}/announce'.format(host, port, route)
