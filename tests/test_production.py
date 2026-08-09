import sqlite3
from contextlib import closing
import json
import multiprocessing

import httpx
import pytest

from downloadarr.api import create_app
from downloadarr.process_lock import ProcessLock, ProcessLockError
from downloadarr.maintenance import create_backup, doctor, restore_backup, verify_backup

from test_vertical_api import FakeDownloader, FakeProvider, settings
from test_vertical_api import MAGNET, login


def _hold_process_lock(url, ready, release):
    lock = ProcessLock.for_database(url)
    lock.acquire()
    ready.set()
    release.wait(10)
    lock.release()


def test_process_lock_rejects_second_owner_and_releases(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    first = ProcessLock.for_database(url)
    second = ProcessLock.for_database(url)
    first.acquire()
    assert first.owned
    with pytest.raises(ProcessLockError, match="already owns"):
        second.acquire()
    first.release()
    second.acquire()
    assert second.owned
    second.release()


def test_process_lock_rejects_independent_process(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_process_lock, args=(url, ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ProcessLockError, match="already owns"):
            ProcessLock.for_database(url).acquire()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


async def test_sqlite_pragmas_and_readiness(tmp_path):
    app = create_app(settings(tmp_path), FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        pragmas = await app.state.database.pragmas()
        assert pragmas["foreign_keys"] == 1
        assert pragmas["busy_timeout"] == 5000
        assert str(pragmas["journal_mode"]).lower() == "wal"
        assert pragmas["synchronous"] == 2
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/readyz")).json() == {"status": "ready"}


async def test_newer_database_schema_is_refused_before_startup(tmp_path):
    configured = settings(tmp_path)
    database_path = tmp_path / "jobs.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE downloadarr_schema_version "
                           "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO downloadarr_schema_version VALUES (999, 'now')")
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    with pytest.raises(RuntimeError, match="newer"):
        async with app.router.lifespan_context(app):
            pass
    lock = ProcessLock.for_database(configured.database_url)
    lock.acquire()
    lock.release()


async def test_online_backup_verify_and_offline_restore(tmp_path):
    configured = settings(tmp_path)
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps(configured.storage_dict()), encoding="utf-8")
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            await login(client)
            await client.post("/api/v2/torrents/add", data={"urls": MAGNET})
        bundle = create_backup(tmp_path / "backup", config_path)
    verified = verify_backup(bundle)
    assert verified["status"] == "ok" and verified["schema_version"] == 8
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as connection:
        connection.execute("DELETE FROM jobs")
        connection.commit()
    recovery = restore_backup(bundle, config_path, "RESTORE")
    assert recovery.is_dir()
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        assert connection.execute("SELECT COUNT(1) FROM jobs").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_tamper_and_restore_confirmation_are_rejected(tmp_path):
    configured = settings(tmp_path)
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps(configured.storage_dict()), encoding="utf-8")
    with sqlite3.connect(tmp_path / "jobs.db") as connection:
        connection.execute("CREATE TABLE downloadarr_schema_version "
                           "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO downloadarr_schema_version VALUES (8, 'now')")
    bundle = create_backup(tmp_path / "backup", config_path)
    with pytest.raises(ValueError, match="confirm"):
        restore_backup(bundle, config_path, "no")
    (bundle / "settings.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        verify_backup(bundle)


async def test_doctor_report_is_redacted_and_scoped(tmp_path):
    configured = settings(tmp_path)
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps(configured.storage_dict()), encoding="utf-8")
    app = create_app(configured, FakeProvider(), start_poller=False,
                     downloader=FakeDownloader())
    async with app.router.lifespan_context(app):
        pass
    report = doctor(config_path)
    serialized = json.dumps(report)
    assert report["status"] in {"pass", "warn"}
    assert configured.torbox.api_token.get_secret_value() not in serialized
    assert not list(tmp_path.rglob(".downloadarr-doctor-*"))
