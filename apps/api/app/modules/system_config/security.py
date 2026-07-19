import base64
import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AdminAccount, AdminSession
from app.db.session import get_session

SESSION_COOKIE = "mangafinder_admin"
CSRF_COOKIE = "mangafinder_csrf"
SessionDep = Annotated[Session, Depends(get_session)]


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return "scrypt$16384$8$1$" + "$".join(
        base64.urlsafe_b64encode(item).decode().rstrip("=") for item in (salt, digest)
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_value, expected_value = encoded.split("$")
        if algorithm != "scrypt":
            return False
        def decode(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        actual = hashlib.scrypt(
            password.encode(), salt=decode(salt_value), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(actual, decode(expected_value))
    except (TypeError, ValueError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def account_initialized(session: Session) -> bool:
    return session.scalar(select(AdminAccount.id).limit(1)) is not None


def create_login_session(session: Session, response: Response, account: AdminAccount) -> None:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(UTC) + timedelta(hours=max(1, settings.admin_session_hours))
    session.add(
        AdminSession(
            account_id=account.id,
            token_hash=_token_hash(raw_token),
            expires_at=expires,
        )
    )
    session.commit()
    common = {
        "secure": settings.admin_cookie_secure,
        "samesite": "strict",
        "max_age": max(1, settings.admin_session_hours) * 3600,
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, raw_token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def clear_login_session(session: Session, response: Response, raw_token: str | None) -> None:
    if raw_token:
        session.execute(
            delete(AdminSession).where(AdminSession.token_hash == _token_hash(raw_token))
        )
        session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def optional_admin(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> AdminAccount | None:
    if not token:
        return None
    record = session.scalar(
        select(AdminSession).where(AdminSession.token_hash == _token_hash(token))
    )
    if not record or record.expires_at <= datetime.now(UTC):
        if record:
            session.delete(record)
            session.commit()
        return None
    return session.get(AdminAccount, record.account_id)


def require_admin(account: Annotated[AdminAccount | None, Depends(optional_admin)]) -> AdminAccount:
    if not account:
        raise HTTPException(status_code=401, detail="请先以管理员身份登录")
    return account


def require_csrf(
    _: Annotated[AdminAccount, Depends(require_admin)],
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise HTTPException(status_code=403, detail="安全校验已失效，请刷新页面后重试")
