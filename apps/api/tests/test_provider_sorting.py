from datetime import UTC, datetime

from app.providers.base import DiscoveredWork, sort_discovered_works


def test_discovered_works_are_sorted_newest_first_with_undated_last() -> None:
    works = [
        DiscoveredWork(
            external_id="old",
            title="Old",
            source_url="https://example.test/old",
            source_updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        DiscoveredWork(
            external_id="undated",
            title="Undated",
            source_url="https://example.test/undated",
        ),
        DiscoveredWork(
            external_id="new",
            title="New",
            source_url="https://example.test/new",
            source_updated_at=datetime(2026, 7, 13, tzinfo=UTC),
        ),
    ]

    assert [work.external_id for work in sort_discovered_works(works)] == [
        "new",
        "old",
        "undated",
    ]
