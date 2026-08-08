import logging
from dataclasses import dataclass
from typing import Protocol

import aiohttp

from .settings import IntegrationsSettings


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    service: str
    indexer: str | None = None
    indexer_id: int | None = None


class SourceResolver(Protocol):
    def service_for(self, category: str | None) -> str: ...
    async def resolve(self, category: str | None,
                      info_hash: str) -> SourceMetadata: ...
    async def close(self) -> None: ...


class ArrMetadataResolver:
    """Enrich qB submissions from the matching Servarr grab history record."""

    def __init__(self, settings: IntegrationsSettings,
                 session: aiohttp.ClientSession | None = None) -> None:
        self.settings = settings
        self._owned_session = session is None
        self._session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10))

    def service_for(self, category: str | None) -> str:
        if category == self.settings.sonarr.category:
            return "sonarr"
        if category == self.settings.radarr.category:
            return "radarr"
        return "other"

    async def resolve(self, category: str | None,
                      info_hash: str) -> SourceMetadata:
        service = self.service_for(category)
        if service == "other":
            return SourceMetadata(service)
        configured = getattr(self.settings, service)
        if not configured.enabled:
            return SourceMetadata(service)
        try:
            async with self._session.get(
                configured.url + "/api/v3/history",
                headers={"X-Api-Key": configured.api_key.get_secret_value()},
                params={
                    "page": 1,
                    "pageSize": 10,
                    "sortKey": "date",
                    "sortDirection": "descending",
                    # Servarr's downloadId filter is case-sensitive.
                    "downloadId": info_hash.upper(),
                },
            ) as response:
                if response.status >= 400:
                    logger.warning("arr_metadata_failed service=%s status=%d",
                                   service, response.status)
                    return SourceMetadata(service)
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logger.warning("arr_metadata_failed service=%s reason=request_error", service)
            return SourceMetadata(service)
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return SourceMetadata(service)
        record = next((item for item in records if isinstance(item, dict)
                       and item.get("eventType") == "grabbed"), None)
        data = record.get("data") if isinstance(record, dict) else None
        if not isinstance(data, dict):
            return SourceMetadata(service)
        indexer = data.get("indexer")
        indexer_id = data.get("indexerId")
        try:
            parsed_id = int(indexer_id) if indexer_id is not None else None
        except (TypeError, ValueError):
            parsed_id = None
        return SourceMetadata(
            service,
            str(indexer).strip()[:255] if indexer else None,
            parsed_id,
        )

    async def close(self) -> None:
        if self._owned_session:
            await self._session.close()
