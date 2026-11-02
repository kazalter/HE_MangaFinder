"""Offline restore and consistent SQLite snapshots for local disaster recovery.

Snapshots contain private database and key material. They are not encrypted.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.db import models  # noqa: F401  register the current schema
from app.db.base import Base

FORMAT = "mangafinder-sqlite-snapshot-v1"
DATABASE = "mangafinder.db"
KEY = "system-secret.key"
MANIFEST = "manifest.json"


def _digest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def _read_db(path: Path, key_path: Path | None) -> dict[str, Any]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("数据库完整性检查失败")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("数据库存在外键错误")
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"authors", "works", "system_settings"}.issubset(names):
            raise ValueError("数据库缺少必要的数据表")
        missing = []
        extra = sorted(
            name
            for name in names
            if name not in Base.metadata.tables and not name.startswith("sqlite_")
        )
        for table in Base.metadata.sorted_tables:
            if table.name not in names:
                missing.append(table.name)
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table.name}")')}
            expected_columns = {col.name for col in table.columns}
            missing.extend(f"{table.name}.{name}" for name in sorted(expected_columns - columns))
            extra.extend(f"{table.name}.{name}" for name in sorted(columns - expected_columns))
        encrypted = [
            row[0] for row in db.execute("SELECT value FROM system_settings WHERE encrypted = 1")
        ]
        if encrypted and (key_path is None or not key_path.is_file()):
            raise ValueError("备份包含加密设置，但缺少系统密钥")
        if key_path is not None:
            try:
                box = Fernet(key_path.read_bytes().strip())
                for value in encrypted:
                    box.decrypt(value.encode())
            except (ValueError, InvalidToken) as exc:
                raise ValueError("系统密钥与备份中的加密设置不匹配") from exc
        return {
            "sqlite_user_version": db.execute("PRAGMA user_version").fetchone()[0],
            "missing_current_schema": missing,
            "extra_schema": extra,
            "encrypted_settings": len(encrypted),
        }


def _private_copy(source: Path, destination: Path) -> None:
    with source.open("rb") as src, destination.open("xb") as dst:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(src, dst, 1024 * 1024)


def create_snapshot(database: Path, key: Path | None, destination: Path) -> dict[str, Any]:
    if not database.is_file():
        raise ValueError("数据库文件不存在")
    destination.mkdir(mode=0o700)  # must be a new directory
    try:
        target = destination / DATABASE
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as output:
                os.chmod(target, 0o600)
                source.backup(output)
                output.execute("PRAGMA journal_mode=DELETE")
        stored_key = None
        if key is not None and key.is_file():
            stored_key = destination / KEY
            _private_copy(key, stored_key)
        details = _read_db(target, stored_key)
        files = {DATABASE: _digest(target)}
        if stored_key:
            files[KEY] = _digest(stored_key)
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "files": files,
            "schema": details,
        }
        manifest_path = destination / MANIFEST
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
        return verify_snapshot(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise


def verify_snapshot(snapshot: Path) -> dict[str, Any]:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("备份目录不存在或不是普通目录")
    manifest_path = snapshot / MANIFEST
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.stat().st_size > 1024 * 1024
    ):
        raise ValueError("备份清单不存在或过大")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("备份清单无效") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("备份格式不受支持")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) not in ({DATABASE}, {DATABASE, KEY}):
        raise ValueError("备份文件清单无效")
    if {item.name for item in snapshot.iterdir()} != set(files) | {MANIFEST}:
        raise ValueError("备份包含缺失或额外的文件")
    for name, expected in files.items():
        path = snapshot / name
        if path.is_symlink() or not path.is_file() or not isinstance(expected, dict):
            raise ValueError(f"{name} 不是有效的备份文件")
        if _digest(path) != expected:
            raise ValueError(f"{name} 完整性校验失败")
    details = _read_db(snapshot / DATABASE, snapshot / KEY if KEY in files else None)
    return {
        "valid": True,
        "format": FORMAT,
        "created_at": manifest.get("created_at"),
        "files": sorted(files),
        "checksums": files,
        "schema": details,
        "compatibility": "review_required"
        if details["missing_current_schema"]
        or details["extra_schema"]
        or details["sqlite_user_version"]
        else "current_schema",
    }


def restore_snapshot(snapshot: Path, destination: Path) -> dict[str, Any]:
    result = verify_snapshot(snapshot)
    if result["compatibility"] != "current_schema":
        raise ValueError("备份结构与当前版本不同，请先在隔离环境中检查迁移")
    destination.mkdir(mode=0o700)  # never replace an existing live database
    try:
        for name in result["files"]:
            _private_copy(snapshot / name, destination / name)
            if _digest(destination / name) != result["checksums"][name]:
                raise ValueError(f"{name} 恢复校验失败")
        _read_db(destination / DATABASE, destination / KEY if KEY in result["files"] else None)
        return result
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="MangaFinder SQLite 备份与隔离恢复")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database", required=True, type=Path)
    create.add_argument("--key", type=Path)
    create.add_argument("--destination", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("snapshot", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_snapshot(args.database, args.key, args.destination)
        elif args.command == "verify":
            result = verify_snapshot(args.snapshot)
        else:
            result = restore_snapshot(args.snapshot, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
