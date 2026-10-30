# Dovetail

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml)

Dovetail (`pydovetail`) bridges synchronous functions and `asyncio` using a
bounded thread pool. Requires **Python 3.10+**; no runtime dependencies.

```bash
python -m pip install pydovetail
```

## Quick start

```python
import asyncio
from pydovetail import Dovetail

def blocking_double(value):
    return value * 2

async def main():
    with Dovetail(max_workers=8) as dvt:
        result = await dvt.task.to_thread(blocking_double, 4)
        results = await dvt.task.map(blocking_double, range(10), max_concurrency=8)
        print(result, results)

asyncio.run(main())
```

Use `dvt.task.map_blocking(func, items, max_concurrency=8)` from synchronous
code, or `dvt.task.run_blocking(coroutine)` to bridge a synchronous caller.
Never call blocking wrappers from an already-running event loop.

## Capabilities

- `task.to_thread`: await a blocking function in the executor.
- `task.map`: ordered concurrent mapping, with `max_concurrency` and
  `return_exceptions` controls.
- `task.map_blocking`: synchronous mapping wrapper.
- `task.schedule`: schedule coroutine or callable work.
- `task.run_blocking`: bridge synchronous callers.
- Lifecycle events, counters, tracing, rate limiting, retries and timeouts.

Timeouts cannot stop already-running Python threads. Consult the
[migration and concurrency guide](docs/migration.md) for cancellation semantics
and downstream migrations.

## Reference and development

- [API reference](docs/api.md)
- [Concurrency and migration](docs/migration.md)
- [Benchmarks](docs/benchmarks.md)

Run `python -m pip install -e '.[dev]'` then `python tests/test.py`.
CI runs Python 3.10–3.14, linting, a benchmark smoke test and package builds.

MIT licensed; see [LICENSE](LICENSE).
