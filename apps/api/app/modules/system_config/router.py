from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AdminAccount
from app.db.session import get_session
from app.modules.system_config.schemas import (
    AiConnectionInput,
    AuthStatus,
    ConnectionTestResult,
    PasswordInput,
    QqConnectionInput,
    SaveResult,
    SystemConfigRead,
    SystemConfigUpdate,
    XSessionImport,
)
from app.modules.system_config.security import (
    SESSION_COOKIE,
    account_initialized,
    clear_login_session,
    create_login_session,
    optional_admin,
    password_hash,
    require_admin,
    require_csrf,
    verify_password,
)
from app.modules.system_config.service import (
    clear_x_session,
    import_x_session,
    read_config,
    save_config,
    test_agent,
    test_qq,
    test_x_session,
)

router = APIRouter(prefix="/system-config", tags=["system-config"])
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[AdminAccount, Depends(require_admin)]
CsrfDep = Annotated[None, Depends(require_csrf)]


@router.get("/auth", response_model=AuthStatus)
def auth_status(
    session: SessionDep,
    account: Annotated[AdminAccount | None, Depends(optional_admin)],
) -> AuthStatus:
    return AuthStatus(
        initialized=account_initialized(session),
        authenticated=account is not None,
        username=account.username if account else None,
    )


@router.post("/auth/bootstrap", response_model=AuthStatus)
def bootstrap(payload: PasswordInput, response: Response, session: SessionDep) -> AuthStatus:
    if account_initialized(session):
        raise HTTPException(status_code=409, detail="管理员已经初始化")
    account = AdminAccount(username="admin", password_hash=password_hash(payload.password))
    session.add(account)
    session.commit()
    session.refresh(account)
    create_login_session(session, response, account)
    return AuthStatus(initialized=True, authenticated=True, username=account.username)


@router.post("/auth/login", response_model=AuthStatus)
def login(payload: PasswordInput, response: Response, session: SessionDep) -> AuthStatus:
    account = session.scalar(select(AdminAccount).where(AdminAccount.username == "admin"))
    if not account or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=401, detail="管理员密码不正确")
    create_login_session(session, response, account)
    return AuthStatus(initialized=True, authenticated=True, username=account.username)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(request: Request, response: Response, session: SessionDep) -> Response:
    clear_login_session(session, response, request.cookies.get(SESSION_COOKIE))
    response.status_code = 204
    return response


@router.get("", response_model=SystemConfigRead)
async def get_config(_: AdminDep, settings: SettingsDep) -> SystemConfigRead:
    return await read_config(settings)


@router.put("", response_model=SaveResult)
async def update_config(
    payload: SystemConfigUpdate,
    session: SessionDep,
    settings: SettingsDep,
    account: AdminDep,
    _: CsrfDep,
) -> SaveResult:
    return await save_config(session, settings, account, payload)


@router.post("/test/agent", response_model=ConnectionTestResult)
async def test_agent_connection(
    payload: AiConnectionInput, _: AdminDep, __: CsrfDep, settings: SettingsDep
) -> ConnectionTestResult:
    return await test_agent(settings, payload)


@router.post("/test/x", response_model=ConnectionTestResult)
async def test_x_connection(
    _: AdminDep, __: CsrfDep, settings: SettingsDep
) -> ConnectionTestResult:
    return await test_x_session(settings)


@router.post("/test/qq", response_model=ConnectionTestResult)
async def test_qq_connection(
    payload: QqConnectionInput, _: AdminDep, __: CsrfDep, settings: SettingsDep
) -> ConnectionTestResult:
    return await test_qq(settings, payload)


@router.put("/x-session", response_model=ConnectionTestResult)
async def replace_x_session(
    payload: XSessionImport,
    session: SessionDep,
    settings: SettingsDep,
    account: AdminDep,
    _: CsrfDep,
) -> ConnectionTestResult:
    return await import_x_session(session, settings, account, payload)


@router.delete("/x-session", response_model=ConnectionTestResult)
async def delete_x_session(
    session: SessionDep,
    settings: SettingsDep,
    account: AdminDep,
    _: CsrfDep,
) -> ConnectionTestResult:
    return await clear_x_session(session, settings, account)
