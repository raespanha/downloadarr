import asyncio
import logging
from contextlib import suppress

from .service import JobService

logger = logging.getLogger(__name__)


class JobPoller:
    def __init__(self, service: JobService, concurrency: int = 4) -> None:
        if not 1 <= concurrency <= 64:
            raise ValueError("concurrency must be between 1 and 64")
        self.service = service
        self._concurrency = concurrency
        self._workers: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="downloadarr-job-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                self._reap_workers()
                available = max(0, self._concurrency - len(self._workers))
                if available:
                    # Ask for extra candidates because due_job_ids may include
                    # a job already owned by this poller while it checkpoints.
                    ids = await self.service.due_job_ids(
                        limit=max(self._concurrency * 2, available))
                    for job_id in ids:
                        if job_id in self._workers:
                            continue
                        self._workers[job_id] = asyncio.create_task(
                            self._process(job_id), name=f"downloadarr-job-{job_id}")
                        available -= 1
                        if not available:
                            break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            workers = list(self._workers.values())
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            self._workers.clear()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done() and not self._stop.is_set()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def active_workers(self) -> int:
        self._reap_workers()
        return len(self._workers)

    def set_concurrency(self, concurrency: int) -> None:
        if not 1 <= concurrency <= 64:
            raise ValueError("concurrency must be between 1 and 64")
        self._concurrency = concurrency

    def _reap_workers(self) -> None:
        self._workers = {job_id: worker for job_id, worker in self._workers.items()
                         if not worker.done()}

    async def _process(self, job_id: str) -> None:
        try:
            await self.service.process(job_id)
        except Exception:
            logger.exception("Unexpected error while processing job %s", job_id)
