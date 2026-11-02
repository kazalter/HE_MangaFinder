import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Author,
    CatalogEvent,
    PlannedRelease,
    Series,
    SeriesMember,
    Work,
    WorkGroup,
    WorkRelation,
    WorkSource,
)

BACKUP_FORMAT = "mangafinder-backup-v1"
_SENSITIVE_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "signed_url",
    "token",
    "api_key",
}


def _datetime(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _sanitize_url(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS)
    ]
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in _SENSITIVE_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


class CatalogExportService:
    """Builds a portable catalog export without credentials or admin/session records."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def export(self) -> dict[str, Any]:
        authors = list(self.session.scalars(select(Author).order_by(Author.id)))
        works = list(self.session.scalars(select(Work).order_by(Work.id)))
        groups = list(self.session.scalars(select(WorkGroup).order_by(WorkGroup.id)))
        return {
            "format": BACKUP_FORMAT,
            "generated_at": datetime.now(UTC).isoformat(),
            "privacy": {
                "excluded": [
                    "admin accounts and sessions",
                    "system settings and API credentials",
                    "X sessions, cookies and collector tokens",
                    "notification outbox payloads",
                ],
                "raw_metadata_sensitive_keys_removed": True,
            },
            "authors": [
                {
                    "id": item.id,
                    "name": item.name,
                    "aliases": item.aliases or [],
                    "circle_names": item.circle_names or [],
                    "provider_query_names": item.provider_query_names or {},
                    "identity_note": item.identity_note,
                    "created_at": _datetime(item.created_at),
                    "last_checked_at": _datetime(item.last_checked_at),
                }
                for item in authors
            ],
            "works": [self._work(item) for item in works],
            "work_groups": [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "cover_url": _sanitize_url(item.cover_url),
                    "status": item.status,
                    "year": item.year,
                    "language": item.language,
                    "tags": item.tags or [],
                    "member_work_ids": [member.work_id for member in item.members],
                    "manual_metadata": item.manual_metadata or {},
                    "created_at": _datetime(item.created_at),
                    "updated_at": _datetime(item.updated_at),
                }
                for item in groups
            ],
            "series": self._series(),
            "work_relations": [
                {
                    "source_group_id": item.source_group_id,
                    "target_group_id": item.target_group_id,
                    "relation_type": item.relation_type,
                    "is_manual": item.is_manual,
                }
                for item in self.session.scalars(select(WorkRelation).order_by(WorkRelation.id))
            ],
            "planned_releases": [
                {
                    "id": item.id,
                    "author_id": item.author_id,
                    "title": item.title,
                    "status": item.status,
                    "release_date": _datetime(item.release_date),
                    "event_code": item.event_code,
                    "booth": item.booth,
                    "linked_group_id": item.linked_group_id,
                    "suggested_group_id": item.suggested_group_id,
                }
                for item in self.session.scalars(select(PlannedRelease).order_by(PlannedRelease.id))
            ],
            "catalog_events": [
                {
                    "id": item.id,
                    "author_id": item.author_id,
                    "group_id": item.group_id,
                    "provider": item.provider,
                    "external_id": item.external_id,
                    "event_type": item.event_type,
                    "title": item.title,
                    "state": item.state,
                    "created_at": _datetime(item.created_at),
                }
                for item in self.session.scalars(select(CatalogEvent).order_by(CatalogEvent.id))
            ],
        }

    def export_bytes(self) -> bytes:
        return json.dumps(
            self.export(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()

    def backup_bytes(self) -> bytes:
        catalog = self.export_bytes()
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at": datetime.now(UTC).isoformat(),
            "files": {
                "catalog.json": {
                    "sha256": sha256(catalog).hexdigest(),
                    "byte_size": len(catalog),
                }
            },
            "sensitive_data_included": False,
        }
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("catalog.json", catalog)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
            )
        return output.getvalue()

    def _work(self, work: Work) -> dict[str, Any]:
        sources = list(
            self.session.scalars(
                select(WorkSource).where(WorkSource.work_id == work.id).order_by(WorkSource.id)
            )
        )
        return {
            "id": work.id,
            "title": work.title,
            "description": work.description,
            "cover_url": _sanitize_url(work.cover_url),
            "status": work.status,
            "year": work.year,
            "language": work.language,
            "tags": work.tags or [],
            "author_ids": [author.id for author in work.authors],
            "sources": [
                {
                    "provider": item.provider,
                    "external_id": item.external_id,
                    "source_url": _sanitize_url(item.source_url),
                    "source_updated_at": _datetime(item.source_updated_at),
                    "raw_metadata": _sanitize(item.raw_metadata or {}),
                }
                for item in sources
            ],
            "created_at": _datetime(work.created_at),
            "updated_at": _datetime(work.updated_at),
        }

    def _series(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self.session.scalars(select(Series).order_by(Series.id)):
            members = self.session.scalars(
                select(SeriesMember)
                .where(SeriesMember.series_id == item.id)
                .order_by(SeriesMember.volume_number, SeriesMember.group_id)
            )
            result.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "members": [
                        {
                            "group_id": member.group_id,
                            "volume_number": member.volume_number,
                            "label": member.label,
                            "is_manual": member.is_manual,
                        }
                        for member in members
                    ],
                }
            )
        return result


def verify_backup(data: bytes) -> dict[str, Any]:
    if not data or len(data) > 100 * 1024 * 1024:
        raise ValueError("备份为空或超过 100 MB")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if sum(item.file_size for item in entries) > 200 * 1024 * 1024:
                raise ValueError("备份解压后体积过大")
            if sorted(names) != ["catalog.json", "manifest.json"]:
                raise ValueError("备份文件清单不完整或包含多余文件")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
                raise ValueError("备份格式不受支持")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != {"catalog.json"}:
                raise ValueError("备份清单缺少 catalog.json")
            expected = files["catalog.json"]
            if not isinstance(expected, dict):
                raise ValueError("备份清单格式无效")
            payload = archive.read("catalog.json")
            if len(payload) != expected.get("byte_size"):
                raise ValueError("catalog.json 文件大小校验失败")
            if sha256(payload).hexdigest() != expected.get("sha256"):
                raise ValueError("catalog.json SHA-256 校验失败")
            catalog = json.loads(payload)
            if not isinstance(catalog, dict) or catalog.get("format") != BACKUP_FORMAT:
                raise ValueError("目录数据格式不受支持")
            for key in (
                "authors",
                "works",
                "work_groups",
                "series",
                "work_relations",
                "planned_releases",
                "catalog_events",
            ):
                if not isinstance(catalog.get(key), list):
                    raise ValueError(f"目录数据缺少有效的 {key}")
    except (BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("不是有效的 MangaFinder 备份") from exc
    return {
        "valid": True,
        "format": manifest["format"],
        "created_at": manifest.get("created_at"),
        "verified_files": ["catalog.json"],
        "sensitive_data_included": manifest.get("sensitive_data_included", False),
    }
