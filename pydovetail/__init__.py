"""
Package exports.

This package intentionally does not provide a module-level `dovetail`
instance. Consumers should instantiate `Dovetail()` where they need
an executor helper so they control lifetimes and configuration.
"""

import logging

from ._register import registry as DovetailRegistry
from .dovetail import Dovetail

logging.getLogger("dovetail").addHandler(logging.NullHandler())

__all__ = [
    "Dovetail",
    "DovetailRegistry"
]