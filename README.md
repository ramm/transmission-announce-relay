# transmission-announce-relay

A small local relay that makes Transmission's **tracker announces** survive a
hostile network path. It runs on the same host as Transmission, listens on
loopback, and forwards each announce to the real tracker over a **fresh,
certificate-verified HTTPS connection**, hedging stalled connections and
retrying once — all inside the 45 seconds Transmission gives an announce.

Peer traffic is not touched. Nothing is tunnelled. Your public IP does not
change. The relay only makes the tiny HTTP requests that tell a tracker *"I am
still seeding"* reliable again.

Two pieces, deliberately separate:

- **`transmission-announce-relay`** — the daemon (a loopback HTTP server).
- **`tar-ctl`** — a control CLI that lists your torrents, points the matching
  tracker URLs at the relay (by tracker host, regex, name, status, ids — or
  all), and puts the originals back.

Standard library only, Python 3.8+. No dependencies.

## Why this exists: Russian throttling of Cloudflare-fronted sites

In June 2025 Cloudflare published
[*Russian internet users are unable to access the open internet*](https://blog.cloudflare.com/russian-internet-users-are-unable-to-access-the-open-internet/).
According to that report, since 9 June 2025 Russian ISPs — which Cloudflare
attributes to direction from the regulator, Roskomnadzor (RKN) — have been
throttling and partially blocking traffic to websites served through
Cloudflare: connections that are cut off after the first ~16 KB, packets that
are silently dropped, and connections that simply time out. Cloudflare's
description is of interference on the Russian side of the path, not a
Cloudflare ban on Russian users. This project takes that report as context; it
has not independently verified the mechanism.

Many private BitTorrent trackers sit behind Cloudflare. For a BitTorrent client
the effect looks like this:

- Announces (`GET https://tracker.example.org/.../announce?...`) sometimes get
  no reply at all: the TCP connection is up, the request is sent, and nothing
  comes back. Other announces on a *fresh* connection succeed immediately.
- Transmission talks to trackers through libcurl with connection reuse and
  HTTP/2. A connection that has gone silent stays in the pool, so a whole
  batch of announces can fail on it before it is discarded.
- Every failed announce makes Transmission back off: 20 s, then roughly 5, 15
  and 30 minutes. Hours of seeding go unreported. Private trackers then see
  you as *not seeding* and hand out hit-and-run warnings, even though the data
  was available the whole time.

The relay attacks exactly that failure mode. It does not know or care what the
filtering mechanism is; it just refuses to wait on a connection that has gone
quiet.

If you are not on a throttled path, you do not need this. It is not a way to
hide traffic, evade a ban, or reach a tracker your account cannot reach.

## How it works

```
Transmission ──HTTP──▶ 127.0.0.1:19053/r/<route>/announce?…
                            │  paced scheduler (one announce start per 3 s per route)
                            │  fresh TLS connection, HTTP/1.1, Connection: close
                            │  no headers after 3 s?  → open a second socket, first to answer wins
                            │  5xx / stall on both?   → one requeue after 10 s (Retry-After honoured ≤10 s)
                            ▼
                    https://tracker.example.org:8443/<passkey>/announce?…   (bytes forwarded unchanged)
```

- **Named routes.** The relay config maps a route name to the real announce
  URL, passkey included. Transmission's tracker list only ever contains
  `http://127.0.0.1:19053/r/example/announce`. Your passkey never appears in
  Transmission's settings, logs or UI again.
- **Byte-for-byte forwarding.** The query string is passed through untouched
  (binary `info_hash` and `peer_id` are not re-encoded), the response body and
  `Retry-After` headers come back unchanged. The tracker sees a normal
  Transmission announce with Transmission's `User-Agent`.
- **Hard deadline.** Every announce is finished or failed about 40 s after
  arrival at the latest (queue wait, hedge, requeue delay and second attempt
  included; the scheduler polls every 250 ms and the caller is released within
  one further second), so Transmission never waits past its own 45 s timeout
  for us.
- **Never retries a verdict.** Tracker rejections (`failure reason`), 4xx
  responses (except a `429` carrying a short `Retry-After`), redirects and
  malformed bodies are returned as-is; redirects are not followed. Transport
  failures — timeouts, resets, TLS/certificate errors — become a relay `502`
  after the (single) requeue policy above has run out; certificate errors are
  never retried. Ambient `http_proxy`/`https_proxy` are ignored. TLS is always
  verified against the system trust store.
- **Exact duplicates share one upstream request** while it is in flight;
  nothing is cached afterwards.
- **Backpressure, not buffering.** Per route: at most 64 announces waiting or in
  flight (then an immediate `503`), and an announce that could not even start
  within about 20 s is dropped with `503`. So: `503` means *the relay did not
  try*, `502` means *the relay tried and the upstream failed*. Transmission
  retries both on its own schedule.
- **Nothing sensitive is logged.** Request logs contain the route name, event
  type (`started`/`stopped`/`completed`/`update`), outcome, timings and counters
  — never URLs, query strings, hashes or peer ids.

## Install

```sh
pipx install transmission-announce-relay      # or: pip install --user .
```

## Quick start

1. Write the relay config, keep it private (it holds your passkey):

   ```sh
   mkdir -p ~/.config/transmission-announce-relay
   cat > ~/.config/transmission-announce-relay/relay.json <<'EOF'
   {
     "listen": {"host": "127.0.0.1", "port": 19053},
     "routes": {
       "example": {"upstream": "https://tracker.example.org:8443/YOUR_PASSKEY/announce"}
     }
   }
   EOF
   chmod 600 ~/.config/transmission-announce-relay/relay.json
   transmission-announce-relay --config ~/.config/transmission-announce-relay/relay.json --check
   ```

2. Run the relay (see `examples/systemd/` for a user unit with
   `Restart=always`; `examples/docker-compose.yml` for containers):

   ```sh
   transmission-announce-relay --config ~/.config/transmission-announce-relay/relay.json --json
   ```

3. Look at what would change, then apply:

   ```sh
   tar-ctl list --tracker-host tracker.example.org
   tar-ctl route --route example --tracker-host tracker.example.org            # dry run
   tar-ctl route --route example --tracker-host tracker.example.org --apply    # 5 s between torrents
   tar-ctl status
   ```

4. To undo, at any time, relay running or not:

   ```sh
   tar-ctl restore --all          # dry run
   tar-ctl restore --all --apply
   ```

`tar-ctl` talks to Transmission's RPC (`--rpc-url`, `--rpc-user`,
`--rpc-password`, or `TR_RPC_URL` / `TR_RPC_USER` / `TR_RPC_PASSWORD`).
Default: `http://127.0.0.1:9091/transmission/rpc`.

## `tar-ctl` reference

Selectors, usable on `list`, `route` and `restore` (combine freely):

| flag | meaning |
|---|---|
| `--tracker-host HOST` | torrents with a tracker on this host (and, for `route`, the lines to replace) |
| `--tracker-regex RE` | regular expression over tracker URLs |
| `--name-regex RE` | regular expression over torrent names |
| `--status paused\|seeding\|downloading\|active\|queued` | Transmission state |
| `--ids 1,2,3` | Transmission ids |
| `--all` | everything (with the other filters) |

`route` and `restore` are **dry runs unless `--apply`** is given, and require a
selector. `route` replaces only the tracker lines that match; other trackers
and tiers in a multi-tracker torrent are preserved byte-for-byte. Originals are
written to a private state file (`~/.local/state/transmission-announce-relay/routes.json`,
mode 600) *before* Transmission is changed, so `restore` can put back exactly
what was there. `restore` refuses a torrent whose tracker list changed since
routing unless `--force`.

`--pace SECONDS` (default 5) spaces out changes. `--reannounce` asks
Transmission for an announce after each change; without it Transmission
re-announces on its own schedule, which usually is what you want.

Output modes: human (default), `--agent` (deterministic tab-separated), `--json`.
`AGENT_SESSION=1` in the environment selects agent mode.

## Relay config reference

```json
{
  "listen": {"host": "127.0.0.1", "port": 19053},
  "routes": {
    "name": {
      "upstream": "https://host[:port]/path/announce",
      "info_hashes": ["<40 hex chars>", "..."]
    }
  }
}
```

- `listen.host` must be `127.0.0.1` (or `localhost`); the relay carries passkeys
  on behalf of a local client and is not a general proxy. Default
  `127.0.0.1:19053`. IPv6 loopback is not supported.
- `routes.<name>.upstream` must be a clean `https://` URL (no query, fragment or
  credentials). One route per tracker; each route has its own scheduler.
- `routes.<name>.info_hashes` (optional) restricts the route to listed torrents.
- Scrape: `/r/<name>/scrape` is mapped when the upstream path contains
  `announce` (the usual `.../announce` → `.../scrape` convention). Set
  `"scrape": false` on a route to answer scrapes with `404` locally — useful when
  the tracker does not implement scrape, since Transmission scrapes every
  routed torrent and each scrape would otherwise take an announce slot.
  Announces are always started before waiting scrapes.
- `GET /healthz` returns status and counters per route (queue depth, in-flight,
  hedges, hedge wins, requeues, transport and HTTP errors, expired announces)
  plus each route's upstream hostname — but no paths, passkeys, query strings
  or hashes. It is reachable by any local process, like the relay itself.

## Operational notes

- **Which torrents to route.** Any torrent whose tracker is on the affected
  path. `tar-ctl route --tracker-host …` handles hundreds of torrents in one
  paced run; a relay restart while torrents are routed only fails the announces
  in flight, which Transmission retries 20 s later.
- **Capacity.** One announce start per 3 s per route is ~1,200/hour, plenty
  for several hundred torrents at a typical 30–45 minute interval. Bursts
  (starting many torrents at once, or many torrents retrying rejections every
  20 s) can exceed it; the relay then answers `503` early rather than queueing
  for minutes, and Transmission's own backoff spreads the load.
- **Retired torrents.** Trackers answer deleted/trumped torrents with a
  `failure reason`; the relay passes that through unchanged and never retries
  it. Such torrents keep re-announcing on Transmission's failure backoff; pause
  or remove them.
- **Restart safety.** `tar-ctl restore` needs only Transmission, not the relay.
  If the relay is unreachable, routed torrents simply fail their announces until
  it is back or they are restored.
- **Containers.** Transmission announces to `127.0.0.1`, so the relay must share
  its network namespace (`network_mode: host` for both, or
  `network_mode: "service:transmission"` for the relay).

## What it does not do

- It is not a VPN or SOCKS/HTTP proxy and does not carry peer connections.
- It does not spoof, rewrite or add anything to the announce; the tracker sees
  your client, your IP and your stats.
- It does not bypass tracker-side restrictions (revoked download rights,
  hit-and-run limits, unregistered torrents). It only makes sure your
  announces arrive.
- It does not prove what the network is doing; it just stops waiting on
  connections that have gone quiet.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use fake sockets and a fake Transmission; nothing touches the network.
The dispatcher's timing constants are patched down in tests so the suite runs
in seconds.

## License

MIT.
