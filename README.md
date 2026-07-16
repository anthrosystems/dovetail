# Dovetail - Sync / Async wrapper for Python.

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml) 
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/pydovetail/) 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

A lightweight helper for bridging sync and async code, built on Asyncio & ThreadPoolExecutor.

## Table of Contents
- [Features](#features)
- [Why Dovetail?](#why-dovetail)
- [Installing Dovetail](#installing-dovetail)
- [Development](#development)
- [Quick Start](#quick-start)
- [API Summary & Guide](docs/api.md)
- [Benchmarks](docs/benchmarks.md)
- [License](#license)

## Features

- Run async code from synchronous applications.
- Run blocking functions from async applications.
- Bounded threadpool execution.
- Ordered parallel mapping.
- Built-in retries and exponential backoff.
- Per-task timeout support.
- Token-bucket rate limiting.
- Lifecycle event hooks.
- Structured tracing.
- Automatic instance registry and coordinated shutdown.
- Sync and async context-manager support.

## Why Dovetail?

Asyncio is powerful, but it has a cascading problem: the moment you want to
`await` something, that function has to become `async`, which means everything
calling it has to become `async`, and so on until your entire codebase has been
rewritten around it.

If you're adding parallelism to an existing project, or only want to optimise
one bottleneck, rewriting the entire call chain is often unnecessary.

Dovetail solves this by allowing synchronous applications to use asynchronous
execution patterns without forcing an async migration.

Dovetail's advantage is not raw performance. Native asyncio will always be at
least as fast because Dovetail is built on top of it.

The advantage is **surgical parallelism**:

> Add concurrency to one function without rewriting everything around it.

---

### Before Dovetail

Imagine an existing synchronous application:
```python
def process_report(urls):
    results = []

    for url in urls:
        results.append(fetch_url(url))

    return summarise(results)
```

This works, but every request runs sequentially. If 100 requests each take 100ms:
```
100 requests × 100ms = ~10 seconds
```

The obvious asyncio solution is to rewrite everything:
```python
async def process_report(urls):
    tasks = [
        fetch_url(url)
        for url in urls
    ]

    results = await asyncio.gather(*tasks)

    return summarise(results)
```

But now every caller must also become async:
```python
async def application():
    report = await process_report(urls)
```

Which means the async boundary spreads:
```
application()
    ↓
process_report()
    ↓
fetch_url()
    ↓
database()
    ↓
filesystem()
```

For a large existing codebase, this migration can be expensive.

---

### After Dovetail

Dovetail lets you add concurrency only where you need it:
```python
from pydovetail import Dovetail

with Dovetail(max_workers=20) as dvt:

    def process_report(urls):
        results = dvt.task.map_blocking(fetch_url, urls)
        return summarise(results)
```

The rest of the application remains synchronous:
```python
def application():
    report = process_report(urls)
```

**No async migration.**
**No rewriting the call stack.**
**No changing unrelated code.**

The execution model becomes:
```
application()
    ↓
process_report()
    ↓
Dovetail threadpool
    ├── fetch_url()
    ├── fetch_url()
    ├── fetch_url()
    └── fetch_url()
```

The same synchronous interface now benefits from parallel execution.

---

Dovetail is intended for developers who are between **"I need concurrency"** and **"I don't want to rewrite my entire application around async"**

However, if you are starting a new project and are comfortable using async/await everywhere, native asyncio is usually the better choice.

---

## Installing Dovetail

### Requirements:
- Python 3.9+
- asyncio
- ThreadPoolExecutor (standard library)

Dovetail has no runtime dependencies outside Python's standard library.

### Install
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

```python
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

---

## License

Dovetail is released under the MIT License.

See [LICENSE](LICENSE) for details.