import asyncio
import re
import shutil
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Work
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import ProviderCapability
from app.providers.registry import ProviderRegistry

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


@dataclass(frozen=True, slots=True)
class DownloadVerification:
    path: str
    sha256: str
    byte_size: int
    page_count: int
    comic_info: bool


@dataclass(frozen=True, slots=True)
class DownloadStorageStats:
    root: str
    file_count: int
    byte_size: int
    capacity_bytes: int
    free_bytes: int
    quota_bytes: int
    quota_used_ratio: float | None


class DownloadService:
    def __init__(self, session: Session, providers: ProviderRegistry, settings: Settings) -> None:
        self.session = session
        self.providers = providers
        self.settings = settings

    async def download(
        self, work_id: int, provider_name: str, chapter_id: str
    ) -> tuple[str, DownloadVerification]:
        source = CatalogRepository(self.session).get_source(work_id, provider_name)
        if source is None:
            raise ValueError("作品来源不存在")
        provider = self.providers.get_optional(provider_name)
        if provider is None:
            raise ValueError("该来源已停用，只保留历史记录")
        if ProviderCapability.DOWNLOAD not in provider.capabilities:
            raise ValueError("该来源不支持下载")

        work_folder = f"work-{work_id}"
        identity = sha256(f"{provider_name}\0{chapter_id}".encode()).hexdigest()[:24]
        destination = self.settings.downloads_dir / work_folder / f"{identity}.cbz"
        output = await provider.download_chapter(source.external_id, chapter_id, str(destination))
        path = safe_download_path(output, self.settings)
        if path is None:
            raise ValueError("下载来源返回了无效的文件路径")
        fields = self._comic_info_fields(source.work, chapter_id, source.source_url)
        await asyncio.to_thread(self._write_comic_info, path, fields)
        verification = await asyncio.to_thread(verify_cbz, path, self.settings)
        return str(path), verification

    @staticmethod
    def _safe_name(value: str, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")
        return (cleaned or fallback)[:120]

    @staticmethod
    def _comic_info_fields(work: Work, chapter_id: str, source_url: str) -> dict[str, object]:
        # Resolve ORM relationships on the event-loop thread before archive I/O.
        return {
            "Title": work.title,
            "Series": (
                work.group_membership.group.title
                if work.group_membership and work.group_membership.group
                else work.title
            ),
            "Number": chapter_id,
            "Summary": work.description,
            "Year": work.year,
            "Writer": ", ".join(author.name for author in work.authors),
            "Genre": ", ".join(work.tags or []),
            "LanguageISO": work.language,
            "Web": source_url,
            "Manga": "Yes",
        }

    @staticmethod
    def _write_comic_info(path: Path, fields: dict[str, object]) -> None:
        with ZipFile(path) as archive:
            page_count = sum(
                Path(name).suffix.casefold() in _IMAGE_SUFFIXES for name in archive.namelist()
            )
        root = ElementTree.Element("ComicInfo")
        for key, value in {**fields, "PageCount": page_count}.items():
            if value is None or value == "":
                continue
            ElementTree.SubElement(root, key).text = str(value)
        payload = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        _replace_zip_entry(path, "ComicInfo.xml", payload)


class DownloadArchiveService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.downloads_dir.resolve()

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (path for path in self.root.rglob("*.cbz") if path.is_file()),
            key=lambda path: (path.stat().st_mtime, str(path)),
        )

    def stats(self) -> DownloadStorageStats:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        files = self.files()
        size = sum(path.stat().st_size for path in files)
        quota = max(0, self.settings.downloads_max_bytes)
        return DownloadStorageStats(
            root=str(self.root),
            file_count=len(files),
            byte_size=size,
            capacity_bytes=usage.total,
            free_bytes=usage.free,
            quota_bytes=quota,
            quota_used_ratio=(size / quota) if quota else None,
        )

    def cleanup(self, *, execute: bool = False) -> dict[str, object]:
        files = self.files()
        size = sum(path.stat().st_size for path in files)
        quota = max(0, self.settings.downloads_max_bytes)
        target = int(quota * self.settings.downloads_cleanup_target_ratio) if quota else 0
        selected: list[Path] = []
        remaining = size
        if quota and size > quota:
            for path in files:
                selected.append(path)
                remaining -= path.stat().st_size
                if remaining <= target:
                    break
        if execute:
            for path in selected:
                path.unlink(missing_ok=True)
                parent = path.parent
                if parent != self.root and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
        return {
            "execute": execute,
            "before_bytes": size,
            "after_bytes": remaining,
            "reclaimed_bytes": size - remaining,
            "files": [str(path.relative_to(self.root)) for path in selected],
            "policy": ("超过配额时按最早修改时间删除，直到达到目标比例；无限配额不自动选择文件"),
        }

    def verify_all(self, limit: int = 200) -> dict[str, object]:
        valid: list[dict[str, object]] = []
        invalid: list[dict[str, str]] = []
        for path in self.files()[:limit]:
            try:
                valid.append(asdict(verify_cbz(path, self.settings)))
            except ValueError as exc:
                invalid.append({"path": str(path.relative_to(self.root)), "error": str(exc)})
        return {"checked": len(valid) + len(invalid), "valid": valid, "invalid": invalid}


def _replace_zip_entry(path: Path, name: str, payload: bytes) -> None:
    temporary = path.with_suffix(".metadata.tmp")
    try:
        with (
            ZipFile(path, "r") as source,
            ZipFile(temporary, "w", compression=ZIP_DEFLATED) as target,
        ):
            for item in source.infolist():
                if item.filename != name:
                    target.writestr(item, source.read(item.filename))
            target.writestr(name, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_cbz(path: Path, settings: Settings) -> DownloadVerification:
    safe = safe_download_path(str(path), settings)
    if safe is None:
        raise ValueError("CBZ 文件不在下载目录内")
    try:
        with ZipFile(safe) as archive:
            if archive.testzip() is not None:
                raise ValueError("CBZ CRC 校验失败")
            names = archive.namelist()
            if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
                raise ValueError("CBZ 包含不安全路径")
            pages = sum(Path(name).suffix.casefold() in _IMAGE_SUFFIXES for name in names)
            if pages == 0:
                raise ValueError("CBZ 中没有可识别的图片页")
            comic_info = "ComicInfo.xml" in names
    except BadZipFile as exc:
        raise ValueError("CBZ 不是有效的 ZIP 文件") from exc
    digest = sha256()
    with safe.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return DownloadVerification(
        path=str(safe),
        sha256=digest.hexdigest(),
        byte_size=safe.stat().st_size,
        page_count=pages,
        comic_info=comic_info,
    )


def safe_download_path(path_value: str, settings: Settings) -> Path | None:
    root = settings.downloads_dir.resolve()
    path = Path(path_value).resolve()
    if path.is_file() and path.is_relative_to(root):
        return path
    return None
