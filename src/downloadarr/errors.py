class DownloadError(Exception):
    """Base error for a transfer that was not published."""


class ProtocolError(DownloadError):
    """The remote server returned internally inconsistent HTTP metadata."""


class RetryExhausted(DownloadError):
    """A recoverable operation exceeded its retry budget."""


class HttpStatusError(DownloadError):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after
