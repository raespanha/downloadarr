from contextlib import asynccontextmanager
import asyncio
import logging
import os
import shutil
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Response

from ..arr_metadata import ArrMetadataResolver, SourceResolver
from ..db.engine import Database
from ..downloader import Downloader
from ..jobs import JobPoller, JobService
from ..providers.base import TorrentProvider
from ..providers.torbox import TorBoxProvider
from ..process_lock import ProcessLock
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
        process_lock = ProcessLock.for_database(configured.database_url)
        process_lock.acquire()
        database = actual_provider = actual_resolver = poller = monitor_task = None
        monitor_stop = asyncio.Event()
        app.state.draining = False
        try:
            database = Database(configured.database_url)
            await database.migrate()
            if provider is None and not configured.torbox_api_token.get_secret_value():
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
                                 minimum_file_size_mb=configured.download.minimum_file_size_mb,
                                 allowed_file_extensions=(
                                     configured.download.allowed_file_extensions),
                                 blocked_file_extensions=(
                                     configured.download.blocked_file_extensions),
                                     downloader=downloader, source_resolver=actual_resolver)
            for path in {configured.download.path, *configured.download.categories.values()}:
                Path(path).mkdir(parents=True, exist_ok=True)
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

            async def monitor() -> None:
                while not monitor_stop.is_set():
                    try:
                        await job_service.evaluate_alerts()
                    except Exception:
                        logging.getLogger(__name__).exception("Monitoring evaluation failed")
                    try:
                        await asyncio.wait_for(monitor_stop.wait(), timeout=30)
                    except TimeoutError:
                        pass

            app.state.settings = configured
            app.state.settings_service = actual_settings_service
            app.state.database = database
            app.state.provider = actual_provider
            app.state.source_resolver = actual_resolver
            app.state.job_service = job_service
            app.state.auth_sessions = SessionStore()
            app.state.settings_update_lock = asyncio.Lock()
            app.state.poller = poller
            app.state.process_lock = process_lock
            if start_poller:
                poller.start()
                monitor_task = asyncio.create_task(monitor(), name="downloadarr-monitor")
            yield
        finally:
            app.state.draining = True
            monitor_stop.set()
            if monitor_task:
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass
            if start_poller and poller:
                await poller.stop()
            if actual_provider:
                await actual_provider.close()
            if actual_resolver:
                await actual_resolver.close()
            if database:
                await database.close()
            process_lock.release()

    app = FastAPI(title="Downloadarr", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> Response:
        try:
            database_ready = await app.state.database.readiness()
            lock_ready = app.state.process_lock.owned
            poller_ready = (not start_poller or app.state.poller.running)
            current_settings = app.state.settings
            roots = [Path(current_settings.download.path),
                     *[Path(path) for path in current_settings.download.categories.values()]]
            storage_ready = all(path.exists() and os.access(path, os.W_OK) and
                                shutil.disk_usage(path).free >= 64 * 1024 * 1024
                                for path in roots)
            is_ready = (not app.state.draining and database_ready and lock_ready
                        and poller_ready and storage_ready)
        except Exception:
            is_ready = False
        return Response(content='{"status":"ready"}' if is_ready else '{"status":"not_ready"}',
                        media_type="application/json", status_code=200 if is_ready else 503)

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


if __name__ == "__main__":
    main()
