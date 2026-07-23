"""
benchmark.py — Dovetail performance demonstration
==================================================

Compares three execution modes for a batch of I/O-bound tasks:

  1. Serial          — plain for-loop, one task at a time
  2. map_blocking    — Dovetail's bounded threadpool (sync caller)
  3. async gather    — asyncio.gather, fully concurrent (async caller)

Each "task" simulates real I/O work by sleeping for TASK_DURATION seconds.
This is representative because:

  - time.sleep() releases the GIL, allowing worker threads to run in parallel.
  - asyncio.sleep() yields control to the event loop, allowing coroutines to
    interleave concurrently.

The expected speedup for N tasks each taking T seconds:

  Serial:
      ~N X T

  map_blocking:
      ~ceil(N / workers) X T

  async gather:
      ~T (all tasks overlap)

These are theoretical limits. Real runtimes include scheduling,
task creation, context switching, interpreter overhead, and shutdown
costs. Very small task durations can become dominated by framework
overhead rather than execution time.

Reading the results table:

  Expected      — theoretical completion time based on the execution model:

                  Serial:
                      tasks X duration

                  map_blocking:
                      ceil(tasks / workers) X duration

                  async gather:
                      duration (all tasks overlap)


  Actual        — measured wall-clock time to complete all tasks.
                  The benchmark reports the median across --runs to reduce
                  scheduling noise.


  Ceiling Hit   — how closely the implementation reached its theoretical
                  completion limit:

                      Expected / Actual X 100%

                  100% means the implementation reached the predicted
                  runtime for its execution model.

                  Lower values indicate scheduling overhead, runtime
                  coordination, or other practical limitations.


  Speedup       — improvement compared with the serial baseline:

                      Serial Time / Actual Time

                  A value of 10x means the workload completed ten times
                  faster than running tasks sequentially.


The benchmark also reports the maximum theoretical speedup:

    serial_time / ideal_parallel_time = tasks

This represents the mathematical maximum if every task overlaps
perfectly with zero scheduling overhead.

For example, 20 fully-overlapping tasks have a maximum theoretical
speedup of 20x.

After the performance comparison, the script also runs a fast feature
sanity pass covering the current public API.

Usage:
  python benchmark.py
  python benchmark.py --tasks 50 --duration 0.05 --workers 10
  python benchmark.py --observe               # show live events + stats
"""

import sys
import argparse
import asyncio
import logging
import statistics
import time

from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)) # Ensure local package is benchmarked, not PyPI release

from pydovetail import Dovetail, Event


# Module-level logger used when enabling Dovetail trace output from main()
logger = logging.getLogger("dovetail-benchmark")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TASKS = 20       # number of jobs to run
DEFAULT_DURATION = 0.1   # seconds each job sleeps (simulates I/O latency)
DEFAULT_WORKERS = 8      # max threadpool workers for map_blocking
DEFAULT_RUNS = 3         # number of benchmark repetitions (median is reported)


# ---------------------------------------------------------------------------
# Simulated work
# ---------------------------------------------------------------------------

def sync_io_job(task_id: int, duration: float) -> int:
    """Blocking I/O simulation — sleeps then returns the task_id."""
    time.sleep(duration)
    return task_id


async def async_io_job(task_id: int, duration: float) -> int:
    """Async I/O simulation — yields the event loop then returns the task_id."""
    await asyncio.sleep(duration)
    return task_id


# ---------------------------------------------------------------------------
# Observability (--observe mode)
# ---------------------------------------------------------------------------

def attach_observers(dvt: Dovetail, mode_name: str) -> None:
    """
    Attach lifecycle listeners to a Dovetail instance.
    Each prints a one-line entry so you can watch events fire in real time.
    Only called when --observe is set; has no effect on timing otherwise.

    Payload keys (from dvt.events): method, function, execution_id, task.
    """
    tag = f"  [{mode_name}]"

    dvt.events.on(
        Event.QUEUED,
        lambda p: print(
            f"{tag} queued    {p.get('execution_id', '?')}  fn={p.get('function', '?')}"
        ),
    )

    dvt.events.on(
        Event.STARTED,
        lambda p: print(
            f"{tag} started   {p.get('execution_id', '?')}  fn={p.get('function', '?')}"
        ),
    )

    dvt.events.on(
        Event.DONE,
        lambda p: print(
            f"{tag} done      {p.get('execution_id', '?')}  fn={p.get('function', '?')}"
        ),
    )

    dvt.events.on(
        Event.ERROR,
        lambda p: print(
            f"{tag} ERROR     {p.get('execution_id', '?')}  "
            f"fn={p.get('function', '?')}  exc={p.get('exception', '?')}"
        ),
    )

    dvt.events.on(
        Event.RETRY,
        lambda p: print(
            f"{tag} retry     {p.get('execution_id', '?')}  "
            f"fn={p.get('function', '?')}  attempt={p.get('attempt', '?')}"
        ),
    )

    dvt.events.on(
        Event.CANCELLED,
        lambda p: print(
            f"{tag} cancelled {p.get('execution_id', '?')}  fn={p.get('function', '?')}"
        ),
    )


def print_stats(dvt: Dovetail, mode_name: str) -> None:
    """Print dvt.events.stats() in a compact block after a mode completes."""
    s = dvt.events.stats()
    print(f"\n  [{mode_name}] stats")
    print(f"    queued={s['queued']}  started={s['started']}  done={s['done']}"
          f"  error={s['error']}  retries={s['retries']}  throttled={s['throttled']}")


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

def run_serial(n: int, duration: float) -> List[int]:
    """Run jobs one at a time in a plain for-loop. Baseline: ~n X duration."""
    return [sync_io_job(i, duration) for i in range(n)]


def run_map_blocking(n: int, duration: float, max_workers: int, observe: bool = False, context_manager: bool = True, trace: bool = False, trace_prefix: str | None = None) -> List[int]:
    """
    Run jobs in parallel using Dovetail's threadpool.
    map_blocking is the idiomatic Dovetail way to scatter work from sync code.
    Expected time: ~duration (all jobs overlap, bounded by max_workers).
    """
    if context_manager:
        with Dovetail(max_workers=max_workers, trace=trace, trace_logger=logger, trace_prefix=(trace_prefix or "map_blocking")) as dvt:
            if observe:
                attach_observers(dvt, "map_blocking")
                print()
            result = dvt.task.map_blocking(
                lambda task_id: sync_io_job(task_id, duration),
                list(range(n)),
                max_concurrency=max_workers,
            )
            if observe:
                print_stats(dvt, "map_blocking")
            return result

    dvt = Dovetail(max_workers=max_workers, trace=trace, trace_logger=logger, trace_prefix=(trace_prefix or "map_blocking"))
    if observe:
        attach_observers(dvt, "map_blocking")
        print()  # newline so events don't run onto the progress line
    try:
        result = dvt.task.map_blocking(
            lambda task_id: sync_io_job(task_id, duration),
            list(range(n)),
            max_concurrency=max_workers,
        )
        if observe:
            print_stats(dvt, "map_blocking")
        return result
    finally:
        dvt.shutdown()


def run_async_gather(n: int, duration: float, observe: bool = False, context_manager: bool = True, trace: bool = False, trace_prefix: str | None = None) -> List[int]:
    """
    Run jobs concurrently using asyncio.gather via Dovetail's schedule.
    Expected time: ~duration (all coroutines interleave in one event loop).
    """
    # Use context-manager form inside the async runner when useful.
    async def _run_with_context():
        async with Dovetail(trace=trace, trace_logger=logger, trace_prefix=(trace_prefix or "async_gather")) as dvt:
            if observe:
                attach_observers(dvt, "async gather")
                print()
            tasks = [dvt.task.schedule(async_io_job(i, duration)) for i in range(n)]
            result = list(await asyncio.gather(*tasks))
            await asyncio.sleep(0.05)
            if observe:
                print_stats(dvt, "async gather")
            return result

    async def _run_no_context():
        dvt = Dovetail(trace=trace, trace_logger=logger, trace_prefix=(trace_prefix or "async_gather"))
        if observe:
            attach_observers(dvt, "async gather")
        try:
            if observe:
                print()
            tasks = [dvt.task.schedule(async_io_job(i, duration)) for i in range(n)]
            result = list(await asyncio.gather(*tasks))
            await asyncio.sleep(0.05)
            if observe:
                print_stats(dvt, "async gather")
            return result
        finally:
            dvt.shutdown()

    return asyncio.run(_run_with_context() if context_manager else _run_no_context())


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

def timed(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), return (result, elapsed_seconds)."""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start


def median_time(fn, runs: int, *args, **kwargs) -> float:
    """Run fn multiple times and return the median elapsed time."""
    times = []
    for _ in range(runs):
        _, elapsed = timed(fn, *args, **kwargs)
        times.append(elapsed)
    return statistics.median(times)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_results(n: int, duration: float, results: dict) -> None:
    import math

    serial_time = results["Serial"]

    expected = {
        "Serial": n * duration,
        "async gather": duration,
    }

    n_workers = next(
        (int(m.split("(")[1].split(" ")[0]) for m in results if "workers" in m),
        None,
    )

    if n_workers:
        rounds = math.ceil(n / n_workers)
        expected[f"map_blocking ({n_workers} workers)"] = rounds * duration
    else:
        expected["map_blocking"] = duration

    col_w = max(len(mode) for mode in results) + 2
    width = col_w + 70

    theoretical_max_speedup = (n * duration) / duration

    print()
    print("Columns:")
    print("     Expected       — theoretical completion time for this execution model")
    print("     Actual         — measured wall-clock time (median across runs)")
    print("     Ceiling Hit    — how close the run got to its theoretical limit")
    print("     Speedup        — improvement compared to serial execution")
    print()

    if n_workers:
        rounds = math.ceil(n / n_workers)
        print(
            f"    map_blocking uses ceil({n}/{n_workers}) = {rounds} rounds "
            f"x {duration*1000:.0f}ms ≈ {rounds * duration * 1000:.0f}ms"
        )

    print()
    print("=" * width)
    print(f"  Dovetail benchmark  |  {n} tasks X {duration*1000:.0f}ms each")
    print("=" * width)

    print(
        f"  {'Mode':<{col_w}}"
        f" {'Expected':>12}"
        f" {'Actual':>12}"
        f" {'Ceiling Hit':>14}"
        f" {'Speedup':>12}"
    )

    print("  " + "-" * (width - 2))

    for mode, actual in results.items():
        target = expected.get(mode, duration)

        ceiling_hit = (target / actual) * 100
        speedup = serial_time / actual

        print(
            f"  {mode:<{col_w}}"
            f" {target*1000:>10.1f}ms"
            f" {actual*1000:>10.1f}ms"
            f" {ceiling_hit:>12.1f}%"
            f" {speedup:>10.1f}x"
        )

    print()
    print(f"  Theoretical minimum:          {duration*1000:.0f}ms")
    print(f"  Maximum theoretical speedup:  {theoretical_max_speedup:.1f}x")
    print()

    print("  Achieved speedup ceiling:")
    for mode, actual in results.items():
        if mode == "Serial":
            continue

        speedup = serial_time / actual
        ceiling = (speedup / theoretical_max_speedup) * 100

        print(f"    {mode}: {ceiling:.1f}%")

    print("=" * width)
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Dovetail benchmark")
    parser.add_argument("--tasks",    type=int,   default=DEFAULT_TASKS,    help="Number of jobs")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="Job duration (seconds)")
    parser.add_argument("--workers",  type=int,   default=DEFAULT_WORKERS,  help="Max threadpool workers")
    parser.add_argument("--runs",     type=int,   default=DEFAULT_RUNS,     help="Repetitions per mode (median reported)")
    parser.add_argument("--context-manager", dest="context_manager", action="store_true", help="Use context-manager form when creating Dovetail instances (default)")
    parser.add_argument("--no-context-manager", dest="context_manager", action="store_false", help="Disable context-manager usage (use manual creation)")
    parser.add_argument("--compare", action="store_true",                help="Compare runs with and without context-manager usage for concurrent modes")
    parser.add_argument("--observe",  action="store_true",                  help="Print live events and stats")
    parser.add_argument("--trace", action="store_true", help="Enable Dovetail internal tracing (debug output)")
    args = parser.parse_args()

    n         = args.tasks
    duration  = args.duration
    workers   = args.workers
    runs      = args.runs
    context_manager = args.context_manager
    compare = args.compare
    observe   = args.observe
    trace = args.trace

    # Configure module-level logger for trace output if requested
    global logger
    if trace:
        # avoid adding duplicate handlers on repeated runs
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    else:
        # ensure at least a NullHandler exists
        if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
            logger.addHandler(logging.NullHandler())

    if observe:
        print("\n  --observe is on: lifecycle events will print live during the last run only.")
        print("  Serial mode has no Dovetail instance so events are not available for it.")
        # Only show events on the final run (median run), not every repetition.
        # We do this by running normally for (runs - 1) and then one observed run.

    print(f"\nWarming up... ({runs} run(s) per mode)")

    results = {}

    print("  [1/3] Serial...", end=" ", flush=True)
    results["Serial"] = median_time(run_serial, runs, n, duration)
    print(f"{results['Serial']*1000:.1f}ms")

    print("  [2/3] map_blocking (threadpool)...", end=" ", flush=True)
    if compare:
        # Run both variants (no-context and context) and record separately
        label_no = f"map_blocking ({workers} workers) [no-context]"
        label_ctx = f"map_blocking ({workers} workers) [context]"
        if observe and runs > 1:
            silent_no = [timed(run_map_blocking, n, duration, workers, context_manager=False, trace=trace)[1] for _ in range(runs - 1)]
            print()
            _, observed_no = timed(run_map_blocking, n, duration, workers, context_manager=False, observe=True, trace=trace)
            results[label_no] = statistics.median(silent_no + [observed_no])

            silent_ctx = [timed(run_map_blocking, n, duration, workers, context_manager=True, trace=trace)[1] for _ in range(runs - 1)]
            print()
            _, observed_ctx = timed(run_map_blocking, n, duration, workers, context_manager=True, observe=True, trace=trace)
            results[label_ctx] = statistics.median(silent_ctx + [observed_ctx])
        else:
            results[label_no] = median_time(run_map_blocking, runs, n, duration, workers, context_manager=False, observe=observe, trace=trace)
            results[label_ctx] = median_time(run_map_blocking, runs, n, duration, workers, context_manager=True, observe=observe, trace=trace)
    else:
        if observe and runs > 1:
            # Run (runs - 1) silent passes, then one observed pass
            silent = [timed(run_map_blocking, n, duration, workers, context_manager=context_manager, trace=trace)[1] for _ in range(runs - 1)]
            print()  # newline before live events
            _, observed = timed(run_map_blocking, n, duration, workers, context_manager=context_manager, observe=True, trace=trace)
            results[f"map_blocking ({workers} workers)"] = statistics.median(silent + [observed])
        else:
            results[f"map_blocking ({workers} workers)"] = median_time(
                run_map_blocking, runs, n, duration, workers, context_manager=context_manager, observe=observe, trace=trace
            )
    if compare:
        label_no = f"map_blocking ({workers} workers) [no-context]"
        label_ctx = f"map_blocking ({workers} workers) [context]"
        no_ms = results.get(label_no)
        ctx_ms = results.get(label_ctx)
        if no_ms is not None and ctx_ms is not None:
            print(f"\n  map_blocking median (no-context): {no_ms*1000:.1f}ms")
            print(f"  map_blocking median (context):    {ctx_ms*1000:.1f}ms")
        else:
            # fallback to any available key
            key = next((k for k in results if k.startswith("map_blocking (")), None)
            print(f"\n  map_blocking median: {results[key]*1000:.1f}ms" if key else "\n  map_blocking median: <missing>")
    else:
        print(f"\n  map_blocking median: {results[f'map_blocking ({workers} workers)']*1000:.1f}ms" if observe
              else f"{results[f'map_blocking ({workers} workers)']*1000:.1f}ms")

    print("  [3/3] async gather...", end=" ", flush=True)
    if compare:
        label_no = "async gather [no-context]"
        label_ctx = "async gather [context]"
        if observe and runs > 1:
            silent_no = [timed(run_async_gather, n, duration, context_manager=False, trace=trace)[1] for _ in range(runs - 1)]
            print()
            _, observed_no = timed(run_async_gather, n, duration, context_manager=False, observe=True, trace=trace)
            results[label_no] = statistics.median(silent_no + [observed_no])

            silent_ctx = [timed(run_async_gather, n, duration, context_manager=True, trace=trace)[1] for _ in range(runs - 1)]
            print()
            _, observed_ctx = timed(run_async_gather, n, duration, context_manager=True, observe=True, trace=trace)
            results[label_ctx] = statistics.median(silent_ctx + [observed_ctx])
        else:
            results[label_no] = median_time(run_async_gather, runs, n, duration, context_manager=False, observe=observe, trace=trace)
            results[label_ctx] = median_time(run_async_gather, runs, n, duration, context_manager=True, observe=observe, trace=trace)
    else:
        if observe and runs > 1:
            silent = [timed(run_async_gather, n, duration, context_manager=context_manager, trace=trace)[1] for _ in range(runs - 1)]
            print()
            _, observed = timed(run_async_gather, n, duration, context_manager=context_manager, observe=True, trace=trace)
            results["async gather"] = statistics.median(silent + [observed])
        else:
            results["async gather"] = median_time(
                run_async_gather, runs, n, duration, context_manager=context_manager, observe=observe, trace=trace
            )
    if compare:
        no_key = "async gather [no-context]"
        ctx_key = "async gather [context]"
        no_ms = results.get(no_key)
        ctx_ms = results.get(ctx_key)
        if no_ms is not None and ctx_ms is not None:
            print(f"\n  async gather median (no-context): {no_ms*1000:.1f}ms")
            print(f"  async gather median (context):    {ctx_ms*1000:.1f}ms")
        else:
            key = next((k for k in results if k.startswith("async gather")), None)
            print(f"\n  async gather median: {results[key]*1000:.1f}ms" if key else "\n  async gather median: <missing>")
    else:
        print(f"\n  async gather median: {results['async gather']*1000:.1f}ms" if observe
              else f"{results['async gather']*1000:.1f}ms")

    print_results(n, duration, results)


if __name__ == "__main__":
    main()