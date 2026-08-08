from contextlib import asynccontextmanager
import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from downloadarr.api import create_app
from downloadarr.db.models import JobState
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


def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate({
        "database": {"url": f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}"},
        "download": {"path": tmp_path / "downloads"},
        "qbittorrent": {"username": "user", "password": "pass"},
        "torbox": {"api_token": "test"},
        "scheduler": {"poll_interval": 0.01, "queued_poll_interval": 0.01},
    })


@asynccontextmanager
async def client_for(tmp_path, provider=None, downloader=None):
    fake = provider or FakeProvider()
    app = create_app(settings(tmp_path), fake, start_poller=False,
                     downloader=downloader or FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            yield client, app, fake


async def login(client):
    response = await client.post("/api/v2/auth/login", data={"username": "user", "password": "pass"})
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
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
            page = await client.get("/")
            assert page.status_code == 200 and "Downloadarr" in page.text
            assert "unique-dashboard-secret" not in page.text
            jobs = (await client.get("/ui/api/jobs")).json()
            assert jobs[0]["phase"] == "Submitting"
            assert jobs[0]["hash"] == HASH


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
            valid = await client.post("/ui/login", data={"username": "user", "password": "pass"},
                                      follow_redirects=False)
            assert valid.status_code == 303 and "SID=" in valid.headers["set-cookie"]
            response = await client.post("/ui/settings", data={
                "torbox_token": "replacement-secret",
                "transfer_mode": "parallel",
                "connections": "12",
                "provider_max_connections": "4",
                "download_path": "/torbox",
                "categories": '{"tv-sonarr":"/torbox/tv-sonarr"}',
            }, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/?saved=1"
            restored = load_settings(service.path)
            assert restored.torbox_api_token.get_secret_value() == "replacement-secret"
            assert restored.download.connections == 12
            assert restored.download.transfer_mode == "parallel"


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


def test_settings_redact_secrets(tmp_path):
    values = settings(tmp_path).model_dump()
    values["torbox"]["api_token"] = "top-secret"
    values["qbittorrent"]["api_key"] = "api-secret"
    configured = Settings.model_validate(values)
    rendered = repr(configured)
    assert "top-secret" not in rendered and "api-secret" not in rendered
    masked = configured.masked()
    assert masked["torbox"]["api_token"] == "********"
    assert masked["qbittorrent"]["password"] == "********"


def test_json_settings_with_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"username":"from-file","torbox_api_token":"file-secret",'
                    '"provider_concurrency":2}', encoding="utf-8")
    monkeypatch.setenv("DOWNLOADARR_USERNAME", "from-environment")
    configured = load_settings(path)
    assert configured.username == "from-environment"
    assert configured.provider_concurrency == 2
    assert configured.torbox_api_token.get_secret_value() == "file-secret"


def test_invalid_json_settings_are_rejected(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Downloadarr settings file"):
        load_settings(path)


async def test_settings_service_saves_atomically_and_creates_backup(tmp_path):
    path = tmp_path / "config" / "settings.json"
    service = SettingsService(path)
    original = settings(tmp_path)
    await service.save(original)
    assert load_settings(path).username == "user"
    assert load_settings(path).password.get_secret_value() == "pass"
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
