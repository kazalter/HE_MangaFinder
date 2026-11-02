import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, AuthorWork, Work, WorkGroup, WorkGroupMember, WorkSource
from app.modules.catalog.downloads import DownloadArchiveService, DownloadService, verify_cbz
from app.providers.base import ProviderCapability
from app.providers.registry import ProviderRegistry


class DownloadProvider:
    name = "test"
    display_name = "Test"
    capabilities = {ProviderCapability.DOWNLOAD}

    async def download_chapter(
        self, external_id: str, chapter_id: str, destination: str
    ) -> str:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (40, 60), "navy")
        payload = BytesIO()
        image.save(payload, "JPEG")
        with ZipFile(path, "w") as archive:
            archive.writestr("001.jpg", payload.getvalue())
        return str(path)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_download_adds_comic_info_and_verifies_archive(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(downloads_dir=tmp_path)
    with Session(engine) as session:
        author = Author(name="作者")
        work = Work(
            title="作品", description="简介", year=2026, language="ja", tags=["Drama"]
        )
        source = WorkSource(
            work=work,
            provider="test",
            external_id="book",
            source_url="https://example.test/book",
        )
        group = WorkGroup(title="系列作品")
        session.add_all([author, work, source, group])
        session.flush()
        session.add_all(
            [
                AuthorWork(author_id=author.id, work_id=work.id),
                WorkGroupMember(group_id=group.id, work_id=work.id),
            ]
        )
        session.commit()

        path, verification = await DownloadService(
            session, ProviderRegistry([DownloadProvider()]), settings
        ).download(work.id, "test", "1")

        assert verification.page_count == 1
        assert verification.comic_info is True
        assert len(verification.sha256) == 64
        with ZipFile(path) as archive:
            metadata = archive.read("ComicInfo.xml").decode()
        assert "<Series>系列作品</Series>" in metadata
        assert "<Writer>作者</Writer>" in metadata
        assert "<PageCount>1</PageCount>" in metadata


def test_storage_cleanup_is_dry_run_then_removes_oldest(tmp_path: Path) -> None:
    settings = Settings(
        downloads_dir=tmp_path,
        downloads_max_bytes=150,
        downloads_cleanup_target_ratio=0.5,
    )
    paths = [tmp_path / "old.cbz", tmp_path / "new.cbz"]
    for index, path in enumerate(paths):
        with ZipFile(path, "w") as archive:
            archive.writestr("page.jpg", b"x" * 100)
        timestamp = datetime(2026, 1, index + 1, tzinfo=UTC).timestamp()
        os.utime(path, (timestamp, timestamp))
    service = DownloadArchiveService(settings)

    preview = service.cleanup()
    assert preview["execute"] is False
    assert paths[0].exists() and paths[1].exists()
    assert preview["files"]

    result = service.cleanup(execute=True)
    assert result["reclaimed_bytes"] > 0
    assert not paths[0].exists()


def test_verification_rejects_cbz_without_pages(tmp_path: Path) -> None:
    path = tmp_path / "empty.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "empty")
    with pytest.raises(ValueError, match="没有可识别的图片页"):
        verify_cbz(path, Settings(downloads_dir=tmp_path))


@pytest.mark.asyncio
async def test_same_title_and_chapter_do_not_overwrite(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        works = [Work(title="漫画" * 150), Work(title="漫画" * 150)]
        session.add_all([WorkSource(work=work, provider="test", external_id=str(i),
                                   source_url="https://example.test/")
                         for i, work in enumerate(works)])
        session.commit()
        service = DownloadService(session, ProviderRegistry([DownloadProvider()]),
                                  Settings(downloads_dir=tmp_path))
        first, _ = await service.download(works[0].id, "test", "1")
        second, _ = await service.download(works[1].id, "test", "1")
        assert first != second
        assert Path(first).is_file() and Path(second).is_file()
