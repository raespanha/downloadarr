import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX; Windows flushes on close/replace."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
