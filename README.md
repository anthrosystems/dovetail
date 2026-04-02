# Dovetail Usage Guide

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/dovetail/)
[![PyPI - Python Versions](https://img.shields.io/pypi/pyversions/dovetail.svg)](https://pypi.org/project/dovetail/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

Releases
--------
See the changelog for release notes and backward-compatibility guidance: [CHANGELOG.md](CHANGELOG.md).
# Dovetail Usage Guide

Purpose
-------
Compact reference for safely running sync and async work across call sites using `Dovetail.Task.to_thread()`, `Dovetail.Task.schedule()`, and `Dovetail.Task.run_blocking()`.

Formerly part of a project for bulk scraping websites, it has been moved into its own project as I thought it might be more useful that way.

Quick Start
---------------------
Sync caller → sync function (blocks):

```py
from dovetail import Dovetail

result = Dovetail.Task.run_blocking(get_data)
```

Sync caller → async function (blocks):

```py
from dovetail import Dovetail

def get_remote():
    return Dovetail.Task.run_blocking(fetch_data())
```

Async caller → sync function (await result):

```py
from dovetail import Dovetail

result = await Dovetail.Task.to_thread(read_config_file, "config.json")
```

Async caller → async function (await result / fire-and-forget):

```py
from dovetail import Dovetail

task = Dovetail.Task.schedule(fetch_data())  # await or leave un-awaited
```

API (one-line)
---------------
- `Dovetail.Task.to_thread(func, *args, **kwargs)` - async: run a sync callable in the threadpool.
- `Dovetail.Task.schedule(coro_or_callable, *args, type=None, **kwargs)` - async: schedule coroutine/async func or sync callable in threadpool; returns `asyncio.Task`.
- `Dovetail.Task.run_blocking(func_or_coro, *args, **kwargs)` - sync: run sync func or run coroutine via `asyncio.run()`; raises if called from a running loop.

Decision Table
------------------------
| Caller | Callee | Need result? | Use |
|---|---:|---:|---|
| Sync | Sync | Yes | `Dovetail.Task.run_blocking(func, *args)` |
| Sync | Async | Yes | `Dovetail.Task.run_blocking(coro(...))` |
| Async | Async | Either | `await Dovetail.Task.schedule(coro(...))` - or - `Dovetail.Task.schedule(coro(...))` (fire-and-forget) |
| Async | Sync | Yes | `await Dovetail.Task.to_thread(func, *args)` |
| Async | Sync | No  | `Dovetail.Task.schedule(lambda: func(*args), type="io")` |

Concise Examples
----------------
Sync (blocking):

```py
def build_session():
    return Dovetail.Task.run_blocking(get_data)
```

Async (await result):

```py
async def process():
    data = await Dovetail.Task.schedule(fetch_data())
    await Dovetail.Task.to_thread(write_cache, data)
```

Notes
-----
- `Dovetail.Task.run_blocking()` blocks the calling thread - avoid from running event loops.
- Fire-and-forget tasks should handle their own exceptions and logging.
- Standardise `type` labels (e.g., `"io"`, `"cpu"`) and expose constants to aid observability.

When to use `lambda`
---------------------
- Use a `lambda` (or `functools.partial`) when scheduling a *sync* function from async code in fire-and-forget mode so the callable isn't executed immediately. Example: `Dovetail.Task.schedule(lambda: blocking_write(data), type="io")`.
- You do **not** need a `lambda` when you `await Dovetail.Task.to_thread(func, *args)`, or when scheduling an async coroutine object (`Dovetail.Task.schedule(coro())`).
- Avoid pre-calling (e.g. `Dovetail.Task.schedule(func(args))`) - that executes the call synchronously and defeats the executor.