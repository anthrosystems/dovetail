# Event Listeners

Event listeners are the primary way to observe lifecycle events and trigger follow-up behavior.

## Lifecycle Methods

- dvt.events.on_queued(...) -> task_queued
- dvt.events.on_start(...) -> task_started
- dvt.events.on_end(...) -> task_done
- dvt.events.on_error(...) -> task_error
- dvt.events.on_retry(...) -> task_retry
- dvt.events.on_cancel(...) -> task_cancelled

Each method registers a subscription and returns a subscription id.

## Scoping

- No target: global listener for all matching events.
- function_target=<callable>: function-scoped listener. This is the most common and recommended pattern.
- instance_target=<execution_id>: instance-scoped listener. This is advanced and usually only needed for per-instance control.
- Passing both function_target and instance_target raises ValueError.

## Reentry and Depth

- allow_reentry=False (default): blocks callback reentry while callback is active.
- allow_reentry=True: allows nested callback reentry.
- max_chain_depth: maximum nested callback depth when reentry is enabled.

## Example

```py
import asyncio
from dovetail import Dovetail

dvt = Dovetail()

def step_a():
    # Stage A: starting point of the workflow.
    return "A"

def step_b():
    # Stage B: follow-up work triggered after A completes.
    return "B"

captured_instance_ids = []

def on_any_done(payload):
    # Global listener: fires for every completed task.
    print("GLOBAL done:", payload.get("function"), payload.get("execution_id"))

def on_a_start(payload):
    # Function-scoped listener: capture execution ids for step_a instances.
    captured_instance_ids.append(payload.get("execution_id"))

def on_a_end(payload):
    # Function-scoped chaining: when A ends, schedule B.
    dvt.task.schedule(step_b)

# Global listener
# Useful for telemetry/logging across all functions.
dvt.events.on_end(on_any_done)

# Function-scoped listeners
# Most common orchestration style.
dvt.events.on_start(on_a_start, function_target=step_a)
dvt.events.on_end(on_a_end, function_target=step_a)

async def main():
    task = dvt.task.schedule(step_a)
    await task
    await asyncio.sleep(0.05)

    # Optional instance-scoped listener (advanced/rare)
    if captured_instance_ids:
        one_instance_id = captured_instance_ids[0]
        dvt.events.on_end(
            lambda p: print("INSTANCE done:", p.get("function"), p.get("execution_id")),
            instance_target=one_instance_id,
        )

asyncio.run(main())
```

## Common Pitfall

```py
# Incorrect:
dvt.events.on_end(dvt.task.schedule(step_b), function_target=step_a)
```

Why it fails:

- on_end(...) expects a callback function as first argument.
- dvt.task.schedule(step_b) executes immediately and returns an asyncio.Task.
- asyncio.Task is not callable, so listener registration fails.

Correct pattern:

```py
def on_a_end(payload):
    dvt.task.schedule(step_b)

dvt.events.on_end(on_a_end, function_target=step_a)
```
