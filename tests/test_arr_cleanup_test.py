from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import ClientSession, web

from downloadarr.arr_cleanup_test import (ArrApi, ImportEvidence, VerificationError,
                                           build_parser, verify_arr_cleanup)


HASH = "1" * 40
NOW = datetime.now(timezone.utc)


class FakeArr:
    def __init__(self, imports, library_file=None):
        self.imports = list(imports)
        self.file = library_file or {"id": 7, "size": 5}

    async def imported(self, info_hash, since):
        assert info_hash == HASH and since.tzinfo is not None
        return self.imports.pop(0) if len(self.imports) > 1 else self.imports[0]

    async def library_file(self, file_id):
        assert file_id == 7
        return self.file


class FakeDownloadarr:
    def __init__(self, jobs):
        self.jobs = list(jobs)

    async def job(self, info_hash):
        assert info_hash == HASH
        return self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]


def evidence(imported="/series/show/episode.mkv", dropped="/torbox/tv/episode.mkv"):
    return ImportEvidence(7, imported, dropped, 5, NOW)


async def test_verifier_waits_for_import_and_downloadarr_removal():
    arr = FakeArr([None, evidence()])
    downloadarr = FakeDownloadarr([{"state": "pausedUP"}, None])

    result = await verify_arr_cleanup(arr, downloadarr, HASH, timeout=1,
                                      poll_interval=0)

    assert result.info_hash == HASH
    assert result.imported_path == "/series/show/episode.mkv"


async def test_verifier_rejects_removal_before_import():
    arr = FakeArr([None])
    downloadarr = FakeDownloadarr([{"state": "pausedUP"}, None])

    with pytest.raises(VerificationError, match="before Arr recorded an import"):
        await verify_arr_cleanup(arr, downloadarr, HASH, timeout=1, poll_interval=0)


async def test_verifier_checks_mapped_library_and_staging_paths(tmp_path):
    library = tmp_path / "library"
    staging = tmp_path / "staging"
    imported = library / "show" / "episode.mkv"
    imported.parent.mkdir(parents=True)
    imported.write_bytes(b"12345")
    arr = FakeArr([evidence()])
    downloadarr = FakeDownloadarr([{"state": "pausedUP"}, None])

    result = await verify_arr_cleanup(arr, downloadarr, HASH, timeout=1,
                                      poll_interval=0, path_maps=[
                                          ("/series", library), ("/torbox/tv", staging),
                                      ])

    assert result.imported_path == "/series/show/episode.mkv"


async def test_verifier_rejects_missing_target_job():
    with pytest.raises(VerificationError, match="was not present"):
        await verify_arr_cleanup(FakeArr([None]), FakeDownloadarr([None]), HASH,
                                 timeout=1, poll_interval=0)


def test_cli_rejects_invalid_hash_before_contacting_services():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--arr", "sonarr", "--hash", "invalid"])


async def test_arr_history_query_uses_uppercase_qbittorrent_id():
    async def history(request):
        assert request.query["downloadId"] == HASH.upper()
        return web.json_response({"records": [{
            "eventType": "downloadFolderImported",
            "date": NOW.isoformat(),
            "data": {"fileId": 7, "importedPath": "/series/show/episode.mkv",
                     "droppedPath": "/torbox/tv/episode.mkv", "size": 5},
        }]})

    app = web.Application()
    app.router.add_get("/api/v3/history", history)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            item = await ArrApi(session, f"http://127.0.0.1:{port}", "key", "sonarr").imported(
                HASH.lower(), NOW - timedelta(seconds=1))
        assert item is not None and item.file_id == 7
    finally:
        await runner.cleanup()
