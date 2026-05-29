# Detailed Guides

## Table of Contents

- [Observability & Behavior](#observability--behavior)
- [Event Listeners](#event-listeners)
- [Retry, timeout, and rate limit](#retry-timeout-and-rate-limit)
- [Benchmarks](#benchmarks)

## Observability & Behavior

Observability is exposed via lifecycle events, cumulative counters, and opt-in structured tracing. Use listeners for reactive behavior and `dvt.events.stats()` for quick diagnostics.

- **Counters (use `dvt.events.stats()`):**
  - **queued:** task accepted for execution (one per scheduling attempt).
  - **started:** execution attempt started (incremented for each retry attempt).
  - **done:** task completed successfully (after retries, if any).
  - **error:** task failed permanently (including timeouts after retries are exhausted).
  - **retries:** retry attempts performed (each retry increments this counter).
  - **throttled:** increments when a rate limiter delays a start (i.e. non-zero wait).

- **Event payloads:** lifecycle events carry a payload dict with common fields: `method`, `task`, `function`, `execution_id`. Event-specific fields may include `attempt`, `elapsed`, `error`, `sleep`, and `waited`.

- **Rate limiting:** Dovetail uses a token-bucket strategy to control task-start throughput. When `rate_limit_per_sec` and optional `rate_limit_burst` are set, `acquire_async()` may return a non-zero `waited` value; such delays increment `throttled` and emit a `task_rate_limited` event with `waited` in the payload.

- **Retries & timeouts:** Retries apply to synchronous callable execution paths (the threadpool and callable scheduling). Each retry increments `retries` and `started` for the new attempt. Timeouts increment `error` and emit `task_error` but do not forcibly stop an underlying thread; callers should treat timeouts as advisory and design idempotent functions where appropriate.

- **Tracing (opt-in):** Enable structured trace logging with `trace=True` or `DOVETAIL_TRACE=true`. Traces include method, function, thread information, and elapsed times and are intended for short-lived diagnostics rather than high-volume production telemetry.

- **Practical patterns / runbook:**
  - For low overhead, emit structured logs (JSON) with `execution_id` / `task` and query them in your logging backend.
  - For detailed forensics, record events to a secondary store using an async/batched writer (sampling or "record-on-error" reduces runtime cost).
  - To debug an incident: (1) check `dvt.events.stats()` for anomalies; (2) enable tracing for one instance or a time window; (3) attach `on_error` to capture payloads for failed executions.

For deeper examples and usage patterns see the **Event Listeners** and **Retries & Rate Limiting** sections below.


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

```py
import asyncio
from dovetail import Dovetail

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

### Common pitfall

```py
# Incorrect:
dvt.events.on_end(dvt.task.schedule(step_b), function_target=step_a)
```

Why it fails:

- `on_end(...)` expects a callback function as first argument.
- `dvt.task.schedule(step_b)` executes immediately and returns an `asyncio.Task`.
- `asyncio.Task` is not callable, so listener registration fails.

Correct pattern:

```py
def on_a_end(payload):
  dvt.task.schedule(step_b)

dvt.events.on_end(on_a_end, function_target=step_a)
```


## Retry, timeout, and rate limit

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

```py
from dovetail import Dovetail

dvt = Dovetail(
  default_retries=2,
  default_retry_backoff=0.25,
  default_timeout=30.0,
  rate_limit_per_sec=20,
  rate_limit_burst=40,
)
```

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


## Benchmarks

Dovetail is benchmarked against a plain serial for-loop across three workload shapes. Each "task" sleeps for a fixed duration to simulate real I/O (a network request, a file read, a database query).

**Reading the table:**
- **Median time** — wall-clock time to finish all tasks, median across runs to smooth out noise.
- **vs serial** — speedup over the plain for-loop. Higher is better.
- **vs ideal** — distance from the physical ceiling. The ideal is one task duration — if all tasks ran simultaneously, you'd never wait longer than the slowest one. 1.0x is perfect; `map_blocking` lands near `ceil(tasks / workers)` because that's how many rounds a bounded threadpool needs.

Run the benchmarks yourself:

```bash
python -m benchmark
python -m benchmark --tasks 100 --duration 0.1 --workers 10 --runs 5
```

---

### Default — 20 tasks x 100ms

The clean intro. Serial takes exactly what you'd predict (20 x 100ms).
`map_blocking` batches into `ceil(20/8) = 3` rounds. `async gather` nearly
hits the theoretical ceiling.

```
  Mode                        Median time   vs serial    vs ideal
  ----------------------------------------------------------------
  Serial                         2006.1ms       1.0x      20.1x  ██████████████████████████████
  map_blocking (8 workers)        304.3ms       6.6x       3.0x  ████
  async gather                    107.6ms      18.7x       1.1x  █

  Theoretical minimum (all tasks overlap): 100ms
```

---

### Bounded threadpool — 100 tasks x 100ms, 10 workers

`ceil(100/10) = 10` rounds, and `map_blocking` lands at 1012ms — almost
exactly 10 x 100ms. The implementation adds near-zero overhead on top of
the raw threadpool math.

```
  Mode                         Median time   vs serial    vs ideal
  -----------------------------------------------------------------
  Serial                         10026.4ms       1.0x     100.3x  ██████████████████████████████
  map_blocking (10 workers)       1012.3ms       9.9x      10.1x  ███
  async gather                     109.9ms      91.2x       1.1x  

  Theoretical minimum (all tasks overlap): 100ms
```

---

### High task count — 500 tasks x 5ms, 50 workers

At very short task durations, event loop scheduling overhead becomes
significant relative to the actual work. `async gather`'s "vs ideal" climbs
to 4.2x — meaning it now takes 4x longer than the theoretical minimum just
to schedule 500 coroutines. `map_blocking` is less dramatic but more
predictable: its overhead is bounded by the threadpool math, not coroutine
scheduling.

```
  Mode                         Median time   vs serial    vs ideal
  -----------------------------------------------------------------
  Serial                          2684.7ms       1.0x     536.9x  ██████████████████████████████
  map_blocking (50 workers)        114.1ms      23.5x      22.8x  █
  async gather                      20.9ms     128.3x       4.2x  

  Theoretical minimum (all tasks overlap): 5ms
```

> **Takeaway:** `async gather` wins for long-duration I/O (network calls,
> slow DB queries). `map_blocking` is more predictable for short-duration
> work or when calling from sync code.