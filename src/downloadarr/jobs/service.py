import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..config import DownloadConfig
from ..arr_metadata import SourceResolver
from ..db.engine import Database
from ..db.models import (AlertInstance, Category, ControlEvent, ControlState,
                         DeliveryFile, FailureEvent, Job, JobState, LifecycleEvent,
                         MonitorStatus, ProviderJob, TransferHistory)
from ..downloader import Downloader
from ..errors import DownloadError
from ..magnets import MagnetInfo
from ..torrents import TorrentInfo
from ..providers.base import ProviderError, TorrentProvider
from ..state import DownloadResult


logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, database: Database, provider: TorrentProvider, *,
                 poll_interval: float = 5.0, queued_poll_interval: float = 30.0,
                 max_backoff: float = 300.0, download_path: Path = Path("/downloads"),
                 download_connections: int = 4, download_transfer_mode: str = "auto",
                 max_job_failures: int = 5,
                 downloader: Downloader | None = None,
                 source_resolver: SourceResolver | None = None) -> None:
        self.database, self.provider = database, provider
        self.poll_interval, self.queued_poll_interval = poll_interval, queued_poll_interval
        self.max_backoff = max_backoff
        self.max_job_failures = max_job_failures
        self.download_path = Path(download_path)
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._controlling: set[str] = set()
        self._task_lock = asyncio.Lock()
        self.source_resolver = source_resolver
        self.downloader = downloader or Downloader(DownloadConfig(
            connections=download_connections, transfer_mode=download_transfer_mode))

    async def add_magnet(self, magnet: MagnetInfo, category_name: str | None) -> Job:
        return await self._add_source(magnet.info_hash, magnet.display_name, category_name,
                                      "magnet", magnet.uri, None)

    async def add_torrent(self, torrent: TorrentInfo, category_name: str | None) -> Job:
        return await self._add_source(torrent.info_hash, torrent.display_name, category_name,
                                      "torrent", torrent.filename, torrent.payload)

    async def _add_source(self, info_hash: str, name: str | None, category_name: str | None,
                          source_kind: str, source_uri: str, source_data: bytes | None) -> Job:
        async with self.database.session() as session:
            existing = await session.scalar(select(Job).where(Job.info_hash == info_hash))
            if existing:
                return existing
            category = None
            if category_name:
                category = await session.scalar(select(Category).where(Category.name == category_name))
                if category is None:
                    raise ValueError("category does not exist")
            job = Job(info_hash=info_hash, name=name, source_uri=source_uri,
                      source_kind=source_kind, source_data=source_data, category=category,
                      state=JobState.SUBMITTED.value, next_poll_at=_now(),
                      source_service=(self.source_resolver.service_for(category_name)
                                      if self.source_resolver else "other"))
            job.provider_job = ProviderJob(provider="torbox")
            session.add(job)
            try:
                await session.flush()
                now = _now()
                job.phase_started_at = now
                self._lifecycle(session, job, "accepted", event_key=f"accepted:{job.id}",
                                from_phase=None, to_phase=job.state, occurred_at=now)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return await session.scalar(select(Job).where(Job.info_hash == info_hash))
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

    async def transfer_history(self, since: datetime | None = None,
                               limit: int | None = None) -> list[TransferHistory]:
        statement = select(TransferHistory)
        if since is not None:
            statement = statement.where(TransferHistory.completed_at >= since)
        statement = statement.order_by(TransferHistory.completed_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        async with self.database.session() as session:
            values = list((await session.scalars(statement)).all())
        values.reverse()
        return values

    async def failure_history(self, since: datetime | None = None,
                              limit: int | None = None) -> list[FailureEvent]:
        statement = select(FailureEvent)
        if since is not None:
            statement = statement.where(FailureEvent.occurred_at >= since)
        statement = statement.order_by(FailureEvent.occurred_at.desc())
        if limit is not None:
            statement = statement.limit(limit)
        async with self.database.session() as session:
            values = list((await session.scalars(statement)).all())
        values.reverse()
        return values

    async def bootstrap_lifecycle(self) -> None:
        async with self.database.session() as session:
            jobs = list((await session.scalars(select(Job).where(
                Job.phase_started_at.is_(None)))).unique().all())
            now = _now()
            for job in jobs:
                job.phase_started_at = now
                self._lifecycle(session, job, "baseline_snapshot",
                                event_key=f"baseline:{job.id}", from_phase=None,
                                to_phase=job.state, occurred_at=now, partial_history=True,
                                detail="Monitoring began after this job entered its current phase")
            await session.commit()

    async def lifecycle_history(self, since: datetime | None = None, *,
                                service: str | None = None, indexer: str | None = None,
                                limit: int = 500) -> list[LifecycleEvent]:
        statement = select(LifecycleEvent)
        if since is not None:
            statement = statement.where(LifecycleEvent.occurred_at >= since)
        if service:
            statement = statement.where(LifecycleEvent.service == service)
        if indexer:
            statement = statement.where(LifecycleEvent.indexer == indexer)
        statement = statement.order_by(LifecycleEvent.sequence.desc()).limit(min(limit, 5000))
        async with self.database.session() as session:
            values = list((await session.scalars(statement)).all())
        values.reverse()
        return values

    async def alerts(self, limit: int = 200) -> list[AlertInstance]:
        async with self.database.session() as session:
            return list((await session.scalars(select(AlertInstance).order_by(
                AlertInstance.last_seen_at.desc()).limit(min(limit, 1000)))).all())

    async def monitor_status(self) -> MonitorStatus | None:
        async with self.database.session() as session:
            return await session.get(MonitorStatus, 1)

    async def acknowledge_alert(self, alert_id: str) -> bool:
        async with self.database.session() as session:
            item = await session.get(AlertInstance, alert_id)
            if item is None or item.status == "resolved":
                return False
            item.status, item.acknowledged_at = "acknowledged", _now()
            await session.commit()
            return True

    async def prune_telemetry(self, retention_days: int, *, dry_run: bool = True) -> dict:
        if retention_days <= 0:
            return {"enabled": False, "dry_run": dry_run, "rows": {}}
        cutoff = _now() - timedelta(days=retention_days)
        predicates = {
            "lifecycle_events": and_(LifecycleEvent.occurred_at < cutoff,
                                     LifecycleEvent.job_id.not_in(select(Job.id))),
            "control_events": and_(ControlEvent.occurred_at < cutoff,
                                   ControlEvent.job_id.not_in(select(Job.id))),
            "transfer_history": TransferHistory.completed_at < cutoff,
            "failure_events": and_(FailureEvent.resolved_at.is_not(None),
                                   FailureEvent.resolved_at < cutoff),
            "alert_instances": and_(AlertInstance.status == "resolved",
                                    AlertInstance.resolved_at < cutoff),
        }
        models = {"lifecycle_events": LifecycleEvent, "control_events": ControlEvent,
                  "transfer_history": TransferHistory, "failure_events": FailureEvent,
                  "alert_instances": AlertInstance}
        counts = {}
        async with self.database.session() as session:
            for name, predicate in predicates.items():
                ids = list((await session.scalars(select(models[name]).where(
                    predicate).limit(1000))).all())
                counts[name] = len(ids)
                if not dry_run and ids:
                    for item in ids:
                        await session.delete(item)
            status = await session.get(MonitorStatus, 1)
            if not dry_run and status:
                status.last_pruned_at = _now()
            if not dry_run:
                await session.commit()
        return {"enabled": True, "dry_run": dry_run,
                "cutoff": cutoff.isoformat(), "batch_limit": 1000, "rows": counts}

    async def evaluate_alerts(self) -> None:
        now = _now()
        async with self.database.session() as session:
            jobs = list((await session.scalars(select(Job))).unique().all())
            active: dict[str, tuple[Job, str, str, str]] = {}
            for job in jobs:
                if job.control_state == ControlState.PAUSED.value:
                    continue
                if job.state == JobState.FAILED.value:
                    active[f"terminal_failure:{job.id}"] = (
                        job, "error", "Download failed terminally",
                        "Review the latest failure and use Retry after correcting it.")
                if (job.control_state == ControlState.REMOVING.value
                        and job.cleanup_failures > 0):
                    active[f"cleanup_stuck:{job.id}"] = (
                        job, "error", "Cleanup is retrying",
                        "Check TorBox connectivity and filesystem permissions.")
                if job.control_error:
                    active[f"control_warning:{job.id}"] = (
                        job, "warning", "Provider control did not complete",
                        "The local control is safe; inspect TorBox before retrying provider control.")
                if job.state == JobState.RETRY_WAIT.value and job.poll_failures >= 3:
                    active[f"repeated_failure:{job.id}"] = (
                        job, "warning", "Download has repeated transient failures",
                        "Inspect TorBox/CDN availability and the failure timeline.")
            existing = {item.fingerprint: item for item in (await session.scalars(
                select(AlertInstance))).all()}
            for fingerprint, (job, severity, summary, action) in active.items():
                item = existing.get(fingerprint)
                if item is None:
                    session.add(AlertInstance(
                        fingerprint=fingerprint, rule=fingerprint.split(":", 1)[0],
                        severity=severity, status="open", job_id=job.id,
                        info_hash=job.info_hash, service=job.source_service,
                        indexer=job.source_indexer or "Unknown", summary=summary,
                        action=action, occurrences=1, first_seen_at=now,
                        last_seen_at=now))
                else:
                    item.last_seen_at = now
                    item.occurrences += 1
                    if item.status == "resolved":
                        item.status, item.resolved_at = "open", None
            for fingerprint, item in existing.items():
                if fingerprint not in active and item.status != "resolved":
                    item.status, item.resolved_at = "resolved", now
            status = await session.get(MonitorStatus, 1)
            if status is None:
                status = MonitorStatus(id=1)
                session.add(status)
            status.last_evaluated_at, status.last_error = now, None
            await session.commit()

    async def due_job_ids(self, limit: int = 100) -> list[str]:
        terminal = [JobState.COMPLETED.value, JobState.FAILED.value]
        statement = (select(Job.id).where(
                    or_(and_(Job.control_state == ControlState.REMOVING.value,
                             or_(Job.next_poll_at.is_(None), Job.next_poll_at <= _now())),
                        and_(Job.control_state == ControlState.RUNNING.value,
                             Job.state.not_in(terminal),
                             or_(Job.next_poll_at.is_(None), Job.next_poll_at <= _now()))))
                    .order_by(Job.next_poll_at).limit(limit))
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def process(self, job_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        async with self._task_lock:
            if job_id in self._controlling or job_id in self._active_tasks:
                return
            self._active_tasks[job_id] = task
        try:
            await self._process(job_id)
        finally:
            async with self._task_lock:
                if self._active_tasks.get(job_id) is task:
                    self._active_tasks.pop(job_id, None)

    async def _process(self, job_id: str) -> None:
        async with self.database.session() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            if job.control_state == ControlState.REMOVING.value:
                try:
                    await self._cleanup_removing(job, session)
                except (ProviderError, OSError) as error:
                    job.cleanup_failures += 1
                    job.error_code = getattr(error, "code", "CLEANUP_FAILED")
                    job.error_message = str(error)[:512]
                    delay = min(max(5.0, self.poll_interval)
                                * (2 ** (job.cleanup_failures - 1)), self.max_backoff)
                    job.next_poll_at = _now() + timedelta(seconds=delay)
                    self._lifecycle(
                        session, job, "cleanup_failed",
                        event_key=f"cleanup_failed:{job.id}:{job.cleanup_failures}",
                        from_phase=job.state, to_phase=job.state, outcome="retry_scheduled",
                        code=job.error_code, detail=str(error))
                    self._record_failure(session, job, "cleanup", job.error_code,
                                         str(error), True)
                    await session.commit()
                return
            if (job.control_state != ControlState.RUNNING.value
                    or job.state in (JobState.COMPLETED.value, JobState.FAILED.value)):
                return
            failure_stage = _failure_stage(
                _retry_state(job) if job.state == JobState.RETRY_WAIT.value else job.state)
            try:
                await self._enrich_source(job, session)
                if job.provider_job.remote_id is not None:
                    if job.delivery_files:
                        self._transition(session, job, JobState.DELIVERING.value)
                        await self._deliver(job, session)
                    elif job.state == JobState.PROVIDER_READY.value:
                        await self._prepare_delivery(job, session)
                    else:
                        await self._poll_remote(job, session)
                elif job.provider_job.queued_id is not None:
                    await self._poll_queued(job, session)
                else:
                    await self._submit(job, session)
                job.poll_failures = 0
                job.error_code = job.error_message = None
                await session.execute(update(FailureEvent).where(
                    FailureEvent.job_id == job.id,
                    FailureEvent.stage == failure_stage,
                    FailureEvent.resolved_at.is_(None),
                ).values(resolved_at=_now()))
            except ProviderError as error:
                job.poll_failures += 1
                job.error_code, job.error_message = error.code, str(error)[:512]
                if (failure_stage == "submission"
                        and error.code in {"RATE_LIMITED", "AUTHENTICATION_FAILED",
                                           "REQUEST_REJECTED"}):
                    # These responses prove TorBox rejected the create. A
                    # network/5xx failure stays ambiguous and is reconciled.
                    job.provider_job.provider_state = "submit_retry"
                if error.transient and job.poll_failures < self.max_job_failures:
                    self._transition(session, job, JobState.RETRY_WAIT.value,
                                     outcome="retry_scheduled", code=error.code,
                                     detail=str(error))
                    delay = getattr(error, "retry_after", None) or min(
                        self.poll_interval * (2 ** (job.poll_failures - 1)), self.max_backoff)
                    job.next_poll_at = _now() + timedelta(seconds=delay)
                else:
                    self._transition(session, job, JobState.FAILED.value,
                                     outcome="terminal_failure", code=error.code,
                                     detail=str(error))
                    job.next_poll_at = None
                self._record_failure(session, job, failure_stage, error.code,
                                     str(error), error.transient)
            except DownloadError as error:
                job.poll_failures += 1
                job.error_code, job.error_message = "DOWNLOAD_FAILED", str(error)[:512]
                if job.poll_failures < self.max_job_failures:
                    self._transition(session, job, JobState.RETRY_WAIT.value,
                                     outcome="retry_scheduled", code="DOWNLOAD_FAILED",
                                     detail=str(error))
                    job.next_poll_at = _now() + timedelta(seconds=min(
                        self.poll_interval * (2 ** (job.poll_failures - 1)), self.max_backoff))
                else:
                    self._transition(session, job, JobState.FAILED.value,
                                     outcome="retry_exhausted", code="DOWNLOAD_FAILED",
                                     detail=str(error))
                    job.next_poll_at = None
                self._record_failure(session, job, failure_stage, "DOWNLOAD_FAILED",
                                     str(error), True)
            except (OSError, ValueError) as error:
                job.poll_failures += 1
                job.error_code, job.error_message = "DELIVERY_FAILED", str(error)[:512]
                self._transition(session, job, JobState.FAILED.value,
                                 outcome="terminal_failure", code="DELIVERY_FAILED",
                                 detail=str(error))
                job.next_poll_at = None
                self._record_failure(session, job, failure_stage, "DELIVERY_FAILED",
                                     str(error), False)
            await session.commit()

    async def _enrich_source(self, job: Job, session, force: bool = False) -> None:
        if self.source_resolver is None or job.source_indexer is not None:
            return
        now = _now()
        if (not force and job.source_metadata_checked_at is not None
                and now - _aware(job.source_metadata_checked_at) < timedelta(seconds=15)):
            return
        metadata = await self.source_resolver.resolve(
            job.category.name if job.category else None, job.info_hash)
        job.source_service = metadata.service
        job.source_metadata_checked_at = now
        if metadata.indexer:
            job.source_indexer = metadata.indexer
            job.source_indexer_id = metadata.indexer_id
            await session.execute(update(FailureEvent).where(
                FailureEvent.job_id == job.id,
                FailureEvent.indexer == "Unknown",
            ).values(indexer=metadata.indexer))
            await session.execute(update(ControlEvent).where(
                ControlEvent.job_id == job.id,
                ControlEvent.indexer == "Unknown",
            ).values(indexer=metadata.indexer, service=metadata.service))
            await session.execute(update(LifecycleEvent).where(
                LifecycleEvent.job_id == job.id,
                LifecycleEvent.indexer == "Unknown",
            ).values(indexer=metadata.indexer, indexer_id=metadata.indexer_id,
                     service=metadata.service))

    def _record_failure(self, session, job: Job, stage: str, code: str,
                        message: str, transient: bool) -> None:
        safe_message = message[:512]
        event = FailureEvent(
            job_id=job.id,
            info_hash=job.info_hash,
            name=job.name or job.info_hash,
            category=job.category.name if job.category else "",
            service=job.source_service,
            indexer=job.source_indexer or "Unknown",
            stage=stage,
            error_code=code[:64],
            error_message=safe_message,
            transient=transient,
            attempt=job.poll_failures,
            bytes_downloaded=sum(item.downloaded for item in
                                 job.__dict__.get("delivery_files", [])),
            occurred_at=_now(),
        )
        session.add(event)
        logger.warning(
            "transfer_failed info_hash=%s service=%s indexer=%r stage=%s code=%s "
            "attempt=%d transient=%s bytes=%d message=%r",
            job.info_hash, event.service, event.indexer, event.stage, event.error_code,
            event.attempt, event.transient, event.bytes_downloaded, safe_message,
        )

    def _lifecycle(self, session, job: Job, event_type: str, *, event_key: str,
                   from_phase: str | None = None, to_phase: str | None = None,
                   outcome: str = "recorded", code: str | None = None,
                   detail: str | None = None, duration: float | None = None,
                   occurred_at: datetime | None = None,
                   partial_history: bool = False) -> None:
        safe_detail = _safe_detail(detail)
        session.add(LifecycleEvent(
            event_key=event_key[:128], job_id=job.id,
            generation=job.transition_generation, info_hash=job.info_hash,
            name=job.name or job.info_hash,
            category=job.category.name if job.category else "",
            service=job.source_service, indexer=job.source_indexer or "Unknown",
            indexer_id=job.source_indexer_id,
            provider=job.provider_job.provider if job.provider_job else "torbox",
            event_type=event_type[:32], from_phase=from_phase, to_phase=to_phase,
            outcome=outcome[:32], code=code[:64] if code else None,
            detail=safe_detail, progress=min(max(job.progress, 0), 1),
            bytes_downloaded=sum(item.downloaded for item in
                                 job.__dict__.get("delivery_files", [])),
            duration_seconds=max(duration, 0) if duration is not None else None,
            partial_history=partial_history, occurred_at=occurred_at or _now(),
            recorded_at=_now()))

    def _transition(self, session, job: Job, new_state: str, *,
                    outcome: str = "entered", code: str | None = None,
                    detail: str | None = None) -> bool:
        if job.state == new_state:
            return False
        now = _now()
        duration = ((now - _aware(job.phase_started_at)).total_seconds()
                    if job.phase_started_at is not None else None)
        old_state = job.state
        job.transition_generation += 1
        job.state, job.phase_started_at = new_state, now
        self._lifecycle(
            session, job, "phase_transition",
            event_key=f"transition:{job.id}:{job.transition_generation}",
            from_phase=old_state, to_phase=new_state, outcome=outcome,
            code=code, detail=detail, duration=duration, occurred_at=now,
            partial_history=duration is None)
        return True

    async def _submit(self, job: Job, session) -> None:
        # Persist intent before the provider side effect. If the process dies
        # after TorBox accepts but before its ID is committed, restart can bind
        # the existing remote object by info hash instead of submitting twice.
        if job.provider_job.provider_state == "submitting":
            existing = await self.provider.find_torrent(job.info_hash)
            if existing is not None:
                job.provider_job.remote_id = existing.remote_id
                self._apply_torrent(job, existing, session)
                return
            queued = next((item for item in await self.provider.get_queued()
                           if item.info_hash == job.info_hash), None)
            if queued is not None:
                job.provider_job.remote_id = queued.remote_id
                job.provider_job.queued_id = queued.queued_id
                self._transition(session, job, (JobState.PROVIDER_DOWNLOADING.value
                                 if queued.remote_id is not None
                                 else JobState.PROVIDER_QUEUED.value))
                job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)
                return
        else:
            job.provider_job.provider_state = "submitting"
            await session.commit()
        if job.source_kind == "torrent":
            if not job.source_data:
                raise ValueError("persisted torrent source is missing")
            submission = await self.provider.create_torrent(
                job.source_data, job.source_uri, job.info_hash)
        else:
            submission = await self.provider.create_magnet(job.source_uri)
        job.provider_job.remote_id = submission.remote_id
        job.provider_job.queued_id = submission.queued_id
        job.provider_job.provider_state = "submitted"
        self._transition(session, job, (JobState.PROVIDER_QUEUED.value
                         if submission.remote_id is None
                         else JobState.PROVIDER_DOWNLOADING.value))
        job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)

    async def _poll_queued(self, job: Job, session) -> None:
        queued = await self.provider.get_queued()
        match = next((item for item in queued if item.queued_id == job.provider_job.queued_id
                      or item.info_hash == job.info_hash), None)
        if match and match.remote_id is not None:
            job.provider_job.remote_id = match.remote_id
            self._transition(session, job, JobState.PROVIDER_DOWNLOADING.value)
            job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)
        else:
            torrent = await self.provider.find_torrent(job.info_hash)
            if torrent is not None:
                job.provider_job.remote_id = torrent.remote_id
                self._apply_torrent(job, torrent, session)
            else:
                self._transition(session, job, JobState.PROVIDER_QUEUED.value)
                job.next_poll_at = _now() + timedelta(seconds=self.queued_poll_interval)

    async def _poll_remote(self, job: Job, session) -> None:
        torrent = await self.provider.get_torrent(job.provider_job.remote_id)
        self._apply_torrent(job, torrent, session)

    def _apply_torrent(self, job: Job, torrent, session) -> None:
        job.name, job.size = torrent.name, torrent.size
        job.progress, job.download_speed, job.eta = torrent.progress, torrent.download_speed, torrent.eta
        job.provider_job.provider_state = torrent.state
        job.provider_job.last_polled_at = _now()
        job.provider_job.payload = json.dumps({"download_present": torrent.download_present})
        if torrent.download_finished and torrent.download_present:
            self._transition(session, job, JobState.PROVIDER_READY.value)
            job.progress, job.next_poll_at = 0.0, _now()
        else:
            self._transition(session, job, JobState.PROVIDER_DOWNLOADING.value)
            job.next_poll_at = _now() + timedelta(seconds=self.poll_interval)

    async def _prepare_delivery(self, job: Job, session) -> None:
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
        self._transition(session, job, JobState.DELIVERING.value)
        job.next_poll_at = _now()

    async def _deliver(self, job: Job, session) -> None:
        await self._enrich_source(job, session, force=True)
        base = Path(job.category.save_path) if job.category else self.download_path
        total = sum(item.size for item in job.delivery_files)
        for item in job.delivery_files:
            destination = _safe_destination(base, item.relative_path)
            receipt = destination.with_name(destination.name + ".downloadarr.receipt.json")
            result = None
            if destination.exists():
                if (destination.is_file() and destination.stat().st_size == item.size
                        and item.state == "completed"):
                    item.downloaded, item.state, item.error_message = item.size, "completed", None
                    continue
                if destination.is_file() and destination.stat().st_size == item.size:
                    result = _receipt_result(receipt, destination, item.size)
                    if result is not None:
                        item.downloaded, item.state, item.error_message = item.size, "completed", None
                    else:
                        raise ValueError(
                            f"unverified delivery destination already exists: {item.relative_path}")
                else:
                    raise ValueError(f"delivery destination already exists: {item.relative_path}")
            if result is None:
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

            transfer_started_at = _now()
            if result is None:
                result = await self.downloader.download(url_provider, destination, progress)
            if result.byte_count != item.size:
                raise ValueError(f"downloaded size mismatch for {item.relative_path}")
            item.downloaded, item.state = result.byte_count, "completed"
            completed = sum(value.downloaded for value in job.delivery_files)
            job.progress = completed / total if total else 1.0
            job.download_speed = int(result.average_speed)
            session.add(TransferHistory(
                job_id=job.id,
                provider_file_id=item.provider_file_id,
                info_hash=job.info_hash,
                name=job.name or job.info_hash,
                category=job.category.name if job.category else "",
                relative_path=item.relative_path,
                provider=job.provider_job.provider,
                remote_id=job.provider_job.remote_id,
                status="completed",
                service=job.source_service,
                indexer=job.source_indexer or "Unknown",
                indexer_id=job.source_indexer_id,
                total_bytes=result.byte_count,
                transferred_bytes=(result.session_byte_count
                                   if result.session_byte_count is not None
                                   else result.byte_count),
                elapsed=result.elapsed,
                average_speed=int(result.average_speed),
                peak_speed=int(result.peak_speed),
                connections=result.connections,
                used_ranges=result.used_ranges,
                range_requests=result.range_requests,
                retry_count=result.retry_count,
                resumed=result.resumed,
                cdn_host=result.cdn_host,
                started_at=transfer_started_at,
                completed_at=_now(),
            ))
            await session.commit()
            receipt.unlink(missing_ok=True)
            logger.info(
                "transfer_completed info_hash=%s service=%s indexer=%r file=%r "
                "bytes=%d transferred_bytes=%d "
                "elapsed=%.3f average_bps=%d peak_bps=%d connections=%d ranges=%s "
                "range_requests=%d retries=%d resumed=%s cdn=%s",
                job.info_hash, job.source_service, job.source_indexer or "Unknown",
                item.relative_path, result.byte_count,
                result.session_byte_count if result.session_byte_count is not None
                else result.byte_count,
                result.elapsed, int(result.average_speed), int(result.peak_speed),
                result.connections, result.used_ranges, result.range_requests,
                result.retry_count, result.resumed, result.cdn_host or "unknown",
            )
        self._transition(session, job, JobState.COMPLETED.value,
                         outcome="awaiting_client_cleanup",
                         detail="Local delivery complete; Arr import is not yet verified")
        job.progress = 1.0
        job.download_speed, job.eta = 0, 0
        job.completed_at, job.next_poll_at = _now(), None

    async def pause(self, hashes: list[str], actor: str = "qbittorrent") -> None:
        await self._control(hashes, "pause", actor)

    async def resume(self, hashes: list[str], actor: str = "qbittorrent") -> None:
        await self._control(hashes, "resume", actor)

    async def retry(self, hashes: list[str], actor: str = "dashboard") -> None:
        await self._control(hashes, "retry", actor)

    async def _control(self, hashes: list[str], command: str, actor: str) -> None:
        normalized = [value.lower() for value in hashes]
        async with self.database.session() as session:
            jobs = list((await session.scalars(select(Job).where(
                Job.info_hash.in_(normalized)))).unique().all())
            job_ids = [job.id for job in jobs]
        await self._gate_and_stop(job_ids)
        try:
            remote_actions: list[tuple[str, int]] = []
            async with self.database.session() as session:
                jobs = list((await session.scalars(select(Job).where(
                    Job.id.in_(job_ids)))).unique().all())
                for job in jobs:
                    before = job.control_state
                    detail = None
                    if job.control_state == ControlState.REMOVING.value:
                        self._audit(session, job, command, actor, before, before,
                                    "ignored", "removal is already in progress")
                        continue
                    if job.state == JobState.COMPLETED.value:
                        self._audit(session, job, command, actor, before, before,
                                    "noop", "completed jobs remain importable")
                        continue
                    if command == "pause":
                        job.control_state = ControlState.PAUSED.value
                        job.paused_at = _now()
                        job.download_speed, job.eta = 0, None
                        job.control_scope, job.control_error = "local", None
                        if (job.provider_job.remote_id is not None
                                and job.state == JobState.PROVIDER_DOWNLOADING.value):
                            remote_actions.append((job.id, job.provider_job.remote_id))
                    elif command == "resume":
                        if job.control_state != ControlState.PAUSED.value:
                            self._audit(session, job, command, actor, before, before,
                                        "noop", "job is already running")
                            continue
                        job.control_state = ControlState.RUNNING.value
                        job.paused_at, job.control_error = None, None
                        job.next_poll_at = _now()
                        if (job.provider_job.remote_id is not None
                                and job.control_scope == "local_and_provider"):
                            remote_actions.append((job.id, job.provider_job.remote_id))
                        job.control_scope = None
                    elif command == "retry":
                        if job.state not in (JobState.FAILED.value, JobState.RETRY_WAIT.value):
                            self._audit(session, job, command, actor, before, before,
                                        "noop", "job is not failed")
                            continue
                        job.control_state = ControlState.RUNNING.value
                        job.poll_failures = 0
                        job.error_code = job.error_message = None
                        self._transition(session, job, _retry_state(job),
                                         outcome="manual_retry")
                        job.next_poll_at = _now()
                    self._audit(session, job, command, actor, before, job.control_state,
                                "accepted", detail)
                    self._lifecycle(
                        session, job, f"control_{command}",
                        event_key=f"control:{command}:{job.id}:{uuid.uuid4().hex}",
                        from_phase=job.state, to_phase=job.state, outcome="accepted",
                        detail=("Local control; provider scope is reported separately"
                                if command in {"pause", "resume"} else None))
                await session.commit()
            for job_id, remote_id in remote_actions:
                await self._provider_control(job_id, remote_id, command)
        finally:
            async with self._task_lock:
                self._controlling.difference_update(job_ids)

    async def _provider_control(self, job_id: str, remote_id: int, command: str) -> None:
        try:
            if command == "pause":
                await self.provider.pause_torrent(remote_id)
            else:
                await self.provider.resume_torrent(remote_id)
        except ProviderError as error:
            async with self.database.session() as session:
                job = await session.get(Job, job_id)
                if job is not None:
                    job.control_error = str(error)[:512]
                    if command == "resume":
                        job.control_state = ControlState.PAUSED.value
                    self._audit(session, job, command, "system", job.control_state,
                                job.control_state, "provider_warning", str(error))
                    await session.commit()
        else:
            async with self.database.session() as session:
                job = await session.get(Job, job_id)
                if job is not None:
                    job.control_scope = ("local_and_provider" if command == "pause" else None)
                    job.control_error = None
                    await session.commit()

    def _audit(self, session, job: Job, command: str, actor: str, before: str,
               after: str, outcome: str, detail: str | None = None) -> None:
        session.add(ControlEvent(
            job_id=job.id, info_hash=job.info_hash, service=job.source_service,
            indexer=job.source_indexer or "Unknown", command=command[:32],
            actor=actor[:32], from_state=before[:32], to_state=after[:32],
            outcome=outcome[:32], detail=(detail[:512] if detail else None),
            occurred_at=_now()))

    async def remove(self, hashes: list[str], delete_files: bool,
                     actor: str = "qbittorrent") -> None:
        normalized = [value.lower() for value in hashes]
        async with self.database.session() as session:
            jobs = list((await session.scalars(select(Job).where(
                Job.info_hash.in_(normalized)))).unique().all())
            job_ids = [job.id for job in jobs]
        await self._gate_and_stop(job_ids)
        try:
            async with self.database.session() as session:
                jobs = list((await session.scalars(select(Job).where(
                    Job.id.in_(job_ids)))).unique().all())
                for job in jobs:
                    before = job.control_state
                    job.control_state = ControlState.REMOVING.value
                    job.remove_delete_files = bool(job.remove_delete_files or delete_files)
                    job.next_poll_at = _now()
                    self._audit(session, job, "remove", actor, before,
                                ControlState.REMOVING.value, "accepted")
                    if before != ControlState.REMOVING.value:
                        self._lifecycle(
                            session, job, "cleanup_requested",
                            event_key=f"cleanup_requested:{job.id}",
                            from_phase=job.state, to_phase=job.state,
                            outcome="client_cleanup_requested",
                            detail="qBittorrent client requested cleanup; Arr import is unverified")
                await session.commit()
            async with self._task_lock:
                self._controlling.difference_update(job_ids)
            for job_id in job_ids:
                await self.process(job_id)
        finally:
            async with self._task_lock:
                self._controlling.difference_update(job_ids)

    async def _gate_and_stop(self, job_ids: list[str]) -> None:
        current = asyncio.current_task()
        async with self._task_lock:
            self._controlling.update(job_ids)
            tasks = [self._active_tasks[job_id] for job_id in job_ids
                     if job_id in self._active_tasks and self._active_tasks[job_id] is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup_removing(self, job: Job, session) -> None:
        if not job.remote_cleanup_done:
            self._lifecycle(session, job, "remote_cleanup_attempt",
                            event_key=f"remote_cleanup_attempt:{job.id}:{uuid.uuid4().hex}",
                            from_phase=job.state, to_phase=job.state,
                            outcome="attempted")
            await session.commit()
            if job.provider_job.remote_id is not None:
                await self.provider.delete_torrent(job.provider_job.remote_id)
            elif job.provider_job.queued_id is not None:
                await self.provider.delete_queued(job.provider_job.queued_id)
            job.remote_cleanup_done = True
            self._lifecycle(session, job, "remote_cleanup_succeeded",
                            event_key=f"remote_cleanup_succeeded:{job.id}",
                            from_phase=job.state, to_phase=job.state,
                            outcome="succeeded")
            await session.commit()
        if job.remove_delete_files and not job.local_cleanup_done:
            self._lifecycle(session, job, "local_cleanup_attempt",
                            event_key=f"local_cleanup_attempt:{job.id}:{uuid.uuid4().hex}",
                            from_phase=job.state, to_phase=job.state,
                            outcome="attempted")
            await session.commit()
            base = Path(job.category.save_path) if job.category else self.download_path
            for item in job.delivery_files:
                destination = _safe_destination(base, item.relative_path)
                destination.unlink(missing_ok=True)
                destination.with_name(destination.name + ".downloadarr.part").unlink(missing_ok=True)
                destination.with_name(destination.name + ".downloadarr.json").unlink(missing_ok=True)
                destination.with_name(destination.name + ".downloadarr.receipt.json").unlink(missing_ok=True)
                _remove_empty_parents(destination.parent, base.resolve())
            job.local_cleanup_done = True
            self._lifecycle(session, job, "local_cleanup_succeeded",
                            event_key=f"local_cleanup_succeeded:{job.id}",
                            from_phase=job.state, to_phase=job.state,
                            outcome="succeeded")
            await session.commit()
        self._lifecycle(session, job, "job_removed",
                        event_key=f"job_removed:{job.id}", from_phase=job.state,
                        to_phase=None, outcome="cleanup_completed",
                        detail="Operational job removed after client cleanup request")
        await session.delete(job)
        await session.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _failure_stage(state: str) -> str:
    return {
        JobState.SUBMITTED.value: "submission",
        JobState.PROVIDER_QUEUED.value: "provider_queue",
        JobState.PROVIDER_DOWNLOADING.value: "provider",
        JobState.PROVIDER_READY.value: "delivery_setup",
        JobState.DELIVERING.value: "delivery",
        JobState.RETRY_WAIT.value: "retry",
    }.get(state, "unknown")


def _retry_state(job: Job) -> str:
    if job.delivery_files:
        return JobState.DELIVERING.value
    if job.provider_job.remote_id is not None:
        return JobState.PROVIDER_DOWNLOADING.value
    if job.provider_job.queued_id is not None:
        return JobState.PROVIDER_QUEUED.value
    return JobState.SUBMITTED.value


def _receipt_result(path: Path, destination: Path, expected_size: int) -> DownloadResult | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("version") != 1 or int(value.get("size", -1)) != expected_size
                or destination.stat().st_size != expected_size):
            return None
        return DownloadResult(
            path=destination, byte_count=expected_size,
            elapsed=max(float(value.get("elapsed", 0)), 0.000001),
            average_speed=max(float(value.get("average_speed", 0)), 0),
            resumed=bool(value.get("resumed")),
            used_ranges=bool(value.get("used_ranges")),
            session_byte_count=max(0, min(int(value.get("session_bytes", expected_size)),
                                          expected_size)),
            cdn_host=str(value.get("cdn_host"))[:255] if value.get("cdn_host") else None,
            range_requests=max(0, int(value.get("range_requests", 0))),
            retry_count=max(0, int(value.get("retry_count", 0))),
            peak_speed=max(float(value.get("peak_speed", 0)), 0),
            connections=max(1, int(value.get("connections", 1))),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _safe_detail(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if ("http://" in lowered or "https://" in lowered or "token=" in lowered
            or "authorization" in lowered or "api_key" in lowered):
        return "Sensitive detail was redacted"
    return value[:512]


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
