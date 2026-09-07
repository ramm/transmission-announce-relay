# Changelog

## 0.1.3

- Packaging: build from source again on Python 3.8 (setuptools 61+); no code changes.

## 0.1.2

- Relay: per-route `"scrape": false` answers scrapes locally with `404`;
  announces are scheduled before waiting scrapes.

## 0.1.1

- `tar-ctl`: tolerate Transmission re-serialising tracker lists (it appends a
  trailing newline), which made `route --apply` report `not_verified` after a
  successful change and `restore` refuse unchanged torrents.

## 0.1.0

First release.

- `transmission-announce-relay`: loopback announce relay with named routes,
  fresh verified HTTPS connection per announce, 3 s pacing per route, 3 s
  hedge for stalled connections, one 10 s requeue for 5xx/transport stalls,
  40 s hard deadline; scrape mapping; sanitized `/healthz`.
- `tar-ctl`: list / route / restore / status against Transmission RPC, with
  host, regex, name, status and id selectors, dry-run by default, private
  state file holding exact originals.
