import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..config import DownloadConfig
from ..db.engine import Database
from ..db.models import Category, DeliveryFile, Job, JobState, ProviderJob
from ..downloader import Downloader
from ..errors import DownloadError
from ..magnets import MagnetInfo
from ..providers.base import ProviderError, TorrentProvider


class JobService:
    def __init__(self, database: Database, provider: TorrentProvider, *,
                 poll_interval: float = 5.0, queued_poll_interval: float = 30.0,
                 max_backoff: float = 300.0, download_path: Path = Path("/downloads"),
                 download_connections: int = 4, download_transfer_mode: str = "auto",
                 downloader: Downloader | None = None) -> None:
        self.database, self.provider = database, provider
        self.poll_interval, self.queued_poll_interval = poll_interval, queued_poll_interval
        self.max_backoff = max_backoff
        self.download_path = Path(download_path)
        self.downloader = downloader or Downloader(DownloadConfig(
            connections=download_connections, transfer_mode=download_transfer_mode))

    async def add_magnet(self, magnet: MagnetInfo, category_name: str | None) -> Job:
        async with self.database.session() as session:
            existing = await session.scalar(select(Job).where(Job.info_hash == magnet.info_hash))
            if existing:
                return existing
            category = None
            if category_name:
                category = await session.scalar(select(Category).where(Category.name == category_name))
                if category is None:
                    raise ValueError("category does not exist")
            job = Job(info_hash=magnet.info_hash, name=magnet.display_name,
                      source_uri=magnet.uri, category=category,
                      state=JobState.SUBMITTED.value, next_poll_at=_now())
            job.provider_job = ProviderJob(provider="torbox")
            session.add(job)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await session.scalar(select(Job).where(Job.info_hash == magnet.info_hash))
            return job

    async def create_category(self, name: str, save_path: str) -> Category:
        if not name or len(name) > 255:
            raise ValueError("invalid category name")
        async with self.database.session() as session:
            category = await session.scalar(select(Category).where(Category.name == name))
            if category:
                return category
            category = Category(name=name, save_path=save_path)
            session.add(category)
            await session.commit()
            return category

    async def ensure_category(self, name: str, save_path: str) -> Category:
        if not name or len(name) > 255 or not save_path:
            raise ValueError("invalid category")
        async with self.database.session() as session:
            category = await session.scalar(select(Category).where(Category.name == name))
            if category is None:
                category = Category(name=name, save_path=save_path)
                session.add(category)
            elif category.save_path != save_path:
                category.save_path = save_path
            await session.commit()
            return category

    async def categories(self) -> list[Category]:
        async with self.database.session() as session:
            return list((await session.scalars(select(Category).order_by(Category.name))).all())

    async def jobs(self, category: str | None = None, hashes: list[str] | None = None) -> list[Job]:
        statement = select(Job).order_by(Job.created_at)
        if category is not None:
            statement = statement.join(Category).where(Category.name == category)
        if hashes:
            statement = statement.where(Job.info_hash.in_([value.lower() for value in hashes]))
        async with self.database.session() as session:
            return list((await session.scalars(statement)).unique().all())

    async def job(self, info_hash: str) -> Job | None:
        async with self.database.session() as session:
            return await session.scalar(select(Job).where(Job.info_hash == info_hash.lower()))

    async def due_job_ids(self, limit: int = 100) -> list[str]:
        terminal = [JobState.COMPLETED.value, JobState.FAILED.value]
        statement = (select(Job.id).where(Job.state.not_in(terminal),
                    or_(Job.next_poll_at.is_(None), Job.next_poll_at <= _now()))
                    .order_by(Job.next_poll_at).limit(limit))
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def process(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(Job, job_id)
            if job is None or job.state in (JobState.COMPLETED.value, JobState.FAILED.value):
                return
            try:
                if job.provider_job.remote_id is not None:
                    if job.delivery_files:
                        job.state = JobState.DELIVERING.value
                        await self._deliver(job, session)
                    elif job.state == JobState.PROVIDER_READY.value:
                        await self._prepare_delivery(job)
                    else:
                        await self._poll_remote(job)
                elif job.provider_job.queued_id is not None:
                    await self._poll_queued(job)
                else:
                    await self._submit(job)
                job.poll_failures = 0
                job.error_code = job.error_message = None
            except ProviderError as error:
                job.poll_failures += 1
                job.error_code, job.error_message = error.code, str(error)[:512]
                if error.transient:
                    job.state = JobState.RETRY_WAIT.value
                    delay = getattr(error, "retry_after", None) or min(
                        self.poll_interval * (2 ** (job.poll_failures - 1)), self.max_backoff)
                    job.next_poll_at = _now() + timedelta(seconds=delay)
                else:
                    job.state = JobState.FAILED.value
                    job.next_poll_at = None
            except DownloadError as error:
                job.poll_failures += 1
                job.error_code, job.error_message = "DOWNLOAD_FAILED", str(error)[:512]
                job.state = JobState.RETRY_WAIT.value
                job.next_poll_at = _now() + timedelta(seconds=min(
                    self.poll_interval * (2 ** (job.poll_failures - 1)), self.max_backoff))
            except (OSError, ValueError) as error:
                job.poll_failures += 1
                job.error_code, job.error_message = "DELIVERY_FAILED", str(error)[:512]
                job.state, job.next_poll_at = JobState.FAILED.value, None
            await session.commit()

    async def _submit(self, job: Job) -> None:
        submission = await self.provider.create_magnet(job.source_uri)
        job.provider_job.remote_id = submission.remote_id
        job.provider_job.queued_id = submission.queued_id
        job.state = (JobState.PROVIDER_QUEUED.value if submission.remote_id is None
                     else JobState.PROVIDER_DOWNLOADING.value)
        job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)

    async def _poll_queued(self, job: Job) -> None:
        queued = await self.provider.get_queued()
        match = next((item for item in queued if item.queued_id == job.provider_job.queued_id
                      or item.info_hash == job.info_hash), None)
        if match and match.remote_id is not None:
            job.provider_job.remote_id = match.remote_id
            job.state = JobState.PROVIDER_DOWNLOADING.value
            job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)
        else:
            torrent = await self.provider.find_torrent(job.info_hash)
            if torrent is not None:
                job.provider_job.remote_id = torrent.remote_id
                self._apply_torrent(job, torrent)
            else:
                job.state = JobState.PROVIDER_QUEUED.value
                job.next_poll_at = _now() + timedelta(seconds=self.queued_poll_interval)

    async def _poll_remote(self, job: Job) -> None:
        torrent = await self.provider.get_torrent(job.provider_job.remote_id)
        self._apply_torrent(job, torrent)

    def _apply_torrent(self, job: Job, torrent) -> None:
        job.name, job.size = torrent.name, torrent.size
        job.progress, job.download_speed, job.eta = torrent.progress, torrent.download_speed, torrent.eta
        job.provider_job.provider_state = torrent.state
        job.provider_job.last_polled_at = _now()
        job.provider_job.payload = json.dumps({"download_present": torrent.download_present})
        if torrent.download_finished and torrent.download_present:
            job.state, job.progress, job.next_poll_at = JobState.PROVIDER_READY.value, 0.0, _now()
        else:
            job.state = JobState.PROVIDER_DOWNLOADING.value
            job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)

    async def _prepare_delivery(self, job: Job) -> None:
        files = await self.provider.get_files(job.provider_job.remote_id)
        if not job.delivery_files:
            relative_paths = _delivery_paths(job.name or job.info_hash, files)
            job.delivery_files = [DeliveryFile(
                provider_file_id=item.file_id, relative_path=relative,
                size=item.size, downloaded=0, state="pending")
                for item, relative in zip(files, relative_paths, strict=True)]
        job.size = sum(item.size for item in job.delivery_files)
        job.progress = (sum(item.downloaded for item in job.delivery_files) / job.size
                        if job.size else 1.0)
        job.download_speed, job.eta = 0, None
        job.state, job.next_poll_at = JobState.DELIVERING.value, _now()

    async def _deliver(self, job: Job, session) -> None:
        base = Path(job.category.save_path) if job.category else self.download_path
        total = sum(item.size for item in job.delivery_files)
        for item in job.delivery_files:
            destination = _safe_destination(base, item.relative_path)
            if destination.exists():
                if destination.is_file() and destination.stat().st_size == item.size:
                    item.downloaded, item.state, item.error_message = item.size, "completed", None
                    continue
                raise ValueError(f"delivery destination already exists: {item.relative_path}")
            item.state, item.error_message = "downloading", None
            prior = sum(value.downloaded for value in job.delivery_files if value is not item)
            last_checkpoint = 0.0

            async def url_provider(refresh: bool, file_id=item.provider_file_id) -> str:
                return await self.provider.request_download(job.provider_job.remote_id, file_id)

            async def progress(value) -> None:
                nonlocal last_checkpoint
                item.downloaded = min(value.downloaded_bytes, item.size)
                job.progress = ((prior + item.downloaded) / total if total else 1.0)
                speed = value.session_downloaded_bytes / value.elapsed if value.elapsed else 0
                job.download_speed = int(speed)
                remaining = max(total - prior - item.downloaded, 0)
                job.eta = int(remaining / speed) if speed else None
                now = time.monotonic()
                if now - last_checkpoint >= 1.0:
                    last_checkpoint = now
                    await session.commit()

            result = await self.downloader.download(url_provider, destination, progress)
            if result.byte_count != item.size:
                raise ValueError(f"downloaded size mismatch for {item.relative_path}")
            item.downloaded, item.state = result.byte_count, "completed"
            completed = sum(value.downloaded for value in job.delivery_files)
            job.progress = completed / total if total else 1.0
            job.download_speed = int(result.average_speed)
        job.state, job.progress = JobState.COMPLETED.value, 1.0
        job.download_speed, job.eta = 0, 0
        job.completed_at, job.next_poll_at = _now(), None

    async def remove(self, hashes: list[str], delete_files: bool) -> None:
        normalized = [value.lower() for value in hashes]
        async with self.database.session() as session:
            jobs = list((await session.scalars(
                select(Job).where(Job.info_hash.in_(normalized)))).unique().all())
            for job in jobs:
                if job.state not in (JobState.COMPLETED.value, JobState.FAILED.value):
                    raise ValueError("active jobs cannot be removed yet")
                if delete_files:
                    base = Path(job.category.save_path) if job.category else self.download_path
                    for item in job.delivery_files:
                        destination = _safe_destination(base, item.relative_path)
                        destination.unlink(missing_ok=True)
                        _remove_empty_parents(destination.parent, base.resolve())
                await session.delete(job)
            await session.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _delivery_paths(torrent_name: str, files) -> list[str]:
    parsed = [_safe_provider_path(item.path) for item in files]
    if len(parsed) == 1:
        return [parsed[0].name]
    root = _safe_component(torrent_name)
    result = []
    for path in parsed:
        parts = list(path.parts)
        if parts and parts[0].casefold() == root.casefold():
            parts = parts[1:]
        if not parts:
            raise ValueError("provider file path has no filename")
        result.append(str(PurePosixPath(root, *parts)))
    if len(set(result)) != len(result):
        raise ValueError("provider returned duplicate file paths")
    return result


def _safe_provider_path(value: str) -> PurePosixPath:
    if "\0" in value:
        raise ValueError("provider file path contains a null byte")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("provider file path is unsafe")
    if ":" in path.parts[0]:
        raise ValueError("provider file path contains a drive prefix")
    return path


def _safe_component(value: str) -> str:
    component = value.replace("/", "_").replace("\\", "_").strip(" .")
    if not component or component in (".", "..") or "\0" in component or ":" in component:
        raise ValueError("torrent name is unsafe for a local path")
    return component


def _safe_destination(base: Path, relative: str) -> Path:
    root = base.expanduser().resolve()
    path = _safe_provider_path(relative)
    destination = root.joinpath(*path.parts).resolve()
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise ValueError("delivery destination escapes its configured root") from error
    return destination


def _remove_empty_parents(path: Path, base: Path) -> None:
    while path != base:
        try:
            path.rmdir()
        except (FileNotFoundError, OSError):
            return
        path = path.parent
