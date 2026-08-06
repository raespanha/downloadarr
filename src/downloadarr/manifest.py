import asyncio
import json
import os
import tempfile
from pathlib import Path

from .state import ChunkState


class Manifest:
    VERSION = 2

    def __init__(self, path: Path, total: int, ranged: bool, identity: dict,
                 chunks: list[ChunkState]) -> None:
        self.path, self.total, self.ranged = path, total, ranged
        self.identity, self.chunks = identity, chunks
        self._lock = asyncio.Lock()

    def data(self) -> dict:
        return {"version": self.VERSION, "total": self.total, "ranged": self.ranged,
                "identity": self.identity,
                "chunks": [{"index": c.index, "start": c.start, "end": c.end,
                            "downloaded": c.downloaded} for c in self.chunks]}

    async def save(self) -> None:
        async with self._lock:
            payload = json.dumps(self.data(), separators=(",", ":"))
            fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                        dir=self.path.parent)
            temp = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(5):
                    try:
                        await asyncio.to_thread(os.replace, temp, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        await asyncio.sleep(0.02 * (attempt + 1))
            finally:
                temp.unlink(missing_ok=True)

    @classmethod
    def restore(cls, path: Path, total: int, ranged: bool, identity: dict,
                expected: list[ChunkState]) -> "Manifest | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            saved = data["chunks"]
            compatible = (data["version"] == cls.VERSION and data["total"] == total
                          and data["ranged"] is ranged and data["identity"] == identity
                          and len(saved) == len(expected))
            if not compatible:
                return None
            for raw, chunk in zip(saved, expected, strict=True):
                if raw["index"] != chunk.index or raw["start"] != chunk.start or raw["end"] != chunk.end:
                    return None
                value = raw["downloaded"]
                if not isinstance(value, int) or not 0 <= value <= chunk.length:
                    return None
                chunk.downloaded = value
            return cls(path, total, ranged, identity, expected)
        except (OSError, ValueError, KeyError, TypeError):
            return None
