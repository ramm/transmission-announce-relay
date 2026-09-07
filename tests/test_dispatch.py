import concurrent.futures
import queue
import socket
import threading
import time
import unittest
from unittest import mock

from transmission_announce_relay import dispatch, recovery

BODY = b'd8:intervali1800e5:peers0:e'


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.timing = mock.patch.multiple(recovery, PACE=0.01, HEDGE_AFTER=0.05, REQUEUE_DELAY=0.05,
                                          HEADERS_TIMEOUT=0.5, BODY_TIMEOUT=0.5, ATTEMPT_BUDGET=1.0,
                                          HEDGED_ATTEMPT_BUDGET=1.05, ANNOUNCE_DEADLINE=3.0,
                                          CONNECT_TIMEOUT=0.1, MAX_READY_WAIT=0.5)
        self.timing.start()
        self.addCleanup(self.timing.stop)
        self.stopping = threading.Event()
        self.fetch = mock.Mock(return_value=(200, {}, BODY))
        self.dispatcher = dispatch.Dispatcher(lambda *a, **k: self.fetch(*a, **k),
                                              lambda body: b'failure reason' in body,
                                              lambda body: body == BODY, self.stopping)
        self.addCleanup(self.dispatcher.close)

    def test_deadline_aborts_every_stalled_socket_and_returns_timeout(self):
        shutdowns = []
        release = threading.Event()

        def fetch(target, ua, deadline=None, on_headers=None, handle=None):
            sock = mock.Mock()
            sock.shutdown.side_effect = lambda how: (shutdowns.append(how), release.set())
            handle.connection = mock.Mock(sock=sock)
            release.wait(timeout=3)
            raise ConnectionResetError('shut down')
        self.fetch = fetch
        started = time.monotonic()
        with mock.patch.object(recovery, 'ANNOUNCE_DEADLINE', 0.6), self.assertRaises(TimeoutError):
            self.dispatcher.submit('/a', 'ua')
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(shutdowns, [socket.SHUT_RDWR, socket.SHUT_RDWR])
        stats = self.dispatcher.stats()
        self.assertEqual(stats['transport']['hedges'], 1)
        self.assertEqual(stats['transport']['stalled_sockets'], 1)
        self.assertEqual(stats['transport']['upstream_transport_errors'], 2)  # deadline aborts count
        self.assertEqual(stats['active_upstream'], 0)
        self.assertEqual(stats['queue_depth'], 0)

    def test_close_fails_pending_and_inflight_requests_without_forwarding_later(self):
        entered = threading.Event()
        release = threading.Event()

        def fetch(target, ua, deadline=None, on_headers=None, handle=None):
            entered.set()
            release.wait(timeout=3)
            return 200, {}, BODY
        self.fetch = fetch
        with mock.patch.object(recovery, 'PACE', 5.0), mock.patch.object(recovery, 'HEDGE_AFTER', 5.0), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.dispatcher.submit, '/a', 'ua')
            self.assertTrue(entered.wait(timeout=2))
            second = pool.submit(self.dispatcher.submit, '/b', 'ua')
            deadline = time.monotonic() + 2
            while self.dispatcher.stats()['queue_depth'] < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.dispatcher.close()
            release.set()
            for future in (first, second):
                with self.assertRaises(TimeoutError):
                    future.result(timeout=3)
            with self.assertRaises(queue.Full):
                self.dispatcher.submit('/c', 'ua')

    def test_handle_abort_without_connection_is_safe_and_flags_the_socket(self):
        handle = dispatch.Handle()
        handle.abort()
        self.assertTrue(handle.aborted)
        sock = mock.Mock()
        sock.shutdown.side_effect = OSError('already closed')
        handle.connection = mock.Mock(sock=sock)
        handle.abort()
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_requeue_held_past_its_deadline_returns_the_retained_failure(self):
        self.fetch.return_value = (503, {}, b'busy')
        with mock.patch.multiple(recovery, PACE=0.3, ANNOUNCE_DEADLINE=0.2, REQUEUE_DELAY=0.05,
                                 HEDGED_ATTEMPT_BUDGET=0.03, MAX_READY_WAIT=0.5):
            status, headers, body, wait_ms, attempts, joined = self.dispatcher.submit('/a', 'ua')
        self.assertEqual((status, body, attempts), (503, b'busy', 1))
        self.fetch.assert_called_once()
        stats = self.dispatcher.stats()['transport']
        self.assertEqual(stats['requeued_http'], 1)
        self.assertEqual(stats['expired_requeues'], 1)
        self.assertEqual(stats['upstream_retries'], 0)
        self.assertEqual(self.dispatcher.stats()['queue_depth'], 0)

    def test_pace_blocked_requeue_expires_on_time(self):
        self.fetch.return_value = (503, {}, b'busy')
        started = time.monotonic()
        with mock.patch.multiple(recovery, PACE=5.0, ANNOUNCE_DEADLINE=0.2, REQUEUE_DELAY=0.05,
                                 HEDGED_ATTEMPT_BUDGET=0.03, MAX_READY_WAIT=0.5):
            status = self.dispatcher.submit('/a', 'ua')[0]
        self.assertEqual(status, 503)
        self.assertLess(time.monotonic() - started, 0.23)
        self.assertEqual(self.dispatcher.stats()['transport']['expired_requeues'], 1)

    def test_permanent_failure_on_the_hedge_makes_the_attempt_final(self):
        self.fetch.side_effect = [TimeoutError('stall'), ValueError('permanent'), (200, {}, BODY)]
        with self.assertRaises(ValueError):
            self.dispatcher.submit('/a', 'ua')
        self.assertEqual(self.fetch.call_count, 2)
        stats = self.dispatcher.stats()['transport']
        self.assertEqual(stats['hedges'], 1)
        self.assertEqual(stats['requeued_transport'], 0)
        self.assertEqual(stats['upstream_transport_errors'], 2)

    def test_no_socket_is_opened_once_stopping(self):
        self.stopping.set()
        item = dispatch.Item(('/a', 'ua'), '/a', 'ua', time.monotonic())
        with self.assertRaises(TimeoutError):
            self.dispatcher._hedged(item)
        self.fetch.assert_not_called()

    def test_requeued_item_keeps_its_first_start_for_queue_wait_accounting(self):
        self.fetch.side_effect = [(503, {}, b'busy'), (200, {}, BODY)]
        status, headers, body, wait_ms, attempts, joined = self.dispatcher.submit('/a', 'ua')
        self.assertEqual((status, body, attempts, joined), (200, BODY, 2, False))
        self.assertLess(wait_ms, 200)
        self.assertEqual(self.dispatcher.stats()['transport']['requeued_http'], 1)


if __name__ == '__main__':
    unittest.main()
