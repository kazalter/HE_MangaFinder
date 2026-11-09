from dataclasses import asdict
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AdminAccount
from app.db.session import get_session
from app.modules.integrations.opds import build_opds_catalog
from app.modules.integrations.scanners import LibraryScanService
from app.modules.system_config.repository import SystemSettingRepository
from app.modules.system_config.security import require_admin, require_csrf

router = APIRouter(tags=["integrations"])
SessionDep = Annotated[Session, Depends(get_session)]
AdminDep = Annotated[AdminAccount, Depends(require_admin)]


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


class HeManagerLogin(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=256)


@router.get("/integrations/he-manager", dependencies=[Depends(require_admin)])
def he_manager_status() -> dict[str, bool]:
    settings = get_settings()
    return {"configured": bool(settings.he_manager_base_url and settings.he_manager_token)}


@router.post("/integrations/he-manager/login", dependencies=[Depends(require_csrf)])
async def he_manager_login(
    payload: HeManagerLogin, session: SessionDep, account: AdminDep
) -> dict[str, str]:
    settings = get_settings()
    if not settings.he_manager_base_url:
        raise HTTPException(status_code=409, detail="尚未配置 HE Manager 服务地址")
    try:
        async with httpx.AsyncClient(
            base_url=settings.he_manager_base_url.rstrip("/"), timeout=8, trust_env=False
        ) as client:
            response = await client.post("/auth/login", json=payload.model_dump())
            response.raise_for_status()
            token = response.json()["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError("invalid token")
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(
            status_code=502, detail="连接或登录 HE Manager 失败，请检查账号和服务状态"
        ) from None
    repository = SystemSettingRepository(session, settings)
    repository.set_value("he_manager_token", token, encrypted=True)
    repository.audit(account.id, "he_manager.connected", ["he_manager_token"])
    session.commit()
    settings.he_manager_token = token
    return {"message": "HE Manager 已连接"}


@router.delete("/integrations/he-manager", dependencies=[Depends(require_csrf)])
def he_manager_disconnect(session: SessionDep, account: AdminDep) -> dict[str, str]:
    settings = get_settings()
    repository = SystemSettingRepository(session, settings)
    repository.set_value("he_manager_token", "", encrypted=True)
    repository.audit(account.id, "he_manager.disconnected", ["he_manager_token"])
    session.commit()
    settings.he_manager_token = ""
    return {"message": "HE Manager 已断开"}
