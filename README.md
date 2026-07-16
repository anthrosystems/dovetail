# Dovetail - Sync / Async wrapper for Python.

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml) 
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/pydovetail/) 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

A lightweight helper for bridging sync and async code, built on Asyncio & ThreadPoolExecutor.

## Table of Contents

- [Why Dovetail](#why-dovetail)
- [Install](#install)
- [Development](#development)
- [Quick Start](#quick-start)
- [API Summary](#api-summary)
- [Observability & Behavior](#observability--behavior)
- [Event Listeners](#event-listeners)
- [Retries, timeouts, and rate limits](#retries-timeouts-and-rate-limits)
- [Benchmark Usage](#benchmark-usage)
- [Benchmarks](#benchmarks)

## Why Dovetail?

Asyncio is powerful, but it has a cascading problem: the moment you want to
`await` something, that function has to be `async`, which means everything
calling it has to be `async`, and so on until your entire codebase has been
rewritten around it. If you're adding parallelism to an existing project, or
just want to speed up one specific bottleneck, that cost is hard to justify.

Dovetail's advantage isn't raw performance — `asyncio.gather` will always be
at least as fast, since Dovetail is built on top of it. The advantage of Dovetail is its
**surgical approach**: you can parallelise one function in an otherwise
synchronous codebase without touching anything else.

```python
# Existing sync code, completely unchanged around this one call
def process_report():
    data = dvt.task.map_blocking(fetch_url, urls)  # parallel, but sync caller
    return summarise(data)
```

With raw asyncio you can't do this — `fetch_url` being async forces
`process_report` to be async, which forces everything above it to be async.
Dovetail breaks that chain. If you're starting a greenfield project and are
comfortable with async/await throughout, you probably don't need it. Dovetail
is for the developers who are stuck between "I want parallelism" and "I don't want to rewrite everything."

---

## Install

```bash
pip install pydovetail
```

## Development

Run tests locally:

```bash
python -m pytest -q
```

Contributions are welcome, please open an issue or PR.


## Quick Start

Preferred usage: create and manage a `Dovetail` instance with a context manager.

Sync (recommended for most users):

```python
from pydovetail import Dovetail

with Dovetail(max_workers=8) as dvt:
  # Sync caller -> sync function (blocks)
  result = dvt.task.run_blocking(fetch_data)

  # Sync caller -> sync function in threadpool (blocks)
  result = dvt.task.to_thread_blocking(fetch_data, "target.json")

  # Sync caller -> batch map in threadpool (blocks, bounded concurrency)
  results = dvt.task.map_blocking(fetch_data, ["a.json", "b.json"], max_concurrency=4)

  # run an async coroutine from sync code
  value = dvt.task.run_blocking(fetch_data_async())
```

Async (use `async with` in async applications):

```python
async with Dovetail() as dvt:
  # schedule coroutines and await
  tasks = [dvt.task.schedule(fetch_async(i)) for i in items]
  results = await asyncio.gather(*tasks)
```

Manual / library patterns (caller-managed ownership):

```py
# Manual creation: by default Dovetail will attempt to shutdown on
# interpreter exit and on SIGINT/SIGTERM. You can opt out by passing
# `shutdown_on_exit=False` when constructing the instance.
dvt = Dovetail()  # shutdown_on_exit enabled by default
try:
  results = dvt.task.map_blocking(fetch, items)
finally:
  # explicit shutdown is still allowed and idempotent
  dvt.shutdown()

# Library-author pattern (prefer caller-managed instance when possible)
def process(items, dvt: Optional[Dovetail] = None):
  if dvt is None:
    with Dovetail() as _dvt:
      return _dvt.task.map_blocking(worker, items)
  return dvt.task.map_blocking(worker, items)
```

> **Note:** `Dovetail` will, by default, register a best-effort shutdown
> handler that calls `shutdown()` on interpreter exit and when `SIGINT` or
> `SIGTERM` are received. This is opt-outable via `shutdown_on_exit=False`.
> Explicitly calling `dvt.shutdown()` is still supported and idempotent.
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
> Implementation note: `Dovetail` supports both `__enter__/__exit__` and
> `__aenter__/__aexit__`. The async exit runs `shutdown()` in a background
> executor to avoid blocking the running loop. `shutdown()` is idempotent and
> safe to call multiple times.

---

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

**Registry (auto-registration)**

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

```py
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
> ```py
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
> ```py
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

```py
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

## Benchmark Usage

The Dovetail benchmark compares execution strategies under simulated I/O workloads. Each task performs a sleep-based operation to model latency-bound work such as network requests, database calls, filesystem operations, or API calls.

The benchmark compares:

1. **Serial execution**

   * Plain Python `for` loop.
   * One task runs at a time.
   * Used as the baseline.

2. **`map_blocking`**

   * Dovetail's bounded threadpool execution.
   * Designed for calling blocking synchronous functions from synchronous code.
   * Uses worker limits to control concurrency.

3. **`async gather`**

   * Fully concurrent async execution.
   * Uses `asyncio` scheduling through Dovetail.
   * Best represents native async workloads.

### Basic Usage

Run the default benchmark:

```bash
python benchmark.py
```

The default configuration runs:

* 20 tasks
* 100ms duration per task
* 8 worker threads
* 3 benchmark repetitions

The median runtime across repetitions is reported.

---

## Command Line Options

### `--tasks`

Number of jobs to execute.

```bash
python benchmark.py --tasks 100
```

Example:

```bash
python benchmark.py --tasks 1000
```

Useful for testing:

* scheduler overhead
* large task queues
* event dispatch performance
* memory usage with many concurrent tasks

Higher values stress orchestration rather than the actual workload.

---

### `--duration`

Duration of each simulated I/O task in seconds.

```bash
python benchmark.py --duration 0.05
```

Examples:

```bash
# Long I/O workload
python benchmark.py --tasks 500 --duration 1

# Short I/O workload
python benchmark.py --tasks 10000 --duration 0.001
```

The duration changes what is being measured:

| Duration | Measures                         |
| -------- | -------------------------------- |
| 1s+      | Pure concurrency scaling         |
| 10-100ms | Realistic API/network workloads  |
| <10ms    | Scheduler and framework overhead |

Very short tasks are intentionally difficult because the framework overhead becomes comparable to the actual work.

---

### `--workers`

Maximum number of threadpool workers used by `map_blocking`.

```bash
python benchmark.py --workers 50
```

Examples:

```bash
python benchmark.py --tasks 1000 --duration 0.1 --workers 10

python benchmark.py --tasks 1000 --duration 0.1 --workers 200
```

This controls the maximum parallelism for synchronous workloads.

Expected threadpool completion time:

```
ceil(tasks / workers) × duration
```

For example:

```
1000 tasks
100 workers
100ms each

ceil(1000 / 100) = 10 rounds

10 × 100ms = ~1000ms expected
```

Increasing workers improves throughput until thread scheduling overhead becomes larger than the work itself.

---

### `--runs`

Number of benchmark repetitions.

```bash
python benchmark.py --runs 10
```

The benchmark reports the median runtime.

Recommended values:

```bash
# Quick check
--runs 3

# More reliable comparison
--runs 5

# Performance testing
--runs 10+
```

More runs reduce noise caused by:

* OS scheduling
* CPU frequency changes
* background processes
* Python runtime variance

---

## Context Manager Testing

By default, the benchmark uses the context-manager form:

```python
with Dovetail(...) as dvt:
    ...
```

This automatically shuts down resources.

Disable it:

```bash
python benchmark.py --no-context-manager
```

This uses manual lifecycle management:

```python
dvt = Dovetail(...)

try:
    ...
finally:
    dvt.shutdown()
```

The two modes should produce similar results. The context-manager form is recommended because it guarantees cleanup.

---

## Comparing Context Manager Performance

Use:

```bash
python benchmark.py --compare
```

This runs both:

* context-manager usage
* manual shutdown usage

for concurrent workloads.

Example:

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.05 \
    --workers 50 \
    --compare
```

This is useful when investigating:

* lifecycle overhead
* shutdown behavior
* resource cleanup performance

The difference is normally small and workload-dependent.

---

## Observability Mode

Enable lifecycle event output:

```bash
python benchmark.py --observe
```

Example:

```bash
python benchmark.py \
    --observe \
    --tasks 5
```

This prints live Dovetail events:

```
queued
started
done
retry
error
cancelled
```

It also prints final counters:

```
queued=5
started=5
done=5
error=0
retries=0
throttled=0
```

Use smaller workloads because every event is printed individually.

Recommended:

```bash
python benchmark.py --observe --tasks 5
```

Avoid:

```bash
python benchmark.py --observe --tasks 100000
```

as output volume will dominate execution time.

---

## Trace Mode

Enable internal Dovetail tracing:

```bash
python benchmark.py --trace
```

Example:

```bash
python benchmark.py \
    --tasks 100 \
    --duration 0.1 \
    --trace
```

Trace output includes:

* execution identifiers
* scheduling information
* lifecycle transitions
* timing information

Use tracing for debugging rather than performance measurement.

Tracing adds overhead and should not be enabled when collecting benchmark numbers.

---

## Recommended Benchmark Profiles

### Quick Smoke Test

Checks that everything works:

```bash
python benchmark.py \
    --tasks 20 \
    --duration 0.1 \
    --workers 8
```

---

### Normal I/O Workload

Represents typical API/network/database workloads:

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.1 \
    --workers 50 \
    --runs 5
```

Expected behavior:

* serial execution takes roughly 100 seconds
* threadpool execution takes roughly 2 seconds
* async execution approaches the 100ms lower bound

---

### Threadpool Scaling Test

Measures worker scaling:

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.05 \
    --workers 1
```

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.05 \
    --workers 5
```

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.05 \
    --workers 50
```

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.05 \
    --workers 200
```

Expected:

* workers increase throughput
* gains eventually flatten
* excessive workers increase coordination overhead

---

### Large Queue Stress Test

Tests handling many scheduled tasks:

```bash
python benchmark.py \
    --tasks 10000 \
    --duration 0.05 \
    --workers 100 \
    --runs 3
```

This stresses:

* task creation
* scheduling
* event dispatch
* threadpool coordination

---

### Extreme Scheduler Stress Test

Tests framework overhead rather than I/O performance:

```bash
python benchmark.py \
    --tasks 100000 \
    --duration 0.001 \
    --workers 100 \
    --runs 3
```

At this scale, the simulated work is almost free. Results primarily measure:

* coroutine scheduling
* event handling
* queue management
* threadpool overhead

The benchmark should not be interpreted as a real-world I/O workload.

---

### Observability Stress Test

Tests lifecycle events and counters:

```bash
python benchmark.py \
    --tasks 10000 \
    --duration 0.01 \
    --workers 100 \
    --observe
```

Use this to verify:

* queued counts
* started counts
* completion tracking
* event listener behavior

For timing comparisons, disable observability because printing events changes performance characteristics.

---

## Benchmark Output

The benchmark reports:

### Expected

The theoretical runtime based on the execution model.

Examples:

```
Serial:
    tasks × duration

map_blocking:
    ceil(tasks / workers) × duration

async gather:
    duration
```

---

### Actual

Measured wall-clock completion time.

The benchmark uses the median result across `--runs`.

---

### Ceiling Hit

How close execution reached the theoretical minimum:

```
Expected / Actual × 100
```

Examples:

```
100%
```

means the implementation reached the predicted runtime.

Lower values indicate overhead from:

* scheduling
* coordination
* runtime management
* task creation

---

### Speedup

Compared with serial execution:

```
Serial Time / Actual Time
```

Example:

```
100x
```

means the workload completed one hundred times faster than sequential execution.

---

## Recommended Performance Tests

For release testing, run:

```bash
python benchmark.py \
    --tasks 1000 \
    --duration 0.1 \
    --workers 50 \
    --runs 5
```

```bash
python benchmark.py \
    --tasks 10000 \
    --duration 0.05 \
    --workers 100 \
    --runs 3
```

```bash
python benchmark.py \
    --tasks 100000 \
    --duration 0.001 \
    --workers 100 \
    --runs 3
```

These cover:

* realistic I/O workloads
* high concurrency workloads
* framework overhead limits

---

## Benchmarks

Dovetail is benchmarked against a plain serial for-loop using simulated
I/O workloads. The benchmark intentionally includes both realistic I/O
latencies and extreme short-task stress tests.

Each task sleeps for a fixed duration:

- `time.sleep()` releases the GIL, allowing worker threads to execute concurrently.
- `asyncio.sleep()` yields the event loop, allowing coroutines to overlap.

The benchmark measures:

1. Serial execution
2. `map_blocking` — bounded threadpool execution from synchronous code
3. `async gather` — coroutine concurrency from asynchronous code

Long-running I/O tasks demonstrate concurrency benefits.
Very short tasks demonstrate framework scheduling overhead.

The benchmark compares:

1. **Serial** — plain for-loop, one task at a time.
2. **`map_blocking`** — Dovetail's bounded threadpool execution from synchronous code.
3. **`async gather`** — fully concurrent coroutine execution from an async caller.

**Reading the table:**

- **Expected** — theoretical completion time based on the execution model:
  - Serial: `tasks × duration`
  - `map_blocking`: `ceil(tasks / workers) × duration`
  - `async gather`: `duration` (all tasks overlap)

- **Actual** — measured wall-clock time to complete all tasks. The benchmark reports the median across multiple runs to smooth out scheduling noise.

- **Ceiling Hit** — how closely the implementation reached its theoretical completion limit:
  - `Expected / Actual × 100`
  - `100%` means the implementation reached the predicted runtime.
  - Lower values indicate scheduling, coordination, runtime overhead, or other practical limits.

- **Speedup** — improvement compared with the serial baseline:
  - `Serial Time / Actual Time`
  - A value of `10x` means the workload completed ten times faster than running sequentially.

The benchmark also reports the **maximum theoretical speedup**. For example, 20 fully-overlapping tasks have a theoretical maximum speedup of `20x`.

Run the benchmarks yourself:

```bash
python -m benchmark
python -m benchmark --tasks 100 --duration 0.1 --workers 10 --runs 5
```

---

### Default — 20 tasks x 100ms

The clean introduction benchmark.

Serial execution behaves exactly as expected (`20 × 100ms`). `map_blocking` is bounded by the configured worker count, while `async gather` allows all coroutines to overlap.

```
  Mode                           Expected     Actual    Ceiling Hit   Speedup
  --------------------------------------------------------------------------
  Serial                         2000ms      2005ms        99.7%        1.0x
  map_blocking (8 workers)        300ms       304ms        98.5%        6.6x
  async gather                    100ms       171ms        58.3%       11.7x


  Theoretical minimum:          100ms
  Maximum theoretical speedup:   20.0x

  Achieved speedup ceiling:
    map_blocking: 33.0%
    async gather: 58.5%
```

---

### Bounded threadpool — 100 tasks x 100ms, 10 workers

This demonstrates the predictable scaling behaviour of `map_blocking`.

`ceil(100 / 10) = 10` execution rounds, giving an expected runtime of approximately `1000ms`.

The measured result stays close to the threadpool model, showing that Dovetail adds minimal overhead on top of Python's executor scheduling.

```
  Mode                           Expected     Actual    Ceiling Hit   Speedup
  --------------------------------------------------------------------------
  Serial                        10000ms     10026ms        99.7%        1.0x
  map_blocking (10 workers)      1000ms      1012ms        98.8%        9.9x
  async gather                    100ms       109ms        91.0%       91.2x


  Theoretical minimum:          100ms
  Maximum theoretical speedup:  100.0x

  Achieved speedup ceiling:
    map_blocking: 9.9%
    async gather: 91.2%
```

---

### Realistic high-concurrency I/O — 1000 tasks x 100ms, 50 workers

A larger workload that better represents many simultaneous network requests, API calls, or file operations.

`map_blocking` remains predictable:

```
ceil(1000 / 50) = 20 rounds
20 × 100ms = 2000ms theoretical runtime
```

The threadpool reaches almost the expected ceiling, while async execution is limited mostly by coroutine scheduling overhead.

```
  Mode                            Expected       Actual    Ceiling Hit   Speedup
  --------------------------------------------------------------------------------
  Serial                        100000.0ms   100332.7ms         99.7%        1.0x
  map_blocking (50 workers)       2000.0ms     2037.5ms         98.2%       49.2x
  async gather                     100.0ms      187.5ms         53.3%      535.2x


  Theoretical minimum:          100ms
  Maximum theoretical speedup: 1000.0x

  Achieved speedup ceiling:
    map_blocking (50 workers): 4.9%
    async gather: 53.5%
```

---

### Large workload — 10000 tasks x 50ms, 100 workers

This pushes task volume higher while keeping the workload realistic for I/O.

`map_blocking` completes:

```
ceil(10000 / 100) = 100 rounds
100 × 50ms = 5000ms theoretical runtime
```

The benchmark shows the trade-off between massive concurrency and scheduling overhead.

```
  Mode                             Expected       Actual    Ceiling Hit   Speedup
  ---------------------------------------------------------------------------------
  Serial                         500000.0ms   503821.1ms         99.2%        1.0x
  map_blocking (100 workers)       5000.0ms     5406.0ms         92.5%       93.2x
  async gather                       50.0ms      373.4ms         13.4%     1349.2x


  Theoretical minimum:           50ms
  Maximum theoretical speedup: 10000.0x

  Achieved speedup ceiling:
    map_blocking (100 workers): 0.9%
    async gather: 13.5%
```

---

### Very large concurrency — 5000 tasks x 100ms, 200 workers

This tests Dovetail under a high number of simultaneous executor jobs.

The threadpool remains close to the expected bounded-worker model:

```
ceil(5000 / 200) = 25 rounds
25 × 100ms = 2500ms theoretical runtime
```

```
  Mode                             Expected       Actual    Ceiling Hit   Speedup
  ---------------------------------------------------------------------------------
  Serial                         500000.0ms   501670.2ms         99.7%        1.0x
  map_blocking (200 workers)       2500.0ms     2710.5ms         92.2%      185.1x
  async gather                      100.0ms      292.1ms         34.2%     1717.5x


  Theoretical minimum:          100ms
  Maximum theoretical speedup:  5000.0x

  Achieved speedup ceiling:
    map_blocking (200 workers): 3.7%
    async gather: 34.3%
```

---

### Short tasks — where overhead dominates

Very short tasks expose the cost of coordination and scheduling.

This is not a failure case; it demonstrates an important trade-off:

Long-running I/O benefits heavily from concurrency.
Extremely small tasks can spend more time being scheduled than executing useful work.

Example:

```
100000 tasks x 1ms
100 workers
```

```
  Mode                             Expected       Actual    Ceiling Hit   Speedup
  ---------------------------------------------------------------------------------
  Serial                         100000.0ms   150919.8ms         66.3%        1.0x
  map_blocking (100 workers)       1000.0ms    16378.9ms          6.1%        9.2x
  async gather                        1.0ms     4704.6ms          0.0%       32.1x


  Theoretical minimum:            1ms
  Maximum theoretical speedup: 100000.0x

  Achieved speedup ceiling:
    map_blocking (100 workers): 0.0%
    async gather: 0.0%
```

At this scale, the theoretical minimum becomes unrealistic because task scheduling overhead dominates the actual work.

---

### Worker scaling — 1000 tasks x 50ms

Increasing workers improves `map_blocking` almost linearly until coordination overhead becomes noticeable.

```
Workers     Expected     Actual      Speedup
------------------------------------------------
1           50000ms      50632ms      1.0x
5           10000ms      10209ms      4.9x
10           5000ms       5187ms      9.7x
50           1000ms       1095ms     45.9x
200           250ms        339ms    148.3x
```

The key observation:

Increasing workers reduces execution time predictably.
Beyond a certain point, executor management overhead starts dominating.
map_blocking remains bounded and predictable rather than attempting unlimited concurrency.

---

Observability and tracing overhead

Tracing and live event observation are intentionally opt-in.

Example:

`
python -m tests.benchmark \
  --tasks 1000 \
  --duration 0.01 \
  --workers 50 \
  --trace
`

Without tracing:
```
map_blocking (50 workers): 274ms
async gather:              93ms
```

With tracing enabled:
```
map_blocking (50 workers): 616ms
async gather:             385ms
```

This is expected. Structured tracing captures execution metadata, timestamps, and lifecycle information. It should be enabled for diagnostics, not high-volume production telemetry.

---

### **Takeaways:**

- `map_blocking` provides predictable bounded concurrency for synchronous applications.
- `async gather` provides the highest theoretical concurrency when work is already async.
- Both approaches scale extremely well for realistic I/O workloads.
- Threadpool overhead remains low when tasks are long enough to amortise scheduling costs.
- Extremely small tasks are dominated by runtime coordination rather than useful work.
- Observability features are deliberately opt-in because detailed tracing has measurable runtime cost.
- For production workloads:
  - Use map_blocking when integrating blocking libraries, filesystem operations, or synchronous APIs.
  - Use async scheduling when the workload naturally fits asyncio.
  - Use tracing and event listeners selectively for debugging, monitoring, and orchestration.

### **Context-manager performance note:**
Microbenchmarks showed that constructing and using a Dovetail instance through the context-manager form:

```
with Dovetail(...) as dvt:
    ...
```

can be marginally faster than manual lifecycle management:
```
dvt = Dovetail(...)
try:
    ...
finally:
    dvt.shutdown()
```

The difference is workload-dependent and small. The primary recommendation remains using the context manager because it guarantees cleanup during exceptions while also avoiding accidental resource leaks.