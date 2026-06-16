"""Simple per-client rate limiting middleware (§10 NFR).

Sliding fixed-window counter keyed by client IP. In-memory (adequate for a single API
process); swap for a Redis token bucket behind a load balancer.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, max_requests: int = 120, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        # Don't throttle SSE streams (long-lived) or health checks.
        if request.url.path.endswith("/stream") or request.url.path == "/health":
            return await call_next(request)

        client = request.client.host if request.client else "anon"
        now = time.monotonic()
        bucket = self._hits[client]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})
        bucket.append(now)
        return await call_next(request)
