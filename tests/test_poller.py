import asyncio

from downloadarr.jobs.poller import JobPoller


class BlockingJobService:
    def __init__(self) -> None:
        self.due: list[str] = []
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.active: set[str] = set()
        self.maximum_active = 0

    def add(self, job_id: str) -> None:
        self.due.append(job_id)
        self.started[job_id] = asyncio.Event()
        self.release[job_id] = asyncio.Event()

    async def due_job_ids(self, limit: int = 100) -> list[str]:
        return self.due[:limit]

    async def process(self, job_id: str) -> None:
        self.active.add(job_id)
        self.maximum_active = max(self.maximum_active, len(self.active))
        self.started[job_id].set()
        try:
            await self.release[job_id].wait()
        finally:
            self.active.discard(job_id)
            if job_id in self.due:
                self.due.remove(job_id)


async def test_poller_admits_job_added_during_long_running_transfer():
    service = BlockingJobService()
    service.add("first")
    poller = JobPoller(service, concurrency=2)
    poller.start()
    try:
        await asyncio.wait_for(service.started["first"].wait(), 2)
        service.add("second")
        await asyncio.wait_for(service.started["second"].wait(), 2)
        assert service.active == {"first", "second"}
        assert service.maximum_active == 2
    finally:
        await poller.stop()


async def test_poller_concurrency_can_increase_without_restart():
    service = BlockingJobService()
    service.add("first")
    service.add("second")
    poller = JobPoller(service, concurrency=1)
    poller.start()
    try:
        await asyncio.wait_for(service.started["first"].wait(), 2)
        await asyncio.sleep(0.6)
        assert not service.started["second"].is_set()
        poller.set_concurrency(2)
        await asyncio.wait_for(service.started["second"].wait(), 2)
        assert poller.concurrency == 2
        assert poller.active_workers == 2
    finally:
        await poller.stop()


async def test_lowering_concurrency_does_not_cancel_active_jobs():
    service = BlockingJobService()
    service.add("first")
    service.add("second")
    service.add("third")
    poller = JobPoller(service, concurrency=2)
    poller.start()
    try:
        await asyncio.wait_for(service.started["first"].wait(), 2)
        await asyncio.wait_for(service.started["second"].wait(), 2)
        poller.set_concurrency(1)
        await asyncio.sleep(0.6)
        assert service.active == {"first", "second"}
        assert not service.started["third"].is_set()
        service.release["first"].set()
        service.release["second"].set()
        await asyncio.wait_for(service.started["third"].wait(), 2)
    finally:
        await poller.stop()
