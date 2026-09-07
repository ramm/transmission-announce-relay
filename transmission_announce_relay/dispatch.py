"""Paced, non-blocking announce dispatcher with hedged sockets and one requeue.

One scheduler thread starts at most one announce per PACE seconds, always the
oldest *ready* item. An announce that is waiting (hedge in flight, requeue
delay) never blocks the others. Each attempt opens one socket and, if it has
produced no response headers after HEDGE_AFTER (or failed transiently before
that), a second one; the first socket to deliver headers wins and the other is
shut down. Exact duplicates in flight share one item; nothing is cached.
"""
import concurrent.futures
import queue
import socket
import threading
import time

from . import recovery

MAX_ITEMS = 64      # ready + waiting + in flight; above this the caller gets 503 immediately
MAX_INFLIGHT = 8    # announces with open sockets at once (pace already bounds this)


class QueueExpired(Exception):
    pass


class Aborted(Exception):
    """Raised inside a losing hedge socket; never a transport error."""


class Handle:
    """Lets the dispatcher shut down a socket that another thread is blocked on."""

    def __init__(self):
        self.connection = None
        self.aborted = False
        self.lock = threading.Lock()

    def abort(self):
        with self.lock:
            self.aborted = True
            connection = self.connection
        sock = getattr(connection, 'sock', None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Item:
    __slots__ = ('key', 'target', 'user_agent', 'future', 'arrived', 'deadline', 'attempts',
                 'not_before', 'state', 'handles', 'first_start', 'failed_sockets',
                 'failed_http', 'last_failure', 'priority')

    def __init__(self, key, target, user_agent, now, priority=0):
        self.key = key
        self.target = target
        self.user_agent = user_agent
        self.future = concurrent.futures.Future()
        self.arrived = now
        self.deadline = now + recovery.ANNOUNCE_DEADLINE
        self.attempts = 0
        self.not_before = now
        self.state = 'ready'
        self.handles = []
        self.first_start = None
        self.failed_sockets = 0
        self.failed_http = 0
        self.last_failure = None  # ('result', (status, headers, body)) or ('error', exc)
        self.priority = priority  # lower starts first among ready items (announces before scrapes)


class Dispatcher:
    def __init__(self, fetch, is_rejection, is_valid, stopping):
        self.fetch = fetch
        self.is_rejection = is_rejection
        self.is_valid = is_valid
        self.stopping = stopping
        self.cond = threading.Condition()
        self.items = {}
        self.inflight = 0
        self.last_start = None
        self.max_queue_depth = 0
        self.transport = {'upstream_attempts': 0, 'upstream_retries': 0,
                          'upstream_transport_errors': 0, 'upstream_http_errors': 0,
                          'upstream_valid_announces': 0, 'recovered_groups': 0,
                          'coalesced_requests': 0, 'hedges': 0, 'hedge_wins': 0,
                          'stalled_sockets': 0, 'requeued_http': 0,
                          'requeued_transport': 0, 'expired_before_start': 0,
                          'expired_requeues': 0}
        self.thread = threading.Thread(target=self.run, name='announce-scheduler', daemon=True)
        self.thread.start()

    # -- public ------------------------------------------------------------
    def submit(self, target, user_agent, priority=0):
        key = (target, user_agent)
        with self.cond:
            item = self.items.get(key)
            joined = item is not None and not item.future.done()
            if joined:
                self.transport['coalesced_requests'] += 1
            else:
                if len(self.items) >= MAX_ITEMS or self.stopping.is_set():
                    raise queue.Full()
                item = Item(key, target, user_agent, time.monotonic(), priority)
                self.items[key] = item
                self.max_queue_depth = max(self.max_queue_depth, self.waiting_count())
                self.cond.notify_all()
        try:
            status, headers, body, queue_wait_ms, attempts = item.future.result(
                timeout=max(0.0, item.deadline - time.monotonic()) + 1.0)
        except concurrent.futures.TimeoutError:
            raise TimeoutError('relay_deadline')
        return status, headers, body, queue_wait_ms, attempts, joined

    def stats(self):
        with self.cond:
            return {'queue_depth': self.waiting_count(), 'active_upstream': self.inflight,
                    'max_queue_depth': self.max_queue_depth, 'queue_limit': MAX_ITEMS,
                    'inflight_limit': MAX_INFLIGHT,
                    'max_queue_wait_seconds': recovery.MAX_READY_WAIT,
                    'pace_seconds': recovery.PACE, 'hedge_after_seconds': recovery.HEDGE_AFTER,
                    'announce_deadline_seconds': recovery.ANNOUNCE_DEADLINE,
                    'transport': dict(self.transport)}

    def close(self):
        self.stopping.set()
        with self.cond:
            for item in list(self.items.values()):
                if not item.future.done():
                    item.future.set_exception(TimeoutError('relay_stopping'))
                for handle in item.handles:
                    handle.abort()
            self.items.clear()
            self.cond.notify_all()
        self.thread.join(timeout=1)

    # -- scheduler -----------------------------------------------------------
    def waiting_count(self):
        return sum(item.state != 'inflight' for item in self.items.values())

    def run(self):
        while not self.stopping.is_set():
            with self.cond:
                if self.stopping.is_set():
                    break
                now = time.monotonic()
                pick = None
                wake = now + 0.25
                for key, item in list(self.items.items()):
                    if item.state == 'inflight':
                        continue
                    if item.attempts == 0 and now - item.arrived > recovery.MAX_READY_WAIT:
                        self.transport['expired_before_start'] += 1
                        self._finish(item, error=QueueExpired())
                        continue
                    if item.attempts and now + recovery.HEDGED_ATTEMPT_BUDGET > item.deadline:
                        # Pacing or older work held the requeue too long: return the
                        # retained failure instead of starting an attempt that cannot finish.
                        self._expire(item)
                        continue
                    if item.not_before > now:
                        wake = min(wake, min(item.not_before, item.deadline))
                        continue
                    if pick is None or (item.priority, item.arrived) < (pick.priority, pick.arrived):
                        pick = item
                if pick is not None:
                    earliest = now if self.last_start is None else self.last_start + recovery.PACE
                    if earliest > now:
                        wake = min(wake, earliest)
                        if pick.attempts:
                            # A pace-blocked requeue must still be expired on time.
                            wake = min(wake, pick.deadline - recovery.HEDGED_ATTEMPT_BUDGET)
                        pick = None
                    elif self.inflight >= MAX_INFLIGHT:
                        pick = None
                if pick is None:
                    self.cond.wait(timeout=max(0.001, wake - now))
                    continue
                pick.state = 'inflight'
                pick.attempts += 1
                if pick.first_start is None:
                    pick.first_start = now
                self.inflight += 1
                self.last_start = now
                if pick.attempts > 1:
                    self.transport['upstream_retries'] += 1
            threading.Thread(target=self._run_item, args=(pick,), daemon=True,
                             name='announce-attempt').start()

    def _expire(self, item):
        """Called with the condition held; a requeued item ran out of deadline."""
        self.transport['expired_requeues'] += 1
        kind, value = item.last_failure or ('error', TimeoutError('upstream_deadline'))
        if kind == 'result':
            wait_ms = round(((item.first_start or item.arrived) - item.arrived) * 1000)
            self._finish(item, result=(*value, wait_ms, item.attempts))
        else:
            self._finish(item, error=value)

    def _finish(self, item, result=None, error=None):
        """Called with the condition held."""
        if not item.future.done():
            if error is not None:
                item.future.set_exception(error)
            else:
                item.future.set_result(result)
        if self.items.get(item.key) is item:
            del self.items[item.key]
        self.cond.notify_all()

    def _run_item(self, item):
        requeue = None
        result = None
        error = None
        try:
            status, headers, body = self._hedged(item)
            rejection = self.is_rejection(body)
            with self.cond:
                self.transport['upstream_http_errors'] += int(status != 200)
                ok = status == 200 and not rejection and self.is_valid(body)
                self.transport['upstream_valid_announces'] += int(ok)
                if ok and (item.failed_sockets or item.failed_http):
                    self.transport['recovered_groups'] += 1
            if recovery.requeue_http(status, headers, rejection) and item.attempts < recovery.MAX_ATTEMPTS:
                delay = recovery.requeue_delay(headers, item.deadline - time.monotonic())
                if delay is not None:
                    requeue = ('requeued_http', delay)
                    item.failed_http += 1
                    item.last_failure = ('result', (status, headers, body))
            result = (status, headers, body)
        except Exception as exc:
            error = exc
            if (recovery.transient_exception(exc) and item.attempts < recovery.MAX_ATTEMPTS and
                    not self.stopping.is_set()):
                delay = recovery.requeue_delay({}, item.deadline - time.monotonic())
                if delay is not None:
                    requeue = ('requeued_transport', delay)
                    item.last_failure = ('error', exc)
        with self.cond:
            self.inflight -= 1
            item.handles = []
            if requeue is not None and not item.future.done():
                self.transport[requeue[0]] += 1
                item.state = 'ready'
                item.not_before = time.monotonic() + requeue[1]
                self.cond.notify_all()
                return
            if error is not None:
                self._finish(item, error=error)
            else:
                wait_ms = round(((item.first_start or item.arrived) - item.arrived) * 1000)
                self._finish(item, result=(*result, wait_ms, item.attempts))

    # -- hedged attempt ------------------------------------------------------
    def _hedged(self, item):
        """Run one attempt: socket 1, plus socket 2 if 1 stalls or fails early."""
        state = {'winner': None, 'result': None, 'error': None, 'started': 0, 'finished': 0,
                 'headers': False, 'permanent': None, 'accounted': set()}
        cond = threading.Condition()
        handles = {}

        def start_socket(index):
            handle = Handle()
            handles[index] = handle
            with self.cond:
                self.transport['upstream_attempts'] += 1
                item.handles.append(handle)
            state['started'] += 1
            threading.Thread(target=run_socket, args=(index, handle), daemon=True,
                             name='announce-socket').start()

        def claim(index):
            # Called with cond held. First headers win; the other socket is shut down.
            if state['winner'] is None:
                state['winner'] = index
                state['headers'] = True
                if index == 2:
                    with self.cond:
                        self.transport['hedge_wins'] += 1
                other = handles.get(3 - index)
                if other is not None:
                    other.abort()
            return state['winner'] == index

        def run_socket(index, handle):
            def on_headers():
                with cond:
                    if not claim(index):
                        raise Aborted()
            try:
                deadline = min(item.deadline, time.monotonic() + recovery.ATTEMPT_BUDGET)
                if handle.aborted or self.stopping.is_set() or time.monotonic() >= item.deadline:
                    raise Aborted()
                outcome = self.fetch(item.target, item.user_agent, deadline=deadline,
                                     on_headers=on_headers, handle=handle)
                with cond:
                    if claim(index):
                        state['result'] = outcome
            except Exception as exc:
                with cond:
                    if handle.aborted and state['winner'] not in (None, index):
                        pass  # the loser was shut down on purpose: not an error
                    else:
                        # Includes deadline/stop aborts, which are real failures of this socket.
                        if isinstance(exc, Aborted):
                            exc = TimeoutError('upstream_deadline')
                        if index not in state['accounted']:
                            state['accounted'].add(index)
                            with self.cond:
                                self.transport['upstream_transport_errors'] += 1
                            item.failed_sockets += 1
                        if not recovery.transient_exception(exc):
                            state['permanent'] = exc
                        if state['winner'] == index or state['error'] is None:
                            state['error'] = exc
                        if state['winner'] == index:
                            state['winner'] = -1  # winner failed after headers: no more hedging
            finally:
                with cond:
                    state['finished'] += 1
                    cond.notify_all()

        started_at = time.monotonic()
        hedge_at = started_at + recovery.HEDGE_AFTER
        start_socket(1)
        with cond:
            while True:
                now = time.monotonic()
                if state['result'] is not None:
                    return state['result']
                all_done = state['finished'] >= state['started']
                may_hedge = (state['started'] == 1 and state['winner'] is None and
                             not self.stopping.is_set() and
                             now + recovery.CONNECT_TIMEOUT < item.deadline)
                failed_early = all_done and state['error'] is not None
                if may_hedge and (now >= hedge_at or failed_early):
                    if failed_early and state['permanent'] is not None:
                        break
                    with self.cond:
                        self.transport['hedges'] += 1
                        self.transport['stalled_sockets'] += int(not failed_early)
                    start_socket(2)
                    continue
                if all_done:
                    break
                if now >= item.deadline:
                    # Account the still-open sockets now; their threads may notice later.
                    for index, handle in handles.items():
                        handle.abort()
                        if index not in state['accounted']:
                            state['accounted'].add(index)
                            with self.cond:
                                self.transport['upstream_transport_errors'] += 1
                            item.failed_sockets += 1
                    state['error'] = state['error'] or TimeoutError('upstream_deadline')
                    break
                timeout = item.deadline - now
                if may_hedge:
                    timeout = min(timeout, hedge_at - now)
                cond.wait(timeout=max(0.001, timeout))
            if state['result'] is not None:
                return state['result']
            # A permanent failure on either socket makes the attempt final (no requeue).
            raise state['permanent'] or state['error'] or TimeoutError('upstream_deadline')
