"""
Package exports.

This package intentionally does not provide a module-level `dovetail`
instance. Consumers should instantiate `Dovetail()` where they need
an executor helper so they control lifetimes and configuration.
"""

from .dovetail import Dovetail

__all__ = ["Dovetail"]