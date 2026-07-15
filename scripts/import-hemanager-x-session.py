#!/usr/bin/env python3
"""One-way import of an HEManager X cookie into Playwright storage state.

The cookie value is never printed. The generated file is private and an older
runtime state is removed so the newly imported session takes precedence.
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path


REQUIRED_COOKIES = {"auth_token", "ct0", "twid"}


def cookie_pairs(header: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name and value:
            result.append((name, value))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 HEManager 数据库安全迁移 X 登录会话"
    )
    parser.add_argument(
        "--database",
        default="/opt/stacks/he-manager/data/library.db",
        help="HEManager SQLite 数据库",
    )
    parser.add_argument("--source-id", type=int, default=1, help="X 来源 ID")
    parser.add_argument(
        "--output",
        default="/mnt/hdd/mangafinder/x-session/storage-state.json",
        help="Playwright storage state 输出路径",
    )
    args = parser.parse_args()

    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"HEManager 数据库不存在：{database}")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT cookie FROM x_import_sources WHERE id = ?", (args.source_id,)
        ).fetchone()
    if not row or not row[0]:
        raise SystemExit("指定 HEManager X 来源没有保存 Cookie")

    pairs = cookie_pairs(str(row[0]))
    names = {name for name, _ in pairs}
    missing = REQUIRED_COOKIES - names
    if missing:
        raise SystemExit(f"Cookie 缺少必要字段：{', '.join(sorted(missing))}")

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "cookies": [
            {
                "name": name,
                "value": value,
                "domain": ".x.com",
                "path": "/",
                "expires": -1,
                "httpOnly": name == "auth_token",
                "secure": True,
                "sameSite": "None",
            }
            for name, value in pairs
        ],
        "origins": [],
    }
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    runtime = output.with_name("runtime-state.json")
    runtime.unlink(missing_ok=True)
    os.chmod(output.parent, 0o700)
    print(f"已迁移 {len(pairs)} 个 Cookie 到 {output}；旧 runtime state 已失效。")


if __name__ == "__main__":
    main()
