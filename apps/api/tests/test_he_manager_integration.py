import httpx
import pytest

from app.core.config import Settings
from app.modules.integrations import he_manager


def test_source_keys_are_provider_and_id_specific():
    assert he_manager.source_key("https://www.wnacg.com/photos-index-aid-123.html") == (
        "wnacg",
        "123",
    )
    assert he_manager.source_key("https://www.wn08.cfd/photos-index-aid-123.html") == (
        "wnacg",
        "123",
    )
    assert he_manager.source_key("https://nhentai.net/g/123/") == ("nhentai", "123")
    assert he_manager.source_key("https://evil.example/g/123/") is None


@pytest.mark.asyncio
async def test_library_results_and_errors(monkeypatch):
    rows = [
        {
            "id": 1,
            "title": "完全不同",
            "source_url": "https://www.wn08.cfd/photos-index-aid-123.html",
            "is_missing": False,
        },
        {"id": 2, "title": "Example manga title", "source_url": None, "is_missing": False},
        {"id": 3, "title": "Example manga title", "is_missing": True},
    ]
    status = 200

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(status, json=rows if status == 200 else {"detail": "expired"})

    original = httpx.AsyncClient

    def client(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(he_manager.httpx, "AsyncClient", client)
    settings = Settings(
        he_manager_base_url="http://he-manager:8010", he_manager_token="test-token", _env_file=None
    )
    found = await he_manager.check_library(
        settings, "Example manga title", "https://www.wnacg.com/photos-index-aid-123.html"
    )
    assert found["status"] == "found"
    assert [row["id"] for row in found["matches"]] == [1, 2]
    possible = await he_manager.check_library(
        settings, "Example manga title", "https://nhentai.net/g/10/"
    )
    assert possible["status"] == "possible"
    assert [row["id"] for row in possible["matches"]] == [2]
    missing = await he_manager.check_library(
        settings, "Unrelated work title", "https://nhentai.net/g/10/"
    )
    assert missing["status"] == "not_found"
    status = 401
    assert (await he_manager.check_library(settings, "Example manga title", ""))[
        "status"
    ] == "unavailable"
    settings.he_manager_token = ""
    assert (await he_manager.check_library(settings, "Example manga title", ""))[
        "status"
    ] == "unavailable"


@pytest.mark.asyncio
async def test_download_requires_acknowledgement_for_matches_and_unknown(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException

    from app.modules.catalog import router
    from app.modules.catalog.schemas import DownloadCreate
    from app.providers.base import ProviderCapability

    source = SimpleNamespace(
        work=SimpleNamespace(title="Example"), source_url="https://nhentai.net/g/1/"
    )
    repository = MagicMock()
    repository.get_source.return_value = source
    jobs = MagicMock()
    monkeypatch.setattr(router, "CatalogRepository", lambda session: repository)
    monkeypatch.setattr(router, "JobRepository", lambda session: jobs)
    monkeypatch.setattr(router.JobRead, "model_validate", lambda job: job)
    check = AsyncMock(return_value={"status": "found"})
    monkeypatch.setattr(router, "check_library", check)
    provider = SimpleNamespace(capabilities={ProviderCapability.DOWNLOAD})
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(providers=SimpleNamespace(get_optional=lambda name: provider))
        )
    )
    session = MagicMock()
    payload = DownloadCreate(provider="nhentai", chapter_id="1")
    for status in ("found", "possible", "unavailable"):
        check.return_value = {"status": status}
        with pytest.raises(HTTPException) as exc:
            await router.download_chapter(1, payload, request, session)
        assert exc.value.status_code == 409
    jobs.enqueue_download.assert_not_called()
    check.return_value = {"status": "not_found"}
    await router.download_chapter(1, payload, request, session)
    assert jobs.enqueue_download.call_count == 1
    check.return_value = {"status": "found"}
    await router.download_chapter(
        1, payload.model_copy(update={"confirm_library_risk": True}), request, session
    )
    assert jobs.enqueue_download.call_count == 2
