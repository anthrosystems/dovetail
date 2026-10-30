# Migration and concurrency guide

Dovetail 3 requires Python 3.10 or later and has no runtime dependencies.

## Mapping blocking functions from async code

```python
results = await dvt.task.map(validate_gallery, batch, max_concurrency=8,
                             return_exceptions=True)
```

`map` preserves input order, limits active calls, and optionally returns exception
objects at their corresponding positions. Without `return_exceptions`, the first
exception is raised and outstanding asyncio jobs are cancelled. Python cannot
forcibly terminate a function already running in a worker thread.

Use `dvt.task.map_blocking(...)` from synchronous code only; do not await it.
For `manga-scraper-mirror`, this API replaces the manual `asyncio.gather` /
`to_thread` workaround while preserving a maximum of eight active calls.

## Timeouts and shutdown

Timeouts cancel the asyncio wait, **not** the underlying Python thread.
Running functions may complete later, including after a timeout event.
Always design blocking callables to be idempotent or cooperatively cancellable.
Shutdown can wait for running worker functions; avoid invoking blocking shutdown
from a worker callback. Prefer explicit `shutdown()` or a context manager.

## Lifecycle events

`to_thread` worker lifecycle callbacks are delivered on the originating event
loop when it is running. Do not rely on worker-thread execution of listeners.
