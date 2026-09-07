import io
import json
import os
import tempfile
import unittest
from unittest import mock

from transmission_announce_relay import ctl

UP = 'https://tracker.example.org:8443/PASSKEY/announce'
OTHER = 'https://other.example.net/announce'
LOCAL = 'http://127.0.0.1:19053/r/example/announce'


def row(identifier, char, tracker_list, status=6, done=1.0, ok=True, name=None):
    return {'id': identifier, 'hashString': char * 40, 'name': name or 'Torrent {}'.format(identifier),
            'status': status, 'percentDone': done, 'trackerList': tracker_list,
            'trackerStats': [{'lastAnnounceSucceeded': ok, 'announceState': 1}]}


class FakeClient:
    def __init__(self, rows):
        self.rows = {r['hashString']: r for r in rows}
        self.calls = []

    def torrents(self, ids=None, fields=None):
        if ids is None:
            return [dict(r) for r in self.rows.values()]
        return [dict(self.rows[i]) for i in ids if i in self.rows]

    def torrent(self, hash_string, fields=None):
        return dict(self.rows[hash_string])

    def set_tracker_list(self, hash_string, tracker_list):
        self.calls.append(('set', hash_string, tracker_list))
        self.rows[hash_string]['trackerList'] = tracker_list

    def reannounce(self, hash_string):
        self.calls.append(('reannounce', hash_string))


class SubstituteTests(unittest.TestCase):
    def test_only_matching_lines_change_and_layout_is_preserved(self):
        match = ctl.matcher(host='tracker.example.org')
        original = UP + '\n\n' + OTHER + '\n' + UP + '?x=1\n'
        result, changed = ctl.substitute(original, match, LOCAL)
        self.assertEqual(changed, 2)
        self.assertEqual(result, LOCAL + '\n\n' + OTHER + '\n' + LOCAL + '\n')
        self.assertEqual(ctl.substitute(OTHER + '\n', match, LOCAL), (OTHER + '\n', 0))

    def test_regex_and_host_selectors(self):
        self.assertTrue(ctl.matcher(regex=r'/PASSKEY/')(UP))
        self.assertFalse(ctl.matcher(host='tracker.example.org', regex=r'nomatch')(UP))
        with self.assertRaisesRegex(ValueError, 'tracker_selector_required'):
            ctl.matcher()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = os.path.join(self.directory.name, 'state', 'routes.json')
        self.client = FakeClient([
            row(1, 'a', UP + '\n'),
            row(2, 'b', UP + '\n\n' + OTHER + '\n', status=0, done=0.4),
            row(3, 'c', OTHER + '\n', name='Unrelated'),
            row(4, 'd', UP + '\n', name='Skip me'),
        ])
        self.health = {'status': 'ready', 'routes': {'example': {'queue_depth': 0, 'active_upstream': 0,
                                                                 'transport': {'upstream_valid_announces': 5}}}}
        self.patches = [mock.patch.object(ctl.rpc, 'Client', return_value=self.client),
                        mock.patch.object(ctl, 'relay_health', return_value=self.health),
                        mock.patch.object(ctl.time, 'sleep')]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdout', out), mock.patch('sys.stderr', err):
            code = ctl.main(['--state', self.state, '--json'] + list(argv))
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        errors = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        return code, events, errors

    def test_list_filters_and_marks_routed(self):
        code, events, _ = self.run_cli('list', '--tracker-host', 'tracker.example.org')
        self.assertEqual(code, 0)
        rows = events[0]
        self.assertEqual([r['id'] for r in rows], [1, 2, 4])
        self.assertEqual(rows[1]['status'], 'paused')
        self.assertEqual(rows[1]['trackers'], 'other.example.net,tracker.example.org')
        code, events, _ = self.run_cli('list', '--name-regex', 'Unrelated')
        self.assertEqual([r['id'] for r in events[0]], [3])

    def test_route_dry_run_changes_nothing(self):
        code, events, _ = self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org')
        self.assertEqual(code, 0)
        self.assertEqual(events[0], {'event': 'plan', 'selected': 3, 'to_change': 3, 'route': 'example', 'apply': False})
        self.assertEqual(self.client.calls, [])
        self.assertFalse(os.path.exists(self.state))

    def test_route_apply_saves_originals_verifies_and_paces(self):
        with mock.patch.object(ctl.time, 'sleep') as sleep:
            code, events, errors = self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org',
                                                '--name-regex', '^Torrent', '--apply', '--pace', '7')
        self.assertEqual(code, 0, errors)
        self.assertEqual([c[0] for c in self.client.calls], ['set', 'set'])
        self.assertEqual(self.client.rows['a' * 40]['trackerList'], LOCAL + '\n')
        self.assertEqual(self.client.rows['b' * 40]['trackerList'], LOCAL + '\n\n' + OTHER + '\n')
        self.assertEqual(self.client.rows['d' * 40]['trackerList'], UP + '\n')  # excluded by name filter
        sleep.assert_called_once_with(7.0)
        state = ctl.load_state(self.state)
        self.assertEqual(state['torrents']['a' * 40]['original'], UP + '\n')
        self.assertEqual(state['torrents']['b' * 40]['route'], 'example')
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), '0o600')
        # Routing again is a no-op and keeps the first original.
        code, events, _ = self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org', '--apply')
        self.assertEqual(events[0]['to_change'], 1)  # only torrent 4 remains direct
        self.assertEqual(ctl.load_state(self.state)['torrents']['a' * 40]['original'], UP + '\n')

    def test_route_requires_selector_and_known_route(self):
        code, _, errors = self.run_cli('route', '--route', 'example')
        self.assertEqual((code, errors[0]['error']), (1, 'tracker_selector_required'))
        code, _, errors = self.run_cli('route', '--route', 'nope', '--tracker-host', 'tracker.example.org')
        self.assertEqual((code, errors[0]['error']), (1, 'route_not_configured_on_relay'))
        code, _, errors = self.run_cli('route', '--route', 'example', '--tracker-host', 'x', '--pace', '0.5')
        self.assertEqual((code, errors[0]['error']), (1, 'pace_too_short'))

    def test_restore_puts_exact_originals_back_and_refuses_drift(self):
        self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org', '--apply')
        self.client.rows['b' * 40]['trackerList'] = 'http://changed.example/announce\n'
        code, events, errors = self.run_cli('restore', '--all', '--apply', '--reannounce')
        self.assertEqual(code, 0)
        self.assertEqual(self.client.rows['a' * 40]['trackerList'], UP + '\n')
        self.assertEqual(self.client.rows['d' * 40]['trackerList'], UP + '\n')
        self.assertEqual(self.client.rows['b' * 40]['trackerList'], 'http://changed.example/announce\n')
        self.assertEqual([e['event'] for e in errors], ['skipped_unexpected_tracker_list'])
        self.assertIn(('reannounce', 'a' * 40), self.client.calls)
        state = ctl.load_state(self.state)
        self.assertEqual(sorted(state['torrents']), ['b' * 40])
        code, events, _ = self.run_cli('restore', '--all', '--apply', '--force')
        self.assertEqual(self.client.rows['b' * 40]['trackerList'], UP + '\n\n' + OTHER + '\n')
        self.assertEqual(ctl.load_state(self.state)['torrents'], {})

    def test_restore_rechecks_immediately_before_each_change(self):
        self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org', '--apply')
        original_torrent = self.client.torrent

        def drifting_torrent(hash_string, fields=None):
            if hash_string == 'd' * 40:
                self.client.rows['d' * 40]['trackerList'] = 'http://changed.example/announce\n'
            return original_torrent(hash_string, fields)
        with mock.patch.object(self.client, 'torrent', side_effect=drifting_torrent):
            code, events, errors = self.run_cli('restore', '--all', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual([e['event'] for e in errors], ['skipped_changed_meanwhile'])
        self.assertEqual(self.client.rows['d' * 40]['trackerList'], 'http://changed.example/announce\n')
        self.assertEqual(self.client.rows['a' * 40]['trackerList'], UP + '\n')
        self.assertEqual(sorted(ctl.load_state(self.state)['torrents']), ['d' * 40])

    def test_status_reports_relay_and_routed_counts(self):
        self.run_cli('route', '--route', 'example', '--tracker-host', 'tracker.example.org', '--apply')
        code, events, _ = self.run_cli('status')
        self.assertEqual(code, 0)
        self.assertEqual((events[0]['routed_in_state'], events[0]['latest_announce_ok'], events[0]['relay']), (3, 3, 'ready'))
        with mock.patch.object(ctl, 'relay_health', side_effect=OSError):
            code, events, _ = self.run_cli('status')
        self.assertEqual(events[0]['relay'], 'unreachable')
        self.assertNotIn('PASSKEY', json.dumps(events))

    def test_output_modes(self):
        self.assertEqual(ctl.resolve_mode(environ={}), 'human')
        self.assertEqual(ctl.resolve_mode(environ={'AGENT_SESSION': '1'}), 'agent')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr', new_callable=io.StringIO):
            ctl.main(['--agent', '--json', 'status'])
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            ctl.main(['--state', self.state, '--agent', 'list', '--ids', '1'])
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0].split('\t')[0], 'id')
        self.assertTrue(lines[1].startswith('1\t'))


if __name__ == '__main__':
    unittest.main()
