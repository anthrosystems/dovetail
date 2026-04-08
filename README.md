# Dovetail - Sync / Async wrapper for Python.

[![CI](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml/badge.svg)](https://github.com/anthrosystems/dovetail/actions/workflows/ci.yml) 
[![PyPI version](https://img.shields.io/pypi/v/dovetail.svg)](https://pypi.org/project/pydovetail/) 
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/anthrosystems/dovetail.svg)](https://github.com/anthrosystems/dovetail/releases/latest)

A lightweight helper for bridging sync and async code.
Minimal API, no runtime dependencies.

## Install

```bash
pip install pydovetail
```

## Quick Start

Create one `Dovetail` instance and use `dvt.task`:

```py
from dovetail import Dovetail

dvt = Dovetail()

# Sync caller -> sync function (blocks)
result = dvt.task.run_blocking(fetch_data)

# Sync caller -> sync function in threadpool (blocks)
result = dvt.task.to_thread_blocking(fetch_data, "target.json")

# Sync caller -> async function (blocks)
def get_remote():
    return dvt.task.run_blocking(fetch_data())

# Sync caller -> batch map in threadpool (blocks, bounded concurrency)
results = dvt.task.map_blocking(fetch_data, ["a.json", "b.json"], max_concurrency=4)

# Async caller -> sync function (await result)
result = await dvt.task.to_thread(fetch_data, "target.json")

# Async caller -> async function (await or fire-and-forget)
task = dvt.task.schedule(fetch_data())

# Global watch: run callback when any task ends
sub_id = dvt.events.on_end(some_function)

# Optional defaults (all opt-in)
dvt = Dovetail(
    max_workers=8,
    rate_limit_per_sec=20,      # cap task starts/sec
    rate_limit_burst=40,        # allow short bursts
    default_timeout=30.0,       # applies to async waits around tasks
    default_retries=2,          # retries for sync callables in to_thread/schedule
    default_retry_backoff=0.25, # exponential backoff base seconds
)
```

## API Summary

- `dvt.task.to_thread(func, *args, **kwargs)`: await a sync callable in Dovetail's threadpool; supports default timeout/retry settings.
- `dvt.task.to_thread_blocking(func, *args, **kwargs)`: synchronous wrapper for `to_thread` when you are not already in an event loop.
- `dvt.task.schedule(coro_or_callable, *args, type=None, **kwargs)`: create and return an `asyncio.Task` from a coroutine object, async function, or sync callable.
- `dvt.task.run_blocking(func_or_coro, *args, **kwargs)`: run async or sync work from synchronous code.
- `dvt.task.map_blocking(func, items, max_concurrency=None, return_exceptions=False)`: run ordered parallel map over `items` using the threadpool.
- `dvt.events.on_queued(...)`: listen for `task_queued`; callback receives one payload dict; returns a subscription id.
- `dvt.events.on_start(...)`: listen for `task_started`; callback receives one payload dict; returns a subscription id.
- `dvt.events.on_end(...)`: listen for `task_done`; callback receives one payload dict; returns a subscription id.
- `dvt.events.on_error(...)`: listen for `task_error`; callback receives one payload dict; returns a subscription id.
- `dvt.events.on_retry(...)`: listen for `task_retry`; callback receives one payload dict; returns a subscription id.
- `dvt.events.on_cancel(...)`: listen for `task_cancelled`; callback receives one payload dict; returns a subscription id.
- `dvt.events.stats()`: cumulative counters for this instance: `queued`, `started`, `done`, `error`, `retries`, `throttled`.

Listener scope parameters apply to all `dvt.events.on_*` methods:

- `function_target`: only events for that function/callable.
- `instance_target`: only events for that execution id.
- `allow_reentry=False`: block recursive reentry while callback is already active.
- `max_chain_depth=5`: maximum active callback depth when reentry is allowed.

## Detailed Guides

- [Event listeners and chaining](docs/event-listeners.md)
- [Observability (stats, payloads, tracing)](docs/observability.md)
- [Retry, timeout, and rate limit behavior](docs/retry-timeout-rate-limit.md)


## Event Listeners

Use listeners to observe lifecycle events and to trigger follow-up work.
Most users should scope listeners with `function_target`; `instance_target` is available for advanced per-instance control.

For full guidance, examples, and pitfalls, see [docs/event-listeners.md](docs/event-listeners.md).


## Status Model

Counters are available via `dvt.events.stats()` and lifecycle events are emitted during task execution.

For counter meanings and payload details, see [docs/observability.md](docs/observability.md).

## Tracing (Debug)

Tracing is opt-in via `trace=True` or `DOVETAIL_TRACE=true`.

For trace output details and configuration, see [docs/observability.md](docs/observability.md).
For retry/timeout/rate-limit semantics, see [docs/retry-timeout-rate-limit.md](docs/retry-timeout-rate-limit.md).

## Notes

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