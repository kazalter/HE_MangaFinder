import json
import sqlite3

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, SystemSetting
from app.modules.backup.snapshot import create_snapshot, restore_snapshot, verify_snapshot
from app.modules.system_config.repository import SecretBox


def test_live_snapshot_restore_and_corruption_detection(tmp_path):
    database = tmp_path / "live.db"
    key = tmp_path / "key"
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Author(name="作者"))
        session.add(
            SystemSetting(
                key="agent_api_key", value=SecretBox(key).encrypt('"secret"'), encrypted=True
            )
        )
        session.commit()
        backup = tmp_path / "backup"
        result = create_snapshot(database, key, backup)
        assert result["compatibility"] == "current_schema"
        assert result["schema"]["encrypted_settings"] == 1
        assert (backup.stat().st_mode & 0o777) == 0o700
        assert ((backup / "system-secret.key").stat().st_mode & 0o777) == 0o600
        output = tmp_path / "restored"
        restore_snapshot(backup, output)
        with sqlite3.connect(output / "mangafinder.db") as restored:
            assert restored.execute("select name from authors").fetchone() == ("作者",)
        assert (output / "system-secret.key").read_bytes() == key.read_bytes()
        with pytest.raises(FileExistsError):
            restore_snapshot(backup, output)
        (backup / "system-secret.key").write_bytes(b"wrong")
        with pytest.raises(ValueError, match="完整性校验失败"):
            verify_snapshot(backup)


def test_encrypted_backup_rejects_missing_or_incorrect_key(tmp_path):
    database = tmp_path / "live.db"
    key = tmp_path / "key"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            SystemSetting(
                key="agent_api_key", value=SecretBox(key).encrypt('"secret"'), encrypted=True
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="缺少系统密钥"):
        create_snapshot(database, None, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    wrong = tmp_path / "wrong"
    wrong.write_bytes(Fernet.generate_key())
    with pytest.raises(ValueError, match="不匹配"):
        create_snapshot(database, wrong, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_backup_with_incomplete_schema_requires_review(tmp_path):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            "create table authors(id integer); create table works(id integer); "
            "create table system_settings(value text, encrypted integer);"
        )
    backup = tmp_path / "old-backup"
    result = create_snapshot(database, None, backup)
    assert result["compatibility"] == "review_required"
    assert result["schema"]["missing_current_schema"]
    with pytest.raises(ValueError, match="隔离环境"):
        restore_snapshot(backup, tmp_path / "restored")
    assert json.loads((backup / "manifest.json").read_text())["format"] == result["format"]
