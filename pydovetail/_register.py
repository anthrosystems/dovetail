"""
Lightweight registry for tracking active Dovetail instances.

This module provides a global registry that keeps weakrefs to live
Dovetail instances so we can perform coordinated shutdown and warn on
leaked pools.
"""

from __future__ import annotations

import threading
import logging
import weakref

from typing import Callable, List, Optional

_log = logging.getLogger(__name__)


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Store weakref.ref objects
        self._items: set[weakref.ref] = set()
        self._app_hook: Optional[Callable[[], None]] = None

    def register(self, dvt) -> None:
        def _on_dead(wr: weakref.ref) -> None:
            with self._lock:
                self._items.discard(wr)

        # Avoid duplicate registration for the same object
        with self._lock:
            for existing in list(self._items):
                try:
                    if existing() is dvt:
                        return
                except Exception:
                    pass
            wr = weakref.ref(dvt, _on_dead)
            self._items.add(wr)

    def unregister(self, dvt) -> None:
        with self._lock:
            for wr in list(self._items):
                if wr() is dvt:
                    self._items.discard(wr)

    def list_active(self) -> List:
        with self._lock:
            items = []

            for wr in list(self._items):
                obj = wr()
                if obj is not None:
                    items.append(obj)

            return items

    def shutdown_all(self, wait: bool = True) -> None:
        # Best-effort shutdown of all active Dovetail instances.
        for dvt in self.list_active():
            try:
                dvt.shutdown(wait=wait)
            except Exception:
                _log.exception(
                    "Error shutting down Dovetail instance %r",
                    dvt,
                )

    def set_app_shutdown_hook(self, fn: Optional[Callable[[], None]]) -> None:
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
                    _log.exception("App shutdown hook raised an exception")
        finally:
            # fallback: shutdown all
            self.shutdown_all()


# Global registry used by the dovetail package.
global_registry = _Registry()


def register(dvt) -> None:
    global_registry.register(dvt)


def unregister(dvt) -> None:
    global_registry.unregister(dvt)


def list_active() -> List:
    return global_registry.list_active()


def shutdown_all(wait: bool = True) -> None:
    return global_registry.shutdown_all(wait=wait)


def set_app_shutdown_hook(fn: Optional[Callable[[], None]]) -> None:
    global_registry.set_app_shutdown_hook(fn)
