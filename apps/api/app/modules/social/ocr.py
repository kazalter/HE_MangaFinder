import asyncio
import logging
import shutil
from hashlib import sha256
from io import BytesIO
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.core.config import Settings

logger = logging.getLogger(__name__)
ALLOWED_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


class LocalMediaOcr:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.social_media_dir.mkdir(parents=True, exist_ok=True)

    async def extract(self, media: list[dict[str, object]]) -> str | None:
        if not self.settings.social_ocr_enabled or not shutil.which("tesseract"):
            return None
        texts: list[str] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for item in media[:4]:
                url = str(item.get("url", ""))
                parsed = urlparse(url)
                if (
                    not url
                    or parsed.scheme not in {"http", "https"}
                    or parsed.hostname not in ALLOWED_MEDIA_HOSTS
                ):
                    continue
                try:
                    response = await client.get(
                        url, headers={"User-Agent": self.settings.user_agent}
                    )
                    response.raise_for_status()
                    raw = response.content
                    image = Image.open(BytesIO(raw))
                    image.verify()
                    suffix = ".png" if image.format == "PNG" else ".jpg"
                    path = self.settings.social_media_dir / f"{sha256(raw).hexdigest()}{suffix}"
                    if not path.exists():
                        path.write_bytes(raw)
                    result = await asyncio.create_subprocess_exec(
                        "tesseract",
                        str(path),
                        "stdout",
                        "-l",
                        "jpn+jpn_vert+eng",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await result.communicate()
                    text = stdout.decode("utf-8", errors="replace").strip()
                    if text:
                        texts.append(text[:6000])
                except Exception as exc:
                    logger.info("Social media OCR skipped %s: %s", url, exc)
        return "\n\n".join(texts) or None
