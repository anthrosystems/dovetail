"""Sync / Async wrapper for Python, built on Asyncio + ThreadPoolExecutor.

Quick use:
- from async code: await d.task.to_thread(fn, *args)
- from async code: task = d.task.schedule(coro_or_callable, ...)
- from sync code: result = d.task.run_blocking(coro_or_callable, ...)

Optional constructor settings add rate limiting, default timeout, retries,
events, and cumulative status counters.
"""

from __future__ import annotations

import os, time, logging, itertools, signal
import asyncio, threading, concurrent.futures
import weakref

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
        # Atomically reserve tokens. If sufficient tokens are available this
        # returns 0.0 (no wait). When the bucket is exhausted we return the
        # required wait time (in seconds) for the requested tokens to become
        # available so callers can sleep or schedule accordingly.
        with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            self._tokens -= float(tokens)
            if self._tokens >= 0:
                return 0.0
            return (-self._tokens) / self._rate

    async def acquire_async(self, tokens: float = 1.0) -> float:
        # Async-friendly wait helper that uses `reserve` and then sleeps for
        # the required duration if the bucket is currently exhausted.
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
        timeout: Optional[float] = None,
        retries: int = 0,
        retry_backoff: float = 0.0,
        shutdown_on_exit: bool = False,
        auto_register: bool = True,
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
        self._default_timeout = float(timeout) if timeout is not None else None
        self._default_retries = max(0, int(retries or 0))
        self._default_retry_backoff = max(0.0, float(retry_backoff or 0.0))
        self._rate_limiter = None
        if rate_limit_per_sec is not None:
            self._rate_limiter = _TokenBucket(rate_per_sec=float(rate_limit_per_sec), burst=rate_limit_burst)

        # Expose helpers bound to this Dovetail instance.
        self.events = Events(
            weakref.ref(self),
            trace_enabled=trace_enabled,
            trace_logger=trace_logger,
            trace_prefix=trace_prefix,
        )

        self.task = Task(self)
        
        # Shutdown tracking to make shutdown() idempotent and safe
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._finalizer = weakref.finalize(
            self,
            Dovetail._finalize_warning,
            trace_logger,
        )
        
        # Optional automatic shutdown on process exit / signals. When enabled
        # Dovetail will call `shutdown()` during normal interpreter exit
        # (atexit) and when SIGINT / SIGTERM are received.
        self._shutdown_on_exit = bool(shutdown_on_exit)
        self._orig_signal_handlers: dict[int, object] = {}
        self._registered_atexit = False
        self._atexit_callback = None

        if self._shutdown_on_exit:
            try:
                import atexit

                self._atexit_callback = (
                    Dovetail._atexit_shutdown,
                    weakref.ref(self),
                )

                atexit.register(*self._atexit_callback)
                self._registered_atexit = True

            except Exception:
                pass
            
            # Register simple wrappers for SIGINT / SIGTERM where available.
            for _sig in (getattr(signal, 'SIGINT', None), getattr(signal, 'SIGTERM', None)):
                if _sig is None:
                    continue
                try:
                    prev = signal.getsignal(_sig)
                    # store previous handler so we can chain / restore later
                    self._orig_signal_handlers[_sig] = prev
                    signal.signal(_sig, self._signal_handler)
                except Exception:
                    # platforms may restrict signal handling (e.g. some windows
                    # environments) — ignore failures and proceed.
                    pass
        
        # Optional auto-registration with package-level registry. Import
        # lazily to avoid circular imports when the package is imported.
        if auto_register:
            from ._register import register as _dovetail_register
            _dovetail_register(self)

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
        # Return a human-friendly callable name for logging / traces. Where
        # available `__qualname__` is preferred as it often contains class
        # context (useful for bound methods), otherwise fall back to
        # `__name__` or the type name.
        if hasattr(func, "__qualname__"):
            return str(getattr(func, "__qualname__"))
        if hasattr(func, "__name__"):
            return str(getattr(func, "__name__"))
        return type(func).__name__

    @staticmethod
    def _finalize_warning(logger):
        if logger:
            logger.warning(
                "Dovetail instance was garbage collected without shutdown()"
            )
    
    @staticmethod
    def _atexit_shutdown(ref):
        dvt = ref()
        if dvt is not None:
            dvt.shutdown()
        
    def shutdown(self, wait: bool = True) -> None:
        """Shut down the internal threadpool executor."""
        try:
            # Make shutdown idempotent and thread-safe.
            with self._shutdown_lock:
                if self._shutdown:
                    return

                self._shutdown = True

            # Prevent the GC warning because shutdown happened explicitly.
            try:
                if self._finalizer.alive:
                    self._finalizer.detach()
            except Exception:
                pass

            if self.events.trace_enabled:
                self.events.trace(f"Shutdown (wait={wait})")
            try:
                self._threadpool.shutdown(wait=wait)
            except Exception as exc:
                # Defensive: shutdown should not raise during cleanup calls
                if self.events.trace_enabled:
                    self.events.trace(f"Shutdown error: {exc}")
            # If we registered process-level handlers for shutdown, unregister
            # them now so an explicit call to `shutdown()` undoes our modifications
            # and avoids double-invocation or surprising behavior for the caller.
            if self._shutdown_on_exit:
                try:
                    if self._registered_atexit and self._atexit_callback:
                        import atexit

                        if hasattr(atexit, "unregister"):
                            atexit.unregister(self._atexit_callback[0])

                        self._registered_atexit = False
                        self._atexit_callback = None

                except Exception:
                    pass
                
                # Restore previous signal handlers we replaced during init.
                try:
                    for sig, prev in list(self._orig_signal_handlers.items()):
                        try:
                            signal.signal(sig, prev)
                        except Exception:
                            pass
                    self._orig_signal_handlers.clear()
                except Exception:
                    pass
        finally:
            # Remove this instance from the global registry.
            try:
                from ._register import unregister as _dovetail_unregister
                _dovetail_unregister(self)
            except Exception:
                pass

    def _signal_handler(self, signum: int, frame) -> None:
        # Best-effort: ensure we shutdown resources, then delegate to the
        # previous signal handler. If the previous handler was the default
        # action, restore it and re-raise the signal so the process terminates
        # as expected.
        try:
            self.shutdown(wait=True)
        except Exception:
            pass

        prev = self._orig_signal_handlers.get(signum)
        try:
            if prev in (signal.SIG_IGN, signal.SIG_DFL):
                # restore default and re-raise the signal
                signal.signal(signum, prev)
                try:
                    os.kill(os.getpid(), signum)
                except Exception:
                    raise SystemExit
            elif callable(prev):
                try:
                    prev(signum, frame)
                except Exception:
                    pass
        except Exception:
            # give up silently — we've already attempted shutdown
            pass
    
    # Context manager support (sync + async)
    def __enter__(self) -> "Dovetail":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Synchronous context manager: perform blocking shutdown
        self.shutdown(wait=True)

    async def __aenter__(self) -> "Dovetail":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Async context manager: run shutdown in executor to avoid blocking loop
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.shutdown, True)
        except RuntimeError:
            # No running loop; fallback to direct call
            self.shutdown(wait=True)
    
    @property
    def is_shutdown(self) -> bool:
        """Whether this Dovetail instance has been shut down."""
        return self._shutdown