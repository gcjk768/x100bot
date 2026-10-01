"""One exclusive file lock per job in data/locks/. A lock older than stale_after_s is stale and broken.
Adapted from pddbot/lock.py."""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
STALE_AFTER_S = {"sources": 3 * 3600, "plan": 3600, "post": 1800, "firmware": 1800, "seed": 12 * 3600}


class LockBusy(Exception):
    pass


@contextmanager
def job_lock(data_dir: str | Path, job: str, stale_after_s: float | None = None):
    path = Path(data_dir) / "locks" / f"{job}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = stale_after_s or STALE_AFTER_S.get(job, 3600)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if attempt == 1 and age > stale:
                log.warning("breaking stale %s lock, %.0f minutes old", job, age / 60)
                path.unlink(missing_ok=True)
                continue
            raise LockBusy(f"the {job} job is already running (lock {path}, {age / 60:.0f} minutes old)")
    else:
        raise LockBusy(f"could not take {path}")
    os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
    os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
