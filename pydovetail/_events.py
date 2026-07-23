"""
Register and dispatch lifecycle listeners for task execution events.

Listeners can be global (default) or scoped to either a function target
or one execution instance target.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import weakref

from typing import Any, Callable, Dict, Optional, TYPE_CHECKING
from enum import StrEnum

if TYPE_CHECKING:
    from .dovetail import Dovetail

class Event(StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    RETRY = "retry"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    CANCELLED = "cancelled"
    DONE = "done"

    def __str__(self) -> str:
        return self.value

class Events:
    def __init__(
        self,
        dovetail: "weakref.ReferenceType[Dovetail]",
        *,
        trace_enabled: bool = False,
        trace_logger: Optional[logging.Logger] = None,
        trace_prefix: str = "Dovetail",
    ) -> None:
        # Back-reference to the owning Dovetail instance. Used to emit
        # package-level events and consult instance-level configuration.
        self._dovetail = dovetail

        # Lock protecting listener registration tables. We take a coarse
        # lock here because registration/emission is relatively infrequent
        # compared to task execution and this keeps the implementation simple
        # and robust across threads.
        self._lock = threading.Lock()

        # Counter for generating subscription ids.
        self._event_counter = itertools.count(1)

        # Mapping of event name -> subscription id -> callback. Subscriptions
        # metadata is stored separately to allow efficient matching logic.
        self._listeners: Dict[str, Dict[str, Callable[[Dict[str, Any]], None]]] = {}
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        
        # Track decorated listener functions without mutating user functions.
        # Entries disappear automatically when functions are garbage collected.
        self._decorated_functions: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

        # Tracing configuration: feature flag and optional logger/prefix.
        # These are small and cheap to check from hot paths; the actual heavy
        # formatting is delegated to `_trace` helpers.
        self._trace_enabled = bool(trace_enabled)

        if trace_logger is None:
            log = logging.getLogger("dovetail")
            log.propagate = True
            self._trace_logger = log
        else:
            self._trace_logger = trace_logger

        self._trace_prefix = str(trace_prefix or "Dovetail")

        # Counters for cumulative statistics (queued/started/done/etc.). We
        # protect these with a dedicated lock so callers can increment
        # counters cheaply without contending on the main registration lock.
        self._stats_lock = threading.Lock()
        self._stats: Dict[str, int] = {
            "queued": 0,
            "started": 0,
            "done": 0,
            "error": 0,
            "retries": 0,
            "throttled": 0,
        }

    @property
    def dovetail(self) -> "Dovetail":
        dvt = self._dovetail()
        if dvt is None:
            raise RuntimeError("Dovetail instance has been garbage collected")
        return dvt
    
    @property
    def trace_enabled(self) -> bool:
        """Whether structured trace logging is enabled for this Dovetail instance."""
        return self._trace_enabled

    def trace(self, message: str) -> None:
        """Emit one trace log line when tracing is enabled."""
        # Delegate to a single helper so formatting and thread-info are
        # consistent across the package. Import locally to avoid any
        # import-time side-effects and to keep startup fast when tracing is
        # disabled.
        from ._trace import trace as _trace

        _trace(self._trace_enabled, self._trace_logger, self._trace_prefix, message)

    def trace_struct(
        self,
        method: str,
        status: str,
        task: Optional[Any] = None,
        function: Optional[str] = None,
        elapsed: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a multi-line structured trace record when tracing is enabled."""
        # Structured traces are emitted via the central helper which keeps
        # the output consistent and easy to search in logs.
        from ._trace import trace_struct as _trace_struct

        _trace_struct(
            self._trace_enabled,
            self._trace_logger,
            self._trace_prefix,
            method,
            status,
            task=task,
            function=function,
            elapsed=elapsed,
            extra=extra,
        )

    def inc_stat(self, key: str, count: int = 1) -> None:
        """Increment one cumulative stats counter."""
        # Atomic increment for counters. Missing keys are initialised to
        # zero so callers need not pre-create every counter.
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + int(count)

    def stats(self) -> Dict[str, int]:
        """Return cumulative execution counters for this instance."""
        # Return a copy to avoid exposing internal mutable state.
        with self._stats_lock:
            return dict(self._stats)

    @staticmethod
    def _event_name(event: Event | str) -> str:
        """Convert an Event enum or string into a canonical event name."""
        if isinstance(event, Event):
            return event.value

        return str(event).strip()
    
    def _register_listener(
        self,
        event: Event | str,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        execution_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register one listener and return its subscription id.

        Rules:
        - no target => global watcher
        - function_target only => all instances of one function
        - execution_target only => one specific execution
        - both set => ValueError
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        event_name = self._event_name(event)
        
        if not event_name:
            raise ValueError("event cannot be empty")
        
        if function_target is not None and execution_target is not None:
            raise ValueError("function_target and execution_target are mutually exclusive")
        
        if int(max_chain_depth) < 1:
            raise ValueError("max_chain_depth must be >= 1")

        # Create a subscription id and record matching metadata.
        sub_id = f"sub-{next(self._event_counter)}"
        function_key = self._function_scope_key(function_target) if function_target is not None else None
        instance_key = self._instance_scope_key(execution_target) if execution_target is not None else None

        # Registration performed under `_lock` so the listener tables are
        # always consistent when concurrently emitting events.
        with self._lock:
            self._listeners.setdefault(event_name, {})[sub_id] = callback
            self._subscriptions[sub_id] = {
                "subscription_id": sub_id,
                "event": event_name,
                "function_target": function_key,
                "execution_target": instance_key,
                "allow_reentry": bool(allow_reentry),
                "max_chain_depth": int(max_chain_depth),
                "active_depth": 0,
            }
        return sub_id
    
    def on(
        self,
        event: Event,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        *,
        function_target: Optional[Any] = None,
        execution_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str | Callable[[Dict[str, Any]], None]:
        """Register an event listener.

        Can be used directly:

            dvt.events.on(Event.DONE, callback)

        Or as a decorator:

            @dvt.events.on(Event.DONE)
            def callback(event):
                ...

        Returns:
            Subscription id when called directly.
            The original function when used as a decorator.
        """

        def decorator(
            func: Callable[[Dict[str, Any]], None]
        ) -> Callable[[Dict[str, Any]], None]:
            """Register a decorated event listener and return the original function."""

            if not callable(func):
                raise TypeError("decorated event listener must be callable")

            subscription_id = self._register_listener(
                event,
                func,
                function_target=function_target,
                execution_target=execution_target,
                allow_reentry=allow_reentry,
                max_chain_depth=max_chain_depth,
            )

            # Store the subscription id externally instead of mutating the user's function object.
            with self._lock:
                self._decorated_functions[func] = subscription_id

            return func

        if callback is not None:
            return self._register_listener(
                event,
                callback,
                function_target=function_target,
                execution_target=execution_target,
                allow_reentry=allow_reentry,
                max_chain_depth=max_chain_depth,
            )

        return decorator
    
    def off(self, subscription_id: str | Callable[..., Any]) -> bool:
        """Remove an event listener subscription.

        Accepts either:
            - a subscription id returned by ``on()``
            - a decorated listener function

        Returns:
            True if the subscription existed and was removed,
            False otherwise.
        """

        callback = None

        if callable(subscription_id):
            callback = subscription_id
            with self._lock:
                subscription_id = self._decorated_functions.get(callback)

            if subscription_id is None:
                return False

        with self._lock:
            subscription = self._subscriptions.pop(subscription_id, None)

            if subscription is None:
                return False

            event_name = subscription["event"]

            listeners = self._listeners.get(event_name)
            if listeners is not None:
                listeners.pop(subscription_id, None)

                if not listeners:
                    self._listeners.pop(event_name, None)

        if callback is not None:
            try:
                del self._decorated_functions[callback]
            except AttributeError:
                pass

        return True
    
    def clear(self) -> int:
        """Remove all event listener subscriptions.

        Returns:
            Number of subscriptions removed.
        """
        with self._lock:
            count = len(self._subscriptions)

            self._listeners.clear()
            self._subscriptions.clear()
            self._decorated_functions.clear()

            return count

    def emit(
        self,
        event: Event,
        payload: Dict[str, Any],
    ) -> int:
        """Dispatch one emitted event payload to all matching listeners.

        Returns the number of callbacks invoked.
        """
        event_name = self._event_name(event)
        if not event_name:
            return 0

        data = dict(payload or {})
        function_key = self._function_scope_key(data.get("function")) if data.get("function") is not None else None
        instance_key = self._payload_instance_key(data)

        # Snapshot the global listeners under lock then release the lock
        # before performing matching. This prevents holding `_lock` during
        # callback invocation which could lead to deadlocks if callbacks
        # register/unregister listeners.
        with self._lock:
            global_listeners = list(self._listeners.get(event_name, {}).items())

        # First pass: filter the snapshot to the set of listeners that
        # should receive this event. We consult subscription metadata for
        # function/instance scoping without holding the lock while invoking
        # callbacks later.
        matched: list[tuple[str, Callable[[Dict[str, Any]], None]]] = []
        for sub_id, callback in global_listeners:
            with self._lock:
                sub = self._subscriptions.get(sub_id)
            if sub is None:
                # Subscription removed concurrently.
                continue
            sub_function = sub.get("function_target")
            sub_instance = sub.get("execution_target")
            if sub_function is not None and sub_function != function_key:
                continue
            if sub_instance is not None and sub_instance != instance_key:
                continue
            matched.append((sub_id, callback))

        # Second pass: invoke matched callbacks. We maintain an `active_depth`
        # counter per subscription to prevent infinite re-entry. The active
        # depth is incremented immediately before invoking the callback and
        # decremented afterwards under the lock.
        called = 0
        for sub_id, callback in matched:
            with self._lock:
                sub = self._subscriptions.get(sub_id)
                if sub is None:
                    continue
                active_depth = int(sub.get("active_depth", 0))
                allow_reentry = bool(sub.get("allow_reentry", False))
                max_chain_depth = max(1, int(sub.get("max_chain_depth", 5)))
                # If re-entry is disallowed and we're already in the callback,
                # skip invocation. If re-entry is allowed, still cap depth by
                # `max_chain_depth` to avoid runaway recursion.
                if not allow_reentry and active_depth > 0:
                    continue
                if allow_reentry and active_depth >= max_chain_depth:
                    continue
                sub["active_depth"] = active_depth + 1
            try:
                # Provide a shallow copy of `data` to each listener to avoid
                # accidental cross-listener mutation.
                callback(dict(data))
                called += 1
            except Exception as exc:
                # Trace the listener error but do not allow it to propagate.
                self.trace(f"events callback error event={event_name}: {exc}")
            finally:
                with self._lock:
                    sub = self._subscriptions.get(sub_id)
                    if sub is not None:
                        sub["active_depth"] = max(0, int(sub.get("active_depth", 1)) - 1)
        return called

    @staticmethod
    def _instance_scope_key(task: Any) -> str:
        # For asyncio.Task objects use the object's identity as the stable
        # instance key. For other types fall back to the string form which
        # is typically adequate for user-provided identifiers.
        if isinstance(task, asyncio.Task):
            return str(id(task))
        return str(task)

    @staticmethod
    def _function_scope_key(func: Any) -> str:
        # Prefer `__qualname__` since it includes class context for bound
        # methods (e.g. "Class.method") and is more helpful when matching
        # listeners scoped to a function target.
        if hasattr(func, "__qualname__"):
            return str(getattr(func, "__qualname__"))
        if hasattr(func, "__name__"):
            return str(getattr(func, "__name__"))
        return str(func)

    def _payload_instance_key(self, payload: Dict[str, Any]) -> Optional[str]:
        """Extract the instance key used for instance-target listener matching."""
        execution_id = payload.get("execution_id")
        if execution_id is not None:
            return str(execution_id)
        task = payload.get("task")
        if task is not None:
            return self._instance_scope_key(task)
        return None