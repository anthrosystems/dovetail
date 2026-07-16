# Dovetail API

## Table of Contents
- [API Summary](#api-summary)
- [Registry](#registry)
- [Observability & Behavior](#observability--behavior)
- [Event Listeners](#event-listeners)
- [Retries, timeouts, and rate limits](#retries-timeouts-and-rate-limits)
- [Notes](#notes)

## API Summary

**Task execution**

- `dvt.task.to_thread(func, *args, **kwargs)` — await a sync callable in Dovetail's threadpool from async code; supports default timeout/retry settings.
- `dvt.task.to_thread_blocking(func, *args, **kwargs)` — synchronous wrapper for `to_thread` when you are not already in an event loop.
- `dvt.task.schedule(coro_or_callable, *args, **kwargs)` — create and return an `asyncio.Task` from a coroutine object, async function, or sync callable.
- `dvt.task.run_blocking(func_or_coro, *args, **kwargs)` — run async or sync work from synchronous code. Do not call from inside a running event loop.
- `dvt.task.map_blocking(func, items, max_concurrency=None, return_exceptions=False)` — run an ordered parallel map over `items` using the threadpool.

**Events**

- `dvt.events.on_queued(...)` — fires when a task is queued.
- `dvt.events.on_start(...)` — fires when a task starts executing.
- `dvt.events.on_end(...)` — fires when a task resolves (including after retries).
- `dvt.events.on_error(...)` — fires when a task raises an exception.
- `dvt.events.on_retry(...)` — fires on each retry attempt.
- `dvt.events.on_cancel(...)` — fires when a task is cancelled.
- `dvt.events.stats()` — returns cumulative counters: `queued`, `started`, `done`, `error`, `retries`, `throttled`.

## Registry 

- `dovetail` now exposes a lightweight registry to track active `Dovetail` instances. By default `Dovetail(..., auto_register=True)` will register itself with the package registry so applications can perform coordinated shutdown.
- Public helpers:
  - `from pydovetail import register, unregister, list_active, shutdown_all, set_app_shutdown_hook`
  - `register(dvt)` — explicitly register an instance (optional; instances auto-register by default).
  - `unregister(dvt)` — remove from the registry (useful if you manage shutdown yourself).
  - `list_active()` — return a list of active (live) instances.
  - `shutdown_all(wait=True)` — best-effort shutdown of all registered instances (useful for app shutdown hooks).
  - `set_app_shutdown_hook(fn)` — provide an application-level hook that the registry will call during process exit.

Notes:
- The registry uses weakrefs and logs a warning if a `Dovetail` is garbage-collected without an explicit `shutdown()` call.
- To opt-out of auto-registration when creating a `Dovetail`, pass `auto_register=False` to the constructor.

All `dvt.events.on_*` methods accept these optional scope parameters:

- `function_target` — only receive events for a specific callable.
- `instance_target` — only receive events for a specific execution id (available
  as `id` in the event payload).
- `allow_reentry=False` — prevents a listener from triggering itself recursively.
  Set to `True` to allow reentry, bounded by `max_chain_depth`.
- `max_chain_depth=5` — maximum active callback depth when reentry is enabled.

---

## Observability & Behavior

Observability is exposed via lifecycle events, cumulative counters, and opt-in structured tracing. Use listeners for reactive behavior and `dvt.events.stats()` for quick diagnostics.

- **Counters (use `dvt.events.stats()`):**
  - **queued:** task accepted for execution (one per scheduling attempt).
  - **started:** execution attempt started (incremented for each retry attempt).
  - **done:** task completed successfully (after retries, if any).
  - **error:** task failed permanently (including timeouts after retries are exhausted).
  - **retries:** retry attempts performed (each retry increments this counter).
  - **throttled:** increments when a rate limiter delays a start (i.e. non-zero wait).
>
- **Event payloads:** lifecycle events carry a payload dict with common fields: `method`, `task`, `function`, `execution_id`. Event-specific fields may include `attempt`, `elapsed`, `error`, `sleep`, and `waited`.
>
- **Rate limiting:** Dovetail uses a token-bucket strategy to control task-start throughput. When `rate_limit_per_sec` and optional `rate_limit_burst` are set, `acquire_async()` may return a non-zero `waited` value; such delays increment `throttled` and emit a `task_rate_limited` event with `waited` in the payload.
>
- **Retries & timeouts:** Retries apply to synchronous callable execution paths (the threadpool and callable scheduling). Each retry increments `retries` and `started` for the new attempt. Timeouts increment `error` and emit `task_error` but do not forcibly stop an underlying thread; callers should treat timeouts as advisory and design idempotent functions where appropriate.
>
- **Tracing (opt-in):** Enable structured trace logging with `trace=True` or `DOVETAIL_TRACE=true`. Traces include method, function, thread information, and elapsed times and are intended for short-lived diagnostics rather than high-volume production telemetry.
>
- **Practical patterns / runbook:**
  - For low overhead, emit structured logs (JSON) with `execution_id` / `task` and query them in your logging backend.
  - For detailed forensics, record events to a secondary store using an async/batched writer (sampling or "record-on-error" reduces runtime cost).
  - To debug an incident: (1) check `dvt.events.stats()` for anomalies; (2) enable tracing for one instance or a time window; (3) attach `on_error` to capture payloads for failed executions.

For deeper examples and usage patterns see the **Event Listeners** and **Retries & Rate Limiting** sections below.

---

## Event Listeners

Event listeners are the primary way to observe lifecycle events and trigger follow-up behavior.

### Lifecycle methods

- `dvt.events.on_queued(...)` -> `task_queued`
- `dvt.events.on_start(...)` -> `task_started`
- `dvt.events.on_end(...)` -> `task_done`
- `dvt.events.on_error(...)` -> `task_error`
- `dvt.events.on_retry(...)` -> `task_retry`
- `dvt.events.on_cancel(...)` -> `task_cancelled`

Each method registers a subscription and returns a subscription id.

### Scoping

- No target: global listener for all matching events.
- `function_target=<callable>`: function-scoped listener. This is the most common and recommended pattern.
- `instance_target=<execution_id>`: instance-scoped listener. This is advanced and usually only needed for per-instance control.
- Passing both `function_target` and `instance_target` raises `ValueError`.

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
dvt.events.on_end(on_any_done)

# Function-scoped listeners
# Most common orchestration style.
dvt.events.on_start(on_a_start, function_target=step_a)
dvt.events.on_end(on_a_end, function_target=step_a)

async def main():
  task = dvt.task.schedule(step_a)
  await task
  await asyncio.sleep(0.05)

  # Optional instance-scoped listener (advanced/rare)
  if captured_instance_ids:
    one_instance_id = captured_instance_ids[0]
    dvt.events.on_end(
      lambda p: print("INSTANCE done:", p.get("function"), p.get("execution_id")),
      instance_target=one_instance_id,
    )

asyncio.run(main())
```

> ### Common pitfall
> ```python
> # Incorrect:
> dvt.events.on_end(dvt.task.schedule(step_b), function_target=step_a)
> ```
> 
> Why it fails:
> 
> - `on_end(...)` expects a callback function as first argument.
> - `dvt.task.schedule(step_b)` executes immediately and returns an `asyncio.Task`.
> - `asyncio.Task` is not callable, so listener registration fails.
> 
> Correct pattern:
> 
> ```python
> def on_a_end(payload):
>   dvt.task.schedule(step_b)
> 
> dvt.events.on_end(on_a_end, function_target=step_a)
> ```

---

## Retries, timeouts, and rate limits

This guide explains how execution behavior defaults work and how they interact.

### Constructor defaults

Set behavior globally per `Dovetail` instance (defaults shown):

- `max_workers` — threadpool size (default: ThreadPoolExecutor default)
- `default_retries` — number of retry attempts (default: 0)
- `default_retry_backoff` — base backoff in seconds (default: 0.0)
- `default_timeout` — per-call timeout in seconds (default: None — no timeout)
- `rate_limit_per_sec` — rate-limit tokens per second (default: None — disabled)
- `rate_limit_burst` — token-bucket burst capacity (default: None — auto)

Example:

```python
from pydovetail import Dovetail

dvt = Dovetail(
  default_retries=2,
  default_retry_backoff=0.25,
  default_timeout=30.0,
  rate_limit_per_sec=20,
  rate_limit_burst=40,
)
```

---

### Retries

Retries apply to sync callable execution paths in `task.to_thread(...)` and callable scheduling paths.

Behavior:

- Retries happen after failures until retry budget is exhausted.
- Backoff uses exponential growth from `default_retry_backoff`.
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
- Rate-limited starts emit `task_rate_limited` with `waited` duration.

### Tuning suggestions

- Increase `default_retries` for transient network or API instability.
- Keep `default_retry_backoff` non-zero to reduce pressure during repeated failures.
- Use `default_timeout` to prevent indefinitely waiting call paths.
- Set `rate_limit_per_sec` and `rate_limit_burst` to protect upstream services.

---

## Notes:

`Dovetail` will, by default, register a best-effort shutdown handler that calls `shutdown()` on interpreter exit and when `SIGINT` or `SIGTERM` are received. This is opt-outable via `shutdown_on_exit=False`. Explicitly calling `dvt.shutdown()` is still supported and idempotent.

Sync context manager (recommended for synchronous code):
```python
with Dovetail(max_workers=8) as dvt:
    results = dvt.task.map_blocking(fetch, items)
# threadpool shut down on block exit
```

Async context manager (recommended for async code):
```python
async def main():
    async with Dovetail() as dvt:
        tasks = [dvt.task.schedule(coro(i)) for i in items]
        await asyncio.gather(*tasks)
# __aexit__ runs shutdown off the event loop to avoid blocking
```

Implementation note: `Dovetail` supports both `__enter__/__exit__` and `__aenter__/__aexit__`. The async exit runs `shutdown()` in a background executor to avoid blocking the running loop. `shutdown()` is idempotent and safe to call multiple times.

> ### **Context-manager performance note:**
> Microbenchmarks showed that constructing and using a Dovetail > instance through the context-manager form:
> 
> ```python
> with Dovetail(...) as dvt:
>     ...
> ```
> 
> can be marginally faster than manual lifecycle management:
> ```python
> dvt = Dovetail(...)
> try:
>     ...
> finally:
>     dvt.shutdown()
> ```
> 
> The difference is workload-dependent and small. The primary recommendation remains using the context manager because it guarantees cleanup during exceptions while also avoiding accidental resource leaks.