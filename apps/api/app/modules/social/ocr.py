import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps

from app.core.config import Settings
from app.core.observability import record_metric
from app.modules.social.media_io import (
    DECODE_GATE,
    OCR_GATE,
    download_bounded_to_file,
    validate_image_dimensions,
)

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}
_FORMAT_SUFFIXES = {
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
}


@dataclass(frozen=True)
class OcrResult:
    status: str
    text: str | None = None
    error: str | None = None


def _prepare_ocr_image(raw_path: Path, temp_dir: Path, *, max_pixels: int) -> Path:
    """Validate dimensions and integrity, normalize EXIF orientation, and release pixels."""
    with Image.open(raw_path) as opened:
        validate_image_dimensions(opened, max_pixels)
        opened.load()
        orientation = opened.getexif().get(0x0112, 1)
        if orientation in {2, 3, 4, 5, 6, 7, 8}:
            normalized_path = temp_dir / "normalized.png"
            transposed = ImageOps.exif_transpose(opened)
            try:
                transposed.save(normalized_path, format="PNG")
            finally:
                if transposed is not opened:
                    transposed.close()
            return normalized_path
        suffix = _FORMAT_SUFFIXES.get(opened.format or "", ".jpg")
    target_path = temp_dir / f"source{suffix}"
    raw_path.replace(target_path)
    return target_path


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate and reap a child process so timeouts/cancellations leave no orphans."""
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=5.0)
    except BaseException:
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()
        raise


class LocalMediaOcr:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.settings.social_media_dir.mkdir(parents=True, exist_ok=True)

    async def extract(
        self,
        media: list[dict[str, object]],
        local_paths: dict[int, object] | None = None,
    ) -> str | None:
        return (await self.extract_result(media, local_paths)).text

    async def extract_result(
        self,
        media: list[dict[str, object]],
        local_paths: dict[int, object] | None = None,
    ) -> OcrResult:
        images = [
            (index, item)
            for index, item in enumerate(media[:4])
            if item.get("type") == "image" and item.get("url")
        ]
        if not images:
            return OcrResult("not_applicable")
        if not self.settings.social_ocr_enabled:
            return OcrResult("disabled")
        if not shutil.which("tesseract"):
            return OcrResult("retry", error="OCR 程序不可用")
        texts: list[str] = []
        failures: list[str] = []
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            proxy=self.settings.x_proxy_url or None,
        )
        try:
            for index, item in images:
                url = str(item.get("url", ""))
                parsed = urlparse(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or parsed.hostname not in ALLOWED_MEDIA_HOSTS
                ):
                    failures.append("不允许的图片地址")
                    continue
                try:
                    supplied = (local_paths or {}).get(index)
                    if supplied:
                        stdout = await self._recognize(str(supplied))
                    else:
                        with tempfile.TemporaryDirectory(
                            prefix="mangafinder-ocr-",
                            dir=self.settings.social_media_dir,
                        ) as temp:
                            temp_dir = Path(temp)
                            raw_path = temp_dir / "download.bin"
                            with open(raw_path, "wb") as target:
                                await download_bounded_to_file(
                                    client,
                                    url,
                                    target,
                                    headers={"User-Agent": self.settings.user_agent},
                                    max_bytes=self.settings.social_ocr_max_image_bytes,
                                )
                            async with DECODE_GATE.slot(
                                self.settings.social_media_decode_concurrency
                            ):
                                prepared_path = await asyncio.to_thread(
                                    _prepare_ocr_image,
                                    raw_path,
                                    temp_dir,
                                    max_pixels=self.settings.social_ocr_max_image_pixels,
                                )
                            stdout = await self._recognize(str(prepared_path))
                    if stdout is None:
                        failures.append("OCR 超时或执行失败")
                        continue
                    text = stdout.decode("utf-8", errors="replace").strip()
                    if text:
                        texts.append(text[:6000])
                except Exception as exc:
                    logger.info("Social media OCR skipped %s: %s", url, exc)
                    failures.append(type(exc).__name__)
        finally:
            if owns_client:
                await client.aclose()
        text = "\n\n".join(texts) or None
        if failures:
            return OcrResult("retry", text=text, error="; ".join(failures)[:1000])
        return OcrResult("text" if text else "empty", text=text)

    async def _recognize(self, path: str) -> bytes | None:
        async with OCR_GATE.slot(self.settings.social_ocr_concurrency):
            record_metric("ocr_calls")
            result = await asyncio.create_subprocess_exec(
                "tesseract",
                path,
                "stdout",
                "-l",
                "jpn+eng",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    result.communicate(), timeout=self.settings.social_ocr_timeout_seconds
                )
                return stdout if result.returncode == 0 else None
            except TimeoutError:
                await _terminate_process(result)
                return None
            except BaseException:
                await _terminate_process(result)
                raise
