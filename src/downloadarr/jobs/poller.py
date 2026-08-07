import asyncio
import logging
from contextlib import suppress

from .service import JobService

logger = logging.getLogger(__name__)


class JobPoller:
    def __init__(self, service: JobService, concurrency: int = 4) -> None:
        self.service = service
        self._semaphore = asyncio.Semaphore(concurrency)
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
        while not self._stop.is_set():
            ids = await self.service.due_job_ids()
            if ids:
                async with asyncio.TaskGroup() as group:
                    for job_id in ids:
                        group.create_task(self._process(job_id))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def _process(self, job_id: str) -> None:
        async with self._semaphore:
            try:
                await self.service.process(job_id)
            except Exception:
                logger.exception("Unexpected error while processing job %s", job_id)
