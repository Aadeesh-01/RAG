"""In-memory rate limiting and answer caching (single process, single worker)."""

import threading
import time
from collections import OrderedDict, defaultdict, deque


class RateLimiter:
    """Sliding-window limiter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, key, limit, window):
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = self.clock()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, max(q[0] + window - now, 1.0)
            q.append(now)
            return True, 0.0


class AnswerCache:
    """LRU of finished answer streams, so popular demo questions cost no LLM quota."""

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
