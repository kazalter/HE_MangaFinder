from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.models import SystemSetting
from app.db.session import get_session
from app.modules.system_config.repository import (
    SystemSettingRepository,
    apply_runtime_overrides,
)
from app.modules.system_config.router import router
from app.modules.system_config.schemas import XSessionImport
from app.modules.system_config.security import password_hash, verify_password
from app.modules.system_config.service import _normalized_state


def test_secret_settings_are_encrypted_and_apply_as_runtime_overrides(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    key_file = tmp_path / "system-secret.key"
    initial = Settings(system_secret_key_file=key_file, _env_file=None)

    with Session(engine) as session:
        repository = SystemSettingRepository(session, initial)
        repository.set_value("agent_enabled", True)
        repository.set_value("agent_model", "private-model")
        repository.set_value("agent_api_key", "sk-secret-value", encrypted=True)
        session.commit()

        stored = session.get(SystemSetting, "agent_api_key")
        assert stored is not None and stored.encrypted
        assert "sk-secret-value" not in stored.value
        assert key_file.stat().st_mode & 0o777 == 0o600

        restored = Settings(system_secret_key_file=key_file, _env_file=None)
        apply_runtime_overrides(session, restored)
        assert restored.agent_enabled is True
        assert restored.agent_model == "private-model"
        assert restored.agent_api_key == "sk-secret-value"


def test_admin_password_uses_scrypt_and_verifies_without_plaintext() -> None:
    encoded = password_hash("a sufficiently long password")
    assert encoded.startswith("scrypt$")
    assert "sufficiently" not in encoded
    assert verify_password("a sufficiently long password", encoded)
    assert not verify_password("the wrong password", encoded)


def test_x_session_import_keeps_only_x_cookies_and_requires_auth_fields() -> None:
    state = _normalized_state(
        XSessionImport(
            storage_state={
                "cookies": [
                    {"name": "auth_token", "value": "secret", "domain": ".x.com"},
                    {"name": "ct0", "value": "csrf", "domain": "x.com"},
                    {"name": "ignored", "value": "other", "domain": "example.com"},
                ]
            }
        )
    )
    assert {item["name"] for item in state["cookies"]} == {"auth_token", "ct0"}
    assert all(item["domain"] == ".x.com" for item in state["cookies"])

    try:
        _normalized_state(XSessionImport(cookie_header="ct0=csrf"))
    except ValueError as exc:
        assert "auth_token" in str(exc)
    else:
        raise AssertionError("missing auth_token should be rejected")


def test_admin_bootstrap_session_and_csrf_logout() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        assert client.get("/api/system-config/auth").json() == {
            "initialized": False,
            "authenticated": False,
            "username": None,
        }
        response = client.post(
            "/api/system-config/auth/bootstrap",
            json={"password": "a secure admin password"},
        )
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert client.cookies.get("mangafinder_admin")
        csrf = client.cookies.get("mangafinder_csrf")
        assert csrf
        assert client.get("/api/system-config/auth").json()["authenticated"] is True

        forbidden = client.post("/api/system-config/auth/logout")
        assert forbidden.status_code == 403
        logged_out = client.post(
            "/api/system-config/auth/logout", headers={"X-CSRF-Token": csrf}
        )
        assert logged_out.status_code == 204
        assert client.get("/api/system-config/auth").json()["authenticated"] is False
