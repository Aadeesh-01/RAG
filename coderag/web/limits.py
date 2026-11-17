"""
Rate limiting and answer caching, behind small interfaces (RateLimitStore,
AnswerCacheStore) so a shared backend such as Redis can replace the in-memory
ones when the server runs as several processes. Today: in memory, one process.
Neither holds anything that must survive a restart; the database is for that.
"""

import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict, deque


class RateLimitStore(ABC):
    @abstractmethod
    def hit(self, key, limit, window):
        """Record a hit on `key`. Returns (allowed, retry_after_seconds)."""


class AnswerCacheStore(ABC):
    @abstractmethod
    def get(self, key):
        """The cached events for `key`, or None."""

    @abstractmethod
    def put(self, key, events):
        """Cache the events of a finished answer."""


class RateLimiter(RateLimitStore):
    """
    In memory: sliding window, at most `limit` hits per `window` seconds per key.
    Every SWEEP_EVERY hits, buckets whose hits have all expired are dropped, so
    one-off visitors don't accumulate.
    """

    SWEEP_EVERY = 1000

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.hits = defaultdict(deque)
        self.windows = {}
        self.lock = threading.Lock()
        self.calls = 0

    def hit(self, key, limit, window):
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = self.clock()
        with self.lock:
            self.calls += 1
            if self.calls % self.SWEEP_EVERY == 0:
                self._sweep(now)
            self.windows[key] = window
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, max(q[0] + window - now, 1.0)
            q.append(now)
            return True, 0.0

    def _sweep(self, now):
        for key in [k for k, q in self.hits.items() if not q or q[-1] <= now - self.windows.get(k, 0)]:
            del self.hits[key]
            self.windows.pop(key, None)


class AnswerCache(AnswerCacheStore):
    """In memory: LRU of finished answer streams, so popular demo questions cost no LLM quota."""

    def __init__(self, size=500):
        self.size = size
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key not in self.items:
                return None
            self.items.move_to_end(key)
            return self.items[key]

    def put(self, key, events):
        if self.size <= 0:
            return
        with self.lock:
            self.items[key] = events
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)

    def drop(self, predicate):
        """Remove the entries whose key matches; returns how many."""
        with self.lock:
            gone = [k for k in self.items if predicate(k)]
            for k in gone:
                del self.items[k]
            return len(gone)


class LRU:
    """A small thread-safe LRU dict (the graph cache)."""

    def __init__(self, size):
        self.size = max(int(size), 1)
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key, make):
        """The value for `key`, made with make() on a miss (outside the lock)."""
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        value = make()
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return value

    def __len__(self):
        return len(self.items)


class Gate:
    """At most `size` holders at once; try_enter() never waits (the caller answers 503)."""

    def __init__(self, size):
        self.semaphore = threading.BoundedSemaphore(max(int(size), 1))

    def try_enter(self):
        """A release function (idempotent), or None when full."""
        if not self.semaphore.acquire(blocking=False):
            return None
        done = threading.Lock()

        def release():
            if done.acquire(blocking=False):
                self.semaphore.release()
        return release


class DailyUsage:
    """
    Answers given with the server's LLM key today (UTC), for the daily cap:
    signed-in users' answers are counted from the `questions` table (server_key
    rows), so the count survives restarts and is shared by processes; signed-out
    visitors' answers are not stored, so they are counted in memory.
    """

    def __init__(self, db, clock=None):
        from ..db.models import utcnow
        self.db = db
        self.clock = clock or utcnow
        self.lock = threading.Lock()
        self.anonymous = (None, 0)                   # (day, count)

    def _today(self):
        now = self.clock()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def used(self):
        from ..db.models import Question
        today = self._today()
        with self.db.session() as s:
            stored = s.query(Question).filter(Question.server_key.is_(True),
                                              Question.created_at >= today).count()
        with self.lock:
            day, count = self.anonymous
            return stored + (count if day == today else 0)

    def allowed(self, cap):
        return self.used() < cap

    def add_anonymous(self):
        today = self._today()
        with self.lock:
            day, count = self.anonymous
            self.anonymous = (today, (count if day == today else 0) + 1)


def client_ip(request, trusted_proxy_hops=0):
    """
    The client's address. Behind N trusted proxies, the client is the Nth
    X-Forwarded-For entry from the right: entries further left were sent by the
    client itself and can be forged.
    """
    if trusted_proxy_hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",")
                 if p.strip()]
        if len(parts) >= trusted_proxy_hops:
            return parts[-trusted_proxy_hops]
    return request.client.host if request.client else "unknown"
