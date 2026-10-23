"""AUSTRO AI - rate limiter thread-safety regressions.

RATELIMIT-1 (security-relevant)
    `SlidingWindowRateLimiter` created both `_buckets` and `_locks` as
    `defaultdict(...)` and `_cleanup()` deleted both entries for an empty key.
    A request that was already inside the critical section kept a reference to
    the old lock and the old (detached) timestamp list, while a concurrent
    request for the same key created a *new* lock and a *new* empty list. The
    two then evaluated the same empty window and were both admitted - the
    limiter admitted more than `max_requests` for one key, which is the exact
    concurrent scenario it is supposed to defend against (a broadcast/rogue
    client fanning requests out).

    Fix: the per-key lock is created once and never deleted; cleanup prunes the
    bucket only while holding that key's lock.

RATELIMIT-2
    The registry dicts were mutated from multiple threads without any lock, so
    iteration during cleanup could race with insertion. A dedicated structure
    lock now guards them.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefghIJKlmnoPQRstuvwxyz-DummyToken")
os.environ["GEMINI_API_KEY"] = ""

from app.security.rate_limiter import (  # noqa: E402
    RateLimitConfig,
    RateLimiter,
    SlidingWindowRateLimiter,
)


def _exhausted_limiter():
    return SlidingWindowRateLimiter()


def test_cleanup_never_discards_a_lock_object():
    """The lock identity for a key must be stable across cleanup."""
    limiter = _exhausted_limiter()
    limiter.check_rate_limit("command:1", max_requests=5, window_seconds=60, now=1000.0)
    lock = limiter._locks["command:1"]

    # Force an empty bucket whose timestamps are all older than the max window,
    # which is exactly the state in which the old code deleted both entries.
    limiter._cleanup(now=10_000.0)

    assert limiter._locks["command:1"] is lock, (
        "the per-key lock was replaced; an in-flight request holding the old "
        "lock would no longer be serialized against new requests"
    )
    assert "command:1" not in limiter._buckets  # the empty bucket itself is pruned


def test_cleanup_keeps_in_window_usage_and_reuses_the_same_lock():
    """Cleanup must not reset a live window, and must reuse the same lock.

    Sequence: one admission inside the window, cleanup (which must keep the
    live timestamp), then a second request that has to be *blocked*. If cleanup
    had detached the bucket or swapped the lock, the second request would come
    out allowed - the bypass this test locks down.
    """
    limiter = _exhausted_limiter()
    key = "ai_request:42"
    lock = limiter._lock_for(key)

    assert limiter.check_rate_limit(key, max_requests=1, window_seconds=3600, now=0.0).allowed

    limiter._cleanup(now=100.0)  # entry is 100s old, well inside the max window

    assert limiter._locks[key] is lock
    assert limiter._buckets[key] == [0.0], "cleanup dropped a live timestamp"
    blocked = limiter.check_rate_limit(key, max_requests=1, window_seconds=3600, now=100.0)
    assert not blocked.allowed


def test_cleanup_prunes_only_expired_entries():
    limiter = _exhausted_limiter()
    key = "command:9"
    for timestamp in (0.0, 2000.0, 4000.0):
        limiter.check_rate_limit(key, max_requests=10, window_seconds=3600, now=timestamp)

    limiter._cleanup(now=4000.0)  # max window is 3600s -> only t=0 expires
    assert limiter._buckets[key] == [2000.0, 4000.0]


def test_concurrent_admissions_never_exceed_the_limit():
    """End-to-end: hammer one key while cleanup runs on every call."""
    limiter = _exhausted_limiter()
    limit = 5
    outcomes = []
    lock = threading.Lock()

    def worker():
        for _ in range(25):
            limiter._last_cleanup = 0.0  # makes cleanup run on every call
            result = limiter.check_rate_limit(
                "global:ai", max_requests=limit, window_seconds=3600,
                now=time.monotonic(),
            )
            with lock:
                outcomes.append(result.allowed)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: worker(), range(8)))

    admitted = sum(1 for allowed in outcomes if allowed)
    assert admitted == limit, (
        f"{admitted} requests were admitted with max_requests={limit}; "
        "the sliding window is not enforced under concurrency"
    )


def test_rate_limiter_public_api_is_unchanged():
    """The fix must not change the observable contract of the limiter."""
    rl = RateLimiter(limits={
        "command": RateLimitConfig(max_requests=2, window_seconds=60),
    })
    assert rl.check_limit(1, "command", now=10.0).allowed
    assert rl.check_limit(1, "command", now=10.0).allowed
    assert not rl.check_limit(1, "command", now=10.0).allowed
    assert rl.check_limit(2, "command", now=10.0).allowed  # per-user isolation
    assert rl.check_limit(1, "unknown_bucket", now=10.0).allowed
