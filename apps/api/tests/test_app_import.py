def test_application_imports_with_all_routes() -> None:
    from app.main import app

    assert app.title == "MangaFinder"
