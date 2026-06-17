"""Task-queue worker entrypoint (§3 Jobs, §13.4).

Runs execute out-of-band from the HTTP request via RQ + Redis. `enqueue_run` is called by
the API; `run_job` is the function the worker executes.

Start a worker with:
  rq worker deepdd --url $REDIS_URL
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

# macOS aborts (signal 6) when an Objective-C runtime initialized in the parent is used
# after fork(). Set this before anything forks so the RQ work-horse survives.
os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from redis import Redis
from rq import Queue

from app.core.config import get_settings

QUEUE_NAME = "deepdd"


def get_queue() -> Queue:
    settings = get_settings()
    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def run_job(run_id: str) -> None:
    """Executed inside the RQ worker process."""
    from workflow.runner import execute_run

    execute_run(run_id)


def enqueue_run(run_id: str) -> str:
    """Called by the API to schedule a run; returns the job id."""
    q = get_queue()
    job = q.enqueue(run_job, run_id, job_timeout=3600, result_ttl=86400)
    return job.id


if __name__ == "__main__":
    # Allow `python worker.py` to start an RQ worker directly.
    # On macOS, use SimpleWorker (runs jobs in-process, no fork) to avoid the
    # Objective-C-after-fork abort. Override with DEEPDD_WORKER_CLASS=fork.
    from rq import SimpleWorker, Worker

    settings = get_settings()
    conn = Redis.from_url(settings.redis_url)

    # Recover runs that were mid-flight when a previous worker died (network failure /
    # crash): mark them failed so they can be resumed, instead of hanging forever.
    # A Redis lock ensures only ONE worker reconciles at startup (when running a pool of
    # workers for parallel runs), so it can't race another worker's in-flight job.
    try:
        if conn.set("deepdd:reconcile:lock", "1", nx=True, ex=20):
            from workflow.runner import reconcile_orphaned_runs

            reconcile_orphaned_runs()
    except Exception as e:  # noqa: BLE001
        print(f"[worker] reconcile skipped: {e}")

    force = os.getenv("DEEPDD_WORKER_CLASS", "").lower()
    if force == "fork":
        worker_cls = Worker
    elif force == "simple" or platform.system() == "Darwin":
        worker_cls = SimpleWorker
    else:
        worker_cls = Worker
    print(f"[worker] starting {worker_cls.__name__} on queue '{QUEUE_NAME}'")
    worker_cls([QUEUE_NAME], connection=conn).work(with_scheduler=True)
