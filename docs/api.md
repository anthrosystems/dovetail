# Dovetail API

## Table of Contents
- [Constructor Reference](#constructor-reference)
- [API Summary](#api-summary)
- [Registry](#registry)
- [Observability & Behavior](#observability--behavior)
- [Tracing & Custom Instrumentation](#tracing--custom-instrumentation)
- [Event Listeners](#event-listeners)
- [Retries, timeouts, and rate limits](#retries-timeouts-and-rate-limits)
- [Known limitations](#known-limitations)
- [Notes](#notes)

## Constructor Reference

`Dovetail(...)` accepts the following parameters. All are optional.

| Parameter | Default | Description |
|---|---|---|
| `loop` | `None` | An explicit event loop to associate with this instance. When `None`, the currently running loop is used at call time. Most consumers should leave this unset. |
| `auto_register` | `True` | Automatically registers the instance with the package-level registry (see **[Registry](#registry)**) so it appears in `list_active()` and is covered by `shutdown()`. Instances are stored using weak references, so registration does not extend the lifetime of a `Dovetail` object. |
| `max_workers` | `None` (ThreadPoolExecutor default) | Size of the internal threadpool used by `to_thread`, `to_thread_blocking`, and `map_blocking`. |
| `rate_limit_per_sec` | `None` (disabled) | Token-bucket refill rate. When set, task starts may be delayed to stay under this rate. |
| `rate_limit_burst` | `None` (defaults to `rate_limit_per_sec`) | Token-bucket burst capacity — the number of tasks that can start immediately before throttling kicks in. |
| `timeout` | `None` (no timeout) | Per-call timeout in seconds, applied to `to_thread`, `schedule`, and `run_blocking` coroutine paths. |
| `retries` | `0` | Number of retry attempts for synchronous callable execution paths (`to_thread` and callable-based `schedule`). |
| `retry_backoff` | `0.0` | Base backoff in seconds; grows exponentially per retry attempt. |
| `trace` | `False` | Enables structured trace logging for this instance. Can also be enabled globally via the `DOVETAIL_TRACE` environment variable (`1`, `true`, `yes`, or `on`). |
| `trace_logger` | `None` (falls back to `logging.getLogger("dovetail")`) | The `logging.Logger` traces are written to. By default, Dovetail tracing uses the dovetail logger and propagates records to the application's logging configuration. Pass a dedicated logger if you want Dovetail's trace output separated from your application's own log lines — see **[Tracing & Custom Instrumentation](#tracing--custom-instrumentation)**. |
| `trace_prefix` | `"Dovetail"` | Short label included in every trace line (rendered as `DVT-<prefix>`). Useful for telling apart multiple `Dovetail` instances in the same log stream. |
| `shutdown_on_exit` | `True` | Registers process-level cleanup handlers which perform best-effort shutdown during interpreter exit and termination signals. |

Example using several of the less-common parameters together:

```python
import logging
from pydovetail import Dovetail

dvt_logger = logging.getLogger("dovetail.myworker")
dvt_logger.setLevel(logging.DEBUG)

with Dovetail(
    max_workers=8,
    trace=True,
    trace_logger=dvt_logger,
    trace_prefix="MyWorker",
    shutdown_on_exit=False,  # this process manages its own shutdown
) as dvt:
    result = dvt.task.run_blocking(fetch_data)
```

> **NOTE - Context-manager performance:**
> The context-manager form is recommended because it guarantees cleanup, including when exceptions occur.
> 
> ```python
> with Dovetail(...) as dvt:
>     ...
> ```
> 
> rather than manually
> ```python
> dvt = Dovetail(...)
> try:
>     ...
> finally:
>     dvt.shutdown()
> ```

---

## API Summary

### Task execution

- `dvt.task.to_thread(func, *args, **kwargs)` — await a sync callable in Dovetail's threadpool from async code; supports default timeout/retry settings.
- `dvt.task.to_thread_blocking(func, *args, **kwargs)` — synchronous wrapper for `to_thread` when you are not already in an event loop.
- `dvt.task.map_blocking(func, items, max_concurrency=None, return_exceptions=False)` — run an ordered parallel map over `items` using the threadpool.
- `dvt.task.run_blocking(func_or_coro, *args, **kwargs)` — run async or sync work from synchronous code. Do not call from inside a running event loop.
- `dvt.task.schedule(coro_or_callable, *args, **kwargs)` — schedule coroutine objects, async functions, or synchronous callables from async code and return an `asyncio.Task`.
- `dvt.task.managed(func=None, *, method=None)` — decorate a function so every call is routed through Dovetail automatically: sync functions via `to_thread()`, async functions via `schedule()`. Calling the decorated function always returns something awaitable. **Note:** the `method` keyword is accepted but currently has no effect on routing — it isn't read anywhere in the implementation. Don't rely on it to force a particular execution path; that may change in a future release.

### `dvt.task.schedule(...)`

`schedule()` is the async-side entry point for starting work through Dovetail.

It accepts:

- A coroutine object.
- An async function.
- A synchronous callable.

In all cases it returns an `asyncio.Task`, allowing normal asyncio patterns such as:

```python
task = dvt.task.schedule(worker())
result = await task
```

or:

```python
tasks = [
    dvt.task.schedule(fetch_item, item)
    for item in items
]

results = await asyncio.gather(*tasks)
```

---

### Scheduling coroutine objects

Existing coroutine objects can be scheduled directly:

```python
async def fetch_data():
    return "result"

task = dvt.task.schedule(fetch_data())

result = await task
```

The coroutine runs directly on the current event loop.

> **Note:** `retries`, `retry_backoff`, and `rate_limit_per_sec` do not apply here — those only affect sync callable execution paths (see [Retries, timeouts, and rate limits](#retries-timeouts-and-rate-limits)). A coroutine object scheduled this way starts immediately and runs to completion or failure without Dovetail retrying it.

---

### Scheduling coroutine functions

Async functions can be passed without calling them first:

```python
async def fetch_data(url):
    return await request(url)

task = dvt.task.schedule(fetch_data, "https://example.com")

result = await task
```

Arguments and keyword arguments are forwarded when the coroutine is created.

> **Note:** as with coroutine objects above, `retries` and `rate_limit_per_sec` do not apply to an async function scheduled this way.

---

### Scheduling synchronous callables

Normal blocking functions can also be scheduled:

```python
def read_file(path):
    with open(path) as f:
        return f.read()

task = dvt.task.schedule(read_file, "data.txt")

result = await task
```

Synchronous callables run through Dovetail's internal threadpool, preserving:

- retry handling;
- timeout handling;
- rate limiting;
- lifecycle events;
- tracing;
- statistics counters.

---

### Timeout behavior

`schedule()` respects the instance-level `timeout` setting:

```python
dvt = Dovetail(timeout=5)

task = dvt.task.schedule(slow_operation)

await task
```

When the timeout expires:

- the awaiting task raises `asyncio.TimeoutError`;
- the execution is marked as failed;
- an `dvt.TaskEvent.ERROR` lifecycle event is emitted;
- the `error` counter is incremented.

For threadpool-backed synchronous functions, the timeout only stops waiting for the result. Python threads cannot be forcibly cancelled, so the underlying function may continue running.

---

### Invalid inputs

`schedule()` rejects values that are not:

- coroutine objects;
- coroutine functions;
- callable objects.

Example:

```python
dvt.task.schedule(123)
# TypeError
```

---

### `dvt.task.managed(...)`

`managed()` is a decorator that picks a routing path automatically based on the function it's applied to, so call sites don't need to know whether the underlying work is sync or async:

```python
@dvt.task.managed()
def blocking_work(path):
    with open(path) as f:
        return f.read()

@dvt.task.managed()
async def fetch(url):
    return await request(url)

result_a = await blocking_work("data.txt")   # routed through to_thread()
result_b = await fetch("https://example.com")  # routed through schedule()
```

- A synchronous function is wrapped so each call runs through `to_thread()` — threadpool execution, retries, timeout, and rate limiting all apply.
- An async function is wrapped so each call runs through `schedule()` and returns an `asyncio.Task` immediately — the same coroutine-scheduling behavior (and the same retry/rate-limit exclusions) described above.

Either way, calling the decorated function returns something awaitable. As with `schedule()`, the decorated target must be callable — `dvt.task.managed()(123)` raises `TypeError`.

> **Note:** the `method` keyword argument is currently accepted but unused — routing is always inferred from `inspect.iscoroutinefunction(target)`, regardless of what (if anything) is passed to `method`.

---

### Listener chaining

Because `schedule()` always returns an `asyncio.Task`, it can safely be called inside lifecycle callbacks:

```python
def first_step():
    return "done"

def second_step():
    return "follow-up"

def on_first_complete(payload):
    dvt.task.schedule(second_step)

dvt.events.on(
    dvt.TaskEvent.DONE,
    on_first_complete,
    function_target=first_step,
)

await dvt.task.schedule(first_step)
```

Callbacks receive the event payload, and scheduling follow-up work does not block the callback.

> **Important:** pass a callback function to `dvt.events.on()`, not the result of calling `schedule()`.

Incorrect:

```python
dvt.events.on(
    dvt.TaskEvent.DONE,
    dvt.task.schedule(second_step),
)
```

Why it fails:

- `dvt.task.schedule(second_step)` immediately creates an `asyncio.Task`;
- `asyncio.Task` is not a callable callback;
- listeners require a function that accepts the event payload.

Correct:

```python
def callback(payload):
    dvt.task.schedule(second_step)

dvt.events.on(dvt.TaskEvent.DONE, callback)
```

> **NOTE - Known limitations with `dvt.task.run_blocking(func_or_coro, *args, **kwargs)`:**
>
> - **`map_blocking(..., return_exceptions=True)`** does not raise on individual item failures. Instead, the returned list contains the exception object in place of that item's result, at the same index it would otherwise occupy — order is always preserved, matching the input `items` order. When `return_exceptions=False` (the default), the first failing item raises immediately and stops the remaining work.
>
>  ```python
>  results = dvt.task.map_blocking(
>      risky_fetch,
>      urls,
>      return_exceptions=True,
>  )
>
>  for url, result in zip(urls, results):
>      if isinstance(result, Exception):
>          log.warning(f"{url} failed: {result}")
>      else:
>          process(result)
>  ```
>
> - **`map_blocking(..., return_exceptions=False)`** stops waiting as soon as the first item raises, but does not cancel the other items already running in the threadpool — they continue to execute with their results discarded. This mirrors `asyncio.gather(..., return_exceptions=False)`'s own behavior. If you need the remaining work stopped, catch the exception yourself and call `dvt.shutdown()` / cancel outstanding futures explicitly.
>
> - **`retries` / `retry_backoff` / `rate_limit_per_sec` only apply to sync callable execution paths** — `to_thread()` and callable-based `schedule()`/`managed()`. A coroutine object or async function passed to `schedule()` (or wrapped with `managed()`) is **not** retried and is **not** throttled by the rate limiter, regardless of instance configuration. See [Retries, timeouts, and rate limits](#retries-timeouts-and-rate-limits).
>
> - **`set_app_shutdown_hook(fn)` is not wired into any automatic shutdown trigger.** Nothing in Dovetail calls `global_registry.call_app_hook_or_shutdown()` on its own — each `Dovetail` instance's own `atexit`/signal handling (when `shutdown_on_exit=True`) calls that instance's `shutdown()` directly, bypassing the hook entirely. If you want the hook to actually run during process exit, you're responsible for calling `call_app_hook_or_shutdown()` yourself (e.g. from your own `atexit.register(...)`), typically alongside `shutdown_on_exit=False` on your `Dovetail` instances so they don't shut down independently first.


### Lifecycle

- `dvt.shutdown(wait=True)` — shut down the internal threadpool. Idempotent — safe to call more than once, including after an automatic exit-time shutdown has already run. When `wait=True`, blocks until all in-flight threadpool work finishes; `wait=False` returns immediately without waiting. Called automatically at interpreter exit / on `SIGINT`/`SIGTERM` if `shutdown_on_exit=True` is passed to the constructor.


### Events

- `dvt.events.on(dvt.TaskEvent.QUEUED, ...)` — fires when a task is queued.
- `dvt.events.on(dvt.TaskEvent.STARTED, ...)` — fires when a task starts executing.
- `dvt.events.on(dvt.TaskEvent.RATE_LIMITED, ...)` — fires when a task start is delayed by the token-bucket rate limiter.
- `dvt.events.on(dvt.TaskEvent.RETRY, ...)` — fires on each retry attempt.
- `dvt.events.on(dvt.TaskEvent.ERROR, ...)` — fires when a task raises an exception.
- `dvt.events.on(dvt.TaskEvent.CANCELLED, ...)` — fires when a task is cancelled.
- `dvt.events.on(dvt.TaskEvent.DONE, ...)` — fires when a task resolves (including after retries).
- `dvt.events.off(...)` — unregister a listener by subscription id or by passing the decorated listener function directly.
- `dvt.events.clear()` — remove all registered listeners.
- `dvt.events.stats()` — returns cumulative counters: `queued`, `started`, `done`, `error`, `retries`, `throttled`.
- `dvt.events.trace(message)` / `dvt.events.trace_struct(...)` — emit your own trace lines through the same tracing pipeline Dovetail uses internally. See [Tracing & Custom Instrumentation](#tracing--custom-instrumentation).
- `dvt.events.inc_stat(key, count=1)` — increment a custom entry in the same counters dict returned by `stats()`. See [Tracing & Custom Instrumentation](#tracing--custom-instrumentation).

`dvt.events.on()` can be used either directly:

```python
subscription = dvt.events.on(dvt.TaskEvent.DONE, callback)
```

or as a decorator:

```python
@dvt.events.on(dvt.TaskEvent.DONE)
def callback(event):
    print("Task completed!")
```

Decorated listeners can later be removed simply by passing the function:

```python
dvt.events.off(callback)
```

or, if you registered directly, by using the returned subscription id:

```python
dvt.events.off(subscription)
```

> `dvt.events.on()` accepts these optional scope parameters:
> - `function_target` — only receive events for a specific callable.
> - `execution_target` — only receive events for a specific execution id (available as `execution_id` in the event payload).
> - `allow_reentry=False` — prevents a listener from triggering itself recursively. Set to `True` to allow reentry, bounded by `max_chain_depth`.
> - `max_chain_depth=5` — maximum active callback depth when reentry is enabled.


### Registry

- `dovetail` now exposes a lightweight registry to track active `Dovetail` instances. By default `Dovetail(...)` (or explicitly, `Dovetail(auto_register=True, ...)`) will register itself with the package registry so applications can perform coordinated shutdown.
- Public helpers:
  - `from pydovetail import register, unregister, list_active, shutdown, set_app_shutdown_hook`
  - `register(dvt)` — explicitly register an instance (optional; instances auto-register by default).
  - `unregister(dvt)` — remove from the registry (useful if you manage shutdown yourself).
  - `list_active()` — return a list of active (live) instances.
```python
from pydovetail import list_active

# List active instances
print(list_active())
```
  - `shutdown(wait=True)` — best-effort shutdown of all registered instances (useful for app shutdown hooks).
```python
from pydovetail import shutdown

# Application shutdown
shutdown()
```
  - `set_app_shutdown_hook(fn)` — provide an application-level hook that the registry will call during process exit.

>**NOTE - Registry Usage:**
> - The registry participates in coordinated shutdown through `shutdown()`. Individual instances also install their own lifecycle hooks if `shutdown_on_exit=True` is specified.
>
> Sync context manager (recommended for synchronous code):
> ```python
> with Dovetail(max_workers=8) as dvt:
>     results = dvt.task.map_blocking(fetch, items)
> # threadpool shut down on block exit
> ```
>
> Async context manager (recommended for async code):
> ```python
> async def main():
>     async with Dovetail() as dvt:
>         tasks = [dvt.task.schedule(coro(i)) for i in items]
>         await asyncio.gather(*tasks)
> # __aexit__ runs shutdown off the event loop to avoid blocking
> ```
>
> **The registry stores weak references, so registering an instance does not
> prevent garbage collection. Dead entries are automatically ignored and
> cleaned up when the registry is queried.**
>
> **If a `Dovetail` instance is collected without an explicit `shutdown()`,
> Dovetail may emit a warning depending on the configured lifecycle hooks.**

---

## Observability & Behavior

Observability is exposed via lifecycle events, cumulative counters, and opt-in structured tracing. Use listeners for reactive behavior and `dvt.events.stats()` for quick diagnostics.

- **Counters (use `dvt.events.stats()`):**
  - **queued:** task accepted for execution (one per scheduling attempt).
  - **started:** execution attempt started (incremented for each retry attempt).
  - **done:** task completed successfully (after retries, if any).
  - **error:** task failed permanently (including timeouts after retries are exhausted).
  - **retries:** retry attempts performed (each retry increments this counter).
  - **throttled:** increments when a rate limiter delays a start (i.e. non-zero wait). Pairs with `dvt.events.on(dvt.TaskEvent.RATE_LIMITED, ...)` if you also want to react to individual throttling events rather than just read the cumulative count.
>
  Example — checking counters after a batch job:

  ```python
  with Dovetail(max_workers=4) as dvt:
      dvt.task.map_blocking(process_item, items)

      stats = dvt.events.stats()
      if stats["error"]:
          log.warning(f"{stats['error']} of {stats['queued']} items failed")
  ```
>
- **Event payloads:** lifecycle events carry a payload dict with common fields: `method`, `task`, `function`, `execution_id`. Event-specific fields may include `attempt`, `elapsed`, `error`, `sleep`, and `waited`.
>
- **Rate limiting:** Dovetail uses a token-bucket strategy to control task-start throughput on sync callable execution paths (`to_thread` and callable-based `schedule()`) — it does not throttle `schedule()` calls given a coroutine object or async function directly. When `rate_limit_per_sec` and optional `rate_limit_burst` are set, `acquire_async()` may return a non-zero `waited` value; such delays increment `throttled` and emit a `task_rate_limited` event with `waited` in the payload — subscribe to it directly with `dvt.events.on(dvt.TaskEvent.RATE_LIMITED, ...)`.
>
- **Retries & timeouts:** Timeouts apply to waiting for execution, not forcibly cancelling execution. For threadpool tasks, a timeout raises an error to the caller while the underlying function may continue running in the background. Avoid using timeouts with non-idempotent operations unless you have your own cancellation or deduplication strategy.
>
- **Tracing (opt-in):** Enable structured trace logging with `trace=True` or `DOVETAIL_TRACE=true`. Traces include method, function, thread information, and elapsed times and are intended for short-lived diagnostics rather than high-volume production telemetry.
>
- **Practical patterns / runbook:**
  - For low overhead, emit structured logs (JSON) with `execution_id` / `task` and query them in your logging backend.
  - For detailed forensics, record events to a secondary store using an async/batched writer (sampling or "record-on-error" reduces runtime cost).
  - To debug an incident: (1) check `dvt.events.stats()` for anomalies; (2) enable tracing for one instance or a time window; (3) attach `on(dvt.TaskEvent.ERROR, ...)` to capture payloads for failed executions.

For deeper examples and usage patterns see the **[Event Listeners](#event-listeners)**, **[Retries & Rate Limiting](#retries--rate-limiting)**, and **[Tracing & Custom Instrumentation](#tracing--custom-instrumentation)** sections below.

---

## Event Listeners

Event listeners are the primary way to observe lifecycle events and trigger follow-up behavior.

### Lifecycle methods

- `dvt.events.on(dvt.TaskEvent.QUEUED, ...)` -> `task_queued`
- `dvt.events.on(dvt.TaskEvent.STARTED, ...)` -> `task_started`
- `dvt.events.on(dvt.TaskEvent.RETRY, ...)` -> `task_retry`
- `dvt.events.on(dvt.TaskEvent.RATE_LIMITED, ...)` -> `task_rate_limited`
- `dvt.events.on(dvt.TaskEvent.ERROR, ...)` -> `task_error`
- `dvt.events.on(dvt.TaskEvent.CANCELLED, ...)` -> `task_cancelled`
- `dvt.events.on(dvt.TaskEvent.DONE, ...)` -> `task_done`

Each method registers a subscription and returns a subscription id.

### Scoping

- No target: global listener for all matching events.
- `function_target=<callable>`: function-scoped listener. This is the most common and recommended pattern.
- `execution_target=<execution_id>`: instance-scoped listener. This is advanced and usually only needed for per-instance control.
- Passing both `function_target` and `execution_target` raises `ValueError`.

### Reentry and depth

- `allow_reentry=False` (default): blocks callback reentry while callback is active.
- `allow_reentry=True`: allows nested callback reentry.
- `max_chain_depth`: maximum nested callback depth when reentry is enabled.

### Example

```python
import asyncio
from pydovetail import Dovetail

dvt = Dovetail()

def step_a():
  # Stage A: starting point of the workflow.
  return "A"

def step_b():
  # Stage B: follow-up work triggered after A completes.
  return "B"

captured_instance_ids = []

def on_any_done(payload):
  # Global listener: fires for every completed task.
  print("GLOBAL done:", payload.get("function"), payload.get("execution_id"))

def on_a_start(payload):
  # Function-scoped listener: capture execution ids for step_a instances.
  captured_instance_ids.append(payload.get("execution_id"))

def on_a_end(payload):
  # Function-scoped chaining: when A ends, schedule B.
  dvt.task.schedule(step_b)

# Global listener
# Useful for telemetry/logging across all functions.
dvt.events.on(dvt.TaskEvent.DONE, on_any_done)

# Function-scoped listeners
# Most common orchestration style.
dvt.events.on(dvt.TaskEvent.STARTED, on_a_start, function_target=step_a)
dvt.events.on(dvt.TaskEvent.DONE, on_a_end, function_target=step_a)

async def main():
  task = dvt.task.schedule(step_a)
  await task
  await asyncio.sleep(0.05)

  # Optional instance-scoped listener (advanced/rare)
  if captured_instance_ids:
    one_instance_id = captured_instance_ids[0]
    dvt.events.on(dvt.TaskEvent.DONE, 
      lambda p: print("INSTANCE done:", p.get("function"), p.get("execution_id")),
      execution_target=one_instance_id,
    )

asyncio.run(main())
```

### Reacting to queueing, retries, and cancellation

`on(dvt.TaskEvent.STARTED, ...)` and `on(dvt.TaskEvent.DONE, ...)` are the most common listeners, but the same pattern applies to every lifecycle dvt.TaskEvent. A few worked examples:

```python
def unreliable_upload():
    # Simulates a call that sometimes fails before succeeding.
    ...

# React to every retry attempt — useful for surfacing flakiness in a
# dependency without waiting for it to exhaust retries and fail outright.
def on_upload_retry(payload):
    print(f"Retrying {payload['function']} (attempt {payload['attempt']}), "
          f"sleeping {payload['sleep']:.2f}s after: {payload['error']}")

dvt.events.on(dvt.TaskEvent.RETRY, on_upload_retry, function_target=unreliable_upload)

# React to a task being queued — useful for tracking queue depth or
# backpressure before anything has actually started.
def on_any_queued(payload):
    queue_depth_metric.increment()

dvt.events.on(dvt.TaskEvent.QUEUED, on_any_queued)

# React to cancellation — useful for cleanup when a scheduled task is
# cancelled before it completes (e.g. on shutdown or timeout elsewhere).
def on_cancelled(payload):
    print(f"Task {payload['execution_id']} was cancelled before completion")

dvt.events.on(dvt.TaskEvent.CANCELLED, on_cancelled)

# React to rate-limit throttling — useful for alerting when you're
# consistently hitting your own configured rate limit.
def on_throttled(payload):
    print(f"Throttled {payload['function']}, waited {payload['waited']:.2f}s")

dvt.events.on(dvt.TaskEvent.RATE_LIMITED, on_throttled)
```

> ### Common pitfall
> ```python
> # Incorrect:
> dvt.events.on(dvt.TaskEvent.DONE, dvt.task.schedule(step_b), function_target=step_a)
> ```
> 
> Why it fails:
> 
> - `on(dvt.TaskEvent.DONE, ...)` expects a callback function as first argument.
> - `dvt.task.schedule(step_b)` executes immediately and returns an `asyncio.Task`.
> - `asyncio.Task` is not callable, so listener registration fails.
> 
> Correct pattern:
> 
> ```python
> def on_a_end(payload):
>   dvt.task.schedule(step_b)
> 
> dvt.events.on(dvt.TaskEvent.DONE, on_a_end, function_target=step_a)
> ```

---

## Retries, timeouts, and rate limits

This guide explains how execution behavior defaults work and how they interact.

### Constructor defaults

Set behavior globally per `Dovetail` instance (defaults shown):

- `max_workers` — threadpool size (default: ThreadPoolExecutor default)
- `retries` — number of retry attempts (default: 0)
- `retry_backoff` — base backoff in seconds (default: 0.0)
- `timeout` — per-call timeout in seconds (default: None — no timeout)
- `rate_limit_per_sec` — rate-limit tokens per second (default: None — disabled)
- `rate_limit_burst` — token-bucket burst capacity (default: None — auto)

See the **[Constructor Reference](#constructor-reference)** for the full parameter list, including tracing and lifecycle options not covered here.

Example:

```python
from pydovetail import Dovetail

dvt = Dovetail(
  retries=2,
  retry_backoff=0.25,
  timeout=30.0,
  rate_limit_per_sec=20,
  rate_limit_burst=40,
)
```

---

### Retries

Retries apply to sync callable execution paths in `task.to_thread(...)` and callable scheduling paths.

Behavior:

- Retries happen after failures until retry budget is exhausted.
- Backoff uses exponential growth from `retry_backoff`.
- Retry attempts increment `retries` counter and emit `task_retry`.

### Timeouts

Timeouts apply to async waiting around task execution.

Behavior:

- Timeout raises `asyncio.TimeoutError` to caller.
- Timeout increments `error` counter and emits `task_error`.
- Timing out a wait does not force-stop a currently running thread function.

### Rate limiting

Rate limiting controls task start throughput using a token-bucket strategy. Like retries, it applies to sync callable execution paths in `task.to_thread(...)` and callable scheduling paths — it does not apply when `task.schedule(...)` is given a coroutine object or an async function directly; those start immediately, uncapped by `rate_limit_per_sec`.

Behavior:

- Starts may be delayed when token budget is exhausted.
- Delays increment `throttled` counter.
- Rate-limited starts emit `task_rate_limited` with `waited` duration — subscribe with `dvt.events.on(dvt.TaskEvent.RATE_LIMITED, ...)`.

### Tuning suggestions

- Increase `retries` for transient network or API instability.
- Keep `retry_backoff` non-zero to reduce pressure during repeated failures.
- Use `timeout` to prevent indefinitely waiting call paths.
- Set `rate_limit_per_sec` and `rate_limit_burst` to protect upstream services.

---

## Tracing & Custom Instrumentation

Beyond the built-in `trace=True` flag (which logs Dovetail's own internal lifecycle), three methods let you fold your own diagnostics into the same pipeline:

- **`dvt.events.trace(message)`** — emit a single-line debug trace, gated by the same `trace`/`DOVETAIL_TRACE` flag as Dovetail's internal tracing. Useful for adding your own checkpoints without standing up a separate logger.

  ```python
  with Dovetail(trace=True, trace_prefix="Ingest") as dvt:
      dvt.events.trace("Starting batch of 500 records")
      dvt.task.map_blocking(process_record, records)
      dvt.events.trace("Batch complete")
  ```

- **`dvt.events.trace_struct(method, status, task=None, function=None, elapsed=None, extra=None)`** — emit a multi-line structured trace record in the same format Dovetail uses for its own task lifecycle traces. Useful when you want your custom checkpoints to look consistent with the built-in ones in log output.

  ```python
  dvt.events.trace_struct(
      method="ingest_batch",
      status="Start",
      extra={"Records": len(records)},
  )
  ```

- **`dvt.events.inc_stat(key, count=1)`** — increment a counter in the same dict returned by `stats()`. Missing keys are created automatically, so you can track your own custom counters alongside the built-in ones (`queued`, `started`, `done`, `error`, `retries`, `throttled`).

  ```python
  with Dovetail() as dvt:
      for record in records:
          if is_duplicate(record):
              dvt.events.inc_stat("duplicates_skipped")
              continue
          dvt.task.schedule(process_record, record)

      print(dvt.events.stats())
      # {'queued': 40, 'started': 40, 'done': 40, 'error': 0,
      #  'retries': 0, 'throttled': 0, 'duplicates_skipped': 10}
  ```

Both `trace()` and `trace_struct()` route through the same logger and gating flag as Dovetail's internal traces (`trace_logger`/`trace_prefix` from the constructor), so your custom checkpoints appear interleaved with, and formatted like, the built-in ones — set `trace_logger` to a dedicated logger if you want to filter your application's own log level independently of Dovetail's (see the **[Constructor Reference](#constructor-reference)** example).