"""Timing budget and requeue policy for the dispatcher. No I/O.

Transmission gives an announce 45 seconds (`TrAnnounceTimeoutSec`) before it
counts a failure and backs off (20s, then ~5/15/30 minutes). Everything the
relay does for one announce -- queue wait, hedge, requeue delay, second attempt
-- must therefore finish inside ANNOUNCE_DEADLINE.
"""
from datetime import timezone
from email.utils import parsedate_to_datetime
import http.client
import socket
import ssl
import time

PACE = 3.0                 # minimum gap between two announce starts per route
HEDGE_AFTER = 3.0          # open a second socket if the first has no headers yet
CONNECT_TIMEOUT = 3.0      # per socket
HEADERS_TIMEOUT = 8.0      # from socket start until response headers
BODY_TIMEOUT = 4.0         # after headers (tracker bodies are tiny)
ATTEMPT_BUDGET = HEADERS_TIMEOUT + BODY_TIMEOUT      # one socket, worst case
HEDGED_ATTEMPT_BUDGET = HEDGE_AFTER + ATTEMPT_BUDGET  # one attempt with its hedge
REQUEUE_DELAY = 10.0       # default wait before the single requeued attempt
MAX_REQUEUE_DELAY = 10.0   # a longer Retry-After is passed through instead
MAX_ATTEMPTS = 2
ANNOUNCE_DEADLINE = 40.0   # from receipt; below Transmission's 45s
MAX_READY_WAIT = 20.0      # an announce that could not even start by then is dropped
TRANSMISSION_ANNOUNCE_TIMEOUT = 45.0


class ResponseHeaders(dict):
    """Keeps only forwardable headers, and every Retry-After value seen."""

    def __init__(self, pairs):
        super().__init__((key, value) for key, value in pairs
                         if key.lower() in ('content-type', 'content-encoding', 'retry-after'))
        self.retry_after_values = tuple(value for key, value in pairs if key.lower() == 'retry-after')


def transient_exception(error):
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    # socket.timeout became an alias of TimeoutError only in Python 3.10.
    return isinstance(error, (TimeoutError, socket.timeout, ConnectionError,
                              http.client.IncompleteRead, ssl.SSLEOFError))


def requeue_http(status, headers, rejection):
    """Whether an HTTP outcome deserves one delayed second attempt.

    A tracker rejection (bencoded failure reason) is final whatever the status.
    """
    if rejection:
        return False
    if status >= 500:
        return True
    return status == 429 and bool(getattr(headers, 'retry_after_values',
                                          [v for k, v in headers.items() if k.lower() == 'retry-after']))


def requeue_delay(headers, remaining, wall_now=None):
    """Delay before the second attempt, or None to return the failure as-is.

    Never shortens a server-requested delay; refuses when the second attempt
    could not finish inside the announce deadline.
    """
    values = getattr(headers, 'retry_after_values',
                     [value for key, value in headers.items() if key.lower() == 'retry-after'])
    if len(values) > 1:
        return None
    if values:
        value = values[0].strip()
        if value.isascii() and value.isdigit() and len(value) <= 10:
            delay = float(int(value))
        else:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                now = time.time() if wall_now is None else wall_now
                delay = max(0.0, date.timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                return None
    else:
        delay = REQUEUE_DELAY
    if delay > MAX_REQUEUE_DELAY or remaining - delay < HEDGED_ATTEMPT_BUDGET:
        return None
    return delay
