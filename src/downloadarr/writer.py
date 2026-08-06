import asyncio
import os
from pathlib import Path

import aiofiles

from .manifest import Manifest
from .state import ChunkState


class PositionalWriter:
    """Random-access writer; the implementation can later switch to os.pwrite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        self._file = await aiofiles.open(self.path, "r+b")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._file.close()

    async def write(self, offset: int, data: bytes) -> None:
        async with self._lock:
            await self._file.seek(offset)
            written = await self._file.write(data)
            if written != len(data):
                raise OSError(f"short filesystem write: {written}/{len(data)}")

    async def sync(self) -> None:
        async with self._lock:
            await self._file.flush()
            await asyncio.to_thread(os.fsync, self._file.fileno())


class CheckpointWriter:
    """Serializes positional writes and durably checkpoints aggregate progress."""

    def __init__(self, writer: PositionalWriter, manifest: Manifest, *, byte_interval: int,
                 time_interval: float) -> None:
        self.writer, self.manifest = writer, manifest
        self.byte_interval, self.time_interval = byte_interval, time_interval
        self._pending = 0
        self._last = asyncio.get_running_loop().time()
        self._lock = asyncio.Lock()

    async def write(self, chunk: ChunkState, offset: int, data: bytes) -> None:
        async with self._lock:
            await self.writer.write(offset, data)
            chunk.downloaded += len(data)
            self._pending += len(data)
            now = asyncio.get_running_loop().time()
            if self._pending >= self.byte_interval or now - self._last >= self.time_interval:
                await self._checkpoint(now)

    async def checkpoint(self) -> None:
        async with self._lock:
            if self._pending:
                await self._checkpoint(asyncio.get_running_loop().time())

    async def _checkpoint(self, now: float) -> None:
        # The data must reach durable storage before the manifest claims it exists.
        await self.writer.sync()
        await self.manifest.save()
        self._pending = 0
        self._last = now
