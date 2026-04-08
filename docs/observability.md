# Observability

Dovetail observability is exposed through events and trace logging.

## Counters

Use dvt.events.stats() to retrieve cumulative counters for one Dovetail instance.

Available counters:

- queued: task accepted for execution
- started: execution attempt started
- done: task completed successfully
- error: task failed (or timed out) after retries
- retries: retry attempts triggered
- throttled: task start delayed by rate limiter

## Event Payloads

Lifecycle events carry payload dictionaries. Common fields include:

- method
- task
- function
- execution_id

Event-specific fields may include:

- attempt
- elapsed
- error
- sleep
- waited

## Tracing

Enable tracing with:

- constructor flag: trace=True
- environment variable: DOVETAIL_TRACE=true

Customise trace output:

```py
import logging
from dovetail import Dovetail

logger = logging.getLogger("myapp.dovetail")
dvt = Dovetail(trace=True, trace_logger=logger, trace_prefix="DownloaderPool")
```

When enabled, trace logs include lifecycle status, function name, thread details, and elapsed time.

## Practical Pattern

- Use [on_* listeners](docs/event-listeners.md) for structured observability actions.
- Use stats() for cumulative counters.
- Use trace logs for low-level timing/thread execution visibility.
