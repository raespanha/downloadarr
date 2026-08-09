from contextlib import asynccontextmanager
import logging

import uvicorn
from fastapi import FastAPI

from ..arr_metadata import ArrMetadataResolver, SourceResolver
from ..db.engine import Database
from ..downloader import Downloader
from ..jobs import JobPoller, JobService
from ..providers.base import TorrentProvider
from ..providers.torbox import TorBoxProvider
from ..settings import Settings, SettingsService, load_settings
from .auth import SessionStore
from .dashboard import router as dashboard_router
from .qbittorrent import router


def create_app(settings: Settings | None = None, provider: TorrentProvider | None = None,
               *, start_poller: bool = True, downloader: Downloader | None = None,
               settings_service: SettingsService | None = None,
               source_resolver: SourceResolver | None = None) -> FastAPI:
    configured = settings or load_settings()
    actual_settings_service = settings_service or SettingsService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(configured.database_url)
        await database.migrate()
        if provider is None and not configured.torbox_api_token.get_secret_value():
            await database.close()
            raise RuntimeError("TORBOX_API_TOKEN is required")
        actual_provider = provider or TorBoxProvider(
            configured.torbox_api_token.get_secret_value(), configured.torbox_api_base,
            configured.torbox_request_timeout)
        actual_resolver = source_resolver or ArrMetadataResolver(configured.integrations)
        job_service = JobService(database, actual_provider, poll_interval=configured.poll_interval,
                                 queued_poll_interval=configured.queued_poll_interval,
                                 max_backoff=configured.max_poll_backoff,
                                 max_job_failures=configured.max_job_failures,
                                 download_path=configured.download_path,
                                 download_connections=min(configured.download.connections,
                                                          configured.download.provider_max_connections),
                                 download_transfer_mode=configured.download.transfer_mode,
                                 downloader=downloader, source_resolver=actual_resolver)
        for name, path in configured.download.categories.items():
            await job_service.ensure_category(name, path)
        await job_service.bootstrap_lifecycle()
        await job_service.evaluate_alerts()
        if configured.telemetry.retention_days:
            try:
                await job_service.prune_telemetry(
                    configured.telemetry.retention_days, dry_run=False)
            except Exception:
                logging.getLogger(__name__).exception("Telemetry retention pass failed")
        poller = JobPoller(job_service, configured.provider_concurrency)
        app.state.settings = configured
        app.state.settings_service = actual_settings_service
        app.state.database = database
        app.state.provider = actual_provider
        app.state.source_resolver = actual_resolver
        app.state.job_service = job_service
        app.state.auth_sessions = SessionStore()
        app.state.poller = poller
        if start_poller:
            poller.start()
        try:
            yield
        finally:
            if start_poller:
                await poller.stop()
            await actual_provider.close()
            await actual_resolver.close()
            await database.close()

    app = FastAPI(title="Downloadarr", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(router)
    app.include_router(dashboard_router)
    return app


def create_default_app() -> FastAPI:
    return create_app()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run("downloadarr.api.app:create_default_app", factory=True, host="0.0.0.0", port=6500)
