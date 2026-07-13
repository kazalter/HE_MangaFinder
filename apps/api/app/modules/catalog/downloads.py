import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import ProviderCapability
from app.providers.registry import ProviderRegistry


class DownloadService:
    def __init__(
        self, session: Session, providers: ProviderRegistry, settings: Settings
    ) -> None:
        self.session = session
        self.providers = providers
        self.settings = settings

    async def download(self, work_id: int, provider_name: str, chapter_id: str) -> str:
        source = CatalogRepository(self.session).get_source(work_id, provider_name)
        if source is None:
            raise ValueError("作品来源不存在")
        provider = self.providers.get(provider_name)
        if ProviderCapability.DOWNLOAD not in provider.capabilities:
            raise ValueError("该来源不支持下载")

        work_folder = self._safe_name(source.work.title, f"work-{work_id}")
        chapter_name = self._safe_name(chapter_id, "chapter")
        destination = self.settings.downloads_dir / work_folder / f"{chapter_name}.cbz"
        return await provider.download_chapter(
            source.external_id, chapter_id, str(destination)
        )

    @staticmethod
    def _safe_name(value: str, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
        return (cleaned or fallback)[:120]


def safe_download_path(path_value: str, settings: Settings) -> Path | None:
    root = settings.downloads_dir.resolve()
    path = Path(path_value).resolve()
    if path.is_file() and path.is_relative_to(root):
        return path
    return None
