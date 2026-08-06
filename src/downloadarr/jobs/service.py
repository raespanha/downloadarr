import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..db.engine import Database
from ..db.models import Category, Job, JobState, ProviderJob
from ..magnets import MagnetInfo
from ..providers.base import ProviderError, TorrentProvider


class JobService:
    def __init__(self, database: Database, provider: TorrentProvider, *,
                 poll_interval: float = 5.0, queued_poll_interval: float = 30.0,
                 max_backoff: float = 300.0) -> None:
        self.database, self.provider = database, provider
        self.poll_interval, self.queued_poll_interval = poll_interval, queued_poll_interval
        self.max_backoff = max_backoff

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
        terminal = [JobState.PROVIDER_READY.value, JobState.FAILED.value]
        statement = (select(Job.id).where(Job.state.not_in(terminal),
                    or_(Job.next_poll_at.is_(None), Job.next_poll_at <= _now()))
                    .order_by(Job.next_poll_at).limit(limit))
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def process(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(Job, job_id)
            if job is None or job.state in (JobState.PROVIDER_READY.value, JobState.FAILED.value):
                return
            try:
                if job.provider_job.remote_id is not None:
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
            job.state = JobState.PROVIDER_QUEUED.value
            job.next_poll_at = _now() + timedelta(seconds=self.queued_poll_interval)

    async def _poll_remote(self, job: Job) -> None:
        torrent = await self.provider.get_torrent(job.provider_job.remote_id)
        job.name, job.size = torrent.name, torrent.size
        job.progress, job.download_speed, job.eta = torrent.progress, torrent.download_speed, torrent.eta
        job.provider_job.provider_state = torrent.state
        job.provider_job.last_polled_at = _now()
        job.provider_job.payload = json.dumps({"download_present": torrent.download_present})
        if torrent.download_finished and torrent.download_present:
            job.state, job.progress, job.next_poll_at = JobState.PROVIDER_READY.value, 1.0, None
        else:
            job.state = JobState.PROVIDER_DOWNLOADING.value
            job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)


def _now() -> datetime:
    return datetime.now(timezone.utc)
