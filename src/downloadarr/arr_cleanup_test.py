import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import aiohttp


INFO_HASH = re.compile(r"^[0-9a-fA-F]{40}$")


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportEvidence:
    file_id: int
    imported_path: str
    dropped_path: str
    size: int | None
    date: datetime


@dataclass(frozen=True)
class VerificationResult:
    info_hash: str
    imported_path: str
    elapsed: float


class ArrLookup(Protocol):
    async def imported(self, info_hash: str, since: datetime) -> ImportEvidence | None: ...
    async def library_file(self, file_id: int) -> dict[str, Any] | None: ...


class DownloadarrLookup(Protocol):
    async def job(self, info_hash: str) -> dict[str, Any] | None: ...


class ArrApi:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str,
                 kind: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.kind = kind

    async def imported(self, info_hash: str, since: datetime) -> ImportEvidence | None:
        payload = await self._get("/api/v3/history", params={
            "page": "1", "pageSize": "20", "sortKey": "date",
            # Servarr stores qBittorrent download IDs in uppercase and its
            # history filter is case-sensitive.
            "sortDirection": "descending", "downloadId": info_hash.upper(),
        })
        records = payload.get("records", []) if isinstance(payload, dict) else []
        for record in records:
            if record.get("eventType") != "downloadFolderImported":
                continue
            date = _parse_date(record.get("date"))
            if date < since:
                continue
            data = record.get("data") or {}
            try:
                return ImportEvidence(
                    file_id=int(data["fileId"]),
                    imported_path=str(data["importedPath"]),
                    dropped_path=str(data["droppedPath"]),
                    size=int(data["size"]) if data.get("size") is not None else None,
                    date=date,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise VerificationError("Arr returned incomplete import history") from error
        return None

    async def library_file(self, file_id: int) -> dict[str, Any] | None:
        resource = "episodefile" if self.kind == "sonarr" else "moviefile"
        return await self._get(f"/api/v3/{resource}/{file_id}", allow_not_found=True)

    async def _get(self, path: str, *, params: dict[str, str] | None = None,
                   allow_not_found: bool = False) -> Any:
        async with self.session.get(self.base_url + path, headers=self.headers,
                                    params=params) as response:
            if allow_not_found and response.status == 404:
                return None
            if response.status >= 400:
                raise VerificationError(f"Arr API returned HTTP {response.status}")
            return await response.json()


class DownloadarrApi:
    def __init__(self, session: aiohttp.ClientSession, base_url: str,
                 username: str, password: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    async def login(self) -> None:
        async with self.session.post(self.base_url + "/api/v2/auth/login", data={
            "username": self.username, "password": self.password,
        }) as response:
            body = await response.text()
            if response.status != 200 or body.strip() != "Ok.":
                raise VerificationError("Downloadarr authentication failed")

    async def job(self, info_hash: str) -> dict[str, Any] | None:
        async with self.session.get(self.base_url + "/api/v2/torrents/info",
                                    params={"hashes": info_hash}) as response:
            if response.status >= 400:
                raise VerificationError(
                    f"Downloadarr API returned HTTP {response.status}")
            jobs = await response.json()
            return jobs[0] if jobs else None


async def verify_arr_cleanup(arr: ArrLookup, downloadarr: DownloadarrLookup,
                             info_hash: str, *, timeout: float = 7200,
                             poll_interval: float = 5,
                             since: datetime | None = None,
                             path_maps: list[tuple[str, Path]] | None = None
                             ) -> VerificationResult:
    normalized = info_hash.lower()
    if not INFO_HASH.fullmatch(normalized):
        raise VerificationError("info hash must contain exactly 40 hexadecimal characters")
    started = time.monotonic()
    deadline = started + timeout
    earliest = since or datetime.now(timezone.utc) - timedelta(seconds=30)
    seen_job = False
    evidence: ImportEvidence | None = None
    library_file: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        if evidence is None:
            evidence = await arr.imported(normalized, earliest)
        if evidence is not None and library_file is None:
            library_file = await arr.library_file(evidence.file_id)

        job = await downloadarr.job(normalized)
        if job is not None:
            seen_job = True
        elif not seen_job:
            raise VerificationError("target job was not present in Downloadarr")
        elif evidence is None:
            raise VerificationError("Downloadarr removed the job before Arr recorded an import")
        elif library_file is not None:
            _validate_library_file(library_file, evidence)
            _validate_mapped_paths(evidence, path_maps or [])
            return VerificationResult(normalized, evidence.imported_path,
                                      time.monotonic() - started)

        await asyncio.sleep(poll_interval)

    phase = "cleanup" if evidence is not None else "Arr import"
    raise VerificationError(f"timed out waiting for {phase}")


def _validate_library_file(library_file: dict[str, Any], evidence: ImportEvidence) -> None:
    if int(library_file.get("id", -1)) != evidence.file_id:
        raise VerificationError("Arr library record does not match imported file ID")
    library_size = library_file.get("size")
    if evidence.size is not None and library_size is not None \
            and int(library_size) != evidence.size:
        raise VerificationError("Arr library record size does not match import history")


def _validate_mapped_paths(evidence: ImportEvidence,
                           mappings: list[tuple[str, Path]]) -> None:
    if not mappings:
        return
    imported = _mapped_path(evidence.imported_path, mappings)
    dropped = _mapped_path(evidence.dropped_path, mappings)
    if imported is None or dropped is None:
        raise VerificationError("Arr paths were not covered by the supplied path maps")
    if not imported.is_file():
        raise VerificationError("mapped imported library file does not exist")
    if evidence.size is not None and imported.stat().st_size != evidence.size:
        raise VerificationError("mapped imported library file size is incorrect")
    if dropped.exists():
        raise VerificationError("mapped staging path still exists after cleanup")


def _mapped_path(value: str, mappings: list[tuple[str, Path]]) -> Path | None:
    normalized = PurePosixPath(value.replace("\\", "/"))
    choices = sorted(mappings, key=lambda item: len(PurePosixPath(item[0]).parts),
                     reverse=True)
    for container_root, host_root in choices:
        root = PurePosixPath(container_root)
        try:
            relative = normalized.relative_to(root)
        except ValueError:
            continue
        return host_root.joinpath(*relative.parts)
    return None


def _parse_date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise VerificationError("Arr import history has no valid date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VerificationError("Arr import history has an invalid date") from error
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _path_map(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("path maps use CONTAINER=HOST")
    container, host = value.split("=", 1)
    if not container.startswith("/") or not host:
        raise argparse.ArgumentTypeError("path maps use /container/path=host-path")
    return container.rstrip("/") or "/", Path(host).resolve()


def _info_hash(value: str) -> str:
    normalized = value.lower()
    if not INFO_HASH.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            "hash must contain exactly 40 hexadecimal characters")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Passively verify Arr import and Downloadarr post-import cleanup")
    parser.add_argument("--arr", choices=("sonarr", "radarr"), required=True)
    parser.add_argument("--hash", dest="info_hash", required=True, type=_info_hash)
    parser.add_argument("--arr-url", default=os.getenv("DOWNLOADARR_E2E_ARR_URL"))
    parser.add_argument("--arr-api-key", default=os.getenv("DOWNLOADARR_E2E_ARR_API_KEY"))
    parser.add_argument("--downloadarr-url",
                        default=os.getenv("DOWNLOADARR_E2E_DOWNLOADARR_URL",
                                          "http://127.0.0.1:6500"))
    parser.add_argument("--username", default=os.getenv("DOWNLOADARR_E2E_USERNAME"))
    parser.add_argument("--password", default=os.getenv("DOWNLOADARR_E2E_PASSWORD"))
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--lookback", type=float, default=30,
                        help="accept an import recorded this many seconds before startup")
    parser.add_argument("--path-map", action="append", default=[], type=_path_map,
                        metavar="CONTAINER=HOST")
    return parser


async def _run(arguments: argparse.Namespace) -> VerificationResult:
    required = {
        "Arr URL": arguments.arr_url, "Arr API key": arguments.arr_api_key,
        "Downloadarr username": arguments.username,
        "Downloadarr password": arguments.password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise VerificationError("missing " + ", ".join(missing))
    timeout = aiohttp.ClientTimeout(total=30)
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=cookie_jar) as session:
        arr = ArrApi(session, arguments.arr_url, arguments.arr_api_key, arguments.arr)
        downloadarr = DownloadarrApi(session, arguments.downloadarr_url,
                                      arguments.username, arguments.password)
        await downloadarr.login()
        return await verify_arr_cleanup(
            arr, downloadarr, arguments.info_hash, timeout=arguments.timeout,
            poll_interval=arguments.poll_interval,
            since=datetime.now(timezone.utc) - timedelta(seconds=arguments.lookback),
            path_maps=arguments.path_map)


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        result = asyncio.run(_run(arguments))
    except (aiohttp.ClientError, asyncio.TimeoutError, VerificationError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"PASS: Arr imported {result.info_hash} and Downloadarr cleaned it up "
          f"after {result.elapsed:.1f}s")


if __name__ == "__main__":
    main()
