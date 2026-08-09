import os
import socket
from pathlib import Path

from sqlalchemy.engine import make_url


class ProcessLockError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._handle = None
        self._owned = False

    @classmethod
    def for_database(cls, database_url: str) -> "ProcessLock":
        parsed = make_url(database_url)
        if not parsed.drivername.startswith("sqlite"):
            raise ProcessLockError("Downloadarr production persistence supports SQLite only")
        if parsed.database in (None, "", ":memory:"):
            return cls(None)
        database = Path(parsed.database).expanduser().resolve()
        return cls(database.with_name(database.name + ".lock"))

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        if self.path is None:
            self._owned = True
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            handle.close()
            raise ProcessLockError(
                "another Downloadarr process already owns this SQLite database") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n".encode("utf-8"))
        handle.flush()
        self._handle, self._owned = handle, True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        # Closing the descriptor is the authoritative release
                        # on Windows; some runtimes already drop the byte lock.
                        pass
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._owned = False

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()
