import asyncio
import pytest

from dovetail import Dovetail

d = Dovetail()


def blocking_io(x: int) -> int:
    """Blocking helper used in tests."""
    import time

    time.sleep(0.01)
    return x * 2


async def async_coro(x: int) -> int:
    await asyncio.sleep(0.001)
    return x + 1


def test_to_thread() -> None:
    async def _run():
        return await d.task.to_thread(blocking_io, 3)

    res = asyncio.run(_run())
    assert res == 6


def test_schedule_coro() -> None:
    async def _run():
        task = d.task.schedule(async_coro(4))
        return await task

    res = asyncio.run(_run())
    assert res == 5


def test_schedule_sync_callable() -> None:
    async def _run():
        task = d.task.schedule(lambda: blocking_io(5))
        return await task

    res = asyncio.run(_run())
    assert res == 10


def test_run_blocking() -> None:
    # run a coroutine synchronously from sync code
    assert d.task.run_blocking(async_coro(7)) == 8


def test_to_thread_exception() -> None:
    def raises():
        raise ValueError("boom")

    async def _run():
        with pytest.raises(ValueError):
            await d.task.to_thread(raises)

    asyncio.run(_run())


def test_schedule_async_function_and_args() -> None:
    async def afunc(a, b=1):
        await asyncio.sleep(0)
        return a + b

    async def _run():
        task = d.task.schedule(afunc, 2, b=3)
        return await task

    assert asyncio.run(_run()) == 5


def test_schedule_fire_and_forget_runs() -> None:
    async def _run():
        ev = asyncio.Event()

        async def setter():
            await asyncio.sleep(0.001)
            ev.set()

        # schedule and don't await - the task should still run
        d.task.schedule(setter())
        await asyncio.wait_for(ev.wait(), timeout=1.0)

    asyncio.run(_run())


def test_schedule_sync_callable_with_args_kwargs() -> None:
    async def _run():
        def f(a, b=0):
            return a + b

        task = d.task.schedule(f, 2, b=4)
        return await task

    assert asyncio.run(_run()) == 6


def test_schedule_invalid_input() -> None:
    async def _run():
        with pytest.raises(TypeError):
            d.task.schedule(123)

    asyncio.run(_run())


def test_run_blocking_sync_function() -> None:
    def add(a, b):
        return a + b

    assert d.task.run_blocking(add, 2, 3) == 5


def test_run_blocking_from_event_loop_raises() -> None:
    async def _run():
        # Calling run_blocking from inside an event loop should raise
        with pytest.raises(RuntimeError):
            d.task.run_blocking(lambda: 1)

    asyncio.run(_run())


def test_local_dovetail_shutdown() -> None:
    # Create a local Dovetail instance and ensure shutdown works
    from dovetail import Dovetail
    local = Dovetail()
    
    # scheduling a small sync callable works
    res = asyncio.run(local.task.to_thread(lambda: 42))
    assert res == 42
    local.shutdown()


def test_to_thread_retries_eventually_succeeds() -> None:
    local = Dovetail(default_retries=2)
    counter = {"calls": 0}

    def flaky():
        counter["calls"] += 1
        if counter["calls"] < 3:
            raise ValueError("transient")
        return 99

    async def _run():
        return await local.task.to_thread(flaky)

    assert asyncio.run(_run()) == 99
    assert counter["calls"] == 3
    stats = local.events.stats()
    assert stats["retries"] >= 2
    local.shutdown()


def test_default_timeout_on_coroutine_schedule() -> None:
    local = Dovetail(default_timeout=0.01)

    async def slow():
        await asyncio.sleep(0.1)
        return 1

    async def _run():
        task = local.task.schedule(slow())
        with pytest.raises(asyncio.TimeoutError):
            await task

    asyncio.run(_run())
    local.shutdown()


def test_events_fire_for_to_thread_lifecycle() -> None:
    local = Dovetail(default_retries=1)
    events = []

    local.events.on_queued(lambda payload: events.append(("queued", payload.get("method"))))
    local.events.on_start(lambda payload: events.append(("started", payload.get("method"))))
    local.events.on_end(lambda payload: events.append(("done", payload.get("method"))))

    async def _run():
        return await local.task.to_thread(lambda: 5)

    assert asyncio.run(_run()) == 5
    labels = [name for name, _ in events]
    assert "queued" in labels
    assert "started" in labels
    assert "done" in labels
    local.shutdown()


def test_events_on_end_listener_global_and_scoped() -> None:
    local = Dovetail()
    seen = {"global": 0, "scoped": 0}

    async def _run():
        task = local.task.schedule(async_coro, 10)

        local.events.on_end(lambda _payload=None: seen.__setitem__("global", seen["global"] + 1))

        local.events.on_end(
            lambda _payload=None: seen.__setitem__("scoped", seen["scoped"] + 1),
            function_target=async_coro,
        )

        await task
        await asyncio.sleep(0.01)

    asyncio.run(_run())
    assert seen["global"] >= 1
    assert seen["scoped"] >= 1
    local.shutdown()


def test_events_listener_rejects_both_targets() -> None:
    local = Dovetail()

    with pytest.raises(ValueError):
        local.events.on_end(lambda _payload=None: None, function_target="f", instance_target="run-1")

    local.shutdown()
