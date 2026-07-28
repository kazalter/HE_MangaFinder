from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Work, WorkFingerprint, WorkGroup, WorkGroupMember
from app.modules.catalog.cover_fingerprint import fingerprint_image
from app.modules.catalog.image_search import LocalImageSearch


def _cover(color: str, accent: str) -> Image.Image:
    image = Image.new("RGB", (320, 480), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 70, 270, 400), outline=accent, width=18)
    draw.ellipse((100, 140, 220, 260), fill=accent)
    draw.line((40, 430, 280, 30), fill="white", width=12)
    return image


def _indexed_work(
    session: Session, title: str, image: Image.Image
) -> tuple[WorkGroup, Work]:
    work = Work(title=title, cover_url=f"https://example.test/{title}.jpg")
    group = WorkGroup(title=title, cover_url=work.cover_url)
    session.add_all([work, group])
    session.flush()
    session.add_all(
        [
            WorkGroupMember(group_id=group.id, work_id=work.id),
            WorkFingerprint(
                work_id=work.id,
                normalized_title=title.casefold(),
                cover_fingerprint=fingerprint_image(image),
            ),
        ]
    )
    session.flush()
    return group, work


def test_local_image_search_finds_crop_and_returns_grounded_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        expected_image = _cover("#274060", "#f4d35e")
        expected, _ = _indexed_work(session, "Expected", expected_image)
        _indexed_work(session, "Other", _cover("#8f2d56", "#218380"))

        screenshot = expected_image.crop((35, 30, 285, 450))
        matches = LocalImageSearch(session).search(fingerprint_image(screenshot))

        assert matches
        assert matches[0].group_id == expected.id
        assert matches[0].mode in {"full", "crop"}
        assert matches[0].distance <= 20
        assert "组合 dHash、pHash 与颜色分布" in matches[0].evidence
