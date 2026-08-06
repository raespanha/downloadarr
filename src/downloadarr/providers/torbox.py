import email.utils
import json
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..magnets import MagnetError, parse_magnet
from .base import (ProviderError, ProviderFile, ProviderQueuedTorrent, ProviderSubmission,
                   ProviderTorrent)


class TorBoxProvider:
    def __init__(self, token: str, base_url: str, timeout: float = 30.0,
                 session: aiohttp.ClientSession | None = None) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._owned_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Authorization": f"Bearer {token}"},
        )

    async def close(self) -> None:
        if self._owned_session:
            await self._session.close()

    async def create_magnet(self, magnet: str) -> ProviderSubmission:
        form = aiohttp.FormData()
        form.add_field("magnet", magnet)
        form.add_field("allow_zip", "false")
        form.add_field("as_queued", "true")
        form.add_field("add_only_if_cached", "false")
        try:
            data = await self._request("POST", "/torrents/createtorrent", data=form)
        except ProviderError as error:
            if error.code != "REQUEST_REJECTED":
                raise
            try:
                info_hash = parse_magnet(magnet).info_hash
            except MagnetError:
                raise error
            existing = await self.find_torrent(info_hash)
            if existing is not None:
                return ProviderSubmission(remote_id=existing.remote_id)
            queued = next((item for item in await self.get_queued()
                           if item.info_hash == info_hash), None)
            if queued is not None:
                return ProviderSubmission(remote_id=queued.remote_id,
                                          queued_id=queued.queued_id)
            raise error
        if isinstance(data, int):
            return ProviderSubmission(remote_id=data)
        if not isinstance(data, dict):
            raise ProviderError("INVALID_RESPONSE", "TorBox returned invalid submission data",
                                transient=True)
        remote = data.get("torrent_id", data.get("id"))
        queued = data.get("queued_id")
        if remote is None and queued is None:
            raise ProviderError("INVALID_RESPONSE", "TorBox submission has no identifier",
                                transient=True)
        return ProviderSubmission(_integer(remote, "torrent_id") if remote is not None else None,
                                  _integer(queued, "queued_id") if queued is not None else None)

    async def get_torrent(self, remote_id: int) -> ProviderTorrent:
        data = await self._request("GET", "/torrents/mylist",
                                   params={"id": remote_id, "bypass_cache": "true"})
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            raise ProviderError("NOT_FOUND", "TorBox torrent was not found", transient=True)
        return _torrent(data, remote_id)

    async def find_torrent(self, info_hash: str) -> ProviderTorrent | None:
        data = await self._request("GET", "/torrents/mylist",
                                   params={"bypass_cache": "true", "limit": 1000})
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ProviderError("INVALID_RESPONSE", "TorBox torrent list is invalid", transient=True)
        match = next((item for item in data if isinstance(item, dict)
                      and str(item.get("hash") or "").lower() == info_hash.lower()), None)
        return _torrent(match) if match is not None else None

    async def get_queued(self) -> list[ProviderQueuedTorrent]:
        data = await self._request("GET", "/queued/getqueued",
                                   params={"type": "torrent", "bypass_cache": "true", "limit": 1000})
        if data is None:
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ProviderError("INVALID_RESPONSE", "TorBox queued list is invalid", transient=True)
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            queued = item.get("queued_id", item.get("id"))
            if queued is None:
                continue
            result.append(ProviderQueuedTorrent(
                _integer(queued, "queued_id"),
                str(item.get("hash")).lower() if item.get("hash") else None,
                _optional_integer(item.get("torrent_id"), "torrent_id"),
            ))
        return result

    async def get_files(self, remote_id: int) -> list[ProviderFile]:
        data = await self._request("GET", "/torrents/mylist",
                                   params={"id": remote_id, "bypass_cache": "true"})
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            raise ProviderError("INVALID_RESPONSE", "TorBox torrent files are invalid",
                                transient=True)
        files = []
        for item in data["files"]:
            if not isinstance(item, dict):
                raise ProviderError("INVALID_RESPONSE", "TorBox torrent file is invalid",
                                    transient=True)
            path = item.get("name") or item.get("short_name")
            if not isinstance(path, str) or not path:
                raise ProviderError("INVALID_RESPONSE", "TorBox torrent file path is invalid",
                                    transient=True)
            files.append(ProviderFile(
                _integer(item.get("id"), "file.id"), path,
                max(0, _integer(item.get("size"), "file.size"))))
        if not files:
            raise ProviderError("INVALID_RESPONSE", "TorBox torrent contains no files",
                                transient=True)
        return files

    async def request_download(self, remote_id: int, file_id: int) -> str:
        data = await self._request("GET", "/torrents/requestdl", params={
            "token": self._token, "torrent_id": remote_id, "file_id": file_id,
            "zip_link": "false", "redirect": "false", "append_name": "false",
        })
        if not isinstance(data, str) or not data.startswith(("https://", "http://")):
            raise ProviderError("INVALID_RESPONSE", "TorBox returned an invalid download URL",
                                transient=True)
        return data

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            async with self._session.request(method, self._base + path, **kwargs) as response:
                if response.status == 429 or response.status >= 500:
                    delay = _retry_after(response.headers.get("Retry-After"))
                    error = ProviderError("RATE_LIMITED" if response.status == 429 else "UPSTREAM_ERROR",
                                          f"TorBox temporarily unavailable (HTTP {response.status})",
                                          transient=True)
                    error.retry_after = delay
                    raise error
                if response.status in (401, 403):
                    raise ProviderError("AUTHENTICATION_FAILED", "TorBox authentication failed",
                                        transient=False)
                if response.status >= 400:
                    raise ProviderError("REQUEST_REJECTED",
                                        f"TorBox rejected the request (HTTP {response.status})",
                                        transient=False)
                try:
                    body = await response.content.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise ValueError("response is too large")
                    envelope = json.loads(body)
                except (ValueError, UnicodeDecodeError, aiohttp.ClientPayloadError) as error:
                    raise ProviderError("INVALID_RESPONSE", "TorBox returned invalid JSON",
                                        transient=True) from error
        except ProviderError:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise ProviderError("NETWORK_ERROR", "TorBox request failed", transient=True) from error
        if not isinstance(envelope, dict) or not envelope.get("success", False):
            code = str(envelope.get("error") or "PROVIDER_ERROR") if isinstance(envelope, dict) else "INVALID_RESPONSE"
            # Provider detail can echo submitted URLs. Keep exceptions safe for logs.
            message = f"TorBox operation failed ({code[:64]})"
            raise ProviderError(code[:64], message, transient=code in {"DATABASE_ERROR", "UNKNOWN_ERROR"})
        return envelope.get("data")


def _torrent(data: dict, fallback_id: int | None = None) -> ProviderTorrent:
    progress = float(data.get("progress") or 0)
    if progress > 1:
        progress /= 100
    return ProviderTorrent(
        remote_id=_integer(data.get("id", fallback_id), "id"),
        info_hash=str(data.get("hash") or "").lower(),
        name=str(data.get("name") or "Unnamed torrent")[:512],
        state=str(data.get("download_state") or "unknown")[:64],
        size=max(0, _integer(data.get("size", 0), "size")),
        progress=min(max(progress, 0.0), 1.0),
        download_speed=max(0, _integer(data.get("download_speed", 0), "download_speed")),
        eta=_optional_integer(data.get("eta"), "eta"),
        download_finished=bool(data.get("download_finished")),
        download_present=bool(data.get("download_present")),
    )


def _integer(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ProviderError("INVALID_RESPONSE", f"TorBox field {field} is invalid", transient=True) from error


def _optional_integer(value: Any, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
