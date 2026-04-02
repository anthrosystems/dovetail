"""
Dovetail - A lightweight helper for sync/async interoperability.

This module exposes a `Dovetail` helper class that makes it easy to:

- call blocking (synchronous) functions from async code without blocking the event loop,
- schedule coroutines or run sync callables in the background, and
- call async code from sync contexts by blocking the current thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
from typing import Any, Callable, Optional, Coroutine


class Dovetail:
    """Coordinator for running sync and async workloads.

    `Dovetail` wraps a shared `ThreadPoolExecutor` and (optionally) an
    associated event loop to provide three conveniences via its
    `task` helper object:

    - `to_thread(...)` - run blocking callables in the configured
        threadpool from async code without blocking the event loop.
    - `schedule(...)` - schedule coroutines or run callables in the
        executor and return an `asyncio.Task` for awaiting or fire-and-forget.
    - `run_blocking(...)` - run sync or async work synchronously from
        sync code (uses `asyncio.run()` for coroutines).

    Create an instance with `Dovetail(max_workers=...)` and call
    `d = Dovetail(); d.task.to_thread(...)` to use. Call
    `d.shutdown()` to cleanly close the executor when finished.
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None, max_workers: Optional[int] = None):
        # An optional event loop to associate with this helper. When None,
        # the currently running loop (at call time) will be used.
        self._loop = loop

        # ThreadPoolExecutor used for running blocking sync functions.
        self._threadpool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        # Expose a `task` helper bound to this Dovetail instance for
        # ergonomics: `Dovetail.task.to_thread(...)` etc.
        self.task = self.Task(self)
        
    class Task:
        """Helpers for scheduling and bridging sync/async call sites.

        Use the `Task` helper from a `Dovetail` instance (e.g.
        `Dovetail.task`) to run blocking callables in a threadpool,
        schedule coroutines, or run async code from sync contexts.
        """

        def __init__(self, dovetail: "Dovetail") -> None:
            self._dovetail = dovetail

        async def to_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            """Run a blocking sync `func` in a threadpool and return its result.

            This is intended for use from async code and will not block the
            event loop. `func` is executed using the instance threadpool so
            all tasks share the same pool size configured in `Dovetail`.
            """
            loop = asyncio.get_running_loop()
            fn = functools.partial(func, *args, **kwargs)
            return await loop.run_in_executor(self._dovetail._threadpool, fn)

        def run_blocking(self, func_or_coro: Any, *args: Any, **kwargs: Any) -> Any:
            """Run `func_or_coro` synchronously, blocking the current thread.

            Use cases:
            - Running synchronous functions directly from sync code.
            - Running async coroutines synchronously by delegating to
              `asyncio.run()` when a coroutine function or coroutine object
              is passed.

            Important: if an event loop is already running on the current
            thread this method will raise `RuntimeError` to avoid deadlock.
            Callers in async code should use `await d.task.schedule(...)`
            or `await d.task.to_thread(...)` instead.
            """
            try:
                # If a loop is running on this thread, get_running_loop()
                # returns it; we treat that as a fatal usage error.
                _ = asyncio.get_running_loop()
            
            except RuntimeError:
                # No running loop - safe to execute synchronously.
                if inspect.iscoroutinefunction(func_or_coro):
                    # A coroutine function was passed - call it and run it.
                    return asyncio.run(func_or_coro(*args, **kwargs))
                if inspect.iscoroutine(func_or_coro):
                    # A coroutine object was passed - run it directly.
                    return asyncio.run(func_or_coro)
                
                # Regular sync callable - call and return its result.
                return func_or_coro(*args, **kwargs)

            # If we get here, a loop is running on the current thread.
            raise RuntimeError(
                "d.task.run_blocking() cannot be called from a running "
                "event loop. Use await d.task.schedule(...) or await "
                "d.task.to_thread(...) instead."
            )

        def schedule(
            self,
            coro_or_callable: Any,
            *args: Any,
            type: Optional[str] = None,
            **kwargs: Any,
        ) -> asyncio.task:
            """Schedule work from async code and return an `asyncio.task`.

            Behavior:
            - If passed a coroutine object, it is scheduled directly.
            - If passed an async function, it is called with the provided
              args/kwargs and scheduled.
            - If passed a sync callable, it is wrapped and executed in the
              threadpool; an `asyncio.task` wrapping that work is returned.

            The returned `Task` can be awaited for the result or left
            un-awaited for fire-and-forget semantics. The optional `type`
            argument is preserved for observability metadata (not used
            by this helper directly).
            """
            loop = asyncio.get_running_loop()

            # If the user passed a coroutine object, schedule it as-is.
            if inspect.iscoroutine(coro_or_callable):
                return loop.create_task(coro_or_callable)

            # If an async function was passed (callable but coroutine-producing),
            # call it to get the coroutine and schedule that.
            if inspect.iscoroutinefunction(coro_or_callable):
                return loop.create_task(coro_or_callable(*args, **kwargs))

            # If it's a regular callable, run it in the threadpool.
            if callable(coro_or_callable):
                fn = functools.partial(coro_or_callable, *args, **kwargs)

                async def _run_in_executor() -> Any:
                    return await loop.run_in_executor(self._dovetail._threadpool, fn)

                return loop.create_task(_run_in_executor())

            raise TypeError("coro_or_callable must be a coroutine, async function, or callable")

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown internal threadpool."""
        self._threadpool.shutdown(wait=wait)