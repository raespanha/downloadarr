import asyncio
import os
import threading
from pathlib import Path

from .manifest import Manifest
from .state import ChunkState


class PositionalWriter:
    """Concurrent positional writes with a portable independent-handle fallback."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._native = hasattr(os, "pwrite")
        self._local = threading.local()
        self._handles: list[object] = []
        self._handles_lock = threading.Lock()

    @property
    def uses_native_pwrite(self) -> bool:
        return self._native

    async def __aenter__(self):
        self._fd = os.open(self.path, os.O_RDWR)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await asyncio.to_thread(self._close)

    async def write(self, offset: int, data: bytes) -> None:
        if self._fd is None:
            raise RuntimeError("positional writer is not open")
        if self._native:
            await asyncio.to_thread(self._pwrite_all, offset, data)
        else:
            await asyncio.to_thread(self._thread_write_all, offset, data)

    async def sync(self) -> None:
        if self._fd is None:
            raise RuntimeError("positional writer is not open")
        await asyncio.to_thread(os.fsync, self._fd)

    def _pwrite_all(self, offset: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            try:
                count = os.pwrite(self._fd, view[written:], offset + written)
            except InterruptedError:
                continue
            if count <= 0:
                raise OSError(f"short positional write: {written}/{len(view)}")
            written += count

    def _thread_write_all(self, offset: int, data: bytes) -> None:
        handle = getattr(self._local, "handle", None)
        if handle is None:
            handle = open(self.path, "r+b", buffering=0)
            self._local.handle = handle
            with self._handles_lock:
                self._handles.append(handle)
        handle.seek(offset)
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = handle.write(view[written:])
            if count is None or count <= 0:
                raise OSError(f"short positional write: {written}/{len(view)}")
            written += count

    def _close(self) -> None:
        with self._handles_lock:
            handles, self._handles = self._handles, []
        first_error = None
        for handle in handles:
            try:
                handle.close()
            except OSError as error:
                first_error = first_error or error
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as error:
                first_error = first_error or error
            self._fd = None
        if first_error is not None:
            raise first_error


class CheckpointWriter:
    """Writes concurrently and checkpoints durable aggregate progress in the background."""

    def __init__(self, writer: PositionalWriter, manifest: Manifest, *, byte_interval: int,
                 time_interval: float) -> None:
        self.writer, self.manifest = writer, manifest
        self.byte_interval, self.time_interval = byte_interval, time_interval
        self._pending = 0
        self._last = asyncio.get_running_loop().time()
        self._condition = asyncio.Condition()
        self._checkpoint_lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._active_writes = 0
        self._checkpointing = False
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="downloadarr-checkpoint")

    async def write(self, chunk: ChunkState, offset: int, data: bytes) -> None:
        async with self._condition:
            self._raise_error()
            await self._condition.wait_for(lambda: not self._checkpointing)
            self._raise_error()
            self._active_writes += 1
        completed = False
        write_task = asyncio.create_task(self.writer.write(offset, data))
        try:
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                await write_task
                raise
            completed = True
        finally:
            async with self._condition:
                if completed:
                    chunk.downloaded += len(data)
                    self._pending += len(data)
                    now = asyncio.get_running_loop().time()
                    if (self._pending >= self.byte_interval
                            or now - self._last >= self.time_interval):
                        self._event.set()
                self._active_writes -= 1
                self._condition.notify_all()
        self._raise_error()

    async def checkpoint(self) -> None:
        await self._checkpoint_once()

    async def finish(self) -> None:
        if self._task is None:
            await self._checkpoint_once()
            return
        self._stopping = True
        self._event.set()
        await self._task

    async def _run(self) -> None:
        try:
            while True:
                remaining = max(self.time_interval
                                - (asyncio.get_running_loop().time() - self._last), 0.0)
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=remaining)
                except TimeoutError:
                    pass
                self._event.clear()
                if self._pending:
                    await self._checkpoint_once()
                else:
                    # Move the idle deadline forward instead of repeatedly
                    # timing out with a zero-second wait and spinning.
                    self._last = asyncio.get_running_loop().time()
                if self._stopping and not self._pending:
                    return
        except BaseException as error:
            self._error = error
            async with self._condition:
                self._condition.notify_all()
            raise

    async def _checkpoint_once(self) -> None:
        async with self._checkpoint_lock:
            async with self._condition:
                if not self._pending:
                    self._last = asyncio.get_running_loop().time()
                    return
                self._checkpointing = True
                await self._condition.wait_for(lambda: self._active_writes == 0)
                snapshot = self.manifest.data()
                self._pending = 0
                self._checkpointing = False
                self._condition.notify_all()
            # The snapshot only includes writes completed before this fsync.
            # New positional writes may continue while the older snapshot is
            # flushed and published; they will belong to the next checkpoint.
            await self.writer.sync()
            await self.manifest.save(snapshot)
            self._last = asyncio.get_running_loop().time()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error
