from uuid import UUID

from sqlalchemy.orm import Session

from beework.runtime.executor import execute_run
from beework.worker.queue import job_handler


@job_handler("runtime.execute")
def execute_run_job(db: Session, payload: dict) -> None:
    """Worker entry point. Model failures become failed runs, so the job itself is not retried."""
    execute_run(db, UUID(payload["run_id"]))
