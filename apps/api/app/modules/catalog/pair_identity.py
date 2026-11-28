import hashlib
import json

from app.db.models import Work, WorkGroup


def work_source_anchor(work: Work) -> str:
    identities = sorted(f"{source.provider}:{source.external_id}" for source in work.sources)
    if identities:
        return identities[0]
    return f"title:{work.title.casefold()}"


def work_source_identities(work: Work) -> tuple[str, ...]:
    identities = tuple(
        sorted({f"{source.provider}:{source.external_id}" for source in work.sources})
    )
    if identities:
        return identities
    return (f"title:{work.title.casefold()}",)


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


def group_anchor_info(group: WorkGroup) -> tuple[str, tuple[str, ...]]:
    primary = group_source_anchor(group)
    member_anchors: set[str] = {primary}
    for member in group.members:
        member_anchors.add(work_source_anchor(member.work))
    return primary, tuple(sorted(member_anchors))


def candidate_key_for_anchors(left_anchor: str, right_anchor: str) -> str:
    anchors = sorted((left_anchor, right_anchor))
    return hashlib.sha256(json.dumps(anchors, ensure_ascii=False).encode()).hexdigest()


def candidate_key(left: WorkGroup, right: WorkGroup) -> str:
    return candidate_key_for_anchors(group_source_anchor(left), group_source_anchor(right))


def is_anchor_pair_constrained(
    left_primary: str,
    left_members: tuple[str, ...],
    right_primary: str,
    right_members: tuple[str, ...],
    constrained_keys: set[str],
) -> bool:
    if not constrained_keys:
        return False
    if candidate_key_for_anchors(left_primary, right_primary) in constrained_keys:
        return True
    for la in left_members:
        for ra in right_members:
            if candidate_key_for_anchors(la, ra) in constrained_keys:
                return True
    return False
