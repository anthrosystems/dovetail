"""Sync / Async wrapper for Python, built on Asyncio + ThreadPoolExecutor.

Quick use:
- from async code: await d.task.to_thread(fn, *args)
- from async code: task = d.task.schedule(coro_or_callable, ...)
- from sync code: result = d.task.run_blocking(coro_or_callable, ...)

Optional constructor settings add rate limiting, default timeout, retries,
events, and cumulative status counters.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Dict

from ._events import Events
from ._task import Task


class _TokenBucket:
    """Thread-safe token bucket for lightweight rate limiting."""

    def __init__(self, rate_per_sec: float, burst: Optional[float] = None) -> None:
        rate = float(rate_per_sec)
        if rate <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = rate
        self._burst = float(burst) if burst is not None else max(1.0, rate)
        self._tokens = self._burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now

    def reserve(self, tokens: float = 1.0) -> float:
        """Reserve tokens and return required wait time in seconds."""
        with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            self._tokens -= float(tokens)
            if self._tokens >= 0:
                return 0.0
            return (-self._tokens) / self._rate

    async def acquire_async(self, tokens: float = 1.0) -> float:
        wait_for = self.reserve(tokens=tokens)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        return wait_for


class Dovetail:
    """Run sync and async workloads through one helper.

    The `task` helper exposes:
    - `to_thread`: run blocking callables from async code.
    - `schedule`: schedule coroutines or callables.
    - `run_blocking`: run async/sync work from sync code.

    Optional defaults (timeout/retries/rate limit) and events provide
    concise observability via `events.stats()`.
    """

    def __init__(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        max_workers: Optional[int] = None,
        trace: bool = False,
        trace_logger: Optional[logging.Logger] = None,
        trace_prefix: str = "Dovetail",
        rate_limit_per_sec: Optional[float] = None,
        rate_limit_burst: Optional[float] = None,
        default_timeout: Optional[float] = None,
        default_retries: int = 0,
        default_retry_backoff: float = 0.0,
    ):
        # An optional event loop to associate with this helper. When None,
        # the currently running loop (at call time) will be used.
        self._loop = loop

        # ThreadPoolExecutor used for running blocking sync functions.
        self._threadpool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        trace_env = str(os.getenv("DOVETAIL_TRACE", "")).strip().lower()
        trace_enabled = bool(trace) or trace_env in ("1", "true", "yes", "on")
        self._trace_counter = itertools.count(1)
        self._execution_counter = itertools.count(1)
        self._default_timeout = float(default_timeout) if default_timeout is not None else None
        self._default_retries = max(0, int(default_retries or 0))
        self._default_retry_backoff = max(0.0, float(default_retry_backoff or 0.0))
        self._rate_limiter = None
        if rate_limit_per_sec is not None:
            self._rate_limiter = _TokenBucket(rate_per_sec=float(rate_limit_per_sec), burst=rate_limit_burst)

        # Expose helpers bound to this Dovetail instance.
        self.events = Events(
            self,
            trace_enabled=trace_enabled,
            trace_logger=trace_logger,
            trace_prefix=trace_prefix,
        )
        self.task = Task(self)

    def _next_trace_id(self) -> int:
        return next(self._trace_counter)

    def _next_execution_id(self) -> str:
        return f"run-{next(self._execution_counter)}"

    def _emit_event(self, event: str, payload: Dict[str, Any]) -> None:
        self.events.emit(event, payload)

    async def _acquire_rate_limit(
        self,
        method: str,
        task: Optional[Any] = None,
        function: Optional[str] = None,
    ) -> None:
        if self._rate_limiter is None:
            return
        waited = await self._rate_limiter.acquire_async(tokens=1.0)
        if waited > 0:
            self.events.inc_stat("throttled")
            payload = {
                "method": method,
                "task": task,
                "function": function,
                "waited": waited,
            }
            self.events.trace_struct(
                method=method,
                status="Rate Limited",
                task=task,
                function=function,
                elapsed=waited,
            )
            self._emit_event("task_rate_limited", payload)

    @staticmethod
    def _callable_name(func: Any) -> str:
        if hasattr(func, "__qualname__"):
            return str(getattr(func, "__qualname__"))
        if hasattr(func, "__name__"):
            return str(getattr(func, "__name__"))
        return type(func).__name__
        
    def shutdown(self, wait: bool = True) -> None:
        """Shut down the internal threadpool executor."""
        if self.events.trace_enabled:
            self.events.trace(f"Shutdown (wait={wait})")
        self._threadpool.shutdown(wait=wait)