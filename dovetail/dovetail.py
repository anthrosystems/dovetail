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
import itertools
import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Coroutine, Dict


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

    def __init__(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        max_workers: Optional[int] = None,
        trace: bool = False,
        trace_logger: Optional[logging.Logger] = None,
        trace_prefix: str = "Dovetail",
    ):
        # An optional event loop to associate with this helper. When None,
        # the currently running loop (at call time) will be used.
        self._loop = loop

        # ThreadPoolExecutor used for running blocking sync functions.
        self._threadpool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        trace_env = str(os.getenv("DOVETAIL_TRACE", "")).strip().lower()
        self._trace_enabled = bool(trace) or trace_env in ("1", "true", "yes", "on")
        self._trace_logger = trace_logger or logging.getLogger("dovetail")
        self._trace_prefix = str(trace_prefix or "Dovetail")
        self._trace_counter = itertools.count(1)

        # Expose a `task` helper bound to this Dovetail instance for
        # ergonomics: `Dovetail.task.to_thread(...)` etc.
        self.task = self.Task(self)

    def _next_trace_id(self) -> int:
        return next(self._trace_counter)

    def _trace(self, message: str) -> None:
        if not self._trace_enabled:
            return
        current = threading.current_thread()
        self._trace_logger.debug(
            "[%s][thread=%s#%s] %s",
            self._trace_prefix,
            current.name,
            threading.get_ident(),
            message,
        )

    def _trace_struct(
        self,
        method: str,
        status: str,
        task: Optional[Any] = None,
        function: Optional[str] = None,
        elapsed: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._trace_enabled:
            return

        current = threading.current_thread()
        lines = [f"[{self._trace_prefix}] Thread: {current.name}#{threading.get_ident()}:"]

        detail_parts = []
        if task is not None:
            detail_parts.append(f"Task: {task}")
        if function:
            detail_parts.append(f"Function: {function}")
        detail_parts.append(f"Method: {method}")
        lines.append(" | ".join(detail_parts))

        status_parts = [f"Status: {status}"]
        if elapsed is not None:
            status_parts.append(f"Elapsed: {elapsed:.3f}s")
        lines.append(" | ".join(status_parts))

        if extra:
            for key, value in extra.items():
                if value is None:
                    continue
                lines.append(f"{key}: {value}")

        self._trace_logger.debug("\n".join(lines))

    @staticmethod
    def _callable_name(func: Any) -> str:
        if hasattr(func, "__qualname__"):
            return str(getattr(func, "__qualname__"))
        if hasattr(func, "__name__"):
            return str(getattr(func, "__name__"))
        return type(func).__name__
        
    class Task:
        """Helpers for scheduling and bridging sync/async call sites.

        Use the `Task` helper from a `Dovetail` instance (e.g.
        `Dovetail.task`) to run blocking callables in a threadpool,
        schedule coroutines, or run async code from sync contexts.
        """

        def __init__(self, dovetail: "Dovetail") -> None:
            self._dovetail = dovetail

        def _attach_task_trace(self, task: asyncio.Task, label: str) -> None:
            if not self._dovetail._trace_enabled:
                return
            task_name = task.get_name() if hasattr(task, "get_name") else f"task-{id(task)}"
            self._dovetail._trace_struct(
                method="schedule",
                status="Created",
                task=task_name,
                extra={"Label": label},
            )

            def _on_done(done_task: asyncio.Task) -> None:
                try:
                    if done_task.cancelled():
                        self._dovetail._trace_struct(
                            method="schedule",
                            status="Cancelled",
                            task=task_name,
                            extra={"Label": label},
                        )
                        return
                    exc = done_task.exception()
                    if exc is None:
                        self._dovetail._trace_struct(
                            method="schedule",
                            status="Done",
                            task=task_name,
                            extra={"Label": label},
                        )
                    else:
                        self._dovetail._trace_struct(
                            method="schedule",
                            status="Error",
                            task=task_name,
                            extra={"Label": label, "Error": exc},
                        )
                except Exception as callback_error:
                    self._dovetail._trace(f"schedule trace-callback error task={task_name}: {callback_error}")

            task.add_done_callback(_on_done)

        async def to_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            """Run a blocking sync `func` in a threadpool and return its result.

            This is intended for use from async code and will not block the
            event loop. `func` is executed using the instance threadpool so
            all tasks share the same pool size configured in `Dovetail`.
            """
            loop = asyncio.get_running_loop()
            trace_id = self._dovetail._next_trace_id()
            func_name = self._dovetail._callable_name(func)
            self._dovetail._trace_struct(
                method="to_thread",
                status="Queued",
                task=trace_id,
                function=func_name,
            )

            def fn() -> Any:
                self._dovetail._trace_struct(
                    method="to_thread",
                    status="Start",
                    task=trace_id,
                    function=func_name,
                )
                started_at = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    elapsed = time.perf_counter() - started_at
                    self._dovetail._trace_struct(
                        method="to_thread",
                        status="Done",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                    )
                    return result
                except Exception as exc:
                    elapsed = time.perf_counter() - started_at
                    self._dovetail._trace_struct(
                        method="to_thread",
                        status="Error",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                        extra={"Error": exc},
                    )
                    raise

            return await loop.run_in_executor(self._dovetail._threadpool, fn)

        def to_thread_blocking(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            """Run a sync callable in the Dovetail threadpool and block for the result.

            This is a convenience wrapper for sync callers that want the threadpool
            behavior of `to_thread(...)` without writing asyncio orchestration code.
            """
            return self.run_blocking(self.to_thread(func, *args, **kwargs))

        def map_blocking(
            self,
            func: Callable[[Any], Any],
            items: Any,
            max_concurrency: Optional[int] = None,
            return_exceptions: bool = False,
        ) -> list[Any]:
            """Apply `func` to `items` concurrently in the threadpool and block for results.

            - `max_concurrency` bounds in-flight tasks (defaults to len(items), min 1).
            - Returns results in input order.
            - If `return_exceptions` is True, exceptions are returned in result slots.
              Otherwise the first exception is raised.
            """
            items_list = list(items)
            if not items_list:
                return []

            workers = len(items_list) if max_concurrency is None else max(1, int(max_concurrency or 1))
            func_name = self._dovetail._callable_name(func)
            self._dovetail._trace_struct(
                method="map_blocking",
                status="Start",
                function=func_name,
                extra={
                    "Items": len(items_list),
                    "Max Concurrency": workers,
                    "Return Exceptions": return_exceptions,
                },
            )

            async def _run_map() -> list[Any]:
                semaphore = asyncio.Semaphore(workers)
                results: list[Any] = [None] * len(items_list)

                async def _run_one(index: int, item: Any) -> None:
                    async with semaphore:
                        try:
                            results[index] = await self.to_thread(func, item)
                        except Exception as exc:
                            if return_exceptions:
                                results[index] = exc
                            else:
                                raise

                await asyncio.gather(
                    *(_run_one(i, item) for i, item in enumerate(items_list)),
                    return_exceptions=False,
                )
                self._dovetail._trace_struct(
                    method="map_blocking",
                    status="Done",
                    function=func_name,
                    extra={"Items": len(items_list)},
                )
                return results

            return self.run_blocking(_run_map)

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
            started_at = time.perf_counter()
            try:
                # If a loop is running on this thread, get_running_loop()
                # returns it; we treat that as a fatal usage error.
                _ = asyncio.get_running_loop()
            
            except RuntimeError:
                # No running loop - safe to execute synchronously.
                if inspect.iscoroutinefunction(func_or_coro):
                    # A coroutine function was passed - call it and run it.
                    func_name = self._dovetail._callable_name(func_or_coro)
                    self._dovetail._trace_struct(
                        method="run_blocking",
                        status="Start",
                        function=func_name,
                        extra={"Type": "coroutine-function"},
                    )
                    try:
                        result = asyncio.run(func_or_coro(*args, **kwargs))
                        self._dovetail._trace_struct(
                            method="run_blocking",
                            status="Done",
                            function=func_name,
                            elapsed=time.perf_counter() - started_at,
                            extra={"Type": "coroutine-function"},
                        )
                        return result
                    except Exception as exc:
                        self._dovetail._trace_struct(
                            method="run_blocking",
                            status="Error",
                            function=func_name,
                            elapsed=time.perf_counter() - started_at,
                            extra={"Type": "coroutine-function", "Error": exc},
                        )
                        raise
                if inspect.iscoroutine(func_or_coro):
                    # A coroutine object was passed - run it directly.
                    self._dovetail._trace_struct(
                        method="run_blocking",
                        status="Start",
                        extra={"Type": "coroutine-object"},
                    )
                    try:
                        result = asyncio.run(func_or_coro)
                        self._dovetail._trace_struct(
                            method="run_blocking",
                            status="Done",
                            elapsed=time.perf_counter() - started_at,
                            extra={"Type": "coroutine-object"},
                        )
                        return result
                    except Exception as exc:
                        self._dovetail._trace_struct(
                            method="run_blocking",
                            status="Error",
                            elapsed=time.perf_counter() - started_at,
                            extra={"Type": "coroutine-object", "Error": exc},
                        )
                        raise
                
                # Regular sync callable - call and return its result.
                call_name = self._dovetail._callable_name(func_or_coro)
                self._dovetail._trace_struct(
                    method="run_blocking",
                    status="Start",
                    function=call_name,
                    extra={"Type": "callable"},
                )
                try:
                    result = func_or_coro(*args, **kwargs)
                    self._dovetail._trace_struct(
                        method="run_blocking",
                        status="Done",
                        function=call_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "callable"},
                    )
                    return result
                except Exception as exc:
                    self._dovetail._trace_struct(
                        method="run_blocking",
                        status="Error",
                        function=call_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "callable", "Error": exc},
                    )
                    raise

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
                task = loop.create_task(coro_or_callable)
                self._attach_task_trace(task, "coroutine-object")
                return task

            # If an async function was passed (callable but coroutine-producing),
            # call it to get the coroutine and schedule that.
            if inspect.iscoroutinefunction(coro_or_callable):
                label = f"coroutine-function:{self._dovetail._callable_name(coro_or_callable)}"
                task = loop.create_task(coro_or_callable(*args, **kwargs))
                self._attach_task_trace(task, label)
                return task

            # If it's a regular callable, run it in the threadpool.
            if callable(coro_or_callable):
                fn = functools.partial(coro_or_callable, *args, **kwargs)
                trace_id = self._dovetail._next_trace_id()
                call_name = self._dovetail._callable_name(coro_or_callable)

                async def _run_in_executor() -> Any:
                    self._dovetail._trace_struct(
                        method="schedule",
                        status="Queued",
                        task=trace_id,
                        function=call_name,
                    )

                    def _wrapped() -> Any:
                        self._dovetail._trace_struct(
                            method="schedule",
                            status="Start",
                            task=trace_id,
                            function=call_name,
                        )
                        started_at = time.perf_counter()
                        try:
                            result = fn()
                            elapsed = time.perf_counter() - started_at
                            self._dovetail._trace_struct(
                                method="schedule",
                                status="Done",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                            )
                            return result
                        except Exception as exc:
                            elapsed = time.perf_counter() - started_at
                            self._dovetail._trace_struct(
                                method="schedule",
                                status="Error",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                                extra={"Error": exc},
                            )
                            raise

                    return await loop.run_in_executor(self._dovetail._threadpool, _wrapped)

                task = loop.create_task(_run_in_executor())
                self._attach_task_trace(task, f"callable:{call_name}")
                return task

            raise TypeError("coro_or_callable must be a coroutine, async function, or callable")

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown internal threadpool."""
        if self._trace_enabled:
            current = threading.current_thread()
            self._trace_logger.debug(
                "[%s] Thread: %s#%s: Shutdown (wait=%s)",
                self._trace_prefix,
                current.name,
                threading.get_ident(),
                wait,
            )
        self._threadpool.shutdown(wait=wait)