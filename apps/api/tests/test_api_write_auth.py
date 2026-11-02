from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app


def test_api_writes_require_session_and_csrf() -> None:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    def session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)  # no lifespan: use only the isolated database
    try:
        assert client.get("/api/authors").status_code == 200
        assert client.post("/api/authors", json={"name": "Test"}).status_code == 401
        assert (
            client.post(
                "/api/system-config/auth/bootstrap", json={"password": "secure password 123"}
            ).status_code
            == 200
        )
        assert client.post("/api/authors", json={"name": "Test"}).status_code == 403
        csrf = client.cookies.get("mangafinder_csrf")
        headers = {"X-CSRF-Token": csrf}
        assert (
            client.post("/api/authors", json={"name": "Test"}, headers=headers).status_code == 201
        )
        assert len(client.get("/api/authors").json()) == 1
        assert client.delete("/api/authors/1", headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.delete("/api/authors/1", headers=headers).status_code == 204
    finally:
        client.close()
        app.dependency_overrides.pop(get_session, None)
