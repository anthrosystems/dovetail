import asyncio
import pytest

from dovetail import Dovetail


def blocking_io(x: int) -> int:
    """Blocking helper used in tests."""
    import time

    time.sleep(0.01)
    return x * 2


async def async_coro(x: int) -> int:
    await asyncio.sleep(0.001)
    return x + 1


@pytest.mark.asyncio
async def test_to_thread() -> None:
    res = await Dovetail.Task.to_thread(blocking_io, 3)
    assert res == 6


@pytest.mark.asyncio
async def test_schedule_coro() -> None:
    task = Dovetail.Task.schedule(async_coro(4))
    res = await task
    assert res == 5


@pytest.mark.asyncio
async def test_schedule_sync_callable() -> None:
    task = Dovetail.Task.schedule(lambda: blocking_io(5))
    res = await task
    assert res == 10


def test_run_blocking() -> None:
    # run a coroutine synchronously from sync code
    assert Dovetail.Task.run_blocking(async_coro(7)) == 8


@pytest.mark.asyncio
async def test_to_thread_exception() -> None:
    def raises():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await Dovetail.Task.to_thread(raises)


@pytest.mark.asyncio
async def test_schedule_async_function_and_args() -> None:
    async def afunc(a, b=1):
        await asyncio.sleep(0)
        return a + b

    task = Dovetail.Task.schedule(afunc, 2, b=3)
    assert await task == 5


@pytest.mark.asyncio
async def test_schedule_fire_and_forget_runs() -> None:
    ev = asyncio.Event()

    async def setter():
        await asyncio.sleep(0.001)
        ev.set()

    # schedule and don't await - the task should still run
    Dovetail.Task.schedule(setter())
    await asyncio.wait_for(ev.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_schedule_sync_callable_with_args_kwargs() -> None:
    def f(a, b=0):
        return a + b

    task = Dovetail.Task.schedule(f, 2, b=4)
    assert await task == 6


@pytest.mark.asyncio
async def test_schedule_invalid_input() -> None:
    with pytest.raises(TypeError):
        Dovetail.Task.schedule(123)


def test_run_blocking_sync_function() -> None:
    def add(a, b):
        return a + b

    assert Dovetail.Task.run_blocking(add, 2, 3) == 5


@pytest.mark.asyncio
async def test_run_blocking_from_event_loop_raises() -> None:
    # Calling run_blocking from inside an event loop should raise
    with pytest.raises(RuntimeError):
        Dovetail.Task.run_blocking(lambda: 1)


def test_local_dovetail_shutdown() -> None:
    # Create a local Dovetail instance and ensure shutdown works
    from dovetail import Dovetail
    local = Dovetail()
    
    # scheduling a small sync callable works
    res = asyncio.get_event_loop().run_until_complete(local.Task.to_thread(lambda: 42))
    assert res == 42
    local.shutdown()
