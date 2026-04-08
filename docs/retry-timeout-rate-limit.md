# Retry, Timeout, and Rate Limit

This guide explains how execution behavior defaults work and how they interact.

## Constructor Defaults

Set behavior globally per Dovetail instance:

- default_retries
- default_retry_backoff
- default_timeout
- rate_limit_per_sec
- rate_limit_burst

Example:

```py
from dovetail import Dovetail

dvt = Dovetail(
    default_retries=2,
    default_retry_backoff=0.25,
    default_timeout=30.0,
    rate_limit_per_sec=20,
    rate_limit_burst=40,
)
```

## Retries

Retries apply to sync callable execution paths in task.to_thread(...) and callable scheduling paths.

Behavior:

- Retries happen after failures until retry budget is exhausted.
- Backoff uses exponential growth from default_retry_backoff.
- Retry attempts increment retries counter and emit task_retry.

## Timeouts

Timeouts apply to async waiting around task execution.

Behavior:

- Timeout raises asyncio.TimeoutError to caller.
- Timeout increments error counter and emits task_error.
- Timing out a wait does not force-stop a currently running thread function.

## Rate Limiting

Rate limiting controls task start throughput using a token-bucket strategy.

Behavior:

- Starts may be delayed when token budget is exhausted.
- Delays increment throttled counter.
- Rate-limited starts emit task_rate_limited with waited duration.

## Tuning Suggestions

- Increase default_retries for transient network or API instability.
- Keep default_retry_backoff non-zero to reduce pressure during repeated failures.
- Use default_timeout to prevent indefinitely waiting call paths.
- Set rate_limit_per_sec and rate_limit_burst to protect upstream services.
