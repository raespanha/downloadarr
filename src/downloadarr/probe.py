import re
from dataclasses import dataclass

import aiohttp

from .errors import HttpStatusError, ProtocolError

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


@dataclass(frozen=True, slots=True)
class RemoteInfo:
    size: int
    supports_ranges: bool
    etag: str | None
    last_modified: str | None

    @property
    def validator(self) -> str | None:
        # Strong ETags are valid for If-Range; weak ETags are not.
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified


def parse_content_range(value: str) -> tuple[int, int, int]:
    match = _CONTENT_RANGE.fullmatch(value.strip())
    if not match:
        raise ProtocolError(f"invalid Content-Range: {value!r}")
    start, end, total = map(int, match.groups())
    if total <= 0 or start > end or end >= total:
        raise ProtocolError(f"inconsistent Content-Range: {value!r}")
    return start, end, total


async def probe(session: aiohttp.ClientSession, url: str) -> RemoteInfo:
    async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as response:
        if response.status == 206:
            start, end, total = parse_content_range(response.headers.get("Content-Range", ""))
            body = await response.read()
            if (start, end) != (0, 0) or len(body) != 1:
                raise ProtocolError("range probe did not return exactly byte 0")
            return RemoteInfo(total, True, response.headers.get("ETag"),
                              response.headers.get("Last-Modified"))
        if response.status == 200:
            length = response.headers.get("Content-Length")
            if length is None or not length.isdigit() or int(length) <= 0:
                raise ProtocolError("sequential response has no valid Content-Length")
            # Do not retain a potentially large ignored-range body.
            return RemoteInfo(int(length), False, response.headers.get("ETag"),
                              response.headers.get("Last-Modified"))
        raise HttpStatusError(response.status, response.headers.get("Retry-After"))
