import asyncio
import json
import os

import pytest

from downloadarr.manifest import Manifest
from downloadarr.state import ChunkState
from downloadarr.writer import CheckpointWriter, PositionalWriter


async def test_positional_writer_supports_concurrent_independent_offsets(tmp_path):
    path = tmp_path / "part"
    path.write_bytes(bytes(12_000))
    blocks = [(0, b"a" * 4000), (4000, b"b" * 4000), (8000, b"c" * 4000)]

    async with PositionalWriter(path) as writer:
        await asyncio.gather(*(writer.write(offset, data) for offset, data in blocks))
        await writer.sync()

    assert path.read_bytes() == b"a" * 4000 + b"b" * 4000 + b"c" * 4000


async def test_positional_writer_rejects_short_native_write(tmp_path, monkeypatch):
    path = tmp_path / "part"
    path.write_bytes(bytes(10))
    monkeypatch.setattr(os, "pwrite", lambda *args: 0, raising=False)

    async with PositionalWriter(path) as writer:
        writer._native = True
        with pytest.raises(OSError, match="short positional write"):
            await writer.write(0, b"data")


async def test_background_checkpoint_persists_completed_writes(tmp_path):
    path = tmp_path / "part"
    path.write_bytes(bytes(8))
    chunk = ChunkState(0, 0, 7)
    manifest = Manifest(tmp_path / "state.json", 8, True, {}, [chunk])

    async with PositionalWriter(path) as writer:
        checkpoint = CheckpointWriter(writer, manifest, byte_interval=1, time_interval=60)
        checkpoint.start()
        await checkpoint.write(chunk, 0, b"abcdefgh")
        await checkpoint.finish()

    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["chunks"][0]["downloaded"] == 8
    assert path.read_bytes() == b"abcdefgh"


async def test_cancelled_write_is_awaited_but_not_claimed(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowWriter:
        async def write(self, offset, data):
            started.set()
            await release.wait()

        async def sync(self):
            pass

    chunk = ChunkState(0, 0, 3)
    manifest = Manifest(tmp_path / "state.json", 4, True, {}, [chunk])
    checkpoint = CheckpointWriter(SlowWriter(), manifest, byte_interval=1, time_interval=60)
    checkpoint.start()
    task = asyncio.create_task(checkpoint.write(chunk, 0, b"data"))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await checkpoint.finish()
    assert chunk.downloaded == 0
    assert not (tmp_path / "state.json").exists()


async def test_writes_continue_while_older_snapshot_is_flushed(tmp_path):
    sync_started = asyncio.Event()
    release_sync = asyncio.Event()
    sync_calls = 0

    class DelayedSyncWriter:
        async def write(self, offset, data):
            pass

        async def sync(self):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                sync_started.set()
                await release_sync.wait()

    chunks = [ChunkState(0, 0, 3), ChunkState(1, 4, 7)]
    manifest = Manifest(tmp_path / "state.json", 8, True, {}, chunks)
    checkpoint = CheckpointWriter(
        DelayedSyncWriter(), manifest, byte_interval=1, time_interval=60)
    checkpoint.start()
    await checkpoint.write(chunks[0], 0, b"aaaa")
    await sync_started.wait()

    await asyncio.wait_for(checkpoint.write(chunks[1], 4, b"bbbb"), timeout=0.5)
    release_sync.set()
    await checkpoint.finish()

    saved = json.loads((tmp_path / "state.json").read_text())
    assert [chunk["downloaded"] for chunk in saved["chunks"]] == [4, 4]
