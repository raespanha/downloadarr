import hashlib
import json
from pathlib import Path

import pytest
from aiohttp import web

from downloadarr import DownloadConfig, Downloader, ProtocolError, RetryExhausted
from downloadarr.manifest import Manifest

DATA = bytes(range(251)) * 401


@pytest.fixture
async def server():
    runners = []
    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/file", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/file"
    yield start
    for runner in runners:
        await runner.cleanup()


def range_handler(data=DATA):
    async def handler(request):
        header = request.headers.get("Range")
        if header:
            start, end = map(int, header.removeprefix("bytes=").split("-"))
            body = data[start:end + 1]
            return web.Response(status=206, body=body,
                                headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"})
        return web.Response(body=data)
    return handler


async def provider(url):
    async def get(refresh):
        return url
    return get


def test_parallel_ranges_are_split_into_dynamic_segments():
    downloader = Downloader(DownloadConfig(
        connections=4, transfer_mode="parallel", segments_per_connection=8))
    chunks = downloader._chunks(3200, True)
    assert len(chunks) == 32
    assert chunks[0].start == 0
    assert chunks[-1].end == 3199
    assert sum(chunk.length for chunk in chunks) == 3200


async def test_fresh_auto_mode_uses_full_get_despite_connection_ceiling(server, tmp_path):
    transfer_ranges = []

    async def handler(request):
        header = request.headers.get("Range")
        if header:
            start, end = map(int, header.removeprefix("bytes=").split("-"))
            if (start, end) != (0, 0):
                transfer_ranges.append(header)
            return web.Response(status=206, body=DATA[start:end + 1],
                                headers={"Content-Range": f"bytes {start}-{end}/{len(DATA)}"})
        transfer_ranges.append(None)
        return web.Response(body=DATA)

    url = await server(handler)
    result = await Downloader(DownloadConfig(connections=4, transfer_mode="auto")).download(
        await provider(url), tmp_path / "result.bin")
    assert transfer_ranges == [None]
    assert not result.used_ranges
    assert result.range_requests == 0


@pytest.mark.parametrize("connections", [1, 4, 8, 16])
async def test_parallel_exact_hash(server, tmp_path, connections):
    url = await server(range_handler())
    config = DownloadConfig(connections=connections, transfer_mode="parallel")
    result = await Downloader(config).download(
        await provider(url), tmp_path / "result.bin")
    assert hashlib.sha256(result.path.read_bytes()).digest() == hashlib.sha256(DATA).digest()
    assert result.used_ranges
    assert result.range_requests == len(Downloader(config)._chunks(len(DATA), True))
    assert result.connections == connections
    assert result.peak_speed >= result.average_speed > 0
    assert result.session_byte_count == len(DATA)
    assert result.cdn_host == "127.0.0.1"
    assert not (tmp_path / "result.bin.downloadarr.part").exists()
    assert not (tmp_path / "result.bin.downloadarr.json").exists()


async def test_ignored_range_falls_back_to_sequential(server, tmp_path):
    async def handler(request):
        return web.Response(body=DATA)
    url = await server(handler)
    result = await Downloader().download(await provider(url), tmp_path / "out")
    assert result.path.read_bytes() == DATA
    assert not result.used_ranges


async def test_malformed_probe_is_terminal(server, tmp_path):
    async def handler(request):
        return web.Response(status=206, body=b"x", headers={"Content-Range": "nonsense"})
    url = await server(handler)
    with pytest.raises(ProtocolError):
        await Downloader().download(await provider(url), tmp_path / "out")


async def test_truncated_chunk_preserves_partial_and_no_final(server, tmp_path):
    async def handler(request):
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        body = DATA[start:end + 1]
        if not (start == 0 and end == 0):
            body = body[:-1]
        return web.Response(status=206, body=body,
                            headers={"Content-Range": f"bytes {start}-{end}/{len(DATA)}",
                                     "Content-Length": str(len(body))})
    url = await server(handler)
    output = tmp_path / "out"
    with pytest.raises(ProtocolError):
        await Downloader(DownloadConfig(connections=4, transfer_mode="parallel")).download(
            await provider(url), output)
    assert not output.exists()
    assert Path(str(output) + ".downloadarr.part").exists()


async def test_retry_502_and_refresh_403(server, tmp_path):
    calls = {"requests": 0, "refresh": 0}
    base = range_handler()
    async def handler(request):
        calls["requests"] += 1
        if calls["requests"] == 2:
            return web.Response(status=502)
        if calls["requests"] == 3:
            return web.Response(status=403)
        return await base(request)
    url = await server(handler)
    async def urls(refresh):
        calls["refresh"] += int(refresh)
        return url
    result = await Downloader(DownloadConfig(connections=1, backoff_base=0)).download(urls, tmp_path / "out")
    assert result.path.read_bytes() == DATA
    assert calls["refresh"] == 1


async def test_refreshes_expired_url_during_probe(server, tmp_path):
    good = await server(range_handler())
    async def expired(request):
        return web.Response(status=403)
    bad = await server(expired)
    refreshes = 0
    async def urls(refresh):
        nonlocal refreshes
        refreshes += int(refresh)
        return good if refresh else bad
    result = await Downloader(DownloadConfig(backoff_base=0)).download(urls, tmp_path / "out")
    assert result.path.read_bytes() == DATA
    assert refreshes == 1


async def test_exhaustion_keeps_state(server, tmp_path):
    probe_done = False
    async def handler(request):
        nonlocal probe_done
        if not probe_done:
            probe_done = True
            return await range_handler()(request)
        return web.Response(status=429, headers={"Retry-After": "0"})
    url = await server(handler)
    with pytest.raises(RetryExhausted):
        await Downloader(DownloadConfig(retries=1, backoff_base=0)).download(await provider(url), tmp_path / "out")
    assert (tmp_path / "out.downloadarr.json").exists()


async def test_manifest_resume(server, tmp_path):
    url = await server(range_handler())
    output = tmp_path / "out"
    config = DownloadConfig(connections=4, transfer_mode="parallel")
    chunks = Downloader(config)._chunks(len(DATA), True)
    first = chunks[0]
    part = Path(str(output) + ".downloadarr.part")
    part.write_bytes(DATA[:first.length] + bytes(len(DATA) - first.length))
    manifest = {"version": Manifest.VERSION, "total": len(DATA), "ranged": True,
                "identity": {"resource": "/file", "etag": None, "last_modified": None},
                "chunks": [{"index": c.index, "start": c.start, "end": c.end,
                            "downloaded": c.length if c.index == 0 else 0} for c in chunks]}
    Path(str(output) + ".downloadarr.json").write_text(json.dumps(manifest))
    progress = []
    result = await Downloader(config).download(
        await provider(url), output, progress.append)
    assert result.resumed and output.read_bytes() == DATA
    assert result.session_byte_count == len(DATA) - first.length
    assert result.average_speed == pytest.approx(result.session_byte_count / result.elapsed)
    assert progress[-1].downloaded_bytes == len(DATA)
    assert progress[-1].session_downloaded_bytes == len(DATA) - first.length


async def test_incompatible_manifest_resets(server, tmp_path):
    url = await server(range_handler())
    output = tmp_path / "out"
    Path(str(output) + ".downloadarr.part").write_bytes(bytes(len(DATA)))
    Path(str(output) + ".downloadarr.json").write_text('{"version":0}')
    result = await Downloader(DownloadConfig(connections=4)).download(await provider(url), output)
    assert not result.resumed and output.read_bytes() == DATA


async def test_if_range_and_changed_etag_reset_resume(server, tmp_path):
    seen_if_range = []
    async def handler(request):
        header = request.headers.get("Range")
        start, end = map(int, header.removeprefix("bytes=").split("-"))
        seen_if_range.append(request.headers.get("If-Range"))
        return web.Response(status=206, body=DATA[start:end + 1],
                            headers={"Content-Range": f"bytes {start}-{end}/{len(DATA)}",
                                     "ETag": '"new"'})
    url = await server(handler)
    output = tmp_path / "out"
    part = Path(str(output) + ".downloadarr.part")
    part.write_bytes(bytes(len(DATA)))
    chunks = Downloader(DownloadConfig(connections=1))._chunks(len(DATA), True)
    Path(str(output) + ".downloadarr.json").write_text(json.dumps({
        "version": Manifest.VERSION, "total": len(DATA), "ranged": True,
        "identity": {"resource": "/file", "etag": '"old"', "last_modified": None},
        "chunks": [{"index": 0, "start": 0, "end": len(DATA) - 1, "downloaded": 100}],
    }))
    result = await Downloader(DownloadConfig(connections=1)).download(await provider(url), output)
    assert not result.resumed
    assert seen_if_range[-1] == '"new"'
    assert output.read_bytes() == DATA


async def test_manifest_is_checkpointed_in_batches(server, tmp_path, monkeypatch):
    url = await server(range_handler())
    calls = 0
    original = Manifest.save
    async def counted(self, snapshot=None):
        nonlocal calls
        calls += 1
        await original(self, snapshot)
    monkeypatch.setattr(Manifest, "save", counted)
    config = DownloadConfig(connections=4, transfer_mode="parallel", block_size=1024,
                            checkpoint_bytes=20_000, checkpoint_interval=60)
    await Downloader(config).download(await provider(url), tmp_path / "out")
    assert calls < len(DATA) // config.block_size / 2


async def test_concurrent_expiry_refreshes_once(server, tmp_path):
    requests = 0
    base = range_handler()
    async def handler(request):
        nonlocal requests
        requests += 1
        # Probe succeeds, all initial chunk URLs then expire.
        if 2 <= requests <= 5:
            return web.Response(status=403)
        return await base(request)
    url = await server(handler)
    refreshes = 0
    async def urls(refresh):
        nonlocal refreshes
        refreshes += int(refresh)
        return url
    await Downloader(DownloadConfig(connections=4, transfer_mode="parallel",
                                    backoff_base=0)).download(urls, tmp_path / "out")
    assert refreshes == 1


async def test_premature_eof_retries_from_partial_offset(server, tmp_path):
    attempts = 0
    async def handler(request):
        nonlocal attempts
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        attempts += 1
        if attempts == 2:
            response = web.StreamResponse(status=206, headers={
                "Content-Range": f"bytes {start}-{end}/{len(DATA)}"})
            await response.prepare(request)
            await response.write(DATA[start:start + 4096])
            await response.write_eof()
            return response
        return web.Response(status=206, body=DATA[start:end + 1],
                            headers={"Content-Range": f"bytes {start}-{end}/{len(DATA)}"})
    url = await server(handler)
    result = await Downloader(DownloadConfig(connections=1, backoff_base=0)).download(
        await provider(url), tmp_path / "out")
    assert attempts >= 3
    assert result.path.read_bytes() == DATA
