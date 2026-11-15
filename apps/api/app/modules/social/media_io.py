"""Bounded streaming media I/O, pixel budget checks, and cross-loop concurrency gates."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import BinaryIO

import httpx
from PIL import Image

from app.core.observability import record_metric


@dataclass
class _GateWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    limit: int
    # State transitions: waiting -> notified -> acquired/released, or waiting -> cancelled.
    state: str = "waiting"


class AsyncConcurrencyGate:
    """Thread-safe, cross-event-loop bounded async semaphore with dynamic limit support."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self._waiters: deque[_GateWaiter] = deque()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    async def acquire(self, limit: int) -> None:
        """Acquire a concurrency slot, waiting cleanly across event loops if saturated."""
        effective_limit = max(1, limit)
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._in_flight < effective_limit:
                self._in_flight += 1
                return
            future = loop.create_future()
            waiter = _GateWaiter(loop=loop, future=future, limit=effective_limit)
            self._waiters.append(waiter)

        try:
            await future
        except BaseException:
            release_needed = False
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                    waiter.state = "cancelled"
                elif waiter.state in {"notified", "acquired"}:
                    waiter.state = "released"
                    release_needed = True
            if release_needed:
                self.release()
            raise

    def _wake_waiter(self, waiter: _GateWaiter) -> None:
        release_needed = False
        with self._lock:
            if waiter.state != "notified":
                return
            if waiter.future.done():
                waiter.state = "released"
                release_needed = True
            else:
                waiter.state = "acquired"
                waiter.future.set_result(None)
        if release_needed:
            self.release()

    def release(self) -> None:
        """Release a slot and hand it off to the next live waiter whose limit allows it."""
        while True:
            with self._lock:
                if self._in_flight > 0:
                    self._in_flight -= 1
                next_waiter: _GateWaiter | None = None
                skipped: list[_GateWaiter] = []
                while self._waiters:
                    candidate = self._waiters.popleft()
                    if (
                        candidate.state != "waiting"
                        or candidate.future.done()
                        or candidate.loop.is_closed()
                    ):
                        candidate.state = "cancelled"
                        continue
                    if self._in_flight < candidate.limit:
                        self._in_flight += 1
                        candidate.state = "notified"
                        next_waiter = candidate
                        break
                    skipped.append(candidate)
                if skipped:
                    self._waiters.extendleft(reversed(skipped))
            if next_waiter is None:
                return
            try:
                next_waiter.loop.call_soon_threadsafe(self._wake_waiter, next_waiter)
                return
            except RuntimeError:
                # Target event loop closed before callback scheduling; reclaim slot and retry.
                with self._lock:
                    if next_waiter.state != "notified":
                        return
                    next_waiter.state = "released"

    @asynccontextmanager
    async def slot(self, limit: int) -> AsyncIterator[None]:
        """Async context manager holding a bounded concurrency slot."""
        await self.acquire(limit)
        try:
            yield
        finally:
            self.release()


DECODE_GATE = AsyncConcurrencyGate()
OCR_GATE = AsyncConcurrencyGate()


def validate_image_dimensions(image: Image.Image, max_pixels: int) -> None:
    """Reject empty or decompression-bomb images before decoding full pixel buffers."""
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("无效图片尺寸")
    if max_pixels > 0 and width * height > max_pixels:
        raise ValueError(f"图片像素超过 {max_pixels} 上限 ({width}x{height})")


async def download_bounded_to_file(
    client: httpx.AsyncClient,
    url: str,
    target: BinaryIO,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int,
) -> int:
    """Stream response chunks into `target`, aborting as soon as `max_bytes` is exceeded."""
    total_bytes = 0
    try:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            content_encoding = (response.headers.get("content-encoding") or "").strip().lower()
            if (
                content_length is not None
                and content_length.strip().isdigit()
                and int(content_length.strip()) > max_bytes
                and content_encoding in {"", "identity"}
            ):
                raise ValueError(f"媒体大小超过 {max_bytes} 字节上限")
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(f"媒体大小超过 {max_bytes} 字节上限")
                target.write(chunk)
    finally:
        record_metric("media_bytes_downloaded", total_bytes)
    target.flush()
    target.seek(0)
    return total_bytes
