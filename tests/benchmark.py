"""
benchmark.py — Dovetail performance demonstration
==================================================

Compares three execution modes for a batch of I/O-bound tasks:

  1. Serial          — plain for-loop, one task at a time
  2. map_blocking    — Dovetail's bounded threadpool (sync caller)
  3. async gather    — asyncio.gather, fully concurrent (async caller)

Each "task" simulates real I/O work by sleeping for TASK_DURATION seconds,
which is honest: time.sleep() releases the GIL so threads genuinely run
in parallel; asyncio.sleep() yields the event loop so coroutines interleave.

The expected speedup for N tasks each taking T seconds:
  Serial:   ~N x T
  Parallel: ~T (all tasks overlap)

Reading the results table:
  Median time — wall-clock time to complete all tasks, median across --runs
                runs to smooth out scheduling noise.
  vs serial   — how many times faster than the plain for-loop. Higher is better.
                map_blocking at 6.6x means it finished in ~1/6th the serial time.
  vs ideal    — how close to the physical ceiling you are. The ideal is one task
                duration (e.g. 100ms), because if all tasks ran simultaneously
                you'd never wait longer than the slowest one. 1.0x is perfect;
                map_blocking lands near ceil(tasks/workers) because that's how
                many rounds a bounded threadpool needs.

Usage:
  python benchmark.py
  python benchmark.py --tasks 50 --duration 0.05 --workers 10
  python benchmark.py --observe               # show live events + stats
  python benchmark.py --observe --tasks 5     # keep it readable
"""

import argparse
import asyncio
import statistics
import time
from typing import List

from dovetail import Dovetail


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
    Attach all six lifecycle listeners to a Dovetail instance.
    Each prints a one-line entry so you can watch events fire in real time.
    Only called when --observe is set; has no effect on timing otherwise.

    Payload keys (from dvt.events): method, function, execution_id, task.
    """
    tag = f"  [{mode_name}]"

    dvt.events.on_queued(
        lambda p: print(f"{tag} queued    {p.get('execution_id', '?')}  fn={p.get('function', '?')}")
    )
    dvt.events.on_start(
        lambda p: print(f"{tag} started   {p.get('execution_id', '?')}  fn={p.get('function', '?')}")
    )
    dvt.events.on_end(
        lambda p: print(f"{tag} done      {p.get('execution_id', '?')}  fn={p.get('function', '?')}")
    )
    dvt.events.on_error(
        lambda p: print(f"{tag} ERROR     {p.get('execution_id', '?')}  fn={p.get('function', '?')}  exc={p.get('exception', '?')}")
    )
    dvt.events.on_retry(
        lambda p: print(f"{tag} retry     {p.get('execution_id', '?')}  fn={p.get('function', '?')}  attempt={p.get('attempt', '?')}")
    )
    dvt.events.on_cancel(
        lambda p: print(f"{tag} cancelled {p.get('execution_id', '?')}  fn={p.get('function', '?')}")
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
    """Run jobs one at a time in a plain for-loop. Baseline: ~n x duration."""
    return [sync_io_job(i, duration) for i in range(n)]


def run_map_blocking(n: int, duration: float, max_workers: int, observe: bool = False, context_manager: bool = True) -> List[int]:
    """
    Run jobs in parallel using Dovetail's threadpool.
    map_blocking is the idiomatic Dovetail way to scatter work from sync code.
    Expected time: ~duration (all jobs overlap, bounded by max_workers).
    """
    if context_manager:
        with Dovetail(max_workers=max_workers) as dvt:
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

    dvt = Dovetail(max_workers=max_workers)
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


def run_async_gather(n: int, duration: float, observe: bool = False, context_manager: bool = True) -> List[int]:
    """
    Run jobs concurrently using asyncio.gather via Dovetail's schedule.
    Expected time: ~duration (all coroutines interleave in one event loop).
    """
    # Use context-manager form inside the async runner when useful.
    async def _run_with_context():
        async with Dovetail() as dvt:
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
        dvt = Dovetail()
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
    theoretical_min = duration  # all tasks fully overlap

    # Derive worker count from the mode name so we can show the rounds formula.
    n_workers = next(
        (int(m.split("(")[1].split(" ")[0]) for m in results if "workers" in m), None
    )

    col_w = max(len(m) for m in results) + 2
    width = col_w + 46

    print()
    print("  Columns:")
    print("    Median time — wall-clock time to finish all tasks (median across runs)")
    print("    vs serial   — speedup over the plain for-loop; higher is better")
    print("    vs ideal    — distance from the physical ceiling (1.0x means all tasks")
    print("                  ran simultaneously); map_blocking lands near ceil(tasks/workers)")
    if n_workers:
        rounds = math.ceil(n / n_workers)
        print(f"                  = ceil({n}/{n_workers}) = {rounds} rounds x {duration*1000:.0f}ms ≈ {rounds * duration * 1000:.0f}ms")
    print()
    print("=" * width)
    print(f"  Dovetail benchmark  |  {n} tasks x {duration*1000:.0f}ms each")
    print("=" * width)
    print(f"  {'Mode':<{col_w}} {'Median time':>12}  {'vs serial':>10}  {'vs ideal':>10}")
    print("  " + "-" * (width - 2))

    for mode, elapsed in results.items():
        speedup = serial_time / elapsed
        vs_ideal = elapsed / theoretical_min
        bar_len = int((elapsed / serial_time) * 30)
        bar = "█" * bar_len
        print(
            f"  {mode:<{col_w}} {elapsed*1000:>10.1f}ms"
            f"  {speedup:>8.1f}x"
            f"  {vs_ideal:>8.1f}x"
            f"  {bar}"
        )

    print()
    print(f"  Theoretical minimum (all tasks overlap): {theoretical_min*1000:.0f}ms")
    print(f"  Serial baseline (no concurrency):        {serial_time*1000:.1f}ms")
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
    parser.add_argument("--observe",  action="store_true",                  help="Print live events and stats (tip: use --tasks 5 to keep output readable)")
    parser.add_argument("--context-manager", dest="context_manager", action="store_true", help="Use context-manager form when creating Dovetail instances (default)")
    parser.add_argument("--no-context-manager", dest="context_manager", action="store_false", help="Disable context-manager usage (use manual creation)")
    parser.add_argument("--compare", action="store_true",                help="Compare runs with and without context-manager usage for concurrent modes")
    args = parser.parse_args()

    n         = args.tasks
    duration  = args.duration
    workers   = args.workers
    runs      = args.runs
    observe   = args.observe
    context_manager = args.context_manager
    compare = args.compare

    if observe:
        print("\n  --observe is on: lifecycle events will print live during the last run only.")
        print("  Serial mode has no Dovetail instance so events are not available for it.")
        print("  For readable output, consider --tasks 5.")
        # Only show events on the final run (median run), not every repetition.
        # We do this by running normally for (runs - 1) and then one observed run.

    print(f"\nWarming up... ({runs} run(s) per mode, reporting median)")

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
            silent_no = [timed(run_map_blocking, n, duration, workers, context_manager=False)[1] for _ in range(runs - 1)]
            print()
            _, observed_no = timed(run_map_blocking, n, duration, workers, observe=True, context_manager=False)
            results[label_no] = statistics.median(silent_no + [observed_no])

            silent_ctx = [timed(run_map_blocking, n, duration, workers, context_manager=True)[1] for _ in range(runs - 1)]
            print()
            _, observed_ctx = timed(run_map_blocking, n, duration, workers, observe=True, context_manager=True)
            results[label_ctx] = statistics.median(silent_ctx + [observed_ctx])
        else:
            results[label_no] = median_time(run_map_blocking, runs, n, duration, workers, observe=observe, context_manager=False)
            results[label_ctx] = median_time(run_map_blocking, runs, n, duration, workers, observe=observe, context_manager=True)
    else:
        if observe and runs > 1:
            # Run (runs - 1) silent passes, then one observed pass
            silent = [timed(run_map_blocking, n, duration, workers, context_manager=context_manager)[1] for _ in range(runs - 1)]
            print()  # newline before live events
            _, observed = timed(run_map_blocking, n, duration, workers, observe=True, context_manager=context_manager)
            results[f"map_blocking ({workers} workers)"] = statistics.median(silent + [observed])
        else:
            results[f"map_blocking ({workers} workers)"] = median_time(
                run_map_blocking, runs, n, duration, workers, observe=observe, context_manager=context_manager
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
            silent_no = [timed(run_async_gather, n, duration, context_manager=False)[1] for _ in range(runs - 1)]
            print()
            _, observed_no = timed(run_async_gather, n, duration, observe=True, context_manager=False)
            results[label_no] = statistics.median(silent_no + [observed_no])

            silent_ctx = [timed(run_async_gather, n, duration, context_manager=True)[1] for _ in range(runs - 1)]
            print()
            _, observed_ctx = timed(run_async_gather, n, duration, observe=True, context_manager=True)
            results[label_ctx] = statistics.median(silent_ctx + [observed_ctx])
        else:
            results[label_no] = median_time(run_async_gather, runs, n, duration, observe=observe, context_manager=False)
            results[label_ctx] = median_time(run_async_gather, runs, n, duration, observe=observe, context_manager=True)
    else:
        if observe and runs > 1:
            silent = [timed(run_async_gather, n, duration, context_manager=context_manager)[1] for _ in range(runs - 1)]
            print()
            _, observed = timed(run_async_gather, n, duration, observe=True, context_manager=context_manager)
            results["async gather"] = statistics.median(silent + [observed])
        else:
            results["async gather"] = median_time(
                run_async_gather, runs, n, duration, observe=observe, context_manager=context_manager
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