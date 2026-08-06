from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    connections: int = 8
    retries: int = 6
    connect_timeout: float = 30.0
    read_timeout: float = 60.0
    block_size: int = 256 * 1024
    checkpoint_bytes: int = 16 * 1024 * 1024
    checkpoint_interval: float = 1.0
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    resume: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.connections <= 256:
            raise ValueError("connections must be between 1 and 256")
        if self.retries < 0:
            raise ValueError("retries must be non-negative")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if self.block_size <= 0 or self.checkpoint_bytes <= 0:
            raise ValueError("block and checkpoint sizes must be positive")
        if self.checkpoint_interval <= 0:
            raise ValueError("checkpoint_interval must be positive")
        if self.backoff_base < 0 or self.backoff_max < 0:
            raise ValueError("backoff values must be non-negative")
