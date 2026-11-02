from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.db import session as session_module
from app.db.base import Base
from app.db.models import WorkGroup


def test_existing_database_adds_metadata_without_losing_groups(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(WorkGroup(title="Existing", tags=[]))
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE work_groups DROP COLUMN manual_metadata")
    monkeypatch.setattr(session_module, "engine", engine)
    session_module._apply_additive_migrations()
    session_module._apply_additive_migrations()
    assert "manual_metadata" in {col["name"] for col in inspect(engine).get_columns("work_groups")}
    with Session(engine) as session:
        group = session.query(WorkGroup).one()
        assert group.title == "Existing"
        assert group.manual_metadata == {}
