import pytest
from aiohttp import web

from downloadarr.providers import ProviderError, TorBoxProvider

HASH = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
async def torbox_server():
    runners = []
    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/v1/api/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1/api"
    yield start
    for runner in runners:
        await runner.cleanup()


async def test_create_and_poll_torbox(torbox_server):
    seen = []
    async def handler(request):
        seen.append((request.method, request.path, request.headers.get("Authorization")))
        if request.path.endswith("createtorrent"):
            form = await request.post()
            assert form["allow_zip"] == "false" and form["as_queued"] == "true"
            return web.json_response({"success": True, "data": {"torrent_id": 42}})
        return web.json_response({"success": True, "data": {"id": 42, "hash": HASH,
            "name": "Release", "size": 100, "progress": 50, "download_speed": 10,
            "eta": 4, "download_state": "downloading", "download_finished": False,
            "download_present": True}})
    base = await torbox_server(handler)
    provider = TorBoxProvider("secret", base)
    try:
        assert (await provider.create_magnet(f"magnet:?xt=urn:btih:{HASH}")).remote_id == 42
        torrent = await provider.get_torrent(42)
        assert torrent.progress == 0.5 and torrent.info_hash == HASH
        assert (await provider.find_torrent(HASH)).remote_id == 42
        assert all(row[2] == "Bearer secret" for row in seen)
    finally:
        await provider.close()


async def test_create_binary_torrent(torbox_server):
    payload = b"d4:infod6:pieces20:abcdefghijklmnopqrstee"

    async def handler(request):
        assert request.path.endswith("/createtorrent")
        form = await request.post()
        upload = form["file"]
        assert upload.filename == "release.torrent"
        assert upload.file.read() == payload
        assert form["allow_zip"] == "false"
        return web.json_response({"success": True, "data": {"torrent_id": 42}})

    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        result = await provider.create_torrent(payload, "release.torrent", HASH)
        assert result.remote_id == 42
    finally:
        await provider.close()


async def test_current_queued_endpoint_and_parameters(torbox_server):
    async def handler(request):
        assert request.path == "/v1/api/queued/getqueued"
        assert request.query["type"] == "torrent"
        assert request.query["bypass_cache"] == "true"
        return web.json_response({"success": True, "data": {
            "id": 7, "hash": HASH, "torrent_id": 42,
        }})
    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        queued = await provider.get_queued()
        assert queued[0].queued_id == 7
        assert queued[0].remote_id == 42
    finally:
        await provider.close()


async def test_file_discovery_and_signed_download_request(torbox_server):
    async def handler(request):
        if request.path.endswith("/mylist"):
            assert request.query["id"] == "42"
            return web.json_response({"success": True, "data": {"id": 42, "files": [
                {"id": 3, "name": "Release/video.mkv", "short_name": "video.mkv", "size": 99}
            ]}})
        assert request.path.endswith("/requestdl")
        assert request.query["torrent_id"] == "42"
        assert request.query["file_id"] == "3"
        assert request.query["token"] == "secret"
        return web.json_response({"success": True,
                                  "data": "https://cdn.example.test/signed?private=value"})
    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        files = await provider.get_files(42)
        assert files[0].file_id == 3
        assert files[0].path == "Release/video.mkv"
        assert files[0].size == 99
        assert (await provider.request_download(42, 3)).startswith("https://cdn.example.test/")
    finally:
        await provider.close()


async def test_remote_torrent_and_queue_deletion(torbox_server):
    seen = []

    async def handler(request):
        seen.append((request.path, await request.json()))
        return web.json_response({"success": True, "data": {}})

    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        await provider.delete_torrent(42)
        await provider.delete_queued(7)
        assert seen == [
            ("/v1/api/torrents/controltorrent",
             {"torrent_id": 42, "operation": "delete", "all": False}),
            ("/v1/api/queued/controlqueued",
             {"queued_id": 7, "operation": "delete", "all": False}),
        ]
    finally:
        await provider.close()


async def test_rejected_duplicate_magnet_reconciles_existing_torrent(torbox_server):
    async def handler(request):
        if request.path.endswith("/createtorrent"):
            return web.json_response({"success": False, "error": "DIFF_ISSUE"}, status=400)
        assert request.path.endswith("/mylist")
        return web.json_response({"success": True, "data": [{
            "id": 42, "hash": HASH, "name": "Release", "size": 99,
            "progress": 1, "download_state": "cached", "download_finished": True,
            "download_present": True,
        }]})
    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        submission = await provider.create_magnet(f"magnet:?xt=urn:btih:{HASH}")
        assert submission.remote_id == 42 and submission.queued_id is None
    finally:
        await provider.close()


@pytest.mark.parametrize("status,transient", [(401, False), (403, False), (422, False),
                                                (429, True), (502, True)])
async def test_torbox_error_classification(torbox_server, status, transient):
    async def handler(request):
        return web.Response(status=status, headers={"Retry-After": "0"})
    provider = TorBoxProvider("secret", await torbox_server(handler))
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.get_queued()
        assert raised.value.transient is transient
        assert "secret" not in str(raised.value)
    finally:
        await provider.close()
