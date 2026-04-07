# Dovetail - sync/async interoperability helper

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml) 
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/pydovetail/) 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

A lightweight helper for bridging synchronous and asynchronous code in Python.
Minimal surface, no runtime dependencies, suitable for libraries and apps that
need safe threadpool usage and simple sync/async interoperability.

## Install

```bash
pip install pydovetail
```

## Usage (quick)

Create a `Dovetail` instance and use its `Task` helper:

```py
from dovetail import Dovetail

dvt = Dovetail()

# Sync caller -> sync function (blocks)
result = dvt.task.run_blocking(fetch_data)

# Sync caller -> async function (blocks)
def get_remote():
    return dvt.task.run_blocking(fetch_data())

# Async caller -> sync function (await result)
result = await dvt.task.to_thread(fetch_data, "target.json")

# Async caller -> async function (await or fire-and-forget)
task = dvt.task.schedule(fetch_data())
```

### Quick API

- `dvt.task.to_thread(func, *args, **kwargs)` - run a blocking callable in the instance threadpool from async code.
- `dvt.task.schedule(coro_or_callable, *args, type=None, **kwargs)` - schedule a coroutine or execute a sync callable in the threadpool; returns an `asyncio.task`.
- `dvt.task.run_blocking(func_or_coro, *args, **kwargs)` - run sync functions or coroutines synchronously (uses `asyncio.run()`); raises if called inside a running event loop.

## Best practices

- Call `dvt.shutdown()` to cleanly close the threadpool when the instance is no longer needed.
- For fire-and-forget of a sync callable, pass a `lambda` or `functools.partial` so the callable isn't executed eagerly.

## Development

Run tests locally:

```bash
python -m pytest -q
```

Contributions welcome - open an issue or PR.

## License

MIT. See `CHANGELOG.md` for release notes.