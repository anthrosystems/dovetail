"""
Package exports.

This package intentionally does not provide a module-level `dovetail`
instance. Consumers should instantiate `Dovetail()` where they need
an executor helper so they control lifetimes and configuration.
"""

from .dovetail import Dovetail
from ._events import Event
from ._register import (
	register,
	unregister,
	list_active,
	shutdown_all,
	set_app_shutdown_hook,
)

__all__ = [
    "Dovetail",
	"Event",
	"register",
	"unregister",
	"list_active",
	"shutdown_all",
	"set_app_shutdown_hook",
]