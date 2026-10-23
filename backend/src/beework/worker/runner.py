import logging
import os
import socket
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from beework.db import SessionLocal
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
