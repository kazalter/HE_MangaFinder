"""Read-only HE Manager library checks for a requested edition."""

import re
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.modules.catalog.title_identity import normalize_identity_text, weighted_text_similarity


def source_key(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host == "nhentai.net" or host.endswith(".nhentai.net"):
        match = re.search(r"/g/(\d+)(?:/|$)", path)
        return ("nhentai", match.group(1)) if match else None
    if (
        host == "wnacg.com"
        or host.endswith(".wnacg.com")
        or re.fullmatch(r"(?:www\.)?wn\d+\.(?:cfd|shop)", host)
    ):
        match = re.search(r"/(?:photos-index-aid-|albums-index-page-\d+-aid-)(\d+)", path)
        return ("wnacg", match.group(1)) if match else None
    return None


async def check_library(settings: Settings, title: str, source_url: str) -> dict:
    if not settings.he_manager_base_url or not settings.he_manager_token:
        return {
            "status": "unavailable",
            "message": "尚未连接 HE Manager，请在系统设置中登录后再检查。",
            "matches": [],
        }
    wanted_key = source_key(source_url)
    wanted_title = normalize_identity_text(title)
    candidates = []
    try:
        async with httpx.AsyncClient(
            base_url=settings.he_manager_base_url.rstrip("/"), timeout=8, trust_env=False
        ) as client:
            for offset in range(0, 20000, 200):
                response = await client.get(
                    "/media",
                    params={
                        "media_type": "manga",
                        "is_missing": "false",
                        "include_hidden_duplicates": "true",
                        "limit": 200,
                        "offset": offset,
                    },
                    headers={"Authorization": f"Bearer {settings.he_manager_token}"},
                )
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError("invalid media response")
                for row in rows:
                    if not isinstance(row, dict) or row.get("is_missing"):
                        continue
                    row_key = source_key(row.get("source_url") or "")
                    exact_source = bool(wanted_key and wanted_key == row_key)
                    other_title = str(row.get("title") or "")
                    score = weighted_text_similarity(wanted_title, other_title)
                    if exact_source or (len(wanted_title) >= 6 and score >= 0.82):
                        candidates.append(
                            {
                                "id": row.get("id"),
                                "title": other_title,
                                "reason": "来源链接一致"
                                if exact_source
                                else "标题相似，版本待确认",
                                "exact": exact_source,
                                "score": score,
                            }
                        )
                if len(rows) < 200:
                    break
            else:
                return {
                    "status": "unavailable",
                    "message": "馆藏过大，检查未完成，请稍后重试。",
                    "matches": [],
                }
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "status": "unavailable",
            "message": "无法检查 HE Manager，请检查连接或重新登录。",
            "matches": [],
        }
    candidates.sort(key=lambda item: (item["exact"], item["score"]), reverse=True)
    if candidates:
        return {
            "status": "found" if candidates[0]["exact"] else "possible",
            "message": "发现已入库版本"
            if candidates[0]["exact"]
            else "发现可能相同的作品，请确认版本",
            "matches": candidates[:5],
        }
    return {
        "status": "not_found",
        "message": "未找到匹配的已入库作品；资料不完整时仍可能遗漏。",
        "matches": [],
    }
