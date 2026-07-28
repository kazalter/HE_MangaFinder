from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PlannedRelease, ReleaseSignal, WorkGroup
from app.modules.catalog.title_identity import normalize_identity_text

_TRACKED_KINDS = {
    "new_release",
    "release_preview",
    "cover_reveal",
    "preorder",
    "on_sale",
    "delay",
    "cancellation",
}


class PlannedReleaseService:
    """Maintains the pre-catalog lifecycle shared by independently collected signals."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def absorb_signal(self, signal: ReleaseSignal) -> PlannedRelease | None:
        if signal.kind not in _TRACKED_KINDS or not signal.title:
            return None
        normalized = normalize_identity_text(signal.title)
        if not normalized:
            return None
        planned = self.session.scalar(
            select(PlannedRelease).where(
                PlannedRelease.author_id == signal.author_id,
                PlannedRelease.normalized_title == normalized,
            )
        )
        if planned is None:
            planned = PlannedRelease(
                author_id=signal.author_id,
                title=signal.title,
                normalized_title=normalized,
            )
            self.session.add(planned)
            self.session.flush()
        planned.title = signal.title or planned.title
        planned.release_date = signal.release_date or planned.release_date
        planned.event_code = signal.event_code or planned.event_code
        planned.booth = signal.booth or planned.booth
        if signal.kind == "cancellation":
            planned.status = "cancelled"
        elif signal.kind == "on_sale" and planned.status != "cancelled":
            planned.status = "released"
        signal.planned_release_id = planned.id
        self.session.flush()
        return planned

    def suggest_catalog_match(
        self, author_id: int, group: WorkGroup
    ) -> PlannedRelease | None:
        names = {normalize_identity_text(group.title)}
        for member in group.members:
            names.add(normalize_identity_text(member.work.title))
            if member.work.fingerprint:
                names.add(member.work.fingerprint.normalized_title)
                names.update(member.work.fingerprint.title_aliases)
        names.discard("")
        matches = list(
            self.session.scalars(
                select(PlannedRelease).where(
                    PlannedRelease.author_id == author_id,
                    PlannedRelease.normalized_title.in_(names),
                    PlannedRelease.linked_group_id.is_(None),
                    PlannedRelease.status.not_in(["cancelled", "released"]),
                )
            )
        )
        if len(matches) != 1:
            return None
        planned = matches[0]
        planned.suggested_group_id = group.id
        planned.status = "discovered"
        self.session.flush()
        return planned

    def link(self, planned: PlannedRelease, group_id: int) -> PlannedRelease:
        planned.linked_group_id = group_id
        planned.suggested_group_id = None
        planned.status = "linked"
        for signal in self.session.scalars(
            select(ReleaseSignal).where(ReleaseSignal.planned_release_id == planned.id)
        ):
            signal.linked_group_id = group_id
            if signal.status == "confirmed":
                signal.status = "linked"
        self.session.flush()
        return planned


def backfill_planned_releases(session: Session) -> int:
    service = PlannedReleaseService(session)
    count = 0
    signals = list(
        session.scalars(
            select(ReleaseSignal).where(ReleaseSignal.planned_release_id.is_(None))
        )
    )
    for signal in signals:
        planned = service.absorb_signal(signal)
        if planned:
            count += 1
            if signal.linked_group_id:
                service.link(planned, signal.linked_group_id)
    return count
