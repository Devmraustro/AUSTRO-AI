"""
AUSTRO AI - Application-Level Rate Limiter.

Per-user rate limiting for commands, AI requests, uploads, and expensive operations.
Deterministic, in-memory with optional Redis backend for production.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from threading import Lock

from app.config.settings import settings


@dataclass
class RateLimitConfig:
    """Configuration for a rate limit bucket."""
    max_requests: int
    window_seconds: float
    burst_allowance: int = 0  # Extra requests allowed in burst


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""
    allowed: bool
    remaining: int
    reset_at: float
    retry_after: float = 0.0


# Default rate limit configurations
DEFAULT_LIMITS = {
    # Per-user command limits (per minute)
    "command": RateLimitConfig(max_requests=30, window_seconds=60, burst_allowance=5),
    # Per-user AI requests (per minute)
    "ai_request": RateLimitConfig(max_requests=20, window_seconds=60, burst_allowance=3),
    # Per-user file uploads (per hour)
    "upload": RateLimitConfig(max_requests=10, window_seconds=3600, burst_allowance=2),
    # Per-user knowledge ingestion (per hour)
    "ingestion": RateLimitConfig(max_requests=5, window_seconds=3600, burst_allowance=1),
    # Per-user expensive operations (per hour)
    "expensive": RateLimitConfig(max_requests=10, window_seconds=3600, burst_allowance=2),
    # Global AI requests (per minute)
    "global_ai": RateLimitConfig(max_requests=100, window_seconds=60, burst_allowance=10),
}


class TokenBucket:
    """Thread-safe token bucket for rate limiting."""
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate  # tokens per second
        self.last_refill = time.monotonic()
        self._lock = Lock()
    
    def consume(self, tokens: int = 1) -> Tuple[bool, float]:
        """Try to consume tokens. Returns (allowed, retry_after)."""
        with self._lock:
            now = time.monotonic()
            # Refill tokens
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True, 0.0
            else:
                # Calculate time until enough tokens
                needed = tokens - self.tokens
                retry_after = needed / self.refill_rate
                return False, retry_after


class SlidingWindowRateLimiter:
    """Sliding window rate limiter with burst support."""
    
    def __init__(self):
        self._buckets: Dict[str, list] = defaultdict(list)  # key -> list of timestamps
        self._locks: Dict[str, Lock] = defaultdict(Lock)
        self._cleanup_interval = 300  # 5 minutes
        self._last_cleanup = time.monotonic()
    
    def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window_seconds: float,
        burst_allowance: int = 0,
        now: Optional[float] = None,
    ) -> RateLimitResult:
        """Check if request is allowed under rate limit."""
        if now is None:
            now = time.monotonic()
        now = float(now)
        
        # Periodic cleanup
        if now - self._last_cleanup > self._cleanup_interval:
            self._cleanup(now)
        
        lock = self._locks[key]
        with lock:
            timestamps = self._buckets[key]
            window_start = now - window_seconds
            
            # Remove expired timestamps
            while timestamps and timestamps[0] < window_start:
                timestamps.pop(0)
            
            current_count = len(timestamps)
            effective_limit = max_requests + burst_allowance
            
            if current_count < effective_limit:
                timestamps.append(now)
                remaining = effective_limit - current_count - 1
                return RateLimitResult(
                    allowed=True,
                    remaining=max(0, remaining),
                    reset_at=now + window_seconds,
                )
            else:
                # Calculate retry after
                oldest = timestamps[0]
                retry_after = oldest + window_seconds - now
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_at=oldest + window_seconds,
                    retry_after=max(0, retry_after),
                )
    
    def _cleanup(self, now: float):
        """Remove expired entries."""
        window_seconds = 3600  # Max window
        cutoff = now - window_seconds
        for key in list(self._buckets.keys()):
            timestamps = self._buckets[key]
            while timestamps and timestamps[0] < cutoff:
                timestamps.pop(0)
            if not timestamps:
                del self._buckets[key]
                if key in self._locks:
                    del self._locks[key]
        self._last_cleanup = now


class RateLimiter:
    """Application-level rate limiter with per-user buckets."""

    def __init__(self, limits: Optional[Dict[str, RateLimitConfig]] = None):
        self._limiter = SlidingWindowRateLimiter()
        if limits is not None:
            self._configs = DEFAULT_LIMITS.copy()
            self._configs.update(limits)
        else:
            self._configs = self._configs_from_settings()

    @staticmethod
    def _configs_from_settings() -> dict:
        s = settings
        return {
            "command": RateLimitConfig(
                max_requests=s.rate_limit_command_max,
                window_seconds=s.rate_limit_command_window,
            ),
            "ai_request": RateLimitConfig(
                max_requests=s.rate_limit_ai_max,
                window_seconds=s.rate_limit_ai_window,
            ),
            "upload": RateLimitConfig(
                max_requests=s.rate_limit_upload_max,
                window_seconds=s.rate_limit_upload_window,
            ),
            "ingestion": RateLimitConfig(
                max_requests=s.rate_limit_ingestion_max,
                window_seconds=s.rate_limit_ingestion_window,
            ),
            "expensive": RateLimitConfig(
                max_requests=s.rate_limit_expensive_max,
                window_seconds=s.rate_limit_expensive_window,
            ),
            "global_ai": RateLimitConfig(
                max_requests=s.rate_limit_global_ai_max,
                window_seconds=s.rate_limit_global_ai_window,
            ),
        }

    def configure(self, limits: dict):
        """Update rate limit configurations."""
        self._configs.update(limits)
    
    def check_limit(
        self,
        user_id: int,
        limit_type: str,
        custom_key: Optional[str] = None,
        now: Optional[float] = None,
    ) -> RateLimitResult:
        """Check rate limit for a user and limit type."""
        config = self._configs.get(limit_type)
        if not config:
            # No limit configured - allow
            return RateLimitResult(allowed=True, remaining=999, reset_at=0)
        
        key = custom_key or f"{limit_type}:{user_id}"
        return self._limiter.check_rate_limit(
            key=key,
            max_requests=config.max_requests,
            window_seconds=config.window_seconds,
            burst_allowance=config.burst_allowance,
            now=now,
        )
    
    def check_global_ai_limit(self, now: Optional[float] = None) -> RateLimitResult:
        """Check global AI request limit."""
        config = self._configs.get("global_ai", DEFAULT_LIMITS["global_ai"])
        return self._limiter.check_rate_limit(
            key="global:ai",
            max_requests=config.max_requests,
            window_seconds=config.window_seconds,
            burst_allowance=config.burst_allowance,
            now=now,
        )


# Global instance
_rate_limiter: Optional[RateLimiter] = None
_init_lock = Lock()


def get_rate_limiter() -> RateLimiter:
    """Get global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        with _init_lock:
            if _rate_limiter is None:
                _rate_limiter = RateLimiter()
    return _rate_limiter


# Convenience functions
async def check_command_rate_limit(user_id: int) -> RateLimitResult:
    """Check command rate limit for user."""
    return get_rate_limiter().check_limit(user_id, "command")


async def check_ai_request_rate_limit(user_id: int) -> RateLimitResult:
    """Check AI request rate limit for user."""
    return get_rate_limiter().check_limit(user_id, "ai_request")


async def check_upload_rate_limit(user_id: int) -> RateLimitResult:
    """Check file upload rate limit for user."""
    return get_rate_limiter().check_limit(user_id, "upload")


async def check_ingestion_rate_limit(user_id: int) -> RateLimitResult:
    """Check knowledge ingestion rate limit for user."""
    return get_rate_limiter().check_limit(user_id, "ingestion")


async def check_expensive_operation_limit(user_id: int) -> RateLimitResult:
    """Check expensive operation rate limit for user."""
    return get_rate_limiter().check_limit(user_id, "expensive")


async def check_global_ai_limit() -> RateLimitResult:
    """Check global AI request limit."""
    return get_rate_limiter().check_global_ai_limit()


def get_rate_limit_headers(result: RateLimitResult) -> dict:
    """Generate rate limit headers for HTTP responses."""
    return {
        "X-RateLimit-Limit": str(result.remaining + (1 if result.allowed else 0)),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(int(result.reset_at)),
        "Retry-After": str(int(result.retry_after)) if not result.allowed else "",
    }


# Integration with Telegram handlers
class RateLimitMiddleware:
    """Middleware to enforce rate limits in Telegram handlers."""
    
    def __init__(self):
        self.limiter = get_rate_limiter()
    
    async def check_command_limit(self, user_id: int) -> tuple[bool, str]:
        """Check command rate limit, return (allowed, error_message)."""
        result = await check_command_rate_limit(user_id)
        if not result.allowed:
            return False, f"⚡ Too many commands. Try again in {result.retry_after:.0f}s."
        return True, ""
    
    async def check_ai_limit(self, user_id: int) -> tuple[bool, str]:
        """Check AI request limit."""
        result = await check_ai_request_rate_limit(user_id)
        if not result.allowed:
            return False, f"⚡ Too many AI requests. Try again in {result.retry_after:.0f}s."
        return True, ""
    
    async def check_upload_limit(self, user_id: int) -> tuple[bool, str]:
        result = await check_upload_rate_limit(user_id)
        if not result.allowed:
            return False, f"⚡ Upload limit reached. Try again in {result.retry_after:.0f}s."
        return True, ""
    
    async def check_ingestion_limit(self, user_id: int) -> tuple[bool, str]:
        result = await check_ingestion_rate_limit(user_id)
        if not result.allowed:
            return False, f"⚡ Too many ingestions. Try again in {result.retry_after:.0f}s."
        return True, ""


# Global middleware instance
_middleware_instance = None


def get_rate_limit_middleware() -> RateLimitMiddleware:
    global _middleware_instance
    if _middleware_instance is None:
        _middleware_instance = RateLimitMiddleware()
    return _middleware_instance