from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.code, self.transient = code, transient


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    remote_id: int | None = None
    queued_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderTorrent:
    remote_id: int
    info_hash: str
    name: str
    state: str
    size: int
    progress: float
    download_speed: int
    eta: int | None
    download_finished: bool
    download_present: bool


@dataclass(frozen=True, slots=True)
class ProviderQueuedTorrent:
    queued_id: int
    info_hash: str | None
    remote_id: int | None = None


class TorrentProvider(Protocol):
    async def create_magnet(self, magnet: str) -> ProviderSubmission: ...
    async def get_torrent(self, remote_id: int) -> ProviderTorrent: ...
    async def find_torrent(self, info_hash: str) -> ProviderTorrent | None: ...
    async def get_queued(self) -> list[ProviderQueuedTorrent]: ...
    async def close(self) -> None: ...
