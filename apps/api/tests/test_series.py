from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, AuthorWork, SeriesMember, Work, WorkGroup, WorkGroupMember
from app.modules.catalog.series import SeriesService, parse_numbered_title
from app.modules.catalog.series_router import _series_read


def _group(session: Session, author: Author, title: str) -> WorkGroup:
    work = Work(title=title)
    group = WorkGroup(title=title)
    session.add_all([work, group])
    session.flush()
    session.add_all(
        [
            WorkGroupMember(group_id=group.id, work_id=work.id),
            AuthorWork(author_id=author.id, work_id=work.id),
        ]
    )
    session.flush()
    return group


def test_numbered_title_parser_requires_explicit_volume_marker() -> None:
    parsed = parse_numbered_title("夜空 Vol. 3")
    assert parsed is not None
    assert parsed.base == "夜空"
    assert parsed.number == 3
    assert parse_numbered_title("夜空 3") is None


def test_series_candidates_are_author_scoped_and_report_gaps() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作者")
        other = Author(name="別作者")
        session.add_all([author, other])
        session.flush()
        first = _group(session, author, "夜空 Vol. 1")
        third = _group(session, author, "夜空 Vol. 3")
        _group(session, other, "夜空 Vol. 2")

        candidates = SeriesService(session).candidates(author.id)

        assert len(candidates) == 1
        assert candidates[0].missing_volumes == (2,)
        assert [item[0] for item in candidates[0].groups] == [first.id, third.id]


def test_manual_series_correction_and_work_relations() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作者")
        session.add(author)
        session.flush()
        first = _group(session, author, "本篇 第1巻")
        third = _group(session, author, "本篇 第3巻")
        side = _group(session, author, "本篇 番外")
        service = SeriesService(session)
        original = service.create(
            "本篇", [(first.id, 1), (third.id, 3)], manual=False
        )
        corrected = service.create("人工系列", [], manual=True)

        service.set_member(corrected.id, third.id, 2, manual=True)
        relation = service.add_relation(first.id, side.id, "side_story")
        session.flush()

        moved = session.get(SeriesMember, (corrected.id, third.id))
        assert moved is not None and moved.is_manual is True
        assert session.get(SeriesMember, (original.id, third.id)) is None
        assert relation in service.relations_for_group(side.id)
        assert _series_read(session, original).missing_volumes == []
        assert [item.group_id for item in _series_read(session, corrected).members] == [
            third.id
        ]
