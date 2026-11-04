import logging
import os
import socket
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from beework import (
    approvals as _approvals,  # noqa: F401  (registers approvals.apply / approvals.sweep)
)
from beework import (
    assistant_publish as _assistant_publish,  # noqa: F401  (registers the assistant.publish approval action)
)
from beework import (
    channel_publish as _channel_publish,  # noqa: F401  (registers the channel.publish approval action)
)
from beework import (
    gateway_sync as _gateway_sync,  # noqa: F401  (registers the gateway.* jobs)
)
from beework import (
    memory_jobs as _memory_jobs,  # noqa: F401  (registers memory.* jobs and the conversation-ended hook)
)
from beework import (
    notifications as _notifications,  # noqa: F401  (registers the approval notifier)
)
from beework import (
    tenant_lifecycle as _tenant_lifecycle,  # noqa: F401  (registers tenants.auto_lifecycle and the gateway access hooks)
)
from beework import (
    test_cases as _test_cases,  # noqa: F401  (registers the testcases.run job)
)
from beework.db import SessionLocal
from beework.runtime import jobs as _runtime_jobs  # noqa: F401  (registers runtime.execute)
from beework.worker.queue import claim_jobs, execute_job, reclaim_expired_jobs
from beework.worker.scheduler import enqueue_scheduled_jobs

logger = logging.getLogger(__name__)

SCHEDULE_INTERVAL_SECONDS = 60.0
MAX_ERROR_BACKOFF_SECONDS = 30.0


def run_once(
    worker_id: str,
    session_factory: Callable[[], Session] = SessionLocal,
    *,
    schedule: bool = True,
    batch_size: int = 10,
) -> int:
    """Reclaim stale locks, enqueue due scheduled jobs, then claim and run one batch."""
    with session_factory() as db:
        reclaim_expired_jobs(db)
        if schedule:
            enqueue_scheduled_jobs(db)
        job_ids = [job.id for job in claim_jobs(db, worker_id, limit=batch_size)]
        db.commit()
    for job_id in job_ids:
        with session_factory() as db:
            execute_job(db, job_id)
    return len(job_ids)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    worker_id = os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
    poll_seconds = float(os.getenv("WORKER_POLL_SECONDS", "1"))
    last_schedule = float("-inf")
    failures = 0
    while True:
        try:
            schedule = time.monotonic() - last_schedule >= SCHEDULE_INTERVAL_SECONDS
            claimed = run_once(worker_id, schedule=schedule)
            if schedule:
                last_schedule = time.monotonic()
            failures = 0
            if claimed:
                continue  # drain the queue before sleeping
        except Exception:
            # A database or scheduling hiccup must not kill the worker process.
            failures += 1
            logger.exception("Worker loop failed (%s consecutive)", failures)
            time.sleep(min(poll_seconds * 2**failures, MAX_ERROR_BACKOFF_SECONDS))
            continue
        time.sleep(poll_seconds)
