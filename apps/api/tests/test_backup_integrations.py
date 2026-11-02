from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    AdminAccount,
    Author,
    AuthorWork,
    Job,
    JobStatus,
    SystemSetting,
    Work,
    WorkGroup,
    WorkGroupMember,
    WorkSource,
)
from app.modules.backup.service import CatalogExportService, verify_backup
from app.modules.integrations.opds import build_opds_catalog
from app.modules.integrations.scanners import LibraryScanService
from app.modules.jobs.repository import DOWNLOAD_CHAPTER


def _catalog(session: Session) -> tuple[Author, Work, WorkGroup]:
    author = Author(
        name="作者",
        aliases=["Alias"],
        provider_query_names={"test": "query"},
    )
    work = Work(title="作品", tags=["Drama"])
    group = WorkGroup(title="作品")
    source = WorkSource(
        work=work,
        provider="test",
        external_id="1",
        source_url="https://example.test/1",
        raw_metadata={"title": "作品", "api_token": "must-not-export"},
    )
    session.add_all([author, work, group, source])
    session.flush()
    session.add_all(
        [
            AuthorWork(author_id=author.id, work_id=work.id),
            WorkGroupMember(group_id=group.id, work_id=work.id),
        ]
    )
    session.flush()
    return author, work, group


def test_backup_is_verifiable_and_excludes_sensitive_records() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _catalog(session)
        session.add_all(
            [
                SystemSetting(key="agent_api_key", value="encrypted-secret", encrypted=True),
                AdminAccount(username="admin", password_hash="password-hash"),
            ]
        )
        session.commit()

        backup = CatalogExportService(session).backup_bytes()
        result = verify_backup(backup)
        with ZipFile(BytesIO(backup)) as archive:
            catalog = archive.read("catalog.json").decode()

        assert result["valid"] is True
        assert result["sensitive_data_included"] is False
        assert "must-not-export" not in catalog
        assert "encrypted-secret" not in catalog
        assert "password-hash" not in catalog
        assert "Alias" in catalog


def test_backup_verification_detects_tampering() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _catalog(session)
        original = CatalogExportService(session).backup_bytes()
    with ZipFile(BytesIO(original)) as archive:
        manifest = archive.read("manifest.json")
        catalog = archive.read("catalog.json") + b" "
    changed = BytesIO()
    with ZipFile(changed, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("catalog.json", catalog)
    with pytest.raises(ValueError, match="校验失败"):
        verify_backup(changed.getvalue())


def test_opds_lists_catalog_and_completed_cbz_acquisition() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, work, _ = _catalog(session)
        session.add(
            Job(
                kind=DOWNLOAD_CHAPTER,
                status=JobStatus.SUCCEEDED,
                payload={"work_id": work.id, "output_path": "/downloads/book.cbz"},
            )
        )
        session.commit()

        payload = build_opds_catalog(
            session, Settings(public_base_url="http://library.test")
        ).decode()

        assert "MangaFinder 本地资料库" in payload
        assert "作品" in payload
        assert "http://opds-spec.org/acquisition" in payload
        assert "/api/jobs/1/file" in payload


@pytest.mark.asyncio
async def test_komga_and_kavita_scan_triggers_use_configured_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, request=request)

    settings = Settings(
        komga_base_url="http://komga.test",
        komga_api_key="komga-secret",
        komga_library_id="12",
        kavita_base_url="http://kavita.test",
        kavita_api_key="kavita-secret",
        kavita_library_id="34",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = LibraryScanService(settings, client)
        komga = await service.trigger("komga")
        kavita = await service.trigger("kavita")

    assert komga.accepted and kavita.accepted
    assert requests[0].url.path == "/api/v1/libraries/12/scan"
    assert requests[0].headers["X-API-Key"] == "komga-secret"
    assert requests[1].url.params["libraryId"] == "34"
    assert requests[1].headers["Authorization"] == "Bearer kavita-secret"


def test_backup_rejects_empty_manifest_and_non_catalog_payload() -> None:
    import json
    from hashlib import sha256

    def archive_with(manifest, catalog):
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("catalog.json", catalog)
        return output.getvalue()

    with pytest.raises(ValueError, match="缺少 catalog.json"):
        verify_backup(archive_with({"format": "mangafinder-backup-v1", "files": {}}, b"{}"))

    payload = b"{}"
    manifest = {
        "format": "mangafinder-backup-v1",
        "files": {
            "catalog.json": {"byte_size": len(payload), "sha256": sha256(payload).hexdigest()}
        },
    }
    with pytest.raises(ValueError, match="目录数据格式"):
        verify_backup(archive_with(manifest, payload))
