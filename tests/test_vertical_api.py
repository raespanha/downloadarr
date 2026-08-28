from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from downloadarr.api import create_app
from downloadarr.arr_metadata import SourceMetadata
from downloadarr.db.models import ControlState, JobState
from downloadarr.providers.base import (ProviderFile, ProviderQueuedTorrent, ProviderSubmission,
                                        ProviderError, ProviderTorrent)
from downloadarr.settings import Settings, SettingsService, load_settings
from downloadarr.state import DownloadResult

HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Test.Release"
TORRENT_INFO = (b"d6:lengthi5e4:name12:Test.Release12:piece lengthi16384e"
                b"6:pieces20:abcdefghijklmnopqrste")
TORRENT = b"d4:info" + TORRENT_INFO + b"e"
TORRENT_HASH = hashlib.sha1(TORRENT_INFO).hexdigest()


class FakeProvider:
    def __init__(self):
        self.creates = []
        self.torrent_creates = []
        self.torrent = ProviderTorrent(42, HASH, "Test.Release", "downloading", 1000,
                                       0.4, 100, 6, False, True)
        self.queued = []
        self.files = [ProviderFile(0, "Test.Release.mkv", 5)]
        self.download_requests = []
        self.deleted_torrents = []
        self.deleted_queued = []
        self.paused_torrents = []
        self.resumed_torrents = []
        self.closed = False

    async def create_magnet(self, magnet):
        self.creates.append(magnet)
        return ProviderSubmission(remote_id=42)

    async def create_torrent(self, payload, filename, info_hash):
        self.torrent_creates.append((payload, filename, info_hash))
        return ProviderSubmission(remote_id=42)

    async def get_torrent(self, remote_id):
        assert remote_id == 42
        return self.torrent

    async def find_torrent(self, info_hash):
        return self.torrent if self.torrent.info_hash == info_hash else None

    async def get_queued(self):
        return self.queued

    async def get_files(self, remote_id):
        assert remote_id == 42
        return self.files

    async def request_download(self, remote_id, file_id):
        self.download_requests.append((remote_id, file_id))
        return "https://example.test/signed"

    async def delete_torrent(self, remote_id):
        self.deleted_torrents.append(remote_id)

    async def delete_queued(self, queued_id):
        self.deleted_queued.append(queued_id)

    async def pause_torrent(self, remote_id):
        self.paused_torrents.append(remote_id)

    async def resume_torrent(self, remote_id):
        self.resumed_torrents.append(remote_id)

    async def close(self):
        self.closed = True


class FakeDownloader:
    def __init__(self, payload=b"hello"):
        self.payload = payload
        self.destinations = []

    async def download(self, url_provider, destination, progress_callback=None):
        await url_provider(False)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        self.destinations.append(destination)
        return DownloadResult(destination, len(self.payload), 0.1, len(self.payload) * 10,
                              False, True)


class FakeSourceResolver:
    def __init__(self, indexer="The Pirate Bay (Prowlarr)", indexer_id=7):
        self.indexer, self.indexer_id, self.closed = indexer, indexer_id, False
        self.blocklisted = []

    def service_for(self, category):
        return {"tv-sonarr": "sonarr", "radarr": "radarr"}.get(category, "other")

    async def resolve(self, category, info_hash):
        return SourceMetadata(self.service_for(category), self.indexer, self.indexer_id)

    async def blocklist(self, category, info_hash):
        self.blocklisted.append((category, info_hash))
        return True

    async def close(self):
        self.closed = True


def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate({
        "database": {"url": f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}"},
        "download": {"path": tmp_path / "downloads"},
        "qbittorrent": {"username": "user", "password": "test-password-123!"},
        "torbox": {"api_token": "test"},
        "scheduler": {"poll_interval": 0.01, "queued_poll_interval": 0.01},
    })


@asynccontextmanager
async def client_for(tmp_path, provider=None, downloader=None, source_resolver=None):
    fake = provider or FakeProvider()
    app = create_app(settings(tmp_path), fake, start_poller=False,
                     downloader=downloader or FakeDownloader(),
                     source_resolver=source_resolver)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            yield client, app, fake


async def login(client):
    response = await client.post(
        "/api/v2/auth/login", data={"username": "user", "password": "test-password-123!"})
    assert response.status_code == 200 and response.text == "Ok."


async def test_auth_versions_preferences_and_logout(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        assert (await client.get("/api/v2/app/webapiVersion")).text == "2.8.1"
        assert (await client.get("/api/v2/app/preferences")).status_code == 403
        assert (await client.post("/api/v2/auth/login", data={"username": "user", "password": "bad"})).status_code == 403
        await login(client)
        assert (await client.get("/api/v2/app/version")).text == "v4.3.9"
        preferences = (await client.get("/api/v2/app/preferences")).json()
        assert preferences["dht"] is True
        await client.post("/api/v2/auth/logout")
        assert (await client.get("/api/v2/app/preferences")).status_code == 403


async def test_health_handshake_and_transfer_info(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        await login(client)
        build = (await client.get("/api/v2/app/buildInfo")).json()
        assert build["bitness"] == "64"
        assert (await client.get("/api/v2/app/defaultSavePath")).text.endswith("downloads")
        transfer = (await client.get("/api/v2/transfer/info")).json()
        assert transfer["connection_status"] == "connected"


async def test_dashboard_requires_login_and_lists_jobs_without_secrets(tmp_path):
    values = settings(tmp_path).model_dump()
    values["torbox"]["api_token"] = "unique-dashboard-secret"
    configured = Settings.model_validate(values)
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader(),
                     settings_service=SettingsService(tmp_path / "settings.json"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.get("/", follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/ui/login"
            assert (await client.get("/ui/api/jobs")).status_code == 403
            assert (await client.get("/ui/api/performance")).status_code == 403
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            page = await client.get("/")
            assert page.status_code == 200 and "Downloadarr" in page.text
            assert "unique-dashboard-secret" not in page.text
            jobs = (await client.get("/ui/api/jobs")).json()
            assert jobs[0]["phase"] == "Submitting"
            assert jobs[0]["hash"] == HASH
            assert jobs[0]["files"] == []
            assert 'data-tab="monitoring"' in page.text
            assert 'id="downloads-view"' in page.text
            assert 'id="remove-dialog"' in page.text
            assert "Remove download?" in page.text
            assert "Sonarr/Radarr Activity" in page.text
            assert "Plex library files are kept" in page.text
            assert "Confirm and blacklist" in page.text
            assert "confirm(" not in page.text
            assert 'id="lifecycle-pagination"' in page.text
            assert 'data-monitor-tab="operational"' in page.text
            assert 'data-monitor-tab="performance"' in page.text
            assert 'id="transfer-pagination"' in page.text
            assert 'data-transfer-status="all"' in page.text
            assert 'data-transfer-status="succeeded"' in page.text
            assert 'data-transfer-status="failed"' in page.text
            assert "const MONITOR_TABLE_PAGE_SIZE=10" in page.text


async def test_dashboard_remove_and_blacklist_coordinates_arr_before_cleanup(tmp_path):
    resolver = FakeSourceResolver()
    async with client_for(tmp_path, source_resolver=resolver) as (client, app, provider):
        await login(client)
        await app.state.job_service.ensure_category(
            "tv-sonarr", str(tmp_path / "downloads" / "tv-sonarr")
        )
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET,
                                                        "category": "tv-sonarr"})
        csrf = app.state.auth_sessions.csrf(client.cookies.get("SID"))
        response = await client.post(
            f"/ui/jobs/{HASH}/remove",
            data={"csrf_token": csrf, "blocklist": "true", "deleteFiles": "false"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert resolver.blocklisted == [("tv-sonarr", HASH)]
        assert await app.state.job_service.job(HASH) is None


async def test_dashboard_login_and_settings_save(tmp_path):
    service = SettingsService(tmp_path / "settings.json")
    app = create_app(settings(tmp_path), FakeProvider(), start_poller=False,
                     downloader=FakeDownloader(), settings_service=service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            invalid = await client.post("/ui/login", data={"username": "user", "password": "bad"},
                                        follow_redirects=False)
            assert invalid.headers["location"].endswith("error=invalid")
            valid = await client.post("/ui/login", data={"username": "user", "password": "test-password-123!"},
                                      follow_redirects=False)
            assert valid.status_code == 303 and "SID=" in valid.headers["set-cookie"]
            csrf = app.state.auth_sessions.csrf(client.cookies.get("SID"))
            download_path = tmp_path / "dashboard-downloads"
            response = await client.post("/ui/settings", data={
                "csrf_token": csrf,
                "torbox_token": "replacement-secret",
                "transfer_mode": "parallel",
                "connections": "12",
                "provider_max_connections": "4",
                "simultaneous_downloads": "3",
                "minimum_file_size_mb": "50",
                "allowed_file_extensions": ".mkv, MP4",
                "blocked_file_extensions": ".zip; .rar",
                "download_path": str(download_path),
                "categories": json.dumps({"tv-sonarr": str(download_path / "tv-sonarr")}),
            }, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/?saved=1"
            restored = load_settings(service.path)
            assert restored.torbox_api_token.get_secret_value() == "replacement-secret"
            assert restored.download.connections == 12
            assert restored.download.transfer_mode == "parallel"
            assert restored.download.minimum_file_size_mb == 50
            assert restored.download.allowed_file_extensions == [".mkv", ".mp4"]
            assert restored.download.blocked_file_extensions == [".zip", ".rar"]
            assert restored.provider_concurrency == 3
            assert app.state.settings.download.minimum_file_size_mb == 50
            assert app.state.job_service.minimum_file_size_bytes == 50 * 1024 * 1024
            assert app.state.job_service.allowed_file_extensions == {".mkv", ".mp4"}
            assert app.state.job_service.blocked_file_extensions == {".zip", ".rar"}
            assert app.state.poller.concurrency == 3
            page = await client.get(response.headers["location"])
            assert "Settings saved and applied" in page.text
            assert "Restart Downloadarr" not in page.text
            assert "setTimeout(()=>dismissNotice(notice),6000)" in page.text
            assert "Simultaneous downloads" in page.text


async def test_minimum_file_size_filters_delivery_before_download(tmp_path):
    values = settings(tmp_path).model_dump()
    values["download"]["minimum_file_size_mb"] = 1
    configured = Settings.model_validate(values)
    provider = FakeProvider()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 2_500_000,
                                       1, 0, 0, True, True)
    provider.files = [ProviderFile(1, "small.nfo", 100_000),
                      ProviderFile(2, "video.mkv", 2_000_000)]
    app = create_app(configured, provider, start_poller=False, downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            job = (await app.state.job_service.jobs())[0]
            for _ in range(3):
                await app.state.job_service.process(job.id)
            prepared = await app.state.job_service.job(HASH)
            assert [(item.provider_file_id, item.relative_path) for item in
                    prepared.delivery_files] == [(2, "video.mkv")]
            dashboard_job = (await client.get("/ui/api/jobs")).json()[0]
            assert dashboard_job["files"][0]["name"] == "video.mkv"
            assert dashboard_job["files"][0]["progress"] == 0
            assert provider.download_requests == []


async def test_minimum_file_size_fails_when_every_file_is_filtered(tmp_path):
    values = settings(tmp_path).model_dump()
    values["download"]["minimum_file_size_mb"] = 1
    configured = Settings.model_validate(values)
    provider = FakeProvider()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 100_000,
                                       1, 0, 0, True, True)
    provider.files = [ProviderFile(1, "small.nfo", 100_000)]
    app = create_app(configured, provider, start_poller=False, downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            job = (await app.state.job_service.jobs())[0]
            for _ in range(3):
                await app.state.job_service.process(job.id)
            failed = await app.state.job_service.job(HASH)
            assert failed.state == JobState.FAILED.value
            assert failed.error_code == "DELIVERY_FAILED"
            assert "minimum file size" in failed.error_message
            assert provider.download_requests == []


async def test_file_extension_allowlist_skips_executables_before_download(tmp_path):
    configured = settings(tmp_path)
    provider = FakeProvider()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 4_000_000,
                                       1, 0, 0, True, True)
    provider.files = [ProviderFile(1, "video.mkv.exe", 2_000_000),
                      ProviderFile(2, "VIDEO.MKV", 2_000_000)]
    app = create_app(configured, provider, start_poller=False, downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            job = (await app.state.job_service.jobs())[0]
            for _ in range(3):
                await app.state.job_service.process(job.id)
            prepared = await app.state.job_service.job(HASH)
            assert [(item.provider_file_id, item.relative_path) for item in
                    prepared.delivery_files] == [(2, "VIDEO.MKV")]
            assert provider.download_requests == []


async def test_executable_only_release_fails_without_requesting_url(tmp_path):
    configured = settings(tmp_path)
    provider = FakeProvider()
    provider.torrent = ProviderTorrent(42, HASH, "Silo.Release", "cached", 2_000_000,
                                       1, 0, 0, True, True)
    provider.files = [ProviderFile(1, "Silo.S03E07.mkv.exe", 2_000_000)]
    app = create_app(configured, provider, start_poller=False, downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            job = (await app.state.job_service.jobs())[0]
            for _ in range(3):
                await app.state.job_service.process(job.id)
            failed = await app.state.job_service.job(HASH)
            assert failed.state == JobState.FAILED.value
            assert failed.error_code == "DELIVERY_FAILED"
            assert "file extension policy" in failed.error_message
            assert provider.download_requests == []


async def test_dashboard_rejects_cross_origin_writes(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        response = await client.post("/ui/settings", headers={"Origin": "https://evil.example"},
                                     data={})
        assert response.status_code == 403


async def test_configured_categories_are_bootstrapped_and_updated(tmp_path):
    values = settings(tmp_path).model_dump()
    values["download"]["categories"] = {"tv-sonarr": tmp_path / "first"}
    configured = Settings.model_validate(values)
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        categories = await app.state.job_service.categories()
        assert categories[0].name == "tv-sonarr"
        assert categories[0].save_path.endswith("first")
    values["download"]["categories"] = {"tv-sonarr": tmp_path / "changed"}
    configured = Settings.model_validate(values)
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        categories = await app.state.job_service.categories()
        assert categories[0].save_path.endswith("changed")


async def test_category_add_is_persisted_before_provider(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        assert (await client.post("/api/v2/torrents/createCategory",
                                  data={"category": "sonarr", "savePath": "/downloads/tv"})).status_code == 200
        response = await client.post("/api/v2/torrents/add",
                                     data={"urls": MAGNET, "category": "sonarr"})
        assert response.text == "Ok."
        assert provider.creates == []
        values = (await client.get("/api/v2/torrents/info?category=sonarr")).json()
        assert len(values) == 1
        assert values[0]["hash"] == HASH and values[0]["state"] == "metaDL"
        assert values[0]["progress"] == 0


async def test_duplicate_submission_is_idempotent(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        for _ in range(2):
            assert (await client.post("/api/v2/torrents/add", data={"urls": MAGNET})).status_code == 200
        assert len((await client.get("/api/v2/torrents/info")).json()) == 1


async def test_binary_torrent_upload_is_persisted_and_submitted(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        response = await client.post("/api/v2/torrents/add", files={
            "torrents": ("release.torrent", TORRENT, "application/x-bittorrent"),
        })
        assert response.status_code == 200 and response.text == "Ok."
        job = await app.state.job_service.job(TORRENT_HASH)
        assert job.source_kind == "torrent"
        assert job.source_uri == "release.torrent"
        assert job.source_data == TORRENT
        assert provider.torrent_creates == []
        await app.state.job_service.process(job.id)
        assert provider.torrent_creates == [(TORRENT, "release.torrent", TORRENT_HASH)]


async def test_binary_torrent_submission_survives_restart(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", files={
            "torrents": ("release.torrent", TORRENT, "application/x-bittorrent"),
        })
    second = FakeProvider()
    async with client_for(tmp_path, second) as (client, app, provider):
        job = await app.state.job_service.job(TORRENT_HASH)
        await app.state.job_service.process(job.id)
        assert second.torrent_creates == [(TORRENT, "release.torrent", TORRENT_HASH)]


async def test_provider_transitions_never_report_local_completion(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        assert provider.creates == [MAGNET]
        await app.state.job_service.process(job.id)
        value = (await client.get("/api/v2/torrents/info")).json()[0]
        assert value["state"] == "downloading" and value["progress"] == 0.4
        provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "completed", 1000,
                                           1, 0, 0, True, True)
        await app.state.job_service.process(job.id)
        value = (await client.get("/api/v2/torrents/info")).json()[0]
        assert value["state"] == "queuedDL"
        assert (await app.state.job_service.job(HASH)).state == JobState.PROVIDER_READY.value


async def test_ready_torrent_is_delivered_before_reporting_completion(tmp_path):
    provider, downloader = FakeProvider(), FakeDownloader()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)  # submit
        await app.state.job_service.process(job.id)  # provider ready
        assert (await app.state.job_service.job(HASH)).state == JobState.PROVIDER_READY.value
        await app.state.job_service.process(job.id)  # discover files
        assert (await app.state.job_service.job(HASH)).state == JobState.DELIVERING.value
        await app.state.job_service.process(job.id)  # local delivery
        completed = await app.state.job_service.job(HASH)
        assert completed.state == JobState.COMPLETED.value
        assert completed.completed_at is not None
        assert completed.delivery_files[0].state == "completed"
        value = (await client.get("/api/v2/torrents/info")).json()[0]
        assert value["state"] == "pausedUP" and value["progress"] == 1
        assert value["ratio"] == 0 and value["ratio_limit"] == 0
        assert value["seeding_time"] == 0 and value["seeding_time_limit"] == 0
        assert value["content_path"].endswith("/Test.Release.mkv")
        files = (await client.get(f"/api/v2/torrents/files?hash={HASH}")).json()
        assert files == [{"index": 0, "name": "Test.Release.mkv", "size": 5,
                          "progress": 1.0, "priority": 1, "is_seed": True,
                          "availability": 1.0}]
        assert downloader.destinations[0].read_bytes() == b"hello"
        properties = (await client.get(f"/api/v2/torrents/properties?hash={HASH}")).json()
        assert properties["completion_date"] > 0


async def test_performance_history_survives_arr_cleanup(tmp_path):
    class TelemetryDownloader(FakeDownloader):
        async def download(self, url_provider, destination, progress_callback=None):
            await url_provider(False)
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.payload)
            self.destinations.append(destination)
            return DownloadResult(
                destination, len(self.payload), 2.0, 200, False, True,
                session_byte_count=400, cdn_host="cdn.example.test",
                range_requests=8, retry_count=1, peak_speed=350, connections=4,
            )

    provider, downloader = FakeProvider(), TelemetryDownloader()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(4):
            await app.state.job_service.process(job.id)

        values = (await client.get("/ui/api/performance?range=7d")).json()
        assert values["summary"] == {
            "downloads": 1, "files": 1, "bytes": 5,
            "average_speed": 200, "peak_speed": 350, "retries": 1,
            "median_speed": 200, "p95_speed": 200, "sample_files": 1,
            "range_transfers": 1, "resumed": 0,
            "failures": 0, "failure_events": 0, "unresolved_failures": 0,
            "affected_downloads": 0,
        }
        assert values["timeline"][0]["average_speed"] == 200
        assert values["recent"][0]["connections"] == 4
        assert values["recent"][0]["cdn_host"] == "cdn.example.test"
        assert (await client.get("/ui/api/performance?range=invalid")).status_code == 400

        await client.post("/api/v2/torrents/delete",
                          data={"hashes": HASH, "deleteFiles": "true"})
        assert await app.state.job_service.job(HASH) is None
        retained = (await client.get("/ui/api/performance?range=all")).json()
        assert retained["summary"]["downloads"] == 1
        assert retained["recent"][0]["info_hash"] == HASH


async def test_performance_is_attributed_and_filterable_by_arr_source(tmp_path):
    provider, resolver = FakeProvider(), FakeSourceResolver()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, source_resolver=resolver) as (client, app, _):
        await login(client)
        await client.post("/api/v2/torrents/createCategory",
                          data={"category": "radarr", "savePath": str(tmp_path / "movies")})
        await client.post("/api/v2/torrents/add",
                          data={"urls": MAGNET, "category": "radarr"})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(4):
            await app.state.job_service.process(job.id)

        data = (await client.get(
            "/ui/api/performance?range=7d&service=radarr&indexer=The%20Pirate%20Bay%20%28Prowlarr%29"
        )).json()
        assert data["summary"]["downloads"] == 1
        assert data["recent"][0]["service"] == "radarr"
        assert data["recent"][0]["indexer"] == "The Pirate Bay (Prowlarr)"
        assert data["segments"]["services"][0]["name"] == "radarr"
    assert resolver.closed


async def test_delete_endpoint_optionally_removes_delivered_files(tmp_path):
    provider, downloader = FakeProvider(), FakeDownloader()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(4):
            await app.state.job_service.process(job.id)
        delivered = downloader.destinations[0]
        response = await client.post("/api/v2/torrents/delete",
                                     data={"hashes": HASH, "deleteFiles": "true"})
        assert response.status_code == 200
        assert not delivered.exists()
        assert provider.deleted_torrents == [42]
        assert await app.state.job_service.job(HASH) is None


async def test_delete_cancels_and_awaits_active_delivery(tmp_path):
    class BlockingDownloader:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def download(self, url_provider, destination, progress_callback=None):
            await url_provider(False)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    provider, downloader = FakeProvider(), BlockingDownloader()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(3):
            await app.state.job_service.process(job.id)
        processing = asyncio.create_task(app.state.job_service.process(job.id))
        await downloader.started.wait()
        response = await client.post("/api/v2/torrents/delete",
                                     data={"hashes": HASH, "deleteFiles": "true"})
        assert response.status_code == 200
        assert downloader.cancelled.is_set()
        assert processing.done()
        assert provider.deleted_torrents == [42]
        assert await app.state.job_service.job(HASH) is None


async def test_qb_pause_resume_aliases_and_provider_scope(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)

        response = await client.post("/api/v2/torrents/stop", data={"hashes": HASH.upper()})
        assert response.status_code == 200
        paused = await app.state.job_service.job(HASH)
        assert paused.control_state == ControlState.PAUSED.value
        assert paused.control_scope == "local_and_provider"
        assert provider.paused_torrents == [42]
        info = (await client.get("/api/v2/torrents/info", params={"hashes": HASH})).json()[0]
        assert info["state"] == "pausedDL" and info["dlspeed"] == 0
        assert await app.state.job_service.due_job_ids() == []

        assert (await client.post("/api/v2/torrents/start",
                                  data={"hashes": f"unknown|{HASH}"})).status_code == 200
        assert (await app.state.job_service.job(HASH)).control_state == ControlState.RUNNING.value
        assert provider.resumed_torrents == [42]
        assert (await client.post("/api/v2/torrents/resume",
                                  data={"hashes": "all"})).status_code == 200
        assert provider.resumed_torrents == [42]
        snapshot = (await client.get("/api/v2/sync/maindata")).json()
        assert snapshot["full_update"] is True and HASH in snapshot["torrents"]


async def test_pause_active_delivery_preserves_partial_and_resumes(tmp_path):
    class PausableDownloader:
        def __init__(self):
            self.started = asyncio.Event()
            self.allow = asyncio.Event()
            self.calls = 0

        async def download(self, url_provider, destination, progress_callback=None):
            self.calls += 1
            await url_provider(False)
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            part = destination.with_name(destination.name + ".downloadarr.part")
            manifest = destination.with_name(destination.name + ".downloadarr.json")
            part.write_bytes(b"he")
            manifest.write_text('{"checkpoint":2}', encoding="utf-8")
            self.started.set()
            await self.allow.wait()
            destination.write_bytes(b"hello")
            return DownloadResult(destination, 5, 1, 5, self.calls > 1, False,
                                  session_byte_count=3 if self.calls > 1 else 5)

    provider, downloader = FakeProvider(), PausableDownloader()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(3):
            await app.state.job_service.process(job.id)
        processing = asyncio.create_task(app.state.job_service.process(job.id))
        await downloader.started.wait()
        assert (await client.post("/api/v2/torrents/pause",
                                  data={"hashes": HASH})).status_code == 200
        assert processing.done()
        destination = tmp_path / "downloads" / "Test.Release.mkv"
        assert destination.with_name(destination.name + ".downloadarr.part").read_bytes() == b"he"
        assert destination.with_name(destination.name + ".downloadarr.json").exists()
        assert await app.state.job_service.due_job_ids() == []
        downloader.allow.set()
        await client.post("/api/v2/torrents/resume", data={"hashes": HASH})
        await app.state.job_service.process(job.id)
        assert destination.read_bytes() == b"hello"
        assert (await app.state.job_service.job(HASH)).state == JobState.COMPLETED.value
        assert provider.creates == [MAGNET]


async def test_paused_intent_survives_restart(tmp_path):
    configured = settings(tmp_path)
    first = create_app(configured, FakeProvider(), start_poller=False,
                       downloader=FakeDownloader())
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            job = (await first.state.job_service.jobs())[0]
            await first.state.job_service.process(job.id)
            await first.state.job_service.pause([HASH])
    second = create_app(configured, FakeProvider(), start_poller=False,
                        downloader=FakeDownloader())
    async with second.router.lifespan_context(second):
        paused = await second.state.job_service.job(HASH)
        assert paused.control_state == ControlState.PAUSED.value
        assert await second.state.job_service.due_job_ids() == []


async def test_removing_intent_retries_cleanup_after_provider_failure(tmp_path):
    provider = FakeProvider()
    failures = 0

    async def flaky_delete(remote_id):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise ProviderError("UPSTREAM_ERROR", "temporary cleanup failure", transient=True)
        provider.deleted_torrents.append(remote_id)

    provider.delete_torrent = flaky_delete
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        response = await client.post("/api/v2/torrents/delete",
                                     data={"hashes": HASH, "deleteFiles": "false"})
        assert response.status_code == 200
        removing = await app.state.job_service.job(HASH)
        assert removing.control_state == ControlState.REMOVING.value
        assert removing.cleanup_failures == 1
        assert await app.state.job_service.due_job_ids() == []
        events = await app.state.job_service.lifecycle_history()
        assert any(item.event_type == "cleanup_failed" for item in events)
        await app.state.job_service.evaluate_alerts()
        assert (await app.state.job_service.alerts())[0].rule == "cleanup_stuck"
        await app.state.job_service.process(job.id)
        assert await app.state.job_service.job(HASH) is None
        assert provider.deleted_torrents == [42]


async def test_delete_removes_queued_provider_submission(tmp_path):
    provider = FakeProvider()

    async def queued_create(magnet):
        return ProviderSubmission(queued_id=7)

    provider.create_magnet = queued_create
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        response = await client.post("/api/v2/torrents/delete",
                                     data={"hashes": HASH, "deleteFiles": "false"})
        assert response.status_code == 200
        assert provider.deleted_queued == [7]
        assert await app.state.job_service.job(HASH) is None


async def test_unsafe_provider_file_path_fails_without_writing(tmp_path):
    provider = FakeProvider()
    provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                       1, 0, 0, True, True)
    provider.files = [ProviderFile(0, "../outside.mkv", 5)]
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(3):
            await app.state.job_service.process(job.id)
        failed = await app.state.job_service.job(HASH)
        assert failed.state == JobState.FAILED.value
        assert failed.error_code == "DELIVERY_FAILED"
        assert not (tmp_path / "outside.mkv").exists()


async def test_restart_resumes_persisted_delivery(tmp_path):
    first = FakeProvider()
    first.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                    1, 0, 0, True, True)
    async with client_for(tmp_path, first) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        for _ in range(3):
            await app.state.job_service.process(job.id)
        assert (await app.state.job_service.job(HASH)).state == JobState.DELIVERING.value
    second, downloader = FakeProvider(), FakeDownloader()
    async with client_for(tmp_path, second, downloader) as (client, app, provider):
        job = await app.state.job_service.job(HASH)
        assert len(job.delivery_files) == 1
        await app.state.job_service.process(job.id)
        assert (await app.state.job_service.job(HASH)).state == JobState.COMPLETED.value
        assert downloader.destinations[0].is_file()


async def test_properties_and_filters(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        assert (await client.get(f"/api/v2/torrents/properties?hash={HASH}" )).status_code == 200
        assert (await client.get(f"/api/v2/torrents/properties?hash={'f' * 40}" )).status_code == 404
        assert len((await client.get(f"/api/v2/torrents/info?hashes={HASH}")).json()) == 1
        assert (await client.get("/api/v2/torrents/info?category=radarr")).json() == []


async def test_arr_post_add_controls_are_accepted_without_moving_payload(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/createCategory",
                          data={"category": "sonarr", "savePath": "/torbox/tv-sonarr"})
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET, "category": "sonarr"})
        controls = [
            ("setCategory", {"hashes": HASH, "category": "sonarr-imported"}),
            ("topPrio", {"hashes": HASH}),
            ("setForceStart", {"hashes": HASH, "value": "true"}),
            ("setShareLimits", {"hashes": HASH, "ratioLimit": "0", "seedingTimeLimit": "0"}),
        ]
        for endpoint, data in controls:
            response = await client.post(f"/api/v2/torrents/{endpoint}", data=data)
            assert response.status_code == 200
        job = await app.state.job_service.job(HASH)
        assert job.category.name == "sonarr"


async def test_arr_post_add_controls_require_hashes(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        for endpoint in ("setCategory", "topPrio", "setForceStart", "setShareLimits"):
            response = await client.post(f"/api/v2/torrents/{endpoint}", data={})
            assert response.status_code == 400


async def test_restart_recovers_submitted_job(tmp_path):
    first = FakeProvider()
    async with client_for(tmp_path, first) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
    second = FakeProvider()
    async with client_for(tmp_path, second) as (client, app, provider):
        await login(client)
        jobs = await app.state.job_service.jobs()
        assert len(jobs) == 1 and jobs[0].state == JobState.SUBMITTED.value
        await app.state.job_service.process(jobs[0].id)
        assert second.creates == [MAGNET]


async def test_schema_migration_is_idempotent(tmp_path):
    async with client_for(tmp_path):
        pass
    async with client_for(tmp_path):
        pass


async def test_bearer_authentication(tmp_path):
    values = settings(tmp_path).model_dump()
    values["qbittorrent"]["api_key"] = "bearer-secret"
    configured = Settings.model_validate(values)
    app = create_app(configured, FakeProvider(), start_poller=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test",
                                     headers={"Authorization": "Bearer bearer-secret"}) as client:
            assert (await client.get("/api/v2/app/preferences")).status_code == 200


async def test_queued_submission_reconciles_by_hash(tmp_path):
    provider = FakeProvider()
    async def queued_create(magnet):
        provider.creates.append(magnet)
        return ProviderSubmission(queued_id=7)
    provider.create_magnet = queued_create
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        provider.queued = [ProviderQueuedTorrent(7, HASH, 42)]
        await app.state.job_service.process(job.id)
        recovered = await app.state.job_service.job(HASH)
        assert recovered.state == JobState.PROVIDER_DOWNLOADING.value
        assert recovered.provider_job.remote_id == 42


async def test_disappeared_queue_entry_reconciles_with_torrent_list(tmp_path):
    provider = FakeProvider()
    async def queued_create(magnet):
        return ProviderSubmission(queued_id=7)
    provider.create_magnet = queued_create
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        await app.state.job_service.process(job.id)
        recovered = await app.state.job_service.job(HASH)
        assert recovered.state == JobState.PROVIDER_DOWNLOADING.value
        assert recovered.provider_job.remote_id == 42


async def test_stale_queue_entry_prefers_ready_torrent_with_same_hash(tmp_path):
    provider = FakeProvider()
    async def queued_create(magnet):
        return ProviderSubmission(queued_id=7)
    provider.create_magnet = queued_create
    provider.queued = [ProviderQueuedTorrent(7, HASH, None)]
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        await app.state.job_service.process(job.id)
        recovered = await app.state.job_service.job(HASH)
        assert recovered.state == JobState.PROVIDER_DOWNLOADING.value
        assert recovered.provider_job.remote_id == 42


async def test_transient_and_terminal_provider_failures(tmp_path):
    provider = FakeProvider()
    async def transient(magnet):
        raise ProviderError("RATE_LIMITED", "temporarily unavailable", transient=True)
    provider.create_magnet = transient
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        recovered = await app.state.job_service.job(HASH)
        assert recovered.state == JobState.RETRY_WAIT.value
        async def terminal(magnet):
            raise ProviderError("AUTHENTICATION_FAILED", "authentication failed", transient=False)
        provider.create_magnet = terminal
        await app.state.job_service.process(job.id)
        assert (await app.state.job_service.job(HASH)).state == JobState.FAILED.value
        data = (await client.get("/ui/api/performance?range=7d")).json()
        assert data["summary"]["failures"] == 2
        assert data["summary"]["unresolved_failures"] == 2
        assert data["recent_failures"][0]["code"] == "AUTHENTICATION_FAILED"
        assert data["recent_failures"][0]["stage"] == "submission"
        await client.post("/api/v2/torrents/delete",
                          data={"hashes": HASH, "deleteFiles": "false"})
        retained = (await client.get("/ui/api/performance?range=all")).json()
        assert retained["summary"]["failures"] == 2


async def test_transient_failure_is_marked_recovered_after_success(tmp_path):
    provider = FakeProvider()
    attempts = 0

    async def fail_once(magnet):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError("RATE_LIMITED", "temporarily unavailable", transient=True)
        return ProviderSubmission(remote_id=42)

    provider.create_magnet = fail_once
    async with client_for(tmp_path, provider) as (client, app, _):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        assert (await app.state.job_service.failure_history())[0].resolved_at is None
        await app.state.job_service.process(job.id)
        failure = (await app.state.job_service.failure_history())[0]
        assert failure.resolved_at is not None


async def test_lifecycle_transitions_are_ordered_deduplicated_and_survive_cleanup(tmp_path):
    provider, downloader = FakeProvider(), FakeDownloader()
    async with client_for(tmp_path, provider, downloader) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        await app.state.job_service.process(job.id)
        before = await app.state.job_service.lifecycle_history()
        transitions = [item for item in before if item.event_type == "phase_transition"]
        assert [(item.from_phase, item.to_phase) for item in transitions] == [
            (JobState.SUBMITTED.value, JobState.PROVIDER_DOWNLOADING.value)]

        provider.torrent = ProviderTorrent(42, HASH, "Test.Release", "cached", 5,
                                           1, 0, 0, True, True)
        for _ in range(3):
            await app.state.job_service.process(job.id)
        completed = await app.state.job_service.lifecycle_history()
        assert [item.sequence for item in completed] == sorted(item.sequence for item in completed)
        assert all(item.duration_seconds is None or item.duration_seconds >= 0
                   for item in completed)
        assert any(item.to_phase == JobState.COMPLETED.value
                   and item.outcome == "awaiting_client_cleanup" for item in completed)

        await client.post("/api/v2/torrents/delete",
                          data={"hashes": HASH, "deleteFiles": "false"})
        assert await app.state.job_service.job(HASH) is None
        retained = await app.state.job_service.lifecycle_history()
        kinds = [item.event_type for item in retained]
        assert "cleanup_requested" in kinds
        assert "remote_cleanup_succeeded" in kinds
        assert kinds[-1] == "job_removed"


async def test_monitor_alert_dedup_acknowledge_and_resolve(tmp_path):
    provider = FakeProvider()

    async def terminal(magnet):
        raise ProviderError("AUTHENTICATION_FAILED", "authentication failed", transient=False)

    provider.create_magnet = terminal
    async with client_for(tmp_path, provider) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        job = (await app.state.job_service.jobs())[0]
        await app.state.job_service.process(job.id)
        await app.state.job_service.evaluate_alerts()
        await app.state.job_service.evaluate_alerts()
        alerts = await app.state.job_service.alerts()
        assert len(alerts) == 1 and alerts[0].rule == "terminal_failure"
        assert alerts[0].occurrences == 2
        assert await app.state.job_service.acknowledge_alert(alerts[0].id)
        assert (await app.state.job_service.alerts())[0].status == "acknowledged"

        provider.create_magnet = FakeProvider().create_magnet
        await app.state.job_service.retry([HASH])
        await app.state.job_service.process(job.id)
        await app.state.job_service.evaluate_alerts()
        assert (await app.state.job_service.alerts())[0].status == "resolved"
        report = (await client.get("/ui/api/monitoring?range=30d")).json()
        assert report["schema_version"] == 1
        assert report["monitor"]["stale"] is False
        assert report["semantics"]["cleanup"].endswith("unverified")


async def test_telemetry_exports_are_bounded_redacted_and_formula_safe(tmp_path):
    formula_hash = "1123456789abcdef0123456789abcdef01234567"
    formula_magnet = f"magnet:?xt=urn:btih:{formula_hash}&dn=%3DCMD"
    async with client_for(tmp_path) as (client, app, provider):
        assert (await client.get("/ui/api/export")).status_code == 403
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": formula_magnet})
        redacted = await client.get(
            "/ui/api/export?dataset=lifecycle&format=json&range=all")
        assert redacted.status_code == 200
        assert redacted.headers["cache-control"] == "no-store"
        assert redacted.headers["x-content-type-options"] == "nosniff"
        payload = redacted.json()
        assert payload["schema_version"] == 1
        assert "info_hash" not in payload["rows"][0]
        assert formula_hash not in redacted.text

        exported = await client.get(
            "/ui/api/export?dataset=lifecycle&format=csv&range=all&include_identifiers=true")
        assert exported.status_code == 200
        assert "'=CMD" in exported.text
        assert "token=" not in exported.text.lower()


def test_settings_redact_secrets(tmp_path):
    values = settings(tmp_path).model_dump()
    values["torbox"]["api_token"] = "top-secret"
    values["qbittorrent"]["api_key"] = "api-secret"
    values["integrations"]["sonarr"]["api_key"] = "sonarr-secret"
    configured = Settings.model_validate(values)
    rendered = repr(configured)
    assert all(secret not in rendered for secret in
               ("top-secret", "api-secret", "sonarr-secret"))
    masked = configured.masked()
    assert masked["torbox"]["api_token"] == "********"
    assert masked["qbittorrent"]["password"] == "********"
    assert masked["integrations"]["sonarr"]["api_key"] == "********"


def test_json_settings_with_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"username":"from-file","password":"file-password-123",'
                    '"torbox_api_token":"file-secret",'
                    '"provider_concurrency":2}', encoding="utf-8")
    monkeypatch.setenv("DOWNLOADARR_USERNAME", "from-environment")
    monkeypatch.setenv("DOWNLOADARR_RADARR_API_KEY", "radarr-environment-secret")
    configured = load_settings(path)
    assert configured.username == "from-environment"
    assert configured.provider_concurrency == 2
    assert configured.torbox_api_token.get_secret_value() == "file-secret"
    assert configured.integrations.radarr.api_key.get_secret_value() == "radarr-environment-secret"


def test_invalid_json_settings_are_rejected(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Downloadarr settings file"):
        load_settings(path)


@pytest.mark.parametrize("password", [
    "downloadarr",
    "replace-with-a-strong-password",
    "short",
])
def test_insecure_qbittorrent_passwords_are_rejected(tmp_path, monkeypatch, password):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"qbittorrent": {"password": password}}), encoding="utf-8")
    monkeypatch.delenv("DOWNLOADARR_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="qBittorrent password"):
        load_settings(path)


def test_qbittorrent_password_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("DOWNLOADARR_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="password"):
        load_settings(tmp_path / "missing-settings.json")


async def test_settings_service_saves_atomically_and_creates_backup(tmp_path):
    path = tmp_path / "config" / "settings.json"
    service = SettingsService(path)
    original = settings(tmp_path)
    await service.save(original)
    assert load_settings(path).username == "user"
    assert load_settings(path).password.get_secret_value() == "test-password-123!"
    assert load_settings(path).torbox_api_token.get_secret_value() == "test"
    values = original.model_dump()
    values["qbittorrent"]["username"] = "changed"
    await service.save(Settings.model_validate(values))
    assert load_settings(path).username == "changed"
    assert len(list(path.parent.glob("settings.*.bak.json"))) == 1
    assert not list(path.parent.glob("*.tmp"))


async def test_settings_service_preserves_container_paths(tmp_path):
    path = tmp_path / "settings.json"
    values = settings(tmp_path).model_dump()
    values["download"]["path"] = "/torbox"
    values["download"]["categories"] = {"radarr": "/torbox/radarr"}
    await SettingsService(path).save(Settings.model_validate(values))
    raw = path.read_text(encoding="utf-8")
    assert '"path": "/torbox"' in raw
    assert '"radarr": "/torbox/radarr"' in raw
    restored = load_settings(path)
    assert restored.download.path == "/torbox"


def test_settings_service_reports_environment_managed_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TORBOX_API_TOKEN", "managed")
    monkeypatch.setenv("DOWNLOADARR_DOWNLOAD_PATH", "/managed")
    managed = SettingsService(tmp_path / "settings.json").managed_fields()
    assert managed == ["download.path", "torbox.api_token"]
