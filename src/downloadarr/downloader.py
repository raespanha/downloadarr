import asyncio
import email.utils
import inspect
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiofiles
import aiohttp

from .config import DownloadConfig
from .errors import DownloadError, HttpStatusError, ProtocolError, RetryExhausted
from .manifest import Manifest
from .probe import parse_content_range, probe
from .state import ChunkState, DownloadResult, ProgressCallback, TransferProgress, UrlProvider
from .writer import CheckpointWriter, PositionalWriter


class _UrlManager:
    def __init__(self, provider: UrlProvider) -> None:
        self.provider = provider
        self.current = ""
        self.generation = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> str:
        self.current = await self.provider(False)
        return self.current

    async def refresh(self, stale_generation: int) -> tuple[str, int]:
        async with self._lock:
            if self.generation == stale_generation:
                self.current = await self.provider(True)
                self.generation += 1
            return self.current, self.generation


class Downloader:
    def __init__(self, config: DownloadConfig | None = None) -> None:
        self.config = config or DownloadConfig()

    async def download(self, url_provider: UrlProvider, destination: str | os.PathLike[str],
                       progress_callback: ProgressCallback | None = None) -> DownloadResult:
        destination = Path(destination).expanduser().resolve()
        if destination.exists() and destination.is_dir():
            raise ValueError("destination must be a file path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".downloadarr.part")
        manifest_path = destination.with_name(destination.name + ".downloadarr.json")
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=None, connect=self.config.connect_timeout,
                                        sock_read=self.config.read_timeout)
        connector = aiohttp.TCPConnector(limit=self.config.connections + 1)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            urls = _UrlManager(url_provider)
            url, info = await self._probe_with_retry(session, urls)
            identity = self._identity(url, info)
            chunks = self._chunks(info.size, info.supports_ranges)
            manifest = None
            resumed = False
            if self.config.resume and part.is_file() and part.stat().st_size == info.size:
                manifest = Manifest.restore(manifest_path, info.size, info.supports_ranges,
                                            identity, chunks)
                resumed = manifest is not None and any(c.downloaded for c in chunks)
            if manifest is None:
                chunks = self._chunks(info.size, info.supports_ranges)
                manifest = Manifest(manifest_path, info.size, info.supports_ranges, identity, chunks)
                async with aiofiles.open(part, "wb") as handle:
                    await handle.truncate(info.size)
                await manifest.save()

            initial_downloaded = sum(c.downloaded for c in chunks)
            pending_chunks = sum(not chunk.done for chunk in chunks)
            prefer_full_get = (info.supports_ranges
                               and self.config.transfer_mode != "parallel"
                               and initial_downloaded == 0)
            used_ranges = asyncio.Event()
            counters = {"range_requests": 0, "retries": 0}
            speed_samples = deque([(started, 0)])
            speed_metrics = {"peak": 0.0}
            semaphore = asyncio.Semaphore(self.config.connections)
            progress_lock = asyncio.Lock()
            async with PositionalWriter(part) as writer:
                checkpoint = CheckpointWriter(writer, manifest,
                                              byte_interval=self.config.checkpoint_bytes,
                                              time_interval=self.config.checkpoint_interval)
                checkpoint.start()
                async def run(chunk: ChunkState) -> None:
                    await self._download_chunk(session, urls, url, urls.generation, info, chunk,
                                               checkpoint, manifest, semaphore, info.supports_ranges,
                                               started, progress_callback, progress_lock, chunks,
                                               initial_downloaded, prefer_full_get, used_ranges,
                                               counters, speed_samples, speed_metrics)
                try:
                    async with asyncio.TaskGroup() as group:
                        for chunk in chunks:
                            if not chunk.done:
                                group.create_task(run(chunk))
                except* Exception as group_error:
                    errors = [e for e in group_error.exceptions if not isinstance(e, asyncio.CancelledError)]
                    if errors:
                        raise errors[0]
                finally:
                    await self._finish_checkpoint(checkpoint)

            if sum(c.downloaded for c in chunks) != info.size or part.stat().st_size != info.size:
                raise ProtocolError("download completed without exactly the advertised byte count")
            os.replace(part, destination)
            manifest_path.unlink(missing_ok=True)
        elapsed = time.monotonic() - started
        session_bytes = info.size - initial_downloaded
        average_speed = session_bytes / elapsed if elapsed else 0.0
        return DownloadResult(
            destination, info.size, elapsed, average_speed, resumed, used_ranges.is_set(),
            session_byte_count=session_bytes,
            cdn_host=urlsplit(urls.current).hostname,
            range_requests=counters["range_requests"],
            retry_count=counters["retries"],
            peak_speed=max(speed_metrics["peak"], average_speed),
            connections=(max(1, min(self.config.connections, pending_chunks))
                         if used_ranges.is_set() else 1),
        )

    def _chunks(self, total: int, ranged: bool) -> list[ChunkState]:
        count = (min(self.config.connections * self.config.segments_per_connection, total)
                 if (ranged and self.config.transfer_mode == "parallel"
                     and self.config.connections > 1) else 1)
        width, remainder = divmod(total, count)
        chunks, start = [], 0
        for index in range(count):
            length = width + (1 if index < remainder else 0)
            chunks.append(ChunkState(index, start, start + length - 1))
            start += length
        return chunks

    async def _download_chunk(self, session, urls, initial_url, generation, info, chunk, writer,
                              manifest, semaphore, ranged, started, callback, progress_lock,
                              chunks, initial_downloaded, prefer_full_get, used_ranges,
                              counters, speed_samples, speed_metrics) -> None:
        url = initial_url
        for attempt in range(self.config.retries + 1):
            try:
                offset = chunk.start + chunk.downloaded
                request_ranged = ranged and not (prefer_full_get and attempt == 0 and offset == 0)
                headers = {"Range": f"bytes={offset}-{chunk.end}"} if request_ranged else {}
                if request_ranged:
                    used_ranges.set()
                    counters["range_requests"] += 1
                if request_ranged and info.validator:
                    headers["If-Range"] = info.validator
                retry_status = None
                retry_header = None
                async with semaphore:
                    async with session.get(url, headers=headers, allow_redirects=True) as response:
                        if response.status in (401, 403, 429) or 500 <= response.status <= 599:
                            retry_status = response.status
                            retry_header = response.headers.get("Retry-After")
                        else:
                            self._validate_response(response, request_ranged, offset, chunk.end, info)
                            expected = chunk.end - offset + 1
                            received = 0
                            async for block in response.content.iter_chunked(self.config.block_size):
                                if received + len(block) > expected:
                                    raise ProtocolError(f"chunk {chunk.index} exceeded its byte range")
                                await writer.write(chunk, offset + received, block)
                                received += len(block)
                                await self._progress(callback, progress_lock, started, info.size,
                                                     chunks, initial_downloaded, speed_samples,
                                                     speed_metrics)
                            if received != expected:
                                raise aiohttp.ClientPayloadError(
                                    f"truncated chunk {chunk.index}: {received}/{expected}")
                            return
                if retry_status in (401, 403):
                    url, generation = await urls.refresh(generation)
                elif retry_status is not None and retry_status != 429 and retry_status < 500:
                    raise DownloadError(f"HTTP {retry_status}")
                if attempt >= self.config.retries:
                    raise RetryExhausted(f"chunk {chunk.index} exhausted retries after HTTP {retry_status}")
                counters["retries"] += 1
                await asyncio.sleep(self._delay(attempt, retry_header))
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if not ranged and chunk.downloaded:
                    # An ignored-range server can only restart the single stream.
                    chunk.downloaded = 0
                    await manifest.save()
                if attempt >= self.config.retries:
                    raise RetryExhausted(f"chunk {chunk.index} exhausted retries: {error}") from error
                counters["retries"] += 1
                await asyncio.sleep(self._delay(attempt, None))

    async def _probe_with_retry(self, session, urls):
        url = await urls.initialize()
        for attempt in range(self.config.retries + 1):
            try:
                return url, await probe(session, url)
            except HttpStatusError as error:
                retryable = error.status in (401, 403, 429) or 500 <= error.status <= 599
                if not retryable:
                    raise
                if error.status in (401, 403):
                    url, _ = await urls.refresh(urls.generation)
                if attempt >= self.config.retries:
                    raise RetryExhausted(f"probe exhausted retries after HTTP {error.status}") from error
                await asyncio.sleep(self._delay(attempt, error.retry_after))
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt >= self.config.retries:
                    raise RetryExhausted(f"probe exhausted retries: {error}") from error
                await asyncio.sleep(self._delay(attempt, None))

    @staticmethod
    def _validate_response(response, ranged: bool, start: int, end: int, info) -> None:
        if ranged:
            if response.status != 206:
                raise ProtocolError(f"range request returned HTTP {response.status}, expected 206")
            actual = parse_content_range(response.headers.get("Content-Range", ""))
            if actual != (start, end, info.size):
                raise ProtocolError(f"range mismatch: got {actual}, expected {(start, end, info.size)}")
        elif response.status != 200:
            raise ProtocolError(f"sequential request returned HTTP {response.status}, expected 200")
        length = response.headers.get("Content-Length")
        expected = end - start + 1
        if length is not None and (not length.isdigit() or int(length) != expected):
            raise ProtocolError(f"Content-Length mismatch: {length!r}, expected {expected}")
        if info.etag and response.headers.get("ETag") not in (None, info.etag):
            raise ProtocolError("remote ETag changed during download")
        if info.last_modified and response.headers.get("Last-Modified") not in (None, info.last_modified):
            raise ProtocolError("remote Last-Modified changed during download")

    @staticmethod
    def _identity(url: str, info) -> dict:
        parsed = urlsplit(url)
        # Signed CDN URLs commonly rotate host and query while retaining the object path.
        resource = parsed.path
        return {"resource": resource, "etag": info.etag,
                "last_modified": info.last_modified}

    @staticmethod
    async def _finish_checkpoint(checkpoint: CheckpointWriter) -> None:
        task = asyncio.create_task(checkpoint.finish())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                value = float(retry_after)
                return min(max(value, 0.0), self.config.backoff_max)
            except ValueError:
                try:
                    date = email.utils.parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    return min(max((date - datetime.now(timezone.utc)).total_seconds(), 0.0),
                               self.config.backoff_max)
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(self.config.backoff_base * (2 ** attempt), self.config.backoff_max)

    @staticmethod
    async def _progress(callback, lock, started, total, chunks, initial_downloaded,
                        speed_samples, speed_metrics) -> None:
        async with lock:
            downloaded = sum(c.downloaded for c in chunks)
            now = time.monotonic()
            session_downloaded = max(downloaded - initial_downloaded, 0)
            speed_samples.append((now, session_downloaded))
            cutoff = now - 3.0
            while len(speed_samples) > 2 and speed_samples[1][0] <= cutoff:
                speed_samples.popleft()
            sample_started, sample_bytes = speed_samples[0]
            sample_elapsed = now - sample_started
            if sample_elapsed >= 0.25:
                speed_metrics["peak"] = max(
                    speed_metrics["peak"],
                    (session_downloaded - sample_bytes) / sample_elapsed,
                )
            if callback is None:
                return
            value = TransferProgress(
                total, downloaded, now - started,
                sum(c.done for c in chunks), len(chunks),
                session_downloaded,
            )
            result = callback(value)
            if inspect.isawaitable(result):
                await result
