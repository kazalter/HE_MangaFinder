import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.core.config import Settings

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


@dataclass(frozen=True)
class OcrResult:
    status: str
    text: str | None = None
    error: str | None = None


class LocalMediaOcr:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
        failures = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
                        response = await client.get(
                            url, headers={"User-Agent": self.settings.user_agent}
                        )
                        response.raise_for_status()
                        raw = response.content
                        image = Image.open(BytesIO(raw))
                        image.verify()
                        suffix = ".png" if image.format == "PNG" else ".jpg"
                        with tempfile.TemporaryDirectory(prefix="mangafinder-ocr-") as temp:
                            path = f"{temp}/{sha256(raw).hexdigest()}{suffix}"
                            with open(path, "wb") as temporary:
                                temporary.write(raw)
                            stdout = await self._recognize(path)
                    if stdout is None:
                        failures.append("OCR 超时或执行失败")
                        continue
                    text = stdout.decode("utf-8", errors="replace").strip()
                    if text:
                        texts.append(text[:6000])
                except Exception as exc:
                    logger.info("Social media OCR skipped %s: %s", url, exc)
                    failures.append(type(exc).__name__)
        text = "\n\n".join(texts) or None
        if failures:
            return OcrResult("retry", text=text, error="; ".join(failures)[:1000])
        return OcrResult("text" if text else "empty", text=text)

    async def _recognize(self, path: str) -> bytes | None:
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
            result.kill()
            await result.communicate()
            return None
