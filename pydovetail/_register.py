"""
Lightweight registry for tracking active Dovetail instances.

This module provides a global registry that keeps weakrefs to live
Dovetail instances so we can perform coordinated shutdown and warn on
leaked pools.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable

_logger = logging.getLogger(__name__)

__all__ = [
    "registry",
]


class DovetailRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Store weakref.ref objects
        self._items: set[weakref.ref] = set()
        self._app_hook: Callable[[], None] | None = None

    def register(self, dvt) -> None:
        def _on_dead(wr: weakref.ref) -> None:
            with self._lock:
                self._items.discard(wr)

        # Avoid duplicate registration for the same object
        try:
            with self._lock:
                for existing in list(self._items):
                    if existing() is dvt:
                        return
                wr = weakref.ref(dvt, _on_dead)
                self._items.add(wr)
        except Exception:
            _logger.warning(
                "Failed to add instance %r to the global registry",
                dvt,
                exc_info=True,
            )

    def list_active(self) -> list:
        with self._lock:
            items = []

            for wr in list(self._items):
                obj = wr()
                if obj is not None:
                    items.append(obj)

            return items

    def shutdown(self, wait: bool = True) -> None:
        """Shut down all registered Dovetail instances.

        Each active instance is asked to perform its own shutdown cleanup,
        including releasing resources and unregistering itself from the registry.
        """
        # Best-effort shutdown of all active Dovetail instances.
        for dvt in self.list_active():
            try:
                dvt.shutdown(wait=wait)
            except Exception:
                _logger.exception(
                    "Error shutting down Dovetail instance id=%s",
                    id(dvt),
                )

    def set_app_shutdown_hook(self, fn: Callable[[], None] | None) -> None:
        with self._lock:
            self._app_hook = fn

    def call_app_hook_or_shutdown(self) -> None:
        # If an application provided a hook, call it. Otherwise perform
        # a best-effort shutdown of all registered pools.
        try:
            hook = None
            with self._lock:
                hook = self._app_hook
            if hook:
                try:
                    hook()
                    return
                except Exception:
                    _logger.exception("App shutdown hook raised an exception")
        finally:
            # fallback: shutdown all
            self.shutdown()
    
    def unregister(self, dvt) -> None:
            try:
                with self._lock:
                    for wr in list(self._items):
                        if wr() is dvt:
                            self._items.discard(wr)
            except Exception:
                _logger.warning(
                    "Failed to remove instance %r from the global registry",
                    dvt,
                    exc_info=True,
                )

registry = DovetailRegistry()