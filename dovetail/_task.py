from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .dovetail import Dovetail


class Task:
    """Helpers for scheduling and bridging sync/async call sites."""

    def __init__(self, dovetail: "Dovetail") -> None:
        self._dovetail = dovetail

    def _attach_task_trace(self, task: asyncio.Task, label: str) -> None:
        if not self._dovetail.events.trace_enabled:
            return
        task_name = task.get_name() if hasattr(task, "get_name") else f"task-{id(task)}"
        self._dovetail.events.trace_struct(
            method="schedule",
            status="Created",
            task=task_name,
            extra={"Label": label},
        )

        def _on_done(done_task: asyncio.Task) -> None:
            try:
                if done_task.cancelled():
                    self._dovetail.events.trace_struct(
                        method="schedule",
                        status="Cancelled",
                        task=task_name,
                        extra={"Label": label},
                    )
                    return
                exc = done_task.exception()
                if exc is None:
                    self._dovetail.events.trace_struct(
                        method="schedule",
                        status="Done",
                        task=task_name,
                        extra={"Label": label},
                    )
                else:
                    self._dovetail.events.trace_struct(
                        method="schedule",
                        status="Error",
                        task=task_name,
                        extra={"Label": label, "Error": exc},
                    )
            except Exception as callback_error:
                self._dovetail.events.trace(f"schedule trace-callback error task={task_name}: {callback_error}")

        task.add_done_callback(_on_done)

    def _attach_task_events(
        self,
        task: asyncio.Task,
        *,
        method: str,
        function: Optional[str],
        execution_id: str,
    ) -> None:
        payload = {
            "method": method,
            "task": id(task),
            "function": function,
            "execution_id": execution_id,
        }
        self._dovetail._emit_event("task_queued", payload)
        self._dovetail._emit_event("task_started", payload)

        def _on_done(done_task: asyncio.Task) -> None:
            done_payload = {
                "method": method,
                "task": id(done_task),
                "function": function,
                "execution_id": execution_id,
            }
            if done_task.cancelled():
                self._dovetail._emit_event("task_cancelled", done_payload)
                return
            exc = done_task.exception()
            if exc is None:
                self._dovetail._emit_event("task_done", done_payload)
            else:
                done_payload["error"] = exc
                self._dovetail._emit_event("task_error", done_payload)

        task.add_done_callback(_on_done)

    def _resolve_retry_config(
        self,
        retries: Optional[int] = None,
        backoff: Optional[float] = None,
    ) -> tuple[int, float]:
        """Resolve retry count and backoff, falling back to Dovetail defaults."""
        resolved_retries = self._dovetail._default_retries if retries is None else int(retries)
        resolved_backoff = self._dovetail._default_retry_backoff if backoff is None else float(backoff)
        return max(0, resolved_retries), max(0.0, resolved_backoff)

    async def to_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking callable in the threadpool and return its result."""
        loop = asyncio.get_running_loop()
        trace_id = self._dovetail._next_trace_id()
        execution_id = self._dovetail._next_execution_id()
        func_name = self._dovetail._callable_name(func)
        self._dovetail.events.trace_struct(
            method="to_thread",
            status="Queued",
            task=trace_id,
            function=func_name,
        )
        self._dovetail.events.inc_stat("queued")
        self._dovetail._emit_event(
            "task_queued",
            {"method": "to_thread", "task": trace_id, "function": func_name, "execution_id": execution_id},
        )
        await self._dovetail._acquire_rate_limit(method="to_thread", task=trace_id, function=func_name)

        retries, retry_backoff = self._resolve_retry_config()

        def fn() -> Any:
            attempt = 0
            while True:
                attempt += 1
                self._dovetail.events.trace_struct(
                    method="to_thread",
                    status="Start",
                    task=trace_id,
                    function=func_name,
                    extra={"Attempt": attempt},
                )
                self._dovetail.events.inc_stat("started")
                self._dovetail._emit_event(
                    "task_started",
                    {
                        "method": "to_thread",
                        "task": trace_id,
                        "function": func_name,
                        "execution_id": execution_id,
                        "attempt": attempt,
                    },
                )
                started_at = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    elapsed = time.perf_counter() - started_at
                    self._dovetail.events.trace_struct(
                        method="to_thread",
                        status="Done",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                        extra={"Attempt": attempt},
                    )
                    self._dovetail.events.inc_stat("done")
                    self._dovetail._emit_event(
                        "task_done",
                        {
                            "method": "to_thread",
                            "task": trace_id,
                            "function": func_name,
                            "execution_id": execution_id,
                            "attempt": attempt,
                            "elapsed": elapsed,
                        },
                    )
                    return result
                except Exception as exc:
                    elapsed = time.perf_counter() - started_at
                    self._dovetail.events.trace_struct(
                        method="to_thread",
                        status="Error",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                        extra={"Attempt": attempt, "Error": exc},
                    )
                    if attempt > retries:
                        self._dovetail.events.inc_stat("error")
                        self._dovetail._emit_event(
                            "task_error",
                            {
                                "method": "to_thread",
                                "task": trace_id,
                                "function": func_name,
                                "execution_id": execution_id,
                                "attempt": attempt,
                                "elapsed": elapsed,
                                "error": exc,
                            },
                        )
                        raise

                    sleep_for = retry_backoff * (2 ** (attempt - 1)) if retry_backoff > 0 else 0.0
                    self._dovetail.events.inc_stat("retries")
                    self._dovetail.events.trace_struct(
                        method="to_thread",
                        status="Retry",
                        task=trace_id,
                        function=func_name,
                        extra={"Attempt": attempt, "Sleep": f"{sleep_for:.3f}s", "Error": exc},
                    )
                    self._dovetail._emit_event(
                        "task_retry",
                        {
                            "method": "to_thread",
                            "task": trace_id,
                            "function": func_name,
                            "execution_id": execution_id,
                            "attempt": attempt,
                            "sleep": sleep_for,
                            "error": exc,
                        },
                    )
                    if sleep_for > 0:
                        time.sleep(sleep_for)

        future = loop.run_in_executor(self._dovetail._threadpool, fn)
        timeout = self._dovetail._default_timeout
        if timeout is None or timeout <= 0:
            return await future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._dovetail.events.inc_stat("error")
            self._dovetail.events.trace_struct(
                method="to_thread",
                status="Timeout",
                task=trace_id,
                function=func_name,
                elapsed=timeout,
            )
            self._dovetail._emit_event(
                "task_error",
                {
                    "method": "to_thread",
                    "task": trace_id,
                    "function": func_name,
                    "execution_id": execution_id,
                    "error": exc,
                    "timeout": timeout,
                },
            )
            raise

    def to_thread_blocking(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Sync wrapper around to_thread(...) for callers without an event loop."""
        return self.run_blocking(self.to_thread(func, *args, **kwargs))

    def map_blocking(
        self,
        func: Callable[[Any], Any],
        items: Any,
        max_concurrency: Optional[int] = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Apply func to items concurrently and block for ordered results."""
        items_list = list(items)
        if not items_list:
            return []

        workers = len(items_list) if max_concurrency is None else max(1, int(max_concurrency or 1))
        func_name = self._dovetail._callable_name(func)
        self._dovetail.events.trace_struct(
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
            self._dovetail.events.trace_struct(
                method="map_blocking",
                status="Done",
                function=func_name,
                extra={"Items": len(items_list)},
            )
            return results

        return self.run_blocking(_run_map)

    def run_blocking(self, func_or_coro: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a callable/coroutine synchronously from sync code."""
        started_at = time.perf_counter()
        try:
            _ = asyncio.get_running_loop()

        except RuntimeError:
            if inspect.iscoroutinefunction(func_or_coro):
                func_name = self._dovetail._callable_name(func_or_coro)
                self._dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Start",
                    function=func_name,
                    extra={"Type": "coroutine-function"},
                )
                try:
                    coro = func_or_coro(*args, **kwargs)
                    timeout = self._dovetail._default_timeout
                    if timeout is not None and timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=timeout)
                    result = asyncio.run(coro)
                    self._dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Done",
                        function=func_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-function"},
                    )
                    return result
                except Exception as exc:
                    self._dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Error",
                        function=func_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-function", "Error": exc},
                    )
                    raise
            if inspect.iscoroutine(func_or_coro):
                self._dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Start",
                    extra={"Type": "coroutine-object"},
                )
                try:
                    coro = func_or_coro
                    timeout = self._dovetail._default_timeout
                    if timeout is not None and timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=timeout)
                    result = asyncio.run(coro)
                    self._dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Done",
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-object"},
                    )
                    return result
                except Exception as exc:
                    self._dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Error",
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-object", "Error": exc},
                    )
                    raise

            call_name = self._dovetail._callable_name(func_or_coro)
            self._dovetail.events.trace_struct(
                method="run_blocking",
                status="Start",
                function=call_name,
                extra={"Type": "callable"},
            )
            try:
                result = func_or_coro(*args, **kwargs)
                self._dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Done",
                    function=call_name,
                    elapsed=time.perf_counter() - started_at,
                    extra={"Type": "callable"},
                )
                return result
            except Exception as exc:
                self._dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Error",
                    function=call_name,
                    elapsed=time.perf_counter() - started_at,
                    extra={"Type": "callable", "Error": exc},
                )
                raise

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
    ) -> asyncio.Task:
        """Schedule coroutine/callable from async code and return an asyncio.Task."""
        loop = asyncio.get_running_loop()

        if inspect.iscoroutine(coro_or_callable):
            timeout = self._dovetail._default_timeout
            coro = coro_or_callable
            execution_id = self._dovetail._next_execution_id()
            function_name = getattr(getattr(coro_or_callable, "cr_code", None), "co_name", None)
            if timeout is not None and timeout > 0:
                coro = asyncio.wait_for(coro_or_callable, timeout=timeout)
            task = loop.create_task(coro)
            self._attach_task_trace(task, "coroutine-object")
            self._attach_task_events(
                task,
                method="schedule",
                function=function_name,
                execution_id=execution_id,
            )
            return task

        if inspect.iscoroutinefunction(coro_or_callable):
            label = f"coroutine-function:{self._dovetail._callable_name(coro_or_callable)}"
            timeout = self._dovetail._default_timeout
            execution_id = self._dovetail._next_execution_id()
            function_name = self._dovetail._callable_name(coro_or_callable)
            coro = coro_or_callable(*args, **kwargs)
            if timeout is not None and timeout > 0:
                coro = asyncio.wait_for(coro, timeout=timeout)
            task = loop.create_task(coro)
            self._attach_task_trace(task, label)
            self._attach_task_events(
                task,
                method="schedule",
                function=function_name,
                execution_id=execution_id,
            )
            return task

        if callable(coro_or_callable):
            fn = functools.partial(coro_or_callable, *args, **kwargs)
            trace_id = self._dovetail._next_trace_id()
            execution_id = self._dovetail._next_execution_id()
            call_name = self._dovetail._callable_name(coro_or_callable)
            retries, retry_backoff = self._resolve_retry_config()

            async def _run_in_executor() -> Any:
                self._dovetail.events.trace_struct(
                    method="schedule",
                    status="Queued",
                    task=trace_id,
                    function=call_name,
                )
                self._dovetail.events.inc_stat("queued")
                self._dovetail._emit_event(
                    "task_queued",
                    {
                        "method": "schedule",
                        "task": trace_id,
                        "function": call_name,
                        "execution_id": execution_id,
                    },
                )
                await self._dovetail._acquire_rate_limit(
                    method="schedule", task=trace_id, function=call_name
                )

                def _wrapped() -> Any:
                    attempt = 0
                    while True:
                        attempt += 1
                        self._dovetail.events.trace_struct(
                            method="schedule",
                            status="Start",
                            task=trace_id,
                            function=call_name,
                            extra={"Attempt": attempt},
                        )
                        self._dovetail.events.inc_stat("started")
                        self._dovetail._emit_event(
                            "task_started",
                            {
                                "method": "schedule",
                                "task": trace_id,
                                "function": call_name,
                                "execution_id": execution_id,
                                "attempt": attempt,
                            },
                        )
                        started_at = time.perf_counter()
                        try:
                            result = fn()
                            elapsed = time.perf_counter() - started_at
                            self._dovetail.events.trace_struct(
                                method="schedule",
                                status="Done",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                                extra={"Attempt": attempt},
                            )
                            self._dovetail.events.inc_stat("done")
                            self._dovetail._emit_event(
                                "task_done",
                                {
                                    "method": "schedule",
                                    "task": trace_id,
                                    "function": call_name,
                                    "execution_id": execution_id,
                                    "attempt": attempt,
                                    "elapsed": elapsed,
                                },
                            )
                            return result
                        except Exception as exc:
                            elapsed = time.perf_counter() - started_at
                            self._dovetail.events.trace_struct(
                                method="schedule",
                                status="Error",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                                extra={"Attempt": attempt, "Error": exc},
                            )
                            if attempt > retries:
                                self._dovetail.events.inc_stat("error")
                                self._dovetail._emit_event(
                                    "task_error",
                                    {
                                        "method": "schedule",
                                        "task": trace_id,
                                        "function": call_name,
                                        "execution_id": execution_id,
                                        "attempt": attempt,
                                        "elapsed": elapsed,
                                        "error": exc,
                                    },
                                )
                                raise
                            sleep_for = retry_backoff * (2 ** (attempt - 1)) if retry_backoff > 0 else 0.0
                            self._dovetail.events.inc_stat("retries")
                            self._dovetail.events.trace_struct(
                                method="schedule",
                                status="Retry",
                                task=trace_id,
                                function=call_name,
                                extra={"Attempt": attempt, "Sleep": f"{sleep_for:.3f}s", "Error": exc},
                            )
                            self._dovetail._emit_event(
                                "task_retry",
                                {
                                    "method": "schedule",
                                    "task": trace_id,
                                    "function": call_name,
                                    "execution_id": execution_id,
                                    "attempt": attempt,
                                    "sleep": sleep_for,
                                    "error": exc,
                                },
                            )
                            if sleep_for > 0:
                                time.sleep(sleep_for)

                future = loop.run_in_executor(self._dovetail._threadpool, _wrapped)
                timeout = self._dovetail._default_timeout
                if timeout is None or timeout <= 0:
                    return await future
                return await asyncio.wait_for(future, timeout=timeout)

            task = loop.create_task(_run_in_executor())
            self._attach_task_trace(task, f"callable:{call_name}")
            return task

        raise TypeError("coro_or_callable must be a coroutine, async function, or callable")
