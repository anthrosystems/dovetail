"""
Centralised, lightweight tracing helpers used by Dovetail.

This module contains two convenience helpers used throughout the
`dovetail` package to produce consistent debug traces. They are deliberately
small and dependency-free so importing them is cheap and side-effect free.

Design notes:
- Tracing is gated by an explicit `enabled` flag so callers can cheaply
  check tracing without constructing messages.
- A `logger` may be supplied; if omitted we fall back to a logger named by
  the `prefix` (this mirrors prior behaviour and keeps logs grouped).
- `trace_struct` emits multi-line debug output to make long structured
  records easier to read in logs (useful for tracing task lifecycle events).
"""

from __future__ import annotations

import threading
import logging
from typing import Any, Dict, Optional


def trace(enabled: bool, logger: Optional[logging.Logger], prefix: str, message: str) -> None:
    """Emit a single-line debug trace when `enabled` is true.

    Parameters
    - enabled: feature flag to avoid constructing messages when tracing is off.
    - logger: optional logger instance; falls back to `logging.getLogger(prefix)`.
    - prefix: short textual prefix included in each trace line (e.g. "Dovetail").
    - message: the human-readable trace message.

    This function is intentionally minimal: when tracing is disabled it
    returns immediately, avoiding the cost of formatting complex messages.
    """
    if not enabled:
        return

    lg = logger or logging.getLogger(str(prefix or "dovetail"))
    current = threading.current_thread()
    ident = threading.get_ident() # Include thread name and id to help correlate traces in multi-threaded runs.
    native_id = threading.get_native_id() if hasattr(threading, "get_native_id") else None
    if native_id is not None:
        lg.debug("[%s][python thread=%s#%s | cpu thread=%s] %s", prefix, current.name, ident, native_id, message)
    else:
        lg.debug("[%s][thread=%s#%s] %s", prefix, current.name, ident, message)


def trace_struct(
    enabled: bool,
    logger: Optional[logging.Logger],
    prefix: str,
    method: str,
    status: str,
    task: Optional[Any] = None,
    function: Optional[str] = None,
    elapsed: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a multi-line, structured debug trace record. The multi-line form improves readability when
    inspecting logs for task lifecycle events (start/stop/retry/timeout).

    Fields `task`, `function`, `elapsed` and the optional `extra` mapping
    are appended only when present to keep the output concise.
    """
    if not enabled:
        return

    lg = logger or logging.getLogger(str(prefix or "dovetail"))
    current = threading.current_thread()

    # Build the trace record as lines, then emit once. This avoids multiple
    # logger calls and keeps the related information grouped together.
    ident = threading.get_ident()
    native_id = threading.get_native_id() if hasattr(threading, "get_native_id") else None
    if native_id is not None:
        lines = [f"[{prefix}] Python Thread: {current.name}#{ident} (CPU Thread #{native_id}):"]
    else:
        lines = [f"[{prefix}] Thread: {current.name}#{ident}:"]

    detail_parts = []
    if task is not None:
        detail_parts.append(f"Task: {task}")
    if function:
        detail_parts.append(f"Function: {function}")
    detail_parts.append(f"Method: {method}")
    lines.append(" | ".join(detail_parts))

    status_parts = [f"Status: {status}"]
    if elapsed is not None:
        status_parts.append(f"Elapsed: {elapsed:.3f}s")
    lines.append(" | ".join(status_parts))

    if extra:
        for key, value in extra.items():
            if value is None:
                continue
            lines.append(f"{key}: {value}")

    lg.debug("\n".join(lines))