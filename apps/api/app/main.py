import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.modules.agent_review.router import router as agent_reviews_router
from app.modules.assistant.router import router as assistant_router
from app.modules.authors.router import router as authors_router
from app.modules.backup.router import router as backup_router
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.downloads_router import router as downloads_router
from app.modules.catalog.groups_router import router as groups_router
from app.modules.catalog.image_search_router import router as image_search_router
from app.modules.catalog.operations_router import router as operations_router
from app.modules.catalog.router import router as catalog_router
from app.modules.catalog.scheduler import CatalogScheduler
from app.modules.catalog.series_router import router as series_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.integrations.router import router as integrations_router
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.router import router as jobs_router
from app.modules.jobs.worker import (
    CoverWorkerThread,
    DownloadWorker,
    JobWorker,
    NotificationWorkerThread,
)
from app.modules.media.router import router as media_router
from app.modules.media.service import CoverCacheService
from app.modules.social.router import router as social_router
from app.modules.social.scheduler import SocialScheduler
from app.modules.sources.router import router as sources_router
from app.modules.system_config.repository import apply_runtime_overrides
from app.modules.system_config.router import router as system_config_router
from app.modules.system_config.security import protect_api_writes
from app.providers.registry import build_registry

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    with SessionLocal() as session:
        apply_runtime_overrides(session, settings)
        JobRepository(session).recover_interrupted()
        DiscoveryRepository(session).recover_interrupted(datetime.now(UTC))
    providers = build_registry(settings)
    app.state.providers = providers
    app.state.cover_cache = CoverCacheService(settings.cover_cache_dir, providers)
    worker_task = None
    download_worker_task = None
    cover_worker = None
    notification_worker = None
    catalog_scheduler_task = None
    social_scheduler_task = None
    if settings.worker_enabled:
        worker_task = asyncio.create_task(
            JobWorker(
                settings, providers, exclude_downloads=True, exclude_covers=True,
                exclude_notifications=True,
            ).run()
        )
        download_worker_task = asyncio.create_task(DownloadWorker(settings, providers).run())
        cover_worker = CoverWorkerThread(settings)
        cover_worker.start()
        notification_worker = NotificationWorkerThread(settings)
        notification_worker.start()
        catalog_scheduler_task = asyncio.create_task(CatalogScheduler(settings, providers).run())
    if settings.social_enabled:
        social_scheduler_task = asyncio.create_task(SocialScheduler(settings).run())
    yield
    tasks = [
        task
        for task in (
            worker_task,
            download_worker_task,
            catalog_scheduler_task,
            social_scheduler_task,
        )
        if task
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if cover_worker:
        await cover_worker.stop()
    if notification_worker:
        await notification_worker.stop()
    await providers.close()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(protect_api_writes)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(authors_router, prefix="/api")
app.include_router(agent_reviews_router, prefix="/api")
app.include_router(assistant_router, prefix="/api")
app.include_router(backup_router, prefix="/api")
app.include_router(catalog_router, prefix="/api")
app.include_router(downloads_router, prefix="/api")
app.include_router(groups_router, prefix="/api")
app.include_router(operations_router, prefix="/api")
app.include_router(image_search_router, prefix="/api")
app.include_router(series_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(media_router, prefix="/api")
app.include_router(sources_router, prefix="/api")
app.include_router(social_router, prefix="/api")
app.include_router(system_config_router, prefix="/api")

if settings.static_dir.exists():
    assets_dir = settings.static_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        return FileResponse(settings.static_dir / "index.html")
