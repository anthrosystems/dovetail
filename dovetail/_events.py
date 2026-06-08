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
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .dovetail import Dovetail


class Events:
    def __init__(
        self,
        dovetail: "Dovetail",
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
        self._listeners_global: Dict[str, Dict[str, Callable[[Dict[str, Any]], None]]] = {}
        self._subscriptions: Dict[str, Dict[str, Any]] = {}

        # Tracing configuration: feature flag and optional logger/prefix.
        # These are small and cheap to check from hot paths; the actual heavy
        # formatting is delegated to `_trace` helpers.
        self._trace_enabled = bool(trace_enabled)
        self._trace_logger = trace_logger or logging.getLogger("dovetail")
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

    def _register_listener(
        self,
        event: str,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register one listener and return its subscription id.

        Rules:
        - no target => global watcher
        - function_target only => all instances of one function
        - instance_target only => one specific execution
        - both set => ValueError
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        event_name = str(event or "").strip()
        if not event_name:
            raise ValueError("event cannot be empty")
        if function_target is not None and instance_target is not None:
            raise ValueError("function_target and instance_target are mutually exclusive")
        if int(max_chain_depth) < 1:
            raise ValueError("max_chain_depth must be >= 1")

        # Create a subscription id and record matching metadata.
        sub_id = f"sub-{next(self._event_counter)}"
        function_key = self._function_scope_key(function_target) if function_target is not None else None
        instance_key = self._instance_scope_key(instance_target) if instance_target is not None else None

        # Registration performed under `_lock` so the listener tables are
        # always consistent when concurrently emitting events.
        with self._lock:
            self._listeners_global.setdefault(event_name, {})[sub_id] = callback
            self._subscriptions[sub_id] = {
                "subscription_id": sub_id,
                "event": event_name,
                "function_target": function_key,
                "instance_target": instance_key,
                "allow_reentry": bool(allow_reentry),
                "max_chain_depth": int(max_chain_depth),
                "active_depth": 0,
            }
        return sub_id

    def on_queued(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for task queueing events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_queued",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )

    def on_start(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for task start events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_started",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )
    
    def on_retry(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for retry events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_retry",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )

    def on_error(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for failure events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_error",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )
    
    def on_cancel(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for cancellation events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_cancelled",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )
    
    def on_end(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        function_target: Optional[Any] = None,
        instance_target: Optional[Any] = None,
        allow_reentry: bool = False,
        max_chain_depth: int = 5,
    ) -> str:
        """Register a callback for successful completion events.

        Callback receives the emitted payload dictionary.
        Returns a subscription id.
        """
        return self._register_listener(
            "task_done",
            callback,
            function_target=function_target,
            instance_target=instance_target,
            allow_reentry=allow_reentry,
            max_chain_depth=max_chain_depth,
        )

    def emit(self, event: str, payload: Dict[str, Any]) -> int:
        """Dispatch one emitted event payload to all matching listeners.

        Returns the number of callbacks invoked.
        """
        event_name = str(event or "").strip()
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
            global_listeners = list(self._listeners_global.get(event_name, {}).items())

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
            sub_instance = sub.get("instance_target")
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
