from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.core.config import get_settings
from app.modules.catalog.downloads import DownloadArchiveService

router = APIRouter(prefix="/downloads", tags=["downloads"])


class CleanupRequest(BaseModel):
    execute: bool = False


@router.get("/storage")
def download_storage() -> dict[str, object]:
    return asdict(DownloadArchiveService(get_settings()).stats())


@router.post("/verify")
def verify_downloads(
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> dict[str, object]:
    return DownloadArchiveService(get_settings()).verify_all(limit)


@router.post("/cleanup")
def cleanup_downloads(payload: CleanupRequest) -> dict[str, object]:
    return DownloadArchiveService(get_settings()).cleanup(execute=payload.execute)
