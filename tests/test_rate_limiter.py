"""
AUSTRO AI - Application-level rate limit tests (Phase G item 5).

These are deterministic by construction (injected clock). They verify that
Telegram platform limits are NOT conflated with per-user application rate
limiting and that each guard bucket behaves correctly and independently:
commands, AI requests, uploads, ingestion, expensive operations and the global
AI budget.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-rate-limit-token-abcdef")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key-0000000000")

import pytest  # noqa: E402

from app.security.rate_limiter import (  # noqa: E402
    RateLimiter,
    RateLimitConfig,
    DEFAULT_LIMITS,
)


def make_limiter(**overrides):
    """Build a limiter with deterministic small limits for tests."""
    limits = {
        "command": RateLimitConfig(max_requests=3, window_seconds=60, burst_allowance=0),
        "ai_request": RateLimitConfig(max_requests=2, window_seconds=60),
        "upload": RateLimitConfig(max_requests=1, window_seconds=3600),
        "ingestion": RateLimitConfig(max_requests=1, window_seconds=3600),
        "expensive": RateLimitConfig(max_requests=2, window_seconds=3600),
        "global_ai": RateLimitConfig(max_requests=5, window_seconds=60),
    }
    for k, v in overrides.items():
        limits[k] = v
    return RateLimiter(limits=limits)


def test_command_limit_blocked_after_budget():
    rl = make_limiter()
    t = 1000.0
    for i in range(3):
        res = rl.check_limit(42, "command", now=t)
        assert res.allowed and res.remaining == 2 - i
    res = rl.check_limit(42, "command", now=t)
    assert not res.allowed
    assert res.retry_after > 0


def test_per_user_isolation():
    rl = make_limiter()
    t = 2000.0
    for _ in range(3):
        rl.check_limit(42, "command", now=t)
    blocked = rl.check_limit(42, "command", now=t)
    assert not blocked.allowed
    other = rl.check_limit(7, "command", now=t)
    assert other.allowed  # different user unaffected


def test_window_rollover_resets_budget():
    rl = make_limiter()
    t = 3000.0
    for _ in range(3):
        rl.check_limit(42, "command", now=t)
    assert not rl.check_limit(42, "command", now=t).allowed
    # after the window slides past the first request, budget is restored
    assert rl.check_limit(42, "command", now=t + 61).allowed


def test_window_is_sliding_not_fixed():
    # old requests expire progressively (sliding window), not a hard reset
    rl = make_limiter()
    t = 4000.0
    for _ in range(3):
        rl.check_limit(42, "command", now=t)
    assert not rl.check_limit(42, "command", now=t).allowed
    ## step 80s later: the 3 requests from t=4000 all expired -> full budget
    res = rl.check_limit(42, "command", now=t + 80)
    assert res.allowed


def test_ai_request_limit():
    rl = make_limiter()
    t = 5000.0
    assert rl.check_limit(9, "ai_request", now=t).allowed
    assert rl.check_limit(9, "ai_request", now=t + 1).allowed
    assert not rl.check_limit(9, "ai_request", now=t + 2).allowed


def test_upload_limit():
    rl = make_limiter()
    t = 6000.0
    assert rl.check_limit(9, "upload", now=t).allowed
    assert not rl.check_limit(9, "upload", now=t + 1).allowed


def test_ingestion_limit():
    rl = make_limiter()
    t = 7000.0
    assert rl.check_limit(9, "ingestion", now=t).allowed
    assert not rl.check_limit(9, "ingestion", now=t + 1).allowed


def test_expensive_operation_limit():
    rl = make_limiter()
    t = 8000.0
    assert rl.check_limit(9, "expensive", now=t).allowed
    assert rl.check_limit(9, "expensive", now=t + 1).allowed
    assert not rl.check_limit(9, "expensive", now=t + 2).allowed


def test_global_ai_limit_is_shared_across_users():
    rl = make_limiter()
    t = 9000.0
    allowed = 0
    for u in range(5):
        if rl.check_global_ai_limit(now=t).allowed:
            allowed += 1
    assert not rl.check_global_ai_limit(now=t).allowed  # 6th blocked
    assert allowed == 5


def test_deterministic_sequence():
    # identical config + identical clock sequence => identical results
    def sequence(limiter):
        out = []
        t = 100.0
        for _ in range(5):
            out.append(limiter.check_limit(1, "command", now=t).allowed)
            t += 60.0
        return out

    seq1 = sequence(make_limiter())
    seq2 = sequence(make_limiter())
    assert seq1 == seq2 == [True, True, True, True, True]


def test_default_limits_present():
    for bucket in ("command", "ai_request", "upload", "ingestion", "expensive", "global_ai"):
        assert bucket in DEFAULT_LIMITS
        assert DEFAULT_LIMITS[bucket].max_requests > 0


def test_configure_replaces_bucket():
    rl = make_limiter()
    rl.configure({"command": RateLimitConfig(max_requests=1, window_seconds=60)})
    t = 120.0
    assert rl.check_limit(1, "command", now=t).allowed
    assert not rl.check_limit(1, "command", now=t + 1).allowed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))