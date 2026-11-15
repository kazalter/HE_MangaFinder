"""Lightweight, secret-safe observability counters for the social collector."""

from __future__ import annotations

import os
import resource
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

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


def process_memory_kib() -> dict[str, int]:
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
class CollectorMetrics:
    counters: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, name: str, delta: int = 1) -> None:
        if delta == 0:
            return
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + delta

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            filtered = {
                k: v
                for k, v in self.counters.items()
                if not any(part in k.casefold() for part in _SENSITIVE_KEY_PARTS)
            }
            return {
                "counters": filtered,
                "memory": process_memory_kib(),
            }


_GLOBAL_METRICS = CollectorMetrics()
_SCOPED_METRICS: ContextVar[CollectorMetrics | None] = ContextVar(
    "collector_scoped_metrics", default=None
)


def global_metrics() -> CollectorMetrics:
    return _GLOBAL_METRICS


def record_metric(name: str, delta: int = 1) -> None:
    _GLOBAL_METRICS.inc(name, delta)
    scoped = _SCOPED_METRICS.get()
    if scoped is not None:
        scoped.inc(name, delta)


@contextmanager
def capture_metrics() -> Iterator[CollectorMetrics]:
    scoped = CollectorMetrics()
    token = _SCOPED_METRICS.set(scoped)
    try:
        yield scoped
    finally:
        _SCOPED_METRICS.reset(token)


def metrics_log_enabled() -> bool:
    return os.getenv("SOCIAL_COLLECTOR_METRICS_LOG", "1") != "0"
