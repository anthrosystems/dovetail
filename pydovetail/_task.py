"""
Helpers for scheduling and bridging sync/async call sites.
"""

from __future__ import annotations

import logging, time, asyncio
import functools, weakref, inspect

from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .dovetail import Dovetail

from ._events import TaskEvent

_logger = logging.getLogger(__name__)

__all__ = [
    "Task",
]


class Task:
    def __init__(
        self,
        dovetail: "Dovetail"
    ) -> None:
        
        # Back-reference to the owning Dovetail instance. Used to emit
        # package-level events and consult instance-level configuration.
        self._dovetail = weakref.ref(dovetail)
    
    @property
    def dovetail(self) -> "Dovetail":
        dvt = self._dovetail()
        if dvt is None:
            raise RuntimeError("Dovetail instance has been garbage collected")
        return dvt

    def _attach_task_trace(self, task: asyncio.Task, label: str) -> None:
        # Attach lightweight structured traces to the lifecycle of the
        # asyncio.Task. These traces are only generated when tracing is
        # enabled for the parent Dovetail instance to avoid unnecessary
        # overhead in normal runs.
        if not self.dovetail.events.trace_enabled:
            return
        
        task_name = task.get_name() if hasattr(task, "get_name") else f"task-{id(task)}"
        self.dovetail.events.trace_struct(
            method="schedule",
            status="Created",
            task=task_name,
            extra={"Label": label},
        )

        def _on_done(done_task: asyncio.Task) -> None:
            try:
                if done_task.cancelled():
                    self.dovetail.events.trace_struct(
                        method="schedule",
                        status="Cancelled",
                        task=task_name,
                        extra={"Label": label},
                    )
                    return
                exc = done_task.exception()
                if exc is None:
                    self.dovetail.events.trace_struct(
                        method="schedule",
                        status="Done",
                        task=task_name,
                        extra={"Label": label},
                    )
                else:
                    self.dovetail.events.trace_struct(
                        method="schedule",
                        status="Error",
                        task=task_name,
                        extra={"Label": label, "Error": exc},
                    )
            except Exception as callback_error:
                # Trace any error raised by the done-callback itself; do not
                # let it propagate and interfere with the task machinery.
                self.dovetail.events.trace(f"schedule trace-callback error task={task_name}: {callback_error}")

        task.add_done_callback(_on_done)

    def _attach_task_events(
        self,
        task: asyncio.Task,
        *,
        method: str,
        function: Optional[str],
        execution_id: str,
    ) -> None:
        # Emit simple lifecycle events used by consumers interested in
        # queue/start/error/done notifications. The payload intentionally
        # contains primitive values where possible so listeners do not need
        # complex object references.
        payload = {
            "method": method,
            "task": id(task),
            "function": function,
            "execution_id": execution_id,
        }
        
        # Update cumulative counters for coroutine-based tasks so stats()
        # reflect scheduled/started work (other paths already increment).
        self.dovetail.events.inc_stat(TaskEvent.QUEUED)
        
        self.dovetail._emit_event(TaskEvent.QUEUED, payload)
        self.dovetail.events.inc_stat(TaskEvent.STARTED)
        self.dovetail._emit_event(TaskEvent.STARTED, payload)

        def _on_done(done_task: asyncio.Task) -> None:
            done_payload = {
                "method": method,
                "task": id(done_task),
                "function": function,
                "execution_id": execution_id,
            }
            if done_task.cancelled():
                self.dovetail.events.inc_stat(TaskEvent.CANCELLED)
                self.dovetail._emit_event(TaskEvent.CANCELLED, done_payload)
                return
            exc = done_task.exception()
            if exc is None:
                self.dovetail.events.inc_stat(TaskEvent.DONE)
                self.dovetail._emit_event(TaskEvent.DONE, done_payload)
            else:
                self.dovetail.events.inc_stat(TaskEvent.ERROR)
                done_payload["error"] = exc
                self.dovetail._emit_event(TaskEvent.ERROR, done_payload)

        task.add_done_callback(_on_done)

    def _resolve_retry_config(
        self,
        retries: Optional[int] = None,
        backoff: Optional[float] = None,
    ) -> tuple[int, float]:
        """Resolve retry count and backoff, falling back to Dovetail defaults."""
        resolved_retries = self.dovetail._default_retries if retries is None else int(retries)
        resolved_backoff = self.dovetail._default_retry_backoff if backoff is None else float(backoff)
        return max(0, resolved_retries), max(0.0, resolved_backoff)

    async def to_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking callable in the threadpool and return its result."""
        loop = asyncio.get_running_loop()
        trace_id = self.dovetail._next_trace_id()
        execution_id = self.dovetail._next_execution_id()
        func_name = self.dovetail._callable_name(func)
        self.dovetail.events.trace_struct(
            method="to_thread",
            status="Queued",
            task=trace_id,
            function=func_name,
        )
        self.dovetail.events.inc_stat(TaskEvent.QUEUED)
        self.dovetail._emit_event(
            TaskEvent.QUEUED,
            {"method": "to_thread", "task": trace_id, "function": func_name, "execution_id": execution_id},
        )
        await self.dovetail._acquire_rate_limit(method="to_thread", task=trace_id, function=func_name)

        # Resolve retry/backoff configuration (instance defaults are used if
        # not provided). The worker `fn` below implements the retry loop and
        # emits structured traces/events for each attempt.
        retries, retry_backoff = self._resolve_retry_config()

        def fn() -> Any:
            attempt = 0
            while True:
                attempt += 1
                self.dovetail.events.trace_struct(
                    method="to_thread",
                    status="Start",
                    task=trace_id,
                    function=func_name,
                    extra={"Attempt": attempt},
                )
                self.dovetail.events.inc_stat(TaskEvent.STARTED)
                loop.call_soon_threadsafe(self.dovetail._emit_event,
                    TaskEvent.STARTED,
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
                    self.dovetail.events.trace_struct(
                        method="to_thread",
                        status="Done",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                        extra={"Attempt": attempt},
                    )
                    self.dovetail.events.inc_stat(TaskEvent.DONE)
                    loop.call_soon_threadsafe(self.dovetail._emit_event,
                        TaskEvent.DONE,
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
                    self.dovetail.events.trace_struct(
                        method="to_thread",
                        status="Error",
                        task=trace_id,
                        function=func_name,
                        elapsed=elapsed,
                        extra={"Attempt": attempt, "Error": exc},
                    )
                    # If we've exhausted retries, emit an error event and
                    # re-raise so the caller sees the exception.
                    if attempt > retries:
                        self.dovetail.events.inc_stat(TaskEvent.ERROR)
                        loop.call_soon_threadsafe(self.dovetail._emit_event,
                            TaskEvent.ERROR,
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

                    # Exponential backoff before next retry (if configured).
                    sleep_for = retry_backoff * (2 ** (attempt - 1)) if retry_backoff > 0 else 0.0
                    self.dovetail.events.inc_stat(TaskEvent.RETRY)
                    self.dovetail.events.trace_struct(
                        method="to_thread",
                        status="Retry",
                        task=trace_id,
                        function=func_name,
                        extra={"Attempt": attempt, "Sleep": f"{sleep_for:.3f}s", "Error": exc},
                    )
                    loop.call_soon_threadsafe(self.dovetail._emit_event,
                        TaskEvent.RETRY,
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

        future = loop.run_in_executor(self.dovetail._threadpool, fn)
        timeout = self.dovetail._timeout
        if timeout is None or timeout <= 0:
            return await future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.dovetail.events.inc_stat(TaskEvent.ERROR)
            self.dovetail.events.trace_struct(
                method="to_thread",
                status="Timeout",
                task=trace_id,
                function=func_name,
                elapsed=timeout,
            )
            self.dovetail._emit_event(
                TaskEvent.ERROR,
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

    async def map(
        self,
        func: Callable[[Any], Any],
        items: Any,
        *,
        max_concurrency: Optional[int] = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Apply a blocking callable concurrently, preserving input order.

        At most ``max_concurrency`` calls are active simultaneously. On the
        first error (unless ``return_exceptions`` is set), remaining asyncio
        jobs are cancelled; already-running threads cannot be interrupted.
        """
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        values = list(items)
        if not values:
            return []
        limit = max_concurrency if max_concurrency is not None else self.dovetail._threadpool._max_workers
        semaphore = asyncio.Semaphore(limit)
        results: list[Any] = [None] * len(values)

        async def run_one(index: int, value: Any) -> None:
            async with semaphore:
                try:
                    results[index] = await self.to_thread(func, value)
                except Exception as exc:
                    if not return_exceptions:
                        raise
                    results[index] = exc

        jobs = [asyncio.create_task(run_one(i, value)) for i, value in enumerate(values)]
        try:
            await asyncio.gather(*jobs)
        except BaseException:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            raise
        return results

    def map_blocking(
        self,
        func: Callable[[Any], Any],
        items: Any,
        max_concurrency: Optional[int] = None,
        return_exceptions: bool = False,
    ) -> list[Any]:
        """Synchronous counterpart of :meth:`map`."""
        return self.run_blocking(
            self.map, func, items,
            max_concurrency=max_concurrency,
            return_exceptions=return_exceptions,
        )

    def run_blocking(self, func_or_coro: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a callable/coroutine synchronously from sync code."""
        started_at = time.perf_counter()
        try:
            _ = asyncio.get_running_loop()

        except RuntimeError:
            if inspect.iscoroutinefunction(func_or_coro):
                func_name = self.dovetail._callable_name(func_or_coro)
                self.dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Start",
                    function=func_name,
                    extra={"Type": "coroutine-function"},
                )
                try:
                    coro = func_or_coro(*args, **kwargs)
                    timeout = self.dovetail._timeout
                    if timeout is not None and timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=timeout)
                    result = asyncio.run(coro)
                    self.dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Done",
                        function=func_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-function"},
                    )
                    return result
                except Exception as exc:
                    self.dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Error",
                        function=func_name,
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-function", "Error": exc},
                    )
                    raise
            if inspect.iscoroutine(func_or_coro):
                self.dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Start",
                    extra={"Type": "coroutine-object"},
                )
                try:
                    coro = func_or_coro
                    timeout = self.dovetail._timeout
                    if timeout is not None and timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=timeout)
                    result = asyncio.run(coro)
                    self.dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Done",
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-object"},
                    )
                    return result
                except Exception as exc:
                    self.dovetail.events.trace_struct(
                        method="run_blocking",
                        status="Error",
                        elapsed=time.perf_counter() - started_at,
                        extra={"Type": "coroutine-object", "Error": exc},
                    )
                    raise

            call_name = self.dovetail._callable_name(func_or_coro)
            self.dovetail.events.trace_struct(
                method="run_blocking",
                status="Start",
                function=call_name,
                extra={"Type": "callable"},
            )
            try:
                result = func_or_coro(*args, **kwargs)
                self.dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Done",
                    function=call_name,
                    elapsed=time.perf_counter() - started_at,
                    extra={"Type": "callable"},
                )
                return result
            except Exception as exc:
                self.dovetail.events.trace_struct(
                    method="run_blocking",
                    status="Error",
                    function=call_name,
                    elapsed=time.perf_counter() - started_at,
                    extra={"Type": "callable", "Error": exc},
                )
                raise

        if inspect.iscoroutine(func_or_coro):
            func_or_coro.close()
        raise RuntimeError(
            "dvt.task.run_blocking() cannot be called from a running "
            "event loop. Use await dvt.task.schedule(...) or await "
            "dvt.task.to_thread(...) instead."
        )

    def schedule(
        self,
        coro_or_callable: Any,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task:
        """Schedule coroutine/callable from async code and return an asyncio.Task."""
        loop = asyncio.get_running_loop()

        if inspect.iscoroutine(coro_or_callable):
            timeout = self.dovetail._timeout
            coro = coro_or_callable
            execution_id = self.dovetail._next_execution_id()
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
            label = f"coroutine-function:{self.dovetail._callable_name(coro_or_callable)}"
            timeout = self.dovetail._timeout
            execution_id = self.dovetail._next_execution_id()
            function_name = self.dovetail._callable_name(coro_or_callable)
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
            trace_id = self.dovetail._next_trace_id()
            execution_id = self.dovetail._next_execution_id()
            call_name = self.dovetail._callable_name(coro_or_callable)
            retries, retry_backoff = self._resolve_retry_config()

            async def _run_in_executor() -> Any:
                self.dovetail.events.trace_struct(
                    method="schedule",
                    status="Queued",
                    task=trace_id,
                    function=call_name,
                )
                self.dovetail.events.inc_stat(TaskEvent.QUEUED)
                self.dovetail._emit_event(
                    TaskEvent.QUEUED,
                    {
                        "method": "schedule",
                        "task": trace_id,
                        "function": call_name,
                        "execution_id": execution_id,
                    },
                )
                await self.dovetail._acquire_rate_limit(
                    method="schedule", task=trace_id, function=call_name
                )

                def _wrapped() -> Any:
                    attempt = 0
                    while True:
                        attempt += 1
                        self.dovetail.events.trace_struct(
                            method="schedule",
                            status="Start",
                            task=trace_id,
                            function=call_name,
                            extra={"Attempt": attempt},
                        )
                        loop.call_soon_threadsafe(
                            self.dovetail.events.inc_stat,
                            "started"
                        )
                        loop.call_soon_threadsafe(
                            self.dovetail._emit_event,
                            TaskEvent.STARTED,
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
                            self.dovetail.events.trace_struct(
                                method="schedule",
                                status="Done",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                                extra={"Attempt": attempt},
                            )
                            loop.call_soon_threadsafe(
                                self.dovetail.events.inc_stat,
                                "done"
                            )
                            loop.call_soon_threadsafe(
                                self.dovetail._emit_event,
                                TaskEvent.DONE,
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
                            self.dovetail.events.trace_struct(
                                method="schedule",
                                status="Error",
                                task=trace_id,
                                function=call_name,
                                elapsed=elapsed,
                                extra={"Attempt": attempt, "Error": exc},
                            )
                            if attempt > retries:
                                loop.call_soon_threadsafe(
                                    self.dovetail.events.inc_stat,
                                    "error"
                                )
                                loop.call_soon_threadsafe(
                                    self.dovetail._emit_event,
                                    TaskEvent.ERROR,
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
                            loop.call_soon_threadsafe(
                                self.dovetail.events.inc_stat,
                                "retries"
                            )
                            self.dovetail.events.trace_struct(
                                method="schedule",
                                status="Retry",
                                task=trace_id,
                                function=call_name,
                                extra={"Attempt": attempt, "Sleep": f"{sleep_for:.3f}s", "Error": exc},
                            )
                            loop.call_soon_threadsafe(
                                self.dovetail._emit_event,
                                TaskEvent.RETRY,
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

                future = loop.run_in_executor(self.dovetail._threadpool, _wrapped)
                timeout = self.dovetail._timeout
                if timeout is None or timeout <= 0:
                    return await future
                return await asyncio.wait_for(future, timeout=timeout)

            task = loop.create_task(_run_in_executor())
            self._attach_task_trace(task, f"callable:{call_name}")
            return task

        raise TypeError("coro_or_callable must be a coroutine, async function, or callable")
        
    def managed(
        self,
        func: Optional[Callable[..., Any]] = None,
    ) -> Callable[..., Any]:
        """Decorate a callable so execution is managed by Dovetail.

        Sync callables execute through Dovetail's threadpool while
        preserving synchronous calling semantics.

        Async callables are submitted through Dovetail's scheduler while
        preserving asynchronous execution semantics.

        Example:

            @dvt.task.managed()
            def blocking_work():
                ...

            result = blocking_work()

            @dvt.task.managed()
            async def async_work():
                ...

            task = async_work()
            result = await task

        Returns:
            Wrapped callable.
        """

        def decorator(
            target: Callable[..., Any],
        ) -> Callable[..., Any]:

            if not callable(target):
                raise TypeError("managed target must be callable")

            if inspect.iscoroutinefunction(target):

                @functools.wraps(target)
                def async_wrapper(
                    *args: Any,
                    **kwargs: Any,
                ) -> asyncio.Task[Any]:

                    return self.schedule(
                        target,
                        *args,
                        **kwargs,
                    )

                return async_wrapper

            @functools.wraps(target)
            def sync_wrapper(
                *args: Any,
                **kwargs: Any,
            ) -> Any:

                return self.run_blocking(
                    self.to_thread,
                    target,
                    *args,
                    **kwargs,
                )

            return sync_wrapper

        if func is not None:
            return decorator(func)

        return decorator