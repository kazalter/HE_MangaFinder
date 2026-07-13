import hashlib
import json

from app.db.models import WorkGroup


def group_source_anchor(group: WorkGroup) -> str:
    identities = sorted(
        f"{source.provider}:{source.external_id}"
        for member in group.members
        for source in member.work.sources
    )
    if identities:
        return identities[0]
    titles = sorted(member.work.title.casefold() for member in group.members)
    return f"title:{titles[0] if titles else group.title.casefold()}"


def candidate_key(left: WorkGroup, right: WorkGroup) -> str:
    anchors = sorted((group_source_anchor(left), group_source_anchor(right)))
    return hashlib.sha256(json.dumps(anchors, ensure_ascii=False).encode()).hexdigest()
