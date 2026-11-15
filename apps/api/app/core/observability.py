"""Lightweight, secret-safe per-operation observability counters and memory helpers."""

from __future__ import annotations

import json
import logging
import os
import resource
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("app.observability")

_SENSITIVE_KEY_PARTS = (
    "cookie",
    "token",
    "secret",
    "password",
    "authorization",
    "csrf",
    "api_key",
    "apikey",
    "session",
)


def _is_sensitive_key(key: str) -> bool:
    folded = key.casefold()
    return any(part in folded for part in _SENSITIVE_KEY_PARTS)


def sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of fields with any credential-bearing keys removed."""
    return {key: value for key, value in fields.items() if not _is_sensitive_key(key)}


def process_memory_kib() -> dict[str, int]:
    """Read current RSS and peak RSS (KiB) from Linux procfs / rusage."""
    rss_kib = 0
    vm_hwm_kib = 0
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    vm_hwm_kib = int(line.split()[1])
    except OSError:
        pass
    peak_rusage_kib = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "rss_kib": rss_kib,
        "peak_rss_kib": max(vm_hwm_kib, peak_rusage_kib),
    }


@dataclass
class OperationMetrics:
    """Thread-safe counter and attribute bag for one logical job or scan."""

    operation: str
    counters: dict[str, int] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _started_wall: float = field(default_factory=time.monotonic, repr=False)
    _started_cpu: float = field(default_factory=time.process_time, repr=False)

    def inc(self, name: str, delta: int = 1) -> None:
        if delta == 0:
            return
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + delta

    def set_attr(self, name: str, value: Any) -> None:
        if _is_sensitive_key(name):
            return
        with self._lock:
            self.attributes[name] = value

    def snapshot(self) -> dict[str, Any]:
        wall_ms = round((time.monotonic() - self._started_wall) * 1000, 3)
        cpu_ms = round((time.process_time() - self._started_cpu) * 1000, 3)
        with self._lock:
            return {
                "operation": self.operation,
                "wall_ms": wall_ms,
                "cpu_ms": cpu_ms,
                "counters": dict(self.counters),
                "attributes": sanitize_fields(self.attributes),
                "memory": process_memory_kib(),
            }


_CURRENT_METRICS: ContextVar[OperationMetrics | None] = ContextVar(
    "mangafinder_operation_metrics", default=None
)


def current_metrics() -> OperationMetrics | None:
    return _CURRENT_METRICS.get()


def record_metric(name: str, delta: int = 1) -> None:
    metrics = _CURRENT_METRICS.get()
    if metrics is not None:
        metrics.inc(name, delta)


def set_metric_attr(name: str, value: Any) -> None:
    metrics = _CURRENT_METRICS.get()
    if metrics is not None:
        metrics.set_attr(name, value)


@contextmanager
def capture_metrics(
    operation: str, *, log_summary: bool = False
) -> Iterator[OperationMetrics]:
    """Activate an OperationMetrics scope; nested scopes propagate counters upward."""
    parent = _CURRENT_METRICS.get()
    metrics = OperationMetrics(operation=operation)
    token = _CURRENT_METRICS.set(metrics)
    try:
        yield metrics
    finally:
        _CURRENT_METRICS.reset(token)
        snap = metrics.snapshot()
        if parent is not None:
            for key, value in snap["counters"].items():
                parent.inc(key, value)
        if log_summary and os.getenv("MANGAFINDER_METRICS_LOG", "1") != "0":
            logger.info("metrics.summary %s", json.dumps(snap, ensure_ascii=False, sort_keys=True))
