from datetime import datetime, timezone
from email.utils import format_datetime
import http.client
import socket
import ssl
import unittest

from transmission_announce_relay import recovery


class PolicyTests(unittest.TestCase):
    def test_transient_errors_only(self):
        for error in (TimeoutError(), socket.timeout(), ConnectionResetError(), ConnectionRefusedError(),
                      http.client.IncompleteRead(b'x', 20), ssl.SSLEOFError(),
                      socket.gaierror(socket.EAI_AGAIN, 'temporary')):
            self.assertTrue(recovery.transient_exception(error))
        for error in (ssl.SSLCertVerificationError(), ValueError(), RuntimeError(),
                      socket.gaierror(socket.EAI_NONAME, 'permanent')):
            self.assertFalse(recovery.transient_exception(error))

    def test_requeue_delay_is_bounded_and_never_shortened(self):
        budget = recovery.HEDGED_ATTEMPT_BUDGET
        self.assertEqual(recovery.requeue_delay({}, 40), recovery.REQUEUE_DELAY)
        self.assertEqual(recovery.requeue_delay({'Retry-After': '3'}, 40), 3)
        self.assertEqual(recovery.requeue_delay({'retry-after': '0'}, 40), 0)
        self.assertIsNone(recovery.requeue_delay({'Retry-After': '11'}, 40))
        self.assertIsNone(recovery.requeue_delay({'Retry-After': 'invalid'}, 40))
        self.assertIsNone(recovery.requeue_delay({'Retry-After': '-1'}, 40))
        self.assertIsNone(recovery.requeue_delay({'Retry-After': '5'}, 5 + budget - 0.1))
        self.assertEqual(recovery.requeue_delay({'Retry-After': '5'}, 5 + budget), 5)
        self.assertIsNone(recovery.requeue_delay({}, recovery.REQUEUE_DELAY + budget - 1))
        self.assertIsNone(recovery.requeue_delay({'Retry-After': '1', 'retry-after': '2'}, 40))
        now = 1700000000
        date = format_datetime(datetime.fromtimestamp(now + 4, timezone.utc), usegmt=True)
        self.assertEqual(recovery.requeue_delay({'Retry-After': date}, 40, wall_now=now), 4)

    def test_requeue_http_policy(self):
        self.assertTrue(recovery.requeue_http(500, {}, False))
        self.assertTrue(recovery.requeue_http(503, {}, False))
        self.assertTrue(recovery.requeue_http(525, {}, False))
        self.assertFalse(recovery.requeue_http(503, {}, True))
        self.assertTrue(recovery.requeue_http(429, {'Retry-After': '5'}, False))
        self.assertFalse(recovery.requeue_http(429, {}, False))
        for status in (200, 301, 400, 401, 403, 404):
            self.assertFalse(recovery.requeue_http(status, {}, False))

    def test_budget_fits_transmission_timeout(self):
        self.assertLessEqual(recovery.ANNOUNCE_DEADLINE, recovery.TRANSMISSION_ANNOUNCE_TIMEOUT - 5)
        self.assertLessEqual(recovery.HEDGED_ATTEMPT_BUDGET + recovery.MAX_REQUEUE_DELAY +
                             recovery.HEDGED_ATTEMPT_BUDGET, recovery.ANNOUNCE_DEADLINE)


if __name__ == '__main__':
    unittest.main()
