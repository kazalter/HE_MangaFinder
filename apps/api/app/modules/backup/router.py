from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.modules.backup.service import CatalogExportService, verify_backup

router = APIRouter(prefix="/backup", tags=["backup"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/catalog.json")
def export_catalog(session: SessionDep) -> Response:
    payload = CatalogExportService(session).export_bytes()
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="mangafinder-catalog.json"'},
    )


@router.get("/archive")
def create_backup(session: SessionDep) -> Response:
    payload = CatalogExportService(session).backup_bytes()
    date = datetime.now(UTC).date().isoformat()
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="mangafinder-backup-{date}.zip"'
            )
        },
    )


@router.post("/verify")
async def verify_backup_upload(request: Request) -> dict[str, object]:
    try:
        return verify_backup(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
