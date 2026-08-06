from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from downloadarr.api import create_app
from downloadarr.db.models import JobState
from downloadarr.providers.base import (ProviderQueuedTorrent, ProviderSubmission,
                                        ProviderError, ProviderTorrent)
from downloadarr.settings import Settings, load_settings

HASH = "0123456789abcdef0123456789abcdef01234567"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Test.Release"


class FakeProvider:
    def __init__(self):
        self.creates = []
        self.torrent = ProviderTorrent(42, HASH, "Test.Release", "downloading", 1000,
                                       0.4, 100, 6, False, True)
        self.queued = []
        self.closed = False

    async def create_magnet(self, magnet):
        self.creates.append(magnet)
        return ProviderSubmission(remote_id=42)

    async def get_torrent(self, remote_id):
        assert remote_id == 42
        return self.torrent

    async def get_queued(self):
        return self.queued

    async def close(self):
        self.closed = True


def settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}",
                    download_path=tmp_path / "downloads", username="user", password="pass",
                    torbox_api_token="test", poll_interval=0.01,
                    queued_poll_interval=0.01)


@asynccontextmanager
async def client_for(tmp_path, provider=None):
    fake = provider or FakeProvider()
    app = create_app(settings(tmp_path), fake, start_poller=False)
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


async def test_properties_and_filters(tmp_path):
    async with client_for(tmp_path) as (client, app, provider):
        await login(client)
        await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        assert (await client.get(f"/api/v2/torrents/properties?hash={HASH}" )).status_code == 200
        assert (await client.get(f"/api/v2/torrents/properties?hash={'f' * 40}" )).status_code == 404
        assert len((await client.get(f"/api/v2/torrents/info?hashes={HASH}")).json()) == 1
        assert (await client.get("/api/v2/torrents/info?category=radarr")).json() == []


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
    configured = settings(tmp_path).model_copy(update={"api_key": SecretStr("bearer-secret")})
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
    configured = settings(tmp_path).model_copy(update={"torbox_api_token": SecretStr("top-secret"),
                                                        "api_key": SecretStr("api-secret")})
    rendered = repr(configured)
    assert "top-secret" not in rendered and "api-secret" not in rendered


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
