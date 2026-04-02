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
