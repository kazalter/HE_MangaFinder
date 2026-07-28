from datetime import UTC, datetime
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Job, JobStatus
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.jobs.repository import DOWNLOAD_CHAPTER
from app.modules.media.urls import group_cover_url

_ATOM = "http://www.w3.org/2005/Atom"
_OPDS_ACQUISITION = "http://opds-spec.org/acquisition"
ElementTree.register_namespace("", _ATOM)


def _tag(name: str) -> str:
    return f"{{{_ATOM}}}{name}"


def build_opds_catalog(session: Session, settings: Settings) -> bytes:
    base = settings.public_base_url.rstrip("/")
    groups = WorkGroupRepository(session).list_groups()
    root = ElementTree.Element(_tag("feed"))
    ElementTree.SubElement(root, _tag("id")).text = f"{base}/api/opds/v1.2/catalog.xml"
    ElementTree.SubElement(root, _tag("title")).text = "MangaFinder 本地资料库"
    ElementTree.SubElement(root, _tag("updated")).text = datetime.now(UTC).isoformat()
    ElementTree.SubElement(
        root,
        _tag("link"),
        {"rel": "self", "href": f"{base}/api/opds/v1.2/catalog.xml"},
    )

    downloads: dict[int, list[int]] = {}
    jobs = session.scalars(
        select(Job).where(
            Job.kind == DOWNLOAD_CHAPTER,
            Job.status == JobStatus.SUCCEEDED,
        )
    )
    for job in jobs:
        work_id = job.payload.get("work_id")
        output = job.payload.get("output_path")
        if isinstance(work_id, int) and output:
            downloads.setdefault(work_id, []).append(job.id)

    for group in groups:
        entry = ElementTree.SubElement(root, _tag("entry"))
        ElementTree.SubElement(entry, _tag("id")).text = f"urn:mangafinder:group:{group.id}"
        ElementTree.SubElement(entry, _tag("title")).text = group.title
        ElementTree.SubElement(entry, _tag("updated")).text = group.updated_at.isoformat()
        summary = ElementTree.SubElement(entry, _tag("summary"))
        summary.text = group.description or "MangaFinder 聚合作品"
        authors = sorted(
            {
                author.name
                for member in group.members
                for author in member.work.authors
            }
        )
        for name in authors:
            author_node = ElementTree.SubElement(entry, _tag("author"))
            ElementTree.SubElement(author_node, _tag("name")).text = name
        cover = group_cover_url(group)
        if cover:
            ElementTree.SubElement(
                entry,
                _tag("link"),
                {
                    "rel": "http://opds-spec.org/image",
                    "href": f"{base}{cover}",
                    "type": "image/jpeg",
                },
            )
        for member in group.members:
            for job_id in downloads.get(member.work_id, []):
                ElementTree.SubElement(
                    entry,
                    _tag("link"),
                    {
                        "rel": _OPDS_ACQUISITION,
                        "href": f"{base}/api/jobs/{job_id}/file",
                        "type": "application/vnd.comicbook+zip",
                    },
                )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
