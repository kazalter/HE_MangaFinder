#!/usr/bin/env python3
"""Create a Playwright storage-state file without putting X cookies in shell history."""

import argparse
import getpass
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把浏览器中的 X Cookie 请求头保存为采集器 storage-state.json"
    )
    parser.add_argument(
        "--output", default="data/x-session/storage-state.json", help="输出文件"
    )
    args = parser.parse_args()
    cookie_header = getpass.getpass("粘贴 x.com 请求中的完整 Cookie（输入不会回显）：\n").strip()
    pairs: list[tuple[str, str]] = []
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name and value:
            pairs.append((name, value))
    names = {name for name, _ in pairs}
    missing = {"auth_token", "ct0"} - names
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
                "httpOnly": name in {"auth_token"},
                "secure": True,
                "sameSite": "None",
            }
            for name, value in pairs
        ],
        "origins": [],
    }
    output.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(output, 0o600)
    os.chmod(output.parent, 0o700)
    print(f"已写入 {output}，权限为 0600。请勿提交这个文件。")


if __name__ == "__main__":
    main()
