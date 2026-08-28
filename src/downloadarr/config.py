from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    connections: int = 8
    transfer_mode: Literal["auto", "sequential", "parallel"] = "auto"
    retries: int = 6
    connect_timeout: float = 30.0
    read_timeout: float = 60.0
    stall_timeout: float = 30.0
    minimum_chunk_rate: int = 64 * 1024
    block_size: int = 256 * 1024
    segments_per_connection: int = 8
    checkpoint_bytes: int = 16 * 1024 * 1024
    checkpoint_interval: float = 1.0
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    resume: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.connections <= 256:
            raise ValueError("connections must be between 1 and 256")
        if self.transfer_mode not in {"auto", "sequential", "parallel"}:
            raise ValueError("transfer_mode must be auto, sequential, or parallel")
        if self.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.connect_timeout <= 0 or self.read_timeout <= 0 or self.stall_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if self.minimum_chunk_rate < 0:
            raise ValueError("minimum_chunk_rate must be non-negative")
        if self.block_size <= 0 or self.checkpoint_bytes <= 0:
            raise ValueError("block and checkpoint sizes must be positive")
        if not 1 <= self.segments_per_connection <= 1024:
            raise ValueError("segments_per_connection must be between 1 and 1024")
        if self.checkpoint_interval <= 0:
            raise ValueError("checkpoint_interval must be positive")
        if self.backoff_base < 0 or self.backoff_max < 0:
            raise ValueError("backoff values must be non-negative")
