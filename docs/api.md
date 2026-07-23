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
| `max_workers` | `None` (ThreadPoolExecutor default) | Size of the internal threadpool used by `to_thread`, `to_thread_blocking`, and `map_blocking`. |
| `trace` | `False` | Enables structured trace logging for this instance. Can also be enabled globally via the `DOVETAIL_TRACE` environment variable (`1`, `true`, `yes`, or `on`). |
| `trace_logger` | `None` (falls back to `logging.getLogger("dovetail")`) | The `logging.Logger` traces are written to. By default, Dovetail tracing uses the dovetail logger and propagates records to the application's logging configuration. Pass a dedicated logger if you want Dovetail's trace output separated from your application's own log lines — see **[Tracing & Custom Instrumentation](#tracing--custom-instrumentation)**. |
| `trace_prefix` | `"Dovetail"` | Short label included in every trace line (rendered as `DVT-<prefix>`). Useful for telling apart multiple `Dovetail` instances in the same log stream. |
| `rate_limit_per_sec` | `None` (disabled) | Token-bucket refill rate. When set, task starts may be delayed to stay under this rate. |
| `rate_limit_burst` | `None` (defaults to `rate_limit_per_sec`) | Token-bucket burst capacity — the number of tasks that can start immediately before throttling kicks in. |
| `timeout` | `None` (no timeout) | Per-call timeout in seconds, applied to `to_thread`, `schedule`, and `run_blocking` coroutine paths. |
| `retries` | `0` | Number of retry attempts for synchronous callable execution paths (`to_thread` and callable-based `schedule`). |
| `retry_backoff` | `0.0` | Base backoff in seconds; grows exponentially per retry attempt. |
| `shutdown_on_exit` | `True` | Registers process-level cleanup handlers which perform best-effort shutdown during interpreter exit and termination signals. |
| `auto_register` | `True` | Automatically registers the instance with the package-level registry (see **[Registry](#registry)**) so it appears in `list_active()` and is covered by `shutdown_all()`. Instances are stored using weak references, so registration does not extend the lifetime of a `Dovetail` object. |

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
- an `Event.ERROR` lifecycle event is emitted;
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
    Event.DONE,
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
    Event.DONE,
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

dvt.events.on(Event.DONE, callback)
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


### Lifecycle

- `dvt.shutdown(wait=True)` — shut down the internal threadpool. Idempotent — safe to call more than once, including after an automatic exit-time shutdown has already run. When `wait=True`, blocks until all in-flight threadpool work finishes; `wait=False` returns immediately without waiting. Called automatically at interpreter exit / on `SIGINT`/`SIGTERM` unless `shutdown_on_exit=False` was passed to the constructor.


### Events

- `dvt.events.on(Event.QUEUED, ...)` — fires when a task is queued.
- `dvt.events.on(Event.STARTED, ...)` — fires when a task starts executing.
- `dvt.events.on(Event.RATE_LIMITED, ...)` — fires when a task start is delayed by the token-bucket rate limiter.
- `dvt.events.on(Event.RETRY, ...)` — fires on each retry attempt.
- `dvt.events.on(Event.ERROR, ...)` — fires when a task raises an exception.
- `dvt.events.on(Event.CANCELLED, ...)` — fires when a task is cancelled.
- `dvt.events.on(Event.DONE, ...)` — fires when a task resolves (including after retries).
- `dvt.events.off(...)` — unregister a listener by subscription id or by passing the decorated listener function directly.
- `dvt.events.clear()` — remove all registered listeners.
- `dvt.events.stats()` — returns cumulative counters: `queued`, `started`, `done`, `error`, `retries`, `throttled`.
- `dvt.events.trace(message)` / `dvt.events.trace_struct(...)` — emit your own trace lines through the same tracing pipeline Dovetail uses internally. See [Tracing & Custom Instrumentation](#tracing--custom-instrumentation).
- `dvt.events.inc_stat(key, count=1)` — increment a custom entry in the same counters dict returned by `stats()`. See [Tracing & Custom Instrumentation](#tracing--custom-instrumentation).

`dvt.events.on()` can be used either directly:

```python
subscription = dvt.events.on(Event.DONE, callback)
```

or as a decorator:

```python
@dvt.events.on(Event.DONE)
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

- `dovetail` now exposes a lightweight registry to track active `Dovetail` instances. By default `Dovetail(...)` (or explicitly, `Dovetail(..., auto_register=True)`) will register itself with the package registry so applications can perform coordinated shutdown.
- Public helpers:
  - `from pydovetail import register, unregister, list_active, shutdown_all, set_app_shutdown_hook`
  - `register(dvt)` — explicitly register an instance (optional; instances auto-register by default).
  - `unregister(dvt)` — remove from the registry (useful if you manage shutdown yourself).
  - `list_active()` — return a list of active (live) instances.
```python
from pydovetail import list_active

# List active instances
print(list_active())
```
  - `shutdown_all(wait=True)` — best-effort shutdown of all registered instances (useful for app shutdown hooks).
```python
from pydovetail import shutdown_all

# Application shutdown
shutdown_all()
```
  - `set_app_shutdown_hook(fn)` — provide an application-level hook that the registry will call during process exit.

>**NOTE - Registry Usage:**
> - The registry participates in coordinated shutdown through `shutdown_all()`. Individual instances also install their own lifecycle hooks unless `shutdown_on_exit=False` is specified.
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
  - **throttled:** increments when a rate limiter delays a start (i.e. non-zero wait). Pairs with `dvt.events.on(Event.RATE_LIMITED, ...)` if you also want to react to individual throttling events rather than just read the cumulative count.
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
- **Rate limiting:** Dovetail uses a token-bucket strategy to control task-start throughput. When `rate_limit_per_sec` and optional `rate_limit_burst` are set, `acquire_async()` may return a non-zero `waited` value; such delays increment `throttled` and emit a `task_rate_limited` event with `waited` in the payload — subscribe to it directly with `dvt.events.on(Event.RATE_LIMITED, ...)`.
>
- **Retries & timeouts:** Timeouts apply to waiting for execution, not forcibly cancelling execution. For threadpool tasks, a timeout raises an error to the caller while the underlying function may continue running in the background. Avoid using timeouts with non-idempotent operations unless you have your own cancellation or deduplication strategy.
>
- **Tracing (opt-in):** Enable structured trace logging with `trace=True` or `DOVETAIL_TRACE=true`. Traces include method, function, thread information, and elapsed times and are intended for short-lived diagnostics rather than high-volume production telemetry.
>
- **Practical patterns / runbook:**
  - For low overhead, emit structured logs (JSON) with `execution_id` / `task` and query them in your logging backend.
  - For detailed forensics, record events to a secondary store using an async/batched writer (sampling or "record-on-error" reduces runtime cost).
  - To debug an incident: (1) check `dvt.events.stats()` for anomalies; (2) enable tracing for one instance or a time window; (3) attach `on(Event.ERROR, ...)` to capture payloads for failed executions.

For deeper examples and usage patterns see the **[Event Listeners](#event-listeners)**, **[Retries & Rate Limiting](#retries--rate-limiting)**, and **[Tracing & Custom Instrumentation](#tracing--custom-instrumentation)** sections below.

---

## Event Listeners

Event listeners are the primary way to observe lifecycle events and trigger follow-up behavior.

### Lifecycle methods

- `dvt.events.on(Event.QUEUED, ...)` -> `task_queued`
- `dvt.events.on(Event.STARTED, ...)` -> `task_started`
- `dvt.events.on(Event.RETRY, ...)` -> `task_retry`
- `dvt.events.on(Event.RATE_LIMITED, ...)` -> `task_rate_limited`
- `dvt.events.on(Event.ERROR, ...)` -> `task_error`
- `dvt.events.on(Event.CANCELLED, ...)` -> `task_cancelled`
- `dvt.events.on(Event.DONE, ...)` -> `task_done`

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
dvt.events.on(Event.DONE, on_any_done)

# Function-scoped listeners
# Most common orchestration style.
dvt.events.on(Event.STARTED, on_a_start, function_target=step_a)
dvt.events.on(Event.DONE, on_a_end, function_target=step_a)

async def main():
  task = dvt.task.schedule(step_a)
  await task
  await asyncio.sleep(0.05)

  # Optional instance-scoped listener (advanced/rare)
  if captured_instance_ids:
    one_instance_id = captured_instance_ids[0]
    dvt.events.on(Event.DONE, 
      lambda p: print("INSTANCE done:", p.get("function"), p.get("execution_id")),
      execution_target=one_instance_id,
    )

asyncio.run(main())
```

### Reacting to queueing, retries, and cancellation

`on(Event.STARTED, ...)` and `on(Event.DONE, ...)` are the most common listeners, but the same pattern applies to every lifecycle event. A few worked examples:

```python
def unreliable_upload():
    # Simulates a call that sometimes fails before succeeding.
    ...

# React to every retry attempt — useful for surfacing flakiness in a
# dependency without waiting for it to exhaust retries and fail outright.
def on_upload_retry(payload):
    print(f"Retrying {payload['function']} (attempt {payload['attempt']}), "
          f"sleeping {payload['sleep']:.2f}s after: {payload['error']}")

dvt.events.on(Event.RETRY, on_upload_retry, function_target=unreliable_upload)

# React to a task being queued — useful for tracking queue depth or
# backpressure before anything has actually started.
def on_any_queued(payload):
    queue_depth_metric.increment()

dvt.events.on(Event.QUEUED, on_any_queued)

# React to cancellation — useful for cleanup when a scheduled task is
# cancelled before it completes (e.g. on shutdown or timeout elsewhere).
def on_cancelled(payload):
    print(f"Task {payload['execution_id']} was cancelled before completion")

dvt.events.on(Event.CANCELLED, on_cancelled)

# React to rate-limit throttling — useful for alerting when you're
# consistently hitting your own configured rate limit.
def on_throttled(payload):
    print(f"Throttled {payload['function']}, waited {payload['waited']:.2f}s")

dvt.events.on(Event.RATE_LIMITED, on_throttled)
```

> ### Common pitfall
> ```python
> # Incorrect:
> dvt.events.on(Event.DONE, dvt.task.schedule(step_b), function_target=step_a)
> ```
> 
> Why it fails:
> 
> - `on(Event.DONE, ...)` expects a callback function as first argument.
> - `dvt.task.schedule(step_b)` executes immediately and returns an `asyncio.Task`.
> - `asyncio.Task` is not callable, so listener registration fails.
> 
> Correct pattern:
> 
> ```python
> def on_a_end(payload):
>   dvt.task.schedule(step_b)
> 
> dvt.events.on(Event.DONE, on_a_end, function_target=step_a)
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

Rate limiting controls task start throughput using a token-bucket strategy.

Behavior:

- Starts may be delayed when token budget is exhausted.
- Delays increment `throttled` counter.
- Rate-limited starts emit `task_rate_limited` with `waited` duration — subscribe with `dvt.events.on(Event.RATE_LIMITED, ...)`.

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