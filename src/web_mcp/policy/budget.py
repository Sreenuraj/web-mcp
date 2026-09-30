"""Async token-bucket rate limiter (per key), used for per-domain and per-provider throttling."""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, rate_per_sec: float, burst: int = 3, max_wait: float = 15.0):
        self.rate = max(rate_per_sec, 0.01)
        self.burst = max(burst, 1)
        self.max_wait = max_wait
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        """Wait (up to max_wait) for a token. Raises TimeoutError if the wait would be too long."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                tokens, last = self._state.get(key, (float(self.burst), now))
                tokens = min(self.burst, tokens + (now - last) * self.rate)
                if tokens >= 1:
                    self._state[key] = (tokens - 1, now)
                    return
                need = (1 - tokens) / self.rate
                self._state[key] = (tokens, now)
            if waited + need > self.max_wait:
                raise TimeoutError(f"rate limit for {key}: retry later")
            await asyncio.sleep(need)
            waited += need
