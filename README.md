# Dovetail

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml)

Dovetail (`pydovetail`) bridges blocking Python functions and `asyncio` with
an owned `ThreadPoolExecutor`, task lifecycle events, retries, rate limits,
and optional tracing.

**Requires Python 3.10+** and `typing_extensions>=4.0` (for `Self` on Python 3.10).

```bash
python -m pip install pydovetail
```

## Quick start

```python
import asyncio
from pydovetail import Dovetail


def blocking_double(value: int) -> int:
    return value * 2


async def main() -> None:
    async with Dovetail(max_workers=8) as dvt:
        value = await dvt.task.to_thread(blocking_double, 4)
        values = await dvt.task.map(
            blocking_double, range(10), max_concurrency=8
        )
        print(value, values)


asyncio.run(main())
```

From **synchronous** code, use `dvt.task.to_thread_blocking(...)`,
`dvt.task.map_blocking(...)`, or `dvt.task.run_blocking(...)`. Never invoke
these blocking wrappers inside a running event loop.

## Features

- **Blocking work:** `await dvt.task.to_thread(func, *args, **kwargs)`.
- **Bounded ordered mapping:** `await dvt.task.map(func, items, max_concurrency=8,
  return_exceptions=False)`; the mapped function must be synchronous.
- **Scheduling:** `dvt.task.schedule(coroutine_or_callable, ...)` returns an
  `asyncio.Task`; `dvt.task.managed()` wraps sync and async functions.
- **Observability:** `dvt.events.on(...)`, `dvt.events.stats()`, custom counters,
  and optional tracing.
- **Execution policy:** per-instance timeout, retries, retry backoff and rate
  limiting for supported execution paths.
- **Lifecycle:** sync and async context managers, explicit `shutdown()`, and an
  optional weak-reference instance registry.

**Cancellation matters:** a timeout or cancellation of an awaiter cannot stop
an already-running Python thread. Its side effects may continue. Retry backoff
also occupies a worker thread. Use a bounded concurrency setting and make
blocking operations idempotent where possible. Coroutine scheduling and
blocking-function mapping are different APIs: do not pass `async def`
functions to `task.map()`.

For exact signatures, event behaviour, and shutdown semantics, see the
[API reference](docs/api.md). For reproducible performance experiments, see
[Benchmarks](docs/benchmarks.md).

## Development

```bash
python -m pip install -e ".[dev]"
python tests/test.py                  # legacy feature suite
python -m pytest tests/ -v            # pytest-discoverable tests
python -m ruff check .
python -m build
python -m twine check dist/*
```

The CI matrix targets Python 3.10–3.14. Passing the matrix does not imply that
all multi-instance shutdown and cancellation edge cases are covered.

MIT licensed; see [LICENSE](LICENSE).
