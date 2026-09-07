"""tar-ctl: list Transmission torrents, switch their tracker URLs to the relay, and back.

Substitution is per tracker line: only lines whose host matches are replaced, other
trackers and tiers are kept byte-for-byte. Originals are saved in a private state
file before any change, so ``restore`` can put them back exactly.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from . import config as config_module
from . import rpc
from .output import Output, resolve_mode

STATE_VERSION = 1
MIN_PACE = 1.0
STATUS_FILTERS = {'paused': (0,), 'seeding': (6,), 'downloading': (4,), 'active': (4, 6),
                  'queued': (1, 2, 3, 5)}


def default_state_path():
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(base, 'transmission-announce-relay', 'routes.json')


# -- state ---------------------------------------------------------------------

def load_state(path):
    if not os.path.exists(path):
        return {'version': STATE_VERSION, 'torrents': {}}
    with open(path) as handle:
        state = json.load(handle)
    if state.get('version') != STATE_VERSION or not isinstance(state.get('torrents'), dict):
        raise ValueError('state_file_unreadable')
    return state


def save_state(path, state):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = path + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(state, handle, sort_keys=True, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if directory:
        try:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


# -- tracker list handling ----------------------------------------------------------

def tracker_hosts(tracker_list):
    hosts = []
    for line in tracker_list.splitlines():
        line = line.strip()
        if line:
            host = urllib.parse.urlsplit(line).hostname
            hosts.append(host or line)
    return hosts


def matcher(host=None, regex=None):
    """Returns a predicate over tracker URLs."""
    if host is None and regex is None:
        raise ValueError('tracker_selector_required')
    pattern = re.compile(regex) if regex else None

    def match(url):
        parsed = urllib.parse.urlsplit(url)
        if host is not None and (parsed.hostname or '').lower() != host.lower():
            return False
        if pattern is not None and not pattern.search(url):
            return False
        return True
    return match


def substitute(tracker_list, match, replacement):
    """Replace matching tracker lines; everything else is preserved exactly."""
    lines = tracker_list.split('\n')
    changed = 0
    result = []
    for line in lines:
        candidate = line.strip()
        if candidate and match(candidate):
            result.append(replacement)
            changed += 1
        else:
            result.append(line)
    return '\n'.join(result), changed


# -- selection ------------------------------------------------------------------

def add_selection_arguments(parser):
    parser.add_argument('--ids', help='comma-separated Transmission ids')
    parser.add_argument('--tracker-host', help='tracker hostname to match, e.g. tracker.example.org')
    parser.add_argument('--tracker-regex', help='regular expression over tracker URLs')
    parser.add_argument('--name-regex', help='regular expression over torrent names')
    parser.add_argument('--status', choices=sorted(STATUS_FILTERS), help='torrent state filter')
    parser.add_argument('--all', action='store_true', help='select every torrent (with other filters)')


def select(rows, args, require_selector=True):
    ids = None
    if args.ids:
        try:
            ids = {int(item) for item in args.ids.split(',') if item.strip()}
        except ValueError:
            raise ValueError('invalid_ids')
    if require_selector and not any((ids, args.tracker_host, args.tracker_regex, args.name_regex, args.all)):
        raise ValueError('selector_required')
    name_pattern = re.compile(args.name_regex) if args.name_regex else None
    tracker_match = None
    if args.tracker_host or args.tracker_regex:
        tracker_match = matcher(args.tracker_host, args.tracker_regex)
    selected = []
    for row in rows:
        if ids is not None and row['id'] not in ids:
            continue
        if name_pattern is not None and not name_pattern.search(row.get('name', '')):
            continue
        if args.status and row.get('status') not in STATUS_FILTERS[args.status]:
            continue
        if tracker_match is not None and not any(tracker_match(line.strip())
                                                 for line in row.get('trackerList', '').splitlines() if line.strip()):
            continue
        selected.append(row)
    return selected


def summarize(row, relay_prefix):
    stats = row.get('trackerStats') or []
    latest = 'n/a'
    if stats:
        latest = 'ok' if any(s.get('lastAnnounceSucceeded') for s in stats) else \
            ('pending' if any(s.get('announceState') in (2, 3) for s in stats) else 'failed')
    routed = any(line.strip().startswith(relay_prefix) for line in row.get('trackerList', '').splitlines())
    return {'id': row['id'], 'hash': row['hashString'][:8], 'status': rpc.STATUS_NAMES.get(row.get('status'), '?'),
            'done': '{:.0f}%'.format(100 * row.get('percentDone', 0)),
            'trackers': ','.join(sorted(set(tracker_hosts(row.get('trackerList', ''))))),
            'routed': 'yes' if routed else 'no', 'announce': latest,
            'name': (row.get('name') or '')[:60]}


# -- relay check ----------------------------------------------------------------------

def relay_health(relay_url, timeout=5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(relay_url.rstrip('/') + '/healthz', timeout=timeout) as response:
        return json.loads(response.read().decode())


# -- commands ------------------------------------------------------------------------

def cmd_list(client, output, args):
    rows = client.torrents()
    relay_prefix = args.relay_url.rstrip('/') + '/r/'
    selected = select(rows, args, require_selector=False)
    columns = ['id', 'status', 'done', 'trackers', 'routed', 'announce', 'name']
    output.table([summarize(row, relay_prefix) for row in selected], columns)
    return 0


def cmd_route(client, output, args):
    match = matcher(args.tracker_host, args.tracker_regex)
    relay_base = args.relay_url.rstrip('/')
    replacement = relay_base + '/r/' + args.route + '/announce'
    if not args.no_relay_check:
        health = relay_health(args.relay_url)
        if args.route not in health.get('routes', {}):
            raise ValueError('route_not_configured_on_relay')
    rows = select(client.torrents(), args)
    state = load_state(args.state)
    plan = []
    for row in rows:
        current = row['trackerList']
        proposed, changed = substitute(current, match, replacement)
        if not changed or proposed == current:
            continue
        plan.append((row, current, proposed, changed))
    output.emit({'event': 'plan', 'selected': len(rows), 'to_change': len(plan),
                 'route': args.route, 'apply': bool(args.apply)})
    if not args.apply:
        for row, _, _, changed in plan[:args.show]:
            output.emit({'event': 'would_route', 'id': row['id'], 'trackers_replaced': changed,
                         'name': (row.get('name') or '')[:60]})
        return 0
    done = 0
    for index, (row, current, proposed, changed) in enumerate(plan):
        if index:
            time.sleep(args.pace)
        fresh = client.torrent(row['hashString'], ['id', 'hashString', 'trackerList'])
        if fresh['trackerList'] != current:
            output.emit({'event': 'skipped_changed_meanwhile', 'id': row['id']}, error=True)
            continue
        entry = state['torrents'].setdefault(row['hashString'].lower(), {})
        if 'original' not in entry:
            entry['original'] = current  # keep the first original across repeated routing
        entry.update(routed=proposed, route=args.route, since=int(time.time()))
        save_state(args.state, state)  # durable before the RPC
        client.set_tracker_list(row['hashString'], proposed)
        after = client.torrent(row['hashString'], ['id', 'trackerList'])
        if after['trackerList'] != proposed:
            output.emit({'event': 'not_verified', 'id': row['id']}, error=True)
            return 1
        if args.reannounce:
            client.reannounce(row['hashString'])
        done += 1
        output.emit({'event': 'routed', 'id': row['id'], 'trackers_replaced': changed})
    output.emit({'event': 'done', 'routed': done, 'state': args.state})
    return 0


def cmd_restore(client, output, args):
    state = load_state(args.state)
    known = state['torrents']
    if not known:
        output.emit({'event': 'nothing_to_restore'})
        return 0
    rows = client.torrents([hash_string for hash_string in known])
    selected = select(rows, args)  # --all counts as a selector; other filters still apply
    plan = []
    for row in selected:
        entry = known.get(row['hashString'].lower())
        if entry is None:
            continue
        if row['trackerList'] != entry['routed'] and not args.force:
            output.emit({'event': 'skipped_unexpected_tracker_list', 'id': row['id']}, error=True)
            continue
        plan.append((row, entry))
    output.emit({'event': 'plan', 'to_restore': len(plan), 'apply': bool(args.apply)})
    if not args.apply:
        for row, _ in plan[:args.show]:
            output.emit({'event': 'would_restore', 'id': row['id'], 'name': (row.get('name') or '')[:60]})
        return 0
    done = 0
    for index, (row, entry) in enumerate(plan):
        if index:
            time.sleep(args.pace)
        fresh = client.torrent(row['hashString'], ['id', 'hashString', 'trackerList'])
        if fresh['trackerList'] != entry['routed'] and not args.force:
            output.emit({'event': 'skipped_changed_meanwhile', 'id': row['id']}, error=True)
            continue
        client.set_tracker_list(row['hashString'], entry['original'])
        after = client.torrent(row['hashString'], ['id', 'trackerList'])
        if after['trackerList'] != entry['original']:
            output.emit({'event': 'not_verified', 'id': row['id']}, error=True)
            return 1
        del known[row['hashString'].lower()]
        save_state(args.state, state)
        if args.reannounce:
            client.reannounce(row['hashString'])
        done += 1
        output.emit({'event': 'restored', 'id': row['id']})
    output.emit({'event': 'done', 'restored': done, 'still_routed': len(known)})
    return 0


def cmd_status(client, output, args):
    state = load_state(args.state)
    routed_hashes = set(state['torrents'])
    rows = client.torrents(list(routed_hashes)) if routed_hashes else []
    ok = sum(any(s.get('lastAnnounceSucceeded') for s in (row.get('trackerStats') or [])) for row in rows)
    result = {'event': 'status', 'routed_in_state': len(routed_hashes), 'present': len(rows),
              'latest_announce_ok': ok, 'state': args.state}
    try:
        health = relay_health(args.relay_url)
        result['relay'] = 'ready' if health.get('status') == 'ready' else 'unknown'
        result['relay_routes'] = sorted(health.get('routes', {}))
        for name, route in health.get('routes', {}).items():
            transport = route.get('transport', {})
            result['route_' + name] = {'queue': route.get('queue_depth'), 'inflight': route.get('active_upstream'),
                                       'valid_announces': transport.get('upstream_valid_announces'),
                                       'hedge_wins': transport.get('hedge_wins'),
                                       'transport_errors': transport.get('upstream_transport_errors'),
                                       'http_errors': transport.get('upstream_http_errors')}
    except Exception as exc:  # the relay being down is a status, not a crash
        result['relay'] = 'unreachable'
        result['relay_error_type'] = type(exc).__name__
    output.emit(result)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='tar-ctl', description=__doc__.split('\n')[0])
    parser.add_argument('--rpc-url', default=None, help='Transmission RPC URL (env TR_RPC_URL)')
    parser.add_argument('--rpc-user', default=None, help='RPC user (env TR_RPC_USER)')
    parser.add_argument('--rpc-password', default=None, help='RPC password (env TR_RPC_PASSWORD)')
    parser.add_argument('--relay-url', default='http://127.0.0.1:{}'.format(config_module.DEFAULT_PORT))
    parser.add_argument('--state', default=default_state_path(), help='private JSON state file')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--agent', action='store_true')
    modes.add_argument('--json', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)

    listing = commands.add_parser('list', help='show torrents, their trackers and routing state')
    add_selection_arguments(listing)

    route = commands.add_parser('route', help='point matching trackers at the relay')
    add_selection_arguments(route)
    route.add_argument('--route', required=True, help='route name configured on the relay')
    route.add_argument('--apply', action='store_true', help='make changes (default: dry run)')
    route.add_argument('--pace', type=float, default=5.0, help='seconds between changes')
    route.add_argument('--reannounce', action='store_true', help='ask Transmission to announce after each change')
    route.add_argument('--no-relay-check', action='store_true')
    route.add_argument('--show', type=int, default=20, help='dry-run rows to print')

    restore = commands.add_parser('restore', help='put original tracker URLs back')
    add_selection_arguments(restore)
    restore.add_argument('--apply', action='store_true')
    restore.add_argument('--pace', type=float, default=5.0)
    restore.add_argument('--reannounce', action='store_true')
    restore.add_argument('--force', action='store_true', help='restore even if the list changed since routing')
    restore.add_argument('--show', type=int, default=20)

    commands.add_parser('status', help='relay health and routed-torrent summary')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    output = Output(resolve_mode(args.agent, args.json))
    try:
        if getattr(args, 'pace', MIN_PACE) < MIN_PACE:
            raise ValueError('pace_too_short')
        client = rpc.Client(args.rpc_url, args.rpc_user, args.rpc_password)
        handler = {'list': cmd_list, 'route': cmd_route, 'restore': cmd_restore, 'status': cmd_status}[args.command]
        return handler(client, output, args)
    except Exception as exc:
        message = str(exc)
        safe = message if message and all(c.islower() or c == '_' or c.isdigit() for c in message) else 'operation_failed'
        output.emit({'error': safe, 'error_type': type(exc).__name__}, error=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
