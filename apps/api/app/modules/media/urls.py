from hashlib import sha256

from app.db.models import Work, WorkGroup

_PROXIED_PROVIDERS = frozenset({"mangadex", "nhentai", "wnacg"})


def work_cover_url(work: Work) -> str | None:
    if not work.cover_url:
        return None
    if not any(source.provider in _PROXIED_PROVIDERS for source in work.sources):
        return work.cover_url
    version = sha256(work.cover_url.encode()).hexdigest()[:12]
    return f"/api/works/{work.id}/cover?v={version}"


def group_cover_url(group: WorkGroup) -> str | None:
    if not group.cover_url:
        return None
    selected = next(
        (
            member.work
            for member in group.members
            if member.work.cover_url == group.cover_url
        ),
        None,
    )
    if selected is None:
        selected = next(
            (member.work for member in group.members if member.work.cover_url), None
        )
    return work_cover_url(selected) if selected else group.cover_url
