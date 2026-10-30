# Dovetail API reference (3.0.0)

Dovetail exposes `Dovetail` and `DovetailRegistry` from `pydovetail`.
Create an instance explicitly; there is no package-level singleton executor.
Requires Python 3.10+ and `typing_extensions>=4.0`.

## Construction and lifecycle

```python
from pydovetail import Dovetail

dvt = Dovetail(max_workers=8, auto_register=False)
try:
    result = dvt.task.to_thread_blocking(lambda: 42)
finally:
    dvt.shutdown(wait=True)
```

Prefer `with Dovetail(...)` for synchronous code and `async with Dovetail(...)`
for asynchronous code. The async context manager runs its shutdown through an
executor to avoid blocking the event loop. Explicit `shutdown()` is idempotent.

| Constructor parameter | Default | Purpose |
| --- | --- | --- |
| `loop` | `None` | Optional associated event loop; current running loop is otherwise used for async calls |
| `auto_register` | `True` | Register the instance with the weak-reference registry |
| `max_workers` | `None` | Standard `ThreadPoolExecutor` default |
| `rate_limit_per_sec` | `None` | Token-bucket rate; disabled when unset |
| `rate_limit_burst` | `None` | Burst capacity; defaults to rate when rate limiting is enabled |
| `timeout` | `None` | Timeout in seconds on supported execution paths; non-positive means no timeout |
| `retries` | `0` | Additional attempts for blocking callable paths |
| `retry_backoff` | `0.0` | Exponential retry backoff base (seconds) |
| `trace` | `False` | Enable Dovetail traces |
| `trace_logger` | `None` | Optional logging destination |
| `trace_prefix` | `"Dovetail"` | Label for trace messages |
| `shutdown_on_exit` | `False` | Opt in to process-level atexit and signal handling |

`shutdown(wait=True)` waits for executor work to finish; `wait=False` does
not wait for running functions. `shutdown_on_exit=True` installs process-wide
handlers on a per-instance basis: multiple instances can interfere with each
other's signal-handler restoration. Prefer application-owned shutdown and
`shutdown_on_exit=False` for servers.

## Task methods

| Method | Call from | Result | Notes |
| --- | --- | --- | --- |
| `await task.to_thread(func, *args, **kwargs)` | Async | Callable result | Executes a **blocking sync** callable in the owned pool |
| `task.to_thread_blocking(func, *args, **kwargs)` | Sync | Callable result | Synchronous adapter for `to_thread` |
| `await task.map(func, items, *, max_concurrency=None, return_exceptions=False)` | Async | Ordered `list` | Blocking callable per item; bounded active calls |
| `task.map_blocking(func, items, max_concurrency=None, return_exceptions=False)` | Sync | Ordered `list` | Synchronous adapter for `map` |
| `task.run_blocking(func_or_coro, *args, **kwargs)` | Sync | Result | Bridges sync callers; raises in a running loop |
| `task.schedule(coro_or_callable, *args, **kwargs)` | Async | `asyncio.Task` | Accepts coroutine object, coroutine function, or sync callable |
| `task.managed(func=None)` | Sync/async | Wrapped callable | Sync wrapper blocks; async wrapper returns an `asyncio.Task` |

### Blocking calls and ordered mapping

```python
import asyncio
from pydovetail import Dovetail


def blocking_square(x: int) -> int:
    return x * x


async def main():
    async with Dovetail(max_workers=8) as dvt:
        one = await dvt.task.to_thread(blocking_square, 3)
        ordered = await dvt.task.map(
            blocking_square, range(20), max_concurrency=8
        )
        assert one == 9 and ordered[3] == 9


asyncio.run(main())
```

`map()` materialises the input iterable and creates asyncio tasks for its
items. `max_concurrency` limits **active** calls, not the number of queued
asyncio tasks; it must be at least 1. When omitted, it uses the thread pool's
worker count. Results preserve input order. With `return_exceptions=True`,
ordinary exceptions appear at their corresponding positions. Otherwise the
first failure propagates and sibling asyncio tasks are cancelled; running
Python threads may continue. `map()` is **not** an async-function mapper:
passing an `async def` function does not await the coroutine it returns.
Use `asyncio.gather()` with a semaphore for async I/O instead.

### Scheduling and managed functions

```python
import asyncio
from pydovetail import Dovetail


async def get_value():
    await asyncio.sleep(0)
    return 5


async def main():
    async with Dovetail() as dvt:
        task = dvt.task.schedule(get_value())
        assert await task == 5

        @dvt.task.managed()
        async def async_job():
            return 6

        assert await async_job() == 6  # returns asyncio.Task


asyncio.run(main())
```

For a synchronous function, `@dvt.task.managed()` preserves synchronous
calling semantics, including blocking until completion. Do not call its sync
wrapper inside a running event loop. `managed()` does **not** accept a
`method` keyword.

### Retries, timeouts and cancellation

- Instance `retries` and `retry_backoff` apply to blocking callable execution
  (`to_thread` and callable-based scheduling), not coroutine scheduling.
  Exponential retry sleeps currently occupy executor threads.
- Rate limiting applies to supported blocking execution paths; a throttled
  operation produces `RATE_LIMITED` and increments the corresponding metric.
- `timeout` bounds *waiting* on supported calls; it does not kill a running
  thread. A timed-out/cancelled worker can continue producing side effects and
  later events. Configure underlying network/SDK timeouts separately.
- Cancelling an `asyncio.Task` may stop awaiting a result, but cannot forcibly
  terminate its worker function.
- Worker-originated lifecycle events are posted to the asyncio loop. For a
  normally finishing `to_thread` call, Dovetail waits for queued event
  callbacks before returning; after a timeout or cancellation the worker may
  still post later events.
- Dovetail does not promise `contextvars` propagation for `to_thread`; if
  thread-bound code needs request context, propagate it explicitly.

## Events, counters and tracing

Use `dvt.TaskEvent` for enum values and `dvt.events` for event management.
The enum has `QUEUED`, `STARTED`, `RETRY`, `RATE_LIMITED`, `ERROR`,
`CANCELLED` and `DONE`. `RETRY.value` is `"retries"`.

```python
from pydovetail import Dovetail

with Dovetail() as dvt:
    @dvt.events.on(dvt.TaskEvent.DONE)
    def on_done(event):
        print(event.get("function"), event.get("execution_id"))

    print(dvt.task.to_thread_blocking(lambda: "ok"))
    print(dvt.events.stats())
    dvt.events.off(on_done)
```

`events.on(event, callback=None, *, function_target=None,
execution_target=None, allow_reentry=False, max_chain_depth=5)` accepts a
callback directly (returns a subscription ID) or as a decorator (returns the
original function). Supply at most one of `function_target` and
`execution_target`. `events.off(id_or_function)` removes a subscription;
`events.clear()` removes registered listeners. The callback receives a
mapping with execution metadata, varying by event, such as `method`, `task`,
`function`, `execution_id`, `attempt`, `elapsed` or `error`.

Listeners are synchronous. Exceptions from listeners are isolated from task
execution and may be traced/logged. Reentry controls guard recursive callback
chains, but concurrency-sensitive listener interactions still require care.
`events.stats()` returns a snapshot of cumulative counters. Use
`events.inc_stat(key, count=1)` for custom counters.

Enable trace output with `Dovetail(trace=True, trace_logger=logger,
trace_prefix="Worker")`. The instance exposes `events.trace(message)` and
`events.trace_struct(method=..., status=..., ...)` for custom instrumentation.
Traces and event payloads may contain exception details, so configure logging
and retention appropriately.

## Instance registry

The exported `DovetailRegistry` is the shared registry **instance**, not a
class constructor. With `auto_register=True`, it holds weak references and
supports:

```python
from pydovetail import DovetailRegistry

active = DovetailRegistry.list_active()
DovetailRegistry.shutdown(wait=True)
DovetailRegistry.set_app_shutdown_hook(None)
```

`set_app_shutdown_hook(fn)` stores a callback for explicit invocation through
`DovetailRegistry.call_app_hook_or_shutdown()`; it is not automatically wired
to process exit. `auto_register=False` disables automatic registration.
The registry does not keep instances alive.

## Known limitations

- Thread cancellation and timeout cannot preempt a running blocking function.
- `map()` bounds active concurrency, but materialises the entire input.
- Retrying uses blocking worker sleeps; workloads dominated by retries can
  exhaust worker capacity.
- Process-level signal hooks are not coordinated across multiple instances.
- Calling shutdown from callbacks or worker contexts warrants care; avoid
  blocking event-loop callbacks waiting on their own executor work.
- Async callbacks are not supported by the synchronous event-listener API.

See [README](../README.md) and [benchmark guide](benchmarks.md).
