# Dovetail - Sync / Async wrapper for Python.

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml) 
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/pydovetail/) 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

A lightweight helper for bridging sync and async code, built on Asyncio & ThreadPoolExecutor.

## Table of Contents

- [Why Dovetail](#why-dovetail)
- [Install](#install)
- [Quick Start](#quick-start)
- [API Summary](#api-summary)
- [Detailed Guides](docs/detailed_guide.md#L1)
- [Development](#development)
- [License](#license)

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


## Install

```bash
pip install pydovetail
```

> **Note:** The install name is `pydovetail`, but the import name is `dovetail`.
> ```python
> from dovetail import Dovetail
> ```


## Quick Start

Preferred usage: create and manage a `Dovetail` instance with a context manager.

Sync (recommended for most users):

```python
from dovetail import Dovetail

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
# Manual creation (caller must call shutdown())
dvt = Dovetail()
try:
  results = dvt.task.map_blocking(fetch, items)
finally:
  dvt.shutdown()

# Library-author pattern (prefer caller-managed instance when possible)
def process(items, dvt: Optional[Dovetail] = None):
  if dvt is None:
    with Dovetail() as _dvt:
      return _dvt.task.map_blocking(worker, items)
  return dvt.task.map_blocking(worker, items)
```

> **Note:** Call `dvt.shutdown()` to cleanly close the threadpool when the
> instance is no longer needed. To avoid leaking threads (especially on
> exceptions), prefer the context-manager form which automatically shuts down
> resources when the block exits.
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

All `dvt.events.on_*` methods accept these optional scope parameters:

- `function_target` — only receive events for a specific callable.
- `instance_target` — only receive events for a specific execution id (available
  as `id` in the event payload).
- `allow_reentry=False` — prevents a listener from triggering itself recursively.
  Set to `True` to allow reentry, bounded by `max_chain_depth`.
- `max_chain_depth=5` — maximum active callback depth when reentry is enabled.


## Detailed Guides

Full, expanded guides live in the repository under [docs/detailed_guide.md](docs/detailed_guide.md#L1).


## Development

Run tests locally:

```bash
python -m pytest -q
```

Contributions welcome — open an issue or PR.


## License

MIT. See `CHANGELOG.md` for release notes.