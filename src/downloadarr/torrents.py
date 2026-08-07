import hashlib
import re
from dataclasses import dataclass


class TorrentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    info_hash: str
    display_name: str | None
    payload: bytes
    filename: str


MAX_TORRENT_BYTES = 16 * 1024 * 1024


def parse_torrent(payload: bytes, filename: str | None = None) -> TorrentInfo:
    if not isinstance(payload, bytes) or not payload:
        raise TorrentError("torrent file is empty")
    if len(payload) > MAX_TORRENT_BYTES:
        raise TorrentError("torrent file exceeds 16 MiB")
    decoder = _Decoder(payload)
    root, info_span = decoder.parse_root()
    info = root.get(b"info")
    if not isinstance(info, dict) or info_span is None:
        raise TorrentError("torrent has no info dictionary")
    # Downloadarr exposes the qBittorrent v1 40-character hash contract.
    # Hybrid torrents contain v1 pieces too; pure v2 torrents do not.
    if not isinstance(info.get(b"pieces"), bytes):
        raise TorrentError("pure BitTorrent v2 torrents are not supported yet")
    start, end = info_span
    info_hash = hashlib.sha1(payload[start:end]).hexdigest()
    raw_name = info.get(b"name.utf-8", info.get(b"name"))
    display_name = _display_name(raw_name)
    return TorrentInfo(info_hash, display_name, payload, _safe_filename(filename, info_hash))


def _display_name(value) -> str | None:
    if not isinstance(value, bytes):
        return None
    rendered = value.decode("utf-8", errors="replace").replace("\0", "")
    rendered = rendered.replace("/", "_").replace("\\", "_").strip()
    return rendered[:512] or None


def _safe_filename(value: str | None, info_hash: str) -> str:
    rendered = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    rendered = rendered.replace("\0", "").replace("\r", "").replace("\n", "").strip()
    if not rendered:
        rendered = f"{info_hash}.torrent"
    if not rendered.lower().endswith(".torrent"):
        rendered += ".torrent"
    return rendered[:255]


class _Decoder:
    MAX_DEPTH = 100
    MAX_NODES = 200_000

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0
        self.nodes = 0

    def parse_root(self) -> tuple[dict, tuple[int, int] | None]:
        if self._take() != ord("d"):
            raise TorrentError("torrent root must be a dictionary")
        result = {}
        info_span = None
        while self._peek() != ord("e"):
            key = self._string()
            if key in result:
                raise TorrentError("torrent dictionary contains duplicate keys")
            start = self.position
            result[key] = self._value(1)
            if key == b"info":
                info_span = (start, self.position)
        self.position += 1
        if self.position != len(self.payload):
            raise TorrentError("torrent has trailing data")
        return result, info_span

    def _value(self, depth: int):
        self.nodes += 1
        if self.nodes > self.MAX_NODES:
            raise TorrentError("torrent contains too many values")
        if depth > self.MAX_DEPTH:
            raise TorrentError("torrent nesting is too deep")
        token = self._peek()
        if token == ord("i"):
            return self._integer()
        if token == ord("l"):
            self.position += 1
            result = []
            while self._peek() != ord("e"):
                result.append(self._value(depth + 1))
            self.position += 1
            return result
        if token == ord("d"):
            self.position += 1
            result = {}
            while self._peek() != ord("e"):
                key = self._string()
                if key in result:
                    raise TorrentError("torrent dictionary contains duplicate keys")
                result[key] = self._value(depth + 1)
            self.position += 1
            return result
        if ord("0") <= token <= ord("9"):
            return self._string()
        raise TorrentError("torrent contains invalid bencode")

    def _integer(self) -> int:
        self.position += 1
        end = self.payload.find(b"e", self.position)
        if end < 0:
            raise TorrentError("torrent contains an unterminated integer")
        raw = self.payload[self.position:end]
        if raw == b"-0" or re.fullmatch(rb"-?(0|[1-9][0-9]*)", raw) is None:
            raise TorrentError("torrent contains a non-canonical integer")
        try:
            value = int(raw)
        except ValueError as error:
            raise TorrentError("torrent contains an invalid integer") from error
        self.position = end + 1
        return value

    def _string(self) -> bytes:
        colon = self.payload.find(b":", self.position)
        if colon < 0:
            raise TorrentError("torrent contains an invalid byte string")
        raw_length = self.payload[self.position:colon]
        if re.fullmatch(rb"0|[1-9][0-9]*", raw_length) is None:
            raise TorrentError("torrent contains a non-canonical byte string")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise TorrentError("torrent contains an invalid byte string length") from error
        if length < 0:
            raise TorrentError("torrent contains a negative byte string length")
        start, end = colon + 1, colon + 1 + length
        if end > len(self.payload):
            raise TorrentError("torrent byte string is truncated")
        self.position = end
        return self.payload[start:end]

    def _peek(self) -> int:
        if self.position >= len(self.payload):
            raise TorrentError("torrent is truncated")
        return self.payload[self.position]

    def _take(self) -> int:
        value = self._peek()
        self.position += 1
        return value
