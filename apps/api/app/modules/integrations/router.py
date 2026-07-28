from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session
from app.modules.integrations.opds import build_opds_catalog
from app.modules.integrations.scanners import LibraryScanService

router = APIRouter(tags=["integrations"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/opds/v1.2/catalog.xml")
def opds_catalog(session: SessionDep) -> Response:
    return Response(
        build_opds_catalog(session, get_settings()),
        media_type="application/atom+xml;profile=opds-catalog;kind=acquisition",
    )


@router.post("/integrations/{target}/scan")
async def trigger_library_scan(
    target: Literal["komga", "kavita"],
) -> dict[str, object]:
    try:
        return asdict(await LibraryScanService(get_settings()).trigger(target))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
