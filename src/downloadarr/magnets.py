import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote_plus, urlsplit


class MagnetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MagnetInfo:
    info_hash: str
    display_name: str | None
    uri: str


def parse_magnet(uri: str) -> MagnetInfo:
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "magnet":
        raise MagnetError("source must be a magnet URI")
    query = parse_qs(parsed.query, keep_blank_values=True)
    hashes = []
    for value in query.get("xt", []):
        prefix = "urn:btih:"
        if value.lower().startswith(prefix):
            hashes.append(value[len(prefix):])
    if not hashes:
        raise MagnetError("magnet has no BitTorrent v1 info hash")
    normalized = {_normalize_hash(value) for value in hashes}
    if len(normalized) != 1:
        raise MagnetError("magnet contains conflicting info hashes")
    name = query.get("dn", [None])[0]
    return MagnetInfo(normalized.pop(), unquote_plus(name)[:512] if name else None, uri)


def _normalize_hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Z2-7a-z2-7]{32}", value):
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError as error:
            raise MagnetError("invalid base32 info hash") from error
    raise MagnetError("info hash must be 40 hex or 32 base32 characters")
