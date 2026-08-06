from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ..db.engine import Database
from ..downloader import Downloader
from ..jobs import JobPoller, JobService
from ..providers.base import TorrentProvider
from ..providers.torbox import TorBoxProvider
from ..settings import Settings, SettingsService, load_settings
from .auth import SessionStore
from .qbittorrent import router


def create_app(settings: Settings | None = None, provider: TorrentProvider | None = None,
               *, start_poller: bool = True, downloader: Downloader | None = None) -> FastAPI:
    configured = settings or load_settings()
    settings_service = SettingsService()

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
        job_service = JobService(database, actual_provider, poll_interval=configured.poll_interval,
                                 queued_poll_interval=configured.queued_poll_interval,
                                 max_backoff=configured.max_poll_backoff,
                                 download_path=configured.download_path,
                                 download_connections=min(configured.download.connections,
                                                          configured.download.provider_max_connections),
                                 downloader=downloader)
        poller = JobPoller(job_service, configured.provider_concurrency)
        app.state.settings = configured
        app.state.settings_service = settings_service
        app.state.database = database
        app.state.provider = actual_provider
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
            await database.close()

    app = FastAPI(title="Downloadarr", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


def create_default_app() -> FastAPI:
    return create_app()


def main() -> None:
    uvicorn.run("downloadarr.api.app:create_default_app", factory=True, host="0.0.0.0", port=6500)
