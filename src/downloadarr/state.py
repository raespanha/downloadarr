from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable


UrlProvider = Callable[[bool], Awaitable[str]]


@dataclass(slots=True)
class ChunkState:
    index: int
    start: int
    end: int
    downloaded: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def done(self) -> bool:
        return self.downloaded == self.length


@dataclass(slots=True)
class TransferProgress:
    total_bytes: int
    downloaded_bytes: int
    elapsed: float
    chunks_done: int
    chunks_total: int
    session_downloaded_bytes: int = 0


ProgressCallback = Callable[[TransferProgress], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    byte_count: int
    elapsed: float
    average_speed: float
    resumed: bool
    used_ranges: bool
    session_byte_count: int | None = None
    cdn_host: str | None = None
    range_requests: int = 0
    retry_count: int = 0
