# Dovetail Benchmarks

## Table of Contents
- [Benchmark Overview](#benchmark-overview)
- [Running The Benchmarks](#running-the-benchmarks)
- [Benchmark Usage](#benchmark-usage)
- [Benchmarks](#benchmarks)

## Benchmark Overview

Dovetail is benchmarked against a plain serial for-loop using simulated
I/O workloads. The benchmark intentionally includes both realistic I/O
latencies and extreme short-task stress tests.

Each task sleeps for a fixed duration:

- `time.sleep()` releases the GIL, allowing worker threads to execute concurrently.
- `asyncio.sleep()` yields the event loop, allowing coroutines to overlap.

The benchmark measures:

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

---

## Running The Benchmarks

Run the benchmarks yourself:

```bash
python -m benchmark
python -m benchmark --tasks 100 --duration 0.1 --workers 10 --runs 5
```

### Recommended Performance Tests

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

## Benchmarks

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