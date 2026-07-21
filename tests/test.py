"""
test.py — Dovetail API sanity checks
====================================

Fast executable checks for the public Dovetail API.

This is intentionally not a pytest suite. It is a smoke test that validates
that the documented API surface behaves correctly before running benchmarks.

Every check is isolated: a failure in one check is recorded and the suite
continues, so a single regression doesn't hide the state of everything else.
A full pass/fail summary is printed at the end, including every failure's
error message.

Coverage:

  Task execution
    - run_blocking: callable, coroutine function, coroutine object
    - run_blocking: raises RuntimeError inside a running event loop
    - to_thread / to_thread_blocking
    - to_thread: per-call timeout (timeout)
    - map_blocking: basic, empty input, max_concurrency bound,
      return_exceptions=True (order-preserved, non-raising),
      return_exceptions=False (raises on first failure)
    - schedule: coroutine object, coroutine function, callable
    - schedule: per-call timeout (timeout)
    - schedule: raises TypeError for a non-schedulable argument

  Lifecycle
    - shutdown(): idempotency, wait=False
    - shutdown_on_exit=False
    - sync and async context managers shut down on exit

  Registry
    - auto-registration + list_active()
    - unregister()
    - auto_register=False
    - duplicate registration is safe
    - shutdown() removes instance from registry
    - shutdown_all() shuts down all instances
    - shutdown_all() triggers app shutdown hook
    - garbage collection removes dead instances
    - registry does not retain dead Dovetail objects

  Events
    - on_queued / on_start / on_end / on_error / on_retry / on_cancel /
      on_rate_limited, each exercised directly
    - event ordering across a single call (queued -> started -> done)
    - on_start fires once per retry attempt with correct attempt numbers
    - retry exhaustion: on_retry count + final on_error count
    - global (unscoped) listeners
    - function_target scoping
    - execution_target scoping
    - function_target + execution_target together raises ValueError
    - non-callable callback raises TypeError
    - max_chain_depth < 1 raises ValueError
    - reentry disallowed (default) blocks a callback calling itself
    - reentry allowed, bounded by max_chain_depth
    - chained scheduling from within an on_end callback (docs pattern)
    - documented pitfall: passing a Task (not a function) as a callback
      raises TypeError

  Retries, timeouts, rate limiting
    - stats() counters after a batch of mixed-success work
    - rate limiting: task_rate_limited event + throttled stat together

  Tracing & custom instrumentation
    - trace() / trace_struct() write through a caller-supplied logger
    - inc_stat() accumulates a custom counter alongside the built-ins
"""


import asyncio
import logging
import sys
import threading
import time
import traceback

from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)) # Ensure local package is tested, not PyPI release

from pydovetail import (
    Dovetail,
    register,
    unregister,
    list_active,
    shutdown_all,
    set_app_shutdown_hook,
)
from pydovetail._register import global_registry


# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------

class TestResults:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[tuple[str, str]] = []

    def record_pass(self, name: str) -> None:
        self.passed += 1
        print(f"  [PASS] {name}")

    def record_fail(self, name: str, exc: BaseException) -> None:
        self.failed += 1
        err = f"{type(exc).__name__}: {exc}"
        self.failures.append((name, err))
        print(f"  [FAIL] {name} -> {err}")

    def print_summary(self) -> bool:
        total = self.passed + self.failed
        print("\n" + "=" * 72)
        print(f"RESULTS: {self.passed}/{total} passed, {self.failed} failed")
        if self.failures:
            print("\nFailed tests:")
            for name, err in self.failures:
                print(f"  - {name}\n      {err}")
        print("=" * 72)
        return self.failed == 0


RESULTS = TestResults()


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def check_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def run_test(name: str, fn: Callable[[], Any]) -> None:
    """Run a single sync test, recording pass/fail without stopping the suite."""
    print(f"\n--- {name} ---")
    try:
        fn()
        RESULTS.record_pass(name)
    except Exception as exc:
        RESULTS.record_fail(name, exc)
        traceback.print_exc()


async def run_async_test(name: str, coro_fn: Callable[[], Any]) -> None:
    """Run a single async test, recording pass/fail without stopping the suite."""
    print(f"\n--- {name} ---")
    try:
        await coro_fn()
        RESULTS.record_pass(name)
    except Exception as exc:
        RESULTS.record_fail(name, exc)
        traceback.print_exc()


# ===========================================================================
# SYNC TESTS — no running event loop. Anything that internally calls
# run_blocking() (map_blocking, to_thread_blocking) must live here, since
# run_blocking() always raises if called from inside a running loop.
# ===========================================================================

def test_run_blocking_callable() -> None:
    with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        check_equal(
            dvt.task.run_blocking(lambda: 41 + 1),
            42,
            "run_blocking(callable)",
        )


def test_run_blocking_coroutine_function() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        async def work(value: int) -> int:
            await asyncio.sleep(0.001)
            return value + 1

        check_equal(
            dvt.task.run_blocking(work, 41),
            42,
            "run_blocking(coroutine function, *args)",
        )


def test_run_blocking_coroutine_object() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        async def work() -> int:
            await asyncio.sleep(0.001)
            return 42

        check_equal(
            dvt.task.run_blocking(work()),
            42,
            "run_blocking(coroutine object)",
        )


def test_to_thread_blocking() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        check_equal(
            dvt.task.to_thread_blocking(lambda value: value + 1, 41),
            42,
            "to_thread_blocking(callable)",
        )


def test_map_blocking_basic() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        check_equal(
            dvt.task.map_blocking(
                lambda value: value * 2,
                [1, 2, 3],
                max_concurrency=2,
            ),
            [2, 4, 6],
            "map_blocking basic ordering + result",
        )


def test_map_blocking_empty_items() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        check_equal(
            dvt.task.map_blocking(lambda value: value, []),
            [],
            "map_blocking with empty items returns []",
        )


def test_map_blocking_max_concurrency_enforced() -> None:
    with Dovetail(max_workers=8, shutdown_on_exit=False) as dvt:
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def worker(_value: int) -> int:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return _value

        dvt.task.map_blocking(worker, list(range(6)), max_concurrency=2)

        check(
            state["peak"] <= 2,
            f"max_concurrency=2 should bound concurrent workers, saw peak={state['peak']}",
        )


def test_map_blocking_return_exceptions_true() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        def maybe_fail(value: int) -> int:
            if value == 2:
                raise ValueError("bad value")
            return value * 10

        results = dvt.task.map_blocking(
            maybe_fail,
            [1, 2, 3],
            return_exceptions=True,
        )

        check_equal(results[0], 10, "map_blocking return_exceptions: index 0")
        check(
            isinstance(results[1], ValueError),
            "map_blocking return_exceptions: index 1 should hold the exception object",
        )
        check_equal(results[2], 30, "map_blocking return_exceptions: index 2")


def test_map_blocking_return_exceptions_false_raises() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        def maybe_fail(value: int) -> int:
            if value == 2:
                raise ValueError("bad value")
            return value * 10

        try:
            dvt.task.map_blocking(maybe_fail, [1, 2, 3], return_exceptions=False)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "expected ValueError to propagate when return_exceptions=False"
            )


def test_retry_handling() -> None:
    retry_state = {"count": 0}

    def flaky() -> int:
        retry_state["count"] += 1
        if retry_state["count"] < 2:
            raise RuntimeError("transient failure")
        return 7

    with Dovetail(retries=1, retry_backoff=0.0, shutdown_on_exit=False) as dvt:
        check_equal(
            dvt.task.to_thread_blocking(flaky),
            7,
            "retry success after one transient failure",
        )

        stats = dvt.events.stats()
        check_equal(retry_state["count"], 2, "retry attempt count")
        check(stats["retries"] >= 1, "retries stat should have incremented")


def test_shutdown_idempotent() -> None:
    dvt = Dovetail(shutdown_on_exit=False)
    dvt.shutdown()
    dvt.shutdown()  # must not raise the second time
    check(dvt.is_shutdown, "shutdown() should mark the instance as shut down")


def test_shutdown_wait_false() -> None:
    dvt = Dovetail(shutdown_on_exit=False)
    dvt.shutdown(wait=False)
    check(dvt.is_shutdown, "shutdown(wait=False) should still mark shutdown")


def test_sync_context_manager_shuts_down_on_exit() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        pass
    check(dvt.is_shutdown, "sync context manager should shut down on block exit")


def test_registry_auto_register_and_list_active() -> None:
    dvt = Dovetail(shutdown_on_exit=False)
    try:
        check(
            any(item is dvt for item in list_active()),
            "auto-registered instance should appear in list_active()",
        )
    finally:
        unregister(dvt)
        dvt.shutdown()


def test_registry_duplicate_registration_is_safe() -> None:
    dvt = Dovetail(
        shutdown_on_exit=False,
        auto_register=False,
    )

    try:
        register(dvt)
        register(dvt)

        instances = [
            item for item in list_active()
            if item is dvt
        ]

        check_equal(
            len(instances),
            1,
            "registry should not contain duplicate instances",
        )

    finally:
        unregister(dvt)
        dvt.shutdown()


def test_registry_unregister_removes_instance() -> None:
    dvt = Dovetail(shutdown_on_exit=False)
    unregister(dvt)
    check(
        not any(item is dvt for item in list_active()),
        "unregistered instance should not appear in list_active()",
    )
    dvt.shutdown()


def test_registry_auto_register_false() -> None:
    dvt = Dovetail(shutdown_on_exit=False, auto_register=False)
    try:
        check(
            not any(item is dvt for item in list_active()),
            "auto_register=False should keep the instance out of list_active()",
        )
    finally:
        dvt.shutdown()


def test_registry_shutdown_all() -> None:
    d1 = Dovetail(shutdown_on_exit=False)
    d2 = Dovetail(shutdown_on_exit=False)
    try:
        shutdown_all()
        check(
            d1 not in list_active(),
            "shutdown_all should remove d1"
        )

        check(
            d2 not in list_active(),
            "shutdown_all should remove d2"
        )
    finally:
        unregister(d1)
        unregister(d2)


def test_registry_shutdown_removes_instance() -> None:
    dvt = Dovetail(shutdown_on_exit=False)

    check(
        any(item is dvt for item in list_active()),
        "instance should be registered",
    )

    dvt.shutdown()

    check(
        not any(item is dvt for item in list_active()),
        "shutdown should unregister instance",
    )


def test_registry_shutdown_hook_executes() -> None:
    calls = {"count": 0}

    def hook() -> None:
        calls["count"] += 1

    set_app_shutdown_hook(hook)

    try:
        global_registry.call_app_hook_or_shutdown()

        check_equal(
            calls["count"],
            1,
            "shutdown hook should execute once",
        )

    finally:
        set_app_shutdown_hook(None)


def test_registry_gc_cleanup() -> None:
    import gc
    import weakref

    def create_ref():
        dvt = Dovetail(shutdown_on_exit=False)
        return weakref.ref(dvt)

    ref = create_ref()

    # A single collect() isn't a hard guarantee for cyclic garbage —
    # retry a few times before concluding it's truly stuck.
    for _ in range(5):
        if ref() is None:
            break
        gc.collect()

    if ref() is not None:
        print("\nDVT still alive after retries")
        # Diagnostics only — do NOT bind a new strong reference here.
        for r in gc.get_referrers(ref()):
            print("\n---")
            print(type(r))
            print(r)
        # any objgraph diagnostics should also call ref() fresh inline,
        # never store the result in a local that outlives this branch

    check(
        ref() is None,
        "Dovetail instance should be garbage collected",
    )


def test_events_function_and_instance_target_raises() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        def dummy() -> None:
            pass

        try:
            dvt.events.on_start(
                lambda payload: None,
                function_target=dummy,
                execution_target="exec-1",
            )
        except ValueError:
            pass
        else:
            raise AssertionError(
                "expected ValueError when both function_target and execution_target are set"
            )


def test_events_invalid_callback_raises_type_error() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        try:
            dvt.events.on_start("not callable")  # type: ignore[arg-type]
        except TypeError:
            pass
        else:
            raise AssertionError("expected TypeError for a non-callable callback")


def test_events_max_chain_depth_invalid_raises() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        try:
            dvt.events.on_start(lambda payload: None, max_chain_depth=0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for max_chain_depth < 1")


def test_events_reentry_disallowed_by_default() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        call_count = {"n": 0}

        def cb(payload: dict) -> None:
            call_count["n"] += 1
            if call_count["n"] < 3:
                dvt.events.emit("task_queued", {"function": None})

        dvt.events.on_queued(cb, allow_reentry=False)
        dvt.events.emit("task_queued", {"function": None})

        check_equal(
            call_count["n"],
            1,
            "reentry disallowed: nested emit from inside the callback should be skipped",
        )


def test_events_reentry_allowed_bounded_by_max_chain_depth() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        call_count = {"n": 0}

        def cb(payload: dict) -> None:
            call_count["n"] += 1
            if call_count["n"] < 10:
                dvt.events.emit("task_started", {"function": None})

        dvt.events.on_start(cb, allow_reentry=True, max_chain_depth=3)
        dvt.events.emit("task_started", {"function": None})

        check_equal(
            call_count["n"],
            3,
            "reentry allowed: nested calls should stop once max_chain_depth is reached",
        )


def test_tracing_and_instrumentation() -> None:
    captured: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("test.dovetail.tracing")
    logger.setLevel(logging.DEBUG)
    handler = ListHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    try:
        with Dovetail(
            trace=True,
            trace_logger=logger,
            trace_prefix="TraceTest",
            shutdown_on_exit=False,
        ) as dvt:
            dvt.events.trace("custom trace message")
            dvt.events.trace_struct(
                method="custom_method",
                status="Start",
                extra={"Foo": "Bar"},
            )
            dvt.events.inc_stat("custom_counter")
            dvt.events.inc_stat("custom_counter", 4)

            check(
                any("custom trace message" in m for m in captured),
                "trace() should write through the supplied logger",
            )
            check(
                any("custom_method" in m for m in captured),
                "trace_struct() should write through the supplied logger",
            )

            stats = dvt.events.stats()
            check_equal(
                stats.get("custom_counter"),
                5,
                "inc_stat() should accumulate a custom counter alongside the built-ins",
            )
    finally:
        logger.removeHandler(handler)


def test_shutdown_emits_trace() -> None:
    captured = []

    class Handler(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger = logging.getLogger("dovetail.registry.test")
    logger.setLevel(logging.DEBUG)

    handler = Handler()
    logger.addHandler(handler)

    try:
        d1 = Dovetail(
            trace=True,
            trace_logger=logger,
            shutdown_on_exit=False,
        )

        shutdown_all()

        check(
            any("Shutdown" in msg for msg in captured),
            "shutdown_all should trigger shutdown logging",
        )

    finally:
        logger.removeHandler(handler)


# ===========================================================================
# ASYNC TESTS — require a running event loop. Anything exercising
# to_thread / schedule (and the lifecycle events they emit) lives here.
# ===========================================================================

async def test_run_blocking_raises_in_running_loop() -> None:
    with Dovetail(shutdown_on_exit=False) as dvt:
        try:
            dvt.task.run_blocking(lambda: 1)
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "expected RuntimeError when run_blocking() is called inside a running loop"
            )


async def test_to_thread_basic() -> None:
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        result = await dvt.task.to_thread(lambda: "thread")
        check_equal(result, "thread", "to_thread basic result")


async def test_to_thread_timeout() -> None:
    async with Dovetail(timeout=0.01, shutdown_on_exit=False) as dvt:
        def slow() -> str:
            time.sleep(0.05)
            return "done"

        try:
            await dvt.task.to_thread(slow)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("expected asyncio.TimeoutError from to_thread timeout")

        stats = dvt.events.stats()
        check(stats["error"] >= 1, "timeout should increment the error stat")


async def test_schedule_coroutine_object() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        async def work() -> str:
            await asyncio.sleep(0.001)
            return "coro-object"

        result = await dvt.task.schedule(work())
        check_equal(result, "coro-object", "schedule(coroutine object)")


async def test_schedule_coroutine_function() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        async def work(value: int) -> int:
            await asyncio.sleep(0.001)
            return value * 2

        result = await dvt.task.schedule(work, 21)
        check_equal(result, 42, "schedule(coroutine function, *args)")


async def test_schedule_callable() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        def work(x: int, y: int) -> int:
            return x + y

        result = await dvt.task.schedule(work, 20, 22)
        check_equal(result, 42, "schedule(callable, *args)")


async def test_schedule_invalid_type_raises() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        try:
            dvt.task.schedule(12345)  # not a coroutine, coroutine function, or callable
        except TypeError:
            pass
        else:
            raise AssertionError(
                "expected TypeError for a non-schedulable argument"
            )


async def test_schedule_timeout_coroutine() -> None:
    async with Dovetail(timeout=0.01, shutdown_on_exit=False) as dvt:
        async def slow() -> str:
            await asyncio.sleep(0.05)
            return "done"

        task = dvt.task.schedule(slow)
        try:
            await task
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError(
                "expected asyncio.TimeoutError from schedule(coroutine function) timeout"
            )


async def test_async_context_manager_shuts_down_on_exit() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        pass
    check(dvt.is_shutdown, "async context manager should shut down on block exit")


async def test_event_ordering_queued_started_done() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        order: list[str] = []

        def f() -> str:
            return "ok"

        dvt.events.on_queued(lambda p: order.append("queued"), function_target=f)
        dvt.events.on_start(lambda p: order.append("started"), function_target=f)
        dvt.events.on_end(lambda p: order.append("done"), function_target=f)

        result = await dvt.task.to_thread(f)

        check_equal(result, "ok", "to_thread result")
        check_equal(
            order,
            ["queued", "started", "done"],
            "events should fire in queued -> started -> done order",
        )


async def test_global_listener_fires_for_every_function() -> None:
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        hits: list[Any] = []
        dvt.events.on_end(lambda p: hits.append(p.get("function")))

        def f1() -> int:
            return 1

        def f2() -> int:
            return 2

        await dvt.task.to_thread(f1)
        await dvt.task.to_thread(f2)

        check(len(hits) >= 2, "global listener should fire for every completed task")


async def test_function_target_scoping() -> None:
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        hits_a: list[Any] = []

        def func_a() -> str:
            return "a"

        def func_b() -> str:
            return "b"

        dvt.events.on_end(
            lambda p: hits_a.append(p.get("function")),
            function_target=func_a,
        )

        await dvt.task.to_thread(func_a)
        await dvt.task.to_thread(func_b)

        check_equal(
            len(hits_a),
            1,
            "function_target scoping: listener should fire only for func_a",
        )


async def test_instance_target_scoping() -> None:
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        def sample() -> str:
            return "x"

        ids: list[Any] = []
        dvt.events.on_start(
            lambda p: ids.append(p.get("execution_id")),
            function_target=sample,
        )

        t1 = dvt.task.schedule(sample)
        t2 = dvt.task.schedule(sample)
        await asyncio.gather(t1, t2)

        check_equal(len(ids), 2, "expected two start events for two scheduled calls")
        first_id = ids[0]
        check(ids[0] != ids[1], "execution ids should be unique per call")

        hits_first: list[Any] = []
        dvt.events.on_end(
            lambda p: hits_first.append(p.get("execution_id")),
            execution_target=first_id,
        )

        t3 = dvt.task.schedule(sample)
        await t3

        check_equal(
            len(hits_first),
            0,
            "instance-scoped listener should not fire for a different execution id",
        )


async def test_event_callback_exception_does_not_break_task() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        callback_errors: list[str] = []

        def bad_callback(payload: dict) -> None:
            callback_errors.append("called")
            raise RuntimeError("listener exploded")

        dvt.events.on_end(bad_callback)

        result = await dvt.task.to_thread(lambda: 42)

        check_equal(
            result,
            42,
            "event callback failure should not fail the task",
        )

        check_equal(
            callback_errors,
            ["called"],
            "failing callback should still have executed",
        )


async def test_retry_exhaustion_and_error_event() -> None:
    async with Dovetail(retries=2, retry_backoff=0.0, shutdown_on_exit=False) as dvt:
        retry_hits: list[Any] = []
        error_hits: list[Any] = []

        def always_fails() -> None:
            raise RuntimeError("boom")

        dvt.events.on_retry(
            lambda p: retry_hits.append(p.get("attempt")),
            function_target=always_fails,
        )
        dvt.events.on_error(
            lambda p: error_hits.append(p.get("attempt")),
            function_target=always_fails,
        )

        try:
            await dvt.task.to_thread(always_fails)
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "expected RuntimeError to propagate after exhausting retries"
            )

        check_equal(len(retry_hits), 2, "expected 2 retry events (retries=2)")
        check_equal(len(error_hits), 1, "expected exactly 1 final error event")

        stats = dvt.events.stats()
        check(stats["retries"] >= 2, "retries stat should reflect retry attempts")
        check(stats["error"] >= 1, "error stat should reflect the final failure")


async def test_started_fires_once_per_retry_attempt() -> None:
    async with Dovetail(retries=2, retry_backoff=0.0, shutdown_on_exit=False) as dvt:
        attempts_seen: list[Any] = []
        state = {"n": 0}

        def flaky() -> str:
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("fail")
            return "ok"

        dvt.events.on_start(
            lambda p: attempts_seen.append(p.get("attempt")),
            function_target=flaky,
        )

        result = await dvt.task.to_thread(flaky)

        check_equal(result, "ok", "flaky() should eventually succeed")
        check_equal(
            attempts_seen,
            [1, 2, 3],
            "on_start should fire once per attempt with the correct attempt number",
        )


async def test_cancellation_event() -> None:
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        cancel_hits: list[Any] = []
        dvt.events.on_cancel(lambda p: cancel_hits.append(p.get("execution_id")))

        task = dvt.task.schedule(asyncio.sleep(0.2))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.01)

        check_equal(len(cancel_hits), 1, "on_cancel should fire exactly once")


async def test_rate_limiting_with_event_and_stat() -> None:
    async with Dovetail(
        rate_limit_per_sec=1,
        rate_limit_burst=1,
        shutdown_on_exit=False,
    ) as dvt:
        throttle_hits: list[Any] = []
        dvt.events.on_rate_limited(lambda p: throttle_hits.append(p.get("waited")))

        results = await asyncio.gather(
            dvt.task.schedule(lambda: "first"),
            dvt.task.schedule(lambda: "second"),
        )

        check_equal(
            results,
            ["first", "second"],
            "rate-limited schedule results should preserve order",
        )
        check(
            dvt.events.stats()["throttled"] >= 1,
            "throttled stat should increment under rate limiting",
        )
        check(
            len(throttle_hits) >= 1,
            "on_rate_limited listener should fire at least once",
        )


async def test_chained_scheduling_via_on_end() -> None:
    # NOTE: step_a/step_b are coroutine *functions* here, not plain callables.
    async with Dovetail(max_workers=2, shutdown_on_exit=False) as dvt:
        async def step_a() -> str:
            return "A"

        async def step_b() -> str:
            return "B"

        chain_hits: list[str] = []
        b_done = asyncio.Event()

        def on_a_end(payload: dict) -> None:
            chain_hits.append("a_done")
            dvt.task.schedule(step_b)

        def on_b_end(payload: dict) -> None:
            chain_hits.append("b_done")
            b_done.set()

        dvt.events.on_end(on_a_end, function_target=step_a)
        dvt.events.on_end(on_b_end, function_target=step_b)

        await dvt.task.schedule(step_a)
        await asyncio.wait_for(b_done.wait(), timeout=1.0)

        check("a_done" in chain_hits, "step_a completion should trigger the chain")
        check("b_done" in chain_hits, "chained step_b should complete")


async def test_event_listener_requires_callable_callback() -> None:
    async with Dovetail(shutdown_on_exit=False) as dvt:
        task = dvt.task.schedule(lambda: "b")

        try:
            dvt.events.on_end(task)
        except TypeError:
            pass
        else:
            raise AssertionError(
                "expected TypeError when passing a Task as a listener callback"
            )

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

async def test_async_event_callback_behaviour():
    pass


# ===========================================================================
# Orchestration
# ===========================================================================

SYNC_TESTS: list[tuple[str, Callable[[], Any]]] = [
    ("run_blocking(callable)", test_run_blocking_callable),
    ("run_blocking(coroutine function)", test_run_blocking_coroutine_function),
    ("run_blocking(coroutine object)", test_run_blocking_coroutine_object),
    ("to_thread_blocking(callable)", test_to_thread_blocking),
    ("map_blocking basic", test_map_blocking_basic),
    ("map_blocking empty items", test_map_blocking_empty_items),
    ("map_blocking max_concurrency enforced", test_map_blocking_max_concurrency_enforced),
    ("map_blocking return_exceptions=True", test_map_blocking_return_exceptions_true),
    ("map_blocking return_exceptions=False raises", test_map_blocking_return_exceptions_false_raises),
    ("retry handling (to_thread_blocking)", test_retry_handling),
    ("shutdown() idempotent", test_shutdown_idempotent),
    ("shutdown(wait=False)", test_shutdown_wait_false),
    ("sync context manager shuts down on exit", test_sync_context_manager_shuts_down_on_exit),
    ("registry: auto-register + list_active", test_registry_auto_register_and_list_active),
    ("registry: is duplicate registration safe", test_registry_duplicate_registration_is_safe),
    ("registry: unregister", test_registry_unregister_removes_instance),
    ("registry: auto_register=False", test_registry_auto_register_false),
    ("registry: shutdown_all", test_registry_shutdown_all),
    ("registry: test that shutdown() removes instance", test_registry_shutdown_removes_instance),
    ("registry: set and test app shutdown hook", test_registry_shutdown_hook_executes),
    ("registry: gc cleanup", test_registry_gc_cleanup),
    ("events: function_target + execution_target raises ValueError", test_events_function_and_instance_target_raises),
    ("events: non-callable callback raises TypeError", test_events_invalid_callback_raises_type_error),
    ("events: max_chain_depth < 1 raises ValueError", test_events_max_chain_depth_invalid_raises),
    ("events: reentry disallowed by default", test_events_reentry_disallowed_by_default),
    ("events: reentry allowed, bounded by max_chain_depth", test_events_reentry_allowed_bounded_by_max_chain_depth),
    ("tracing & custom instrumentation", test_tracing_and_instrumentation),
    ("tracing & custom instrumentation: shutdown trace emit", test_shutdown_emits_trace),
]

ASYNC_TESTS: list[tuple[str, Callable[[], Any]]] = [
    ("run_blocking raises inside a running loop", test_run_blocking_raises_in_running_loop),
    ("to_thread basic", test_to_thread_basic),
    ("to_thread per-call timeout", test_to_thread_timeout),
    ("schedule(coroutine object)", test_schedule_coroutine_object),
    ("schedule(coroutine function)", test_schedule_coroutine_function),
    ("schedule(callable)", test_schedule_callable),
    ("schedule raises TypeError for invalid input", test_schedule_invalid_type_raises),
    ("schedule per-call timeout", test_schedule_timeout_coroutine),
    ("async context manager shuts down on exit", test_async_context_manager_shuts_down_on_exit),
    ("event ordering: queued -> started -> done", test_event_ordering_queued_started_done),
    ("global (unscoped) listener fires for every function", test_global_listener_fires_for_every_function),
    ("function_target scoping", test_function_target_scoping),
    ("execution_target scoping", test_instance_target_scoping),
    ("events: test callback exception does not break task", test_event_callback_exception_does_not_break_task),
    ("retry exhaustion: retry count + final error event", test_retry_exhaustion_and_error_event),
    ("on_start fires once per retry attempt", test_started_fires_once_per_retry_attempt),
    ("cancellation event", test_cancellation_event),
    ("rate limiting: event + throttled stat together", test_rate_limiting_with_event_and_stat),
    ("chained scheduling via on_end", test_chained_scheduling_via_on_end),
    ("task as callback raises TypeError", test_event_listener_requires_callable_callback),
]


async def run_all_async_tests() -> None:
    for name, fn in ASYNC_TESTS:
        await run_async_test(name, fn)


def run_feature_checks() -> None:
    print("Running sync checks...")
    for name, fn in SYNC_TESTS:
        run_test(name, fn)

    print("\nRunning async checks...")
    asyncio.run(run_all_async_tests())


if __name__ == "__main__":
    import gc
    
    print("Running Dovetail feature checks...")
    run_feature_checks()
    gc.collect()
    all_passed = RESULTS.print_summary()
    sys.exit(0 if all_passed else 1)