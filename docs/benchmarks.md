# Dovetail benchmark guide

`tests/benchmark.py` compares **serial synchronous calls**, Dovetail
`task.map_blocking()` over a thread pool, and `asyncio.gather()` over native
async coroutines. The jobs simulate I/O delays with `time.sleep()` or
`asyncio.sleep()`. These results are demonstrations of scheduling overhead and
concurrency, **not** production throughput guarantees or CPU benchmarks.

## Run

From the repository root:

```bash
python tests/benchmark.py
python tests/benchmark.py --tasks 100 --duration 0.05 --workers 8 --runs 5
python tests/benchmark.py --tasks 1000 --duration 0.01 --workers 32 --runs 3
```

The benchmark adds the local repository to its import path, so it exercises
the checked-out source rather than an installed PyPI version.

| Option | Default | Description |
| --- | --- | --- |
| `--tasks N` | `20` | Number of jobs per mode |
| `--duration SECONDS` | `0.1` | Simulated I/O wait per job |
| `--workers N` | `8` | Maximum executor threads for `map_blocking` |
| `--runs N` | `3` | Repetitions; median reported |
| `--context-manager` | Enabled | Use `with Dovetail(...)` for pool lifecycle |
| `--no-context-manager` | Disabled | Manage the instance and shutdown manually |
| `--compare` | Disabled | Compare context-manager and manual lifecycle variants |
| `--observe` | Disabled | Print lifecycle events/stats for observed runs |
| `--trace` | Disabled | Emit internal Dovetail traces |

Use `python tests/benchmark.py --help` for the command-line help. Large task
counts, trace/observe output and high worker counts can substantially increase
memory, output volume and runtime.

## Suggested profiles

```bash
# Quick CI/local smoke check
python tests/benchmark.py --tasks 20 --duration 0.001 --workers 4 --runs 1

# Measure scaling on an I/O-like workload
python tests/benchmark.py --tasks 500 --duration 0.05 --workers 8 --runs 5
python tests/benchmark.py --tasks 500 --duration 0.05 --workers 32 --runs 5

# Check overhead of context-manager lifecycle
python tests/benchmark.py --tasks 100 --duration 0.01 --workers 8 --compare

# Inspect event and trace overhead
python tests/benchmark.py --tasks 30 --duration 0.01 --workers 8 --observe --trace
```

## Interpret the output

- **Serial:** approximate baseline `tasks × duration`, plus Python overhead.
- **Thread pool:** the ideal I/O-only lower bound is approximately
  `ceil(tasks / workers) × duration`; queueing, executor startup, callbacks
  and system scheduling increase actual elapsed time.
- **Native async gather:** simulated `asyncio.sleep` jobs can overlap on one
  event loop; it is not an apples-to-apples benchmark against blocking SDK
  calls or thread scheduling.
- **Median and speedup:** compare repeated runs on the same machine, Python
  version and workload; use raw elapsed times alongside ratios.

The benchmark does **not** currently measure `task.map()` directly, cancellation
fairness, real network/SDK latency, memory bounds, `contextvars` propagation,
retry-worker saturation or multi-instance shutdown. Profile those separately
before making performance or production scaling claims.

See the [API reference](api.md) for the exact task semantics and limitations.
