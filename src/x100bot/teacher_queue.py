"""The teacher's persistent job queue in SQLite: one job at a time, daily caps per kind through the limiter, a
not_before time for Claude usage limits, and a pause while the 05:30 plan job holds its lock."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .db import tx
from .ratelimit import BudgetExhausted, InCooldown

log = logging.getLogger(__name__)
BUCKET = {"critique": "teacher_critique", "compare": "teacher_critique", "series": "teacher_series",
          "followup": "teacher_followup", "question": "teacher_question", "plan": "teacher_question"}


class QueueFull(Exception):
    pass


def enqueue(x, kind: str, payload: dict) -> tuple[int, int]:
    """Add a job. Returns (job id, how many jobs are ahead of it). Raises QueueFull at queue_max."""
    pending = x.conn.execute("SELECT COUNT(*) FROM teacher_queue WHERE status IN ('pending','running')").fetchone()[0]
    if pending >= x.s.limits.teacher.queue_max:
        raise QueueFull(f"the teacher queue already holds {pending} jobs")
    cur = x.conn.execute("INSERT INTO teacher_queue(kind, payload_json, not_before, attempts, status, created_at) "
                         "VALUES(?,?,?,0,'pending',?)", (kind, json.dumps(payload), None, x.lim.clock()))
    return cur.lastrowid, pending


def plan_running(data_dir: Path) -> bool:
    return (Path(data_dir) / "locks" / "plan.lock").exists()


STALE_RUNNING_S = 20 * 60


def recover_stale(x) -> int:
    """A job left 'running' by a crashed or restarted process goes back to pending after STALE_RUNNING_S."""
    cur = x.conn.execute("UPDATE teacher_queue SET status='pending' WHERE status='running' AND attempts<3 AND "
                         "created_at<?", (x.lim.clock() - STALE_RUNNING_S,))
    return cur.rowcount


def claim(x) -> dict | None:
    """The oldest due job, moved to running inside a transaction so two drains never take the same job."""
    with tx(x.conn):
        row = x.conn.execute("SELECT * FROM teacher_queue WHERE status='pending' AND (not_before IS NULL OR not_before<=?)"
                             " ORDER BY id LIMIT 1", (x.lim.clock(),)).fetchone()
        if not row:
            return None
        x.conn.execute("UPDATE teacher_queue SET status='running', attempts=attempts+1 WHERE id=?", (row["id"],))
    return dict(row)


def finish(x, job_id: int, status: str = "done") -> None:
    x.conn.execute("UPDATE teacher_queue SET status=? WHERE id=?", (status, job_id))


def requeue(x, job_id: int, not_before: float | None) -> None:
    x.conn.execute("UPDATE teacher_queue SET status='pending', not_before=? WHERE id=?", (not_before, job_id))


def can_run(x, kind: str) -> str | None:
    """None when the kind's daily cap has room, else the reason."""
    try:
        x.lim.check(BUCKET[kind])
    except (BudgetExhausted, InCooldown) as ex:
        return str(ex)
    return None


def drain(x, handler, max_jobs: int = 5) -> int:
    """Run due jobs one by one. handler(job) returns 'done', 'failed' or a not_before timestamp to requeue.
    Pauses while the plan job holds its lock (the 05:30 plan has priority)."""
    done = 0
    recover_stale(x)
    for _ in range(max_jobs):
        if plan_running(x.s.data_dir):
            log.info("teacher queue paused: plan job running")
            break
        job = claim(x)
        if not job:
            break
        try:
            out = handler(job)
        except Exception:   # noqa: BLE001 - a crash in one job must not stop the queue
            log.exception("teacher job %s crashed", job["id"])
            out = "failed" if job["attempts"] >= 3 else x.lim.clock() + 600
        if out == "done":
            finish(x, job["id"], "done")
            done += 1
        elif out == "failed":
            finish(x, job["id"], "failed")
        else:
            requeue(x, job["id"], float(out))
    return done


def status(x) -> dict:
    rows = x.conn.execute("SELECT status, COUNT(*) n FROM teacher_queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def add_jobs(sched, s) -> None:
    """Scheduler hooks: drain every 10 minutes (the listener also drains right after enqueueing)."""
    from apscheduler.triggers.interval import IntervalTrigger
    from .teacher import drain_job
    sched.add_job(drain_job, IntervalTrigger(minutes=10), args=[s], id="teacher_drain", max_instances=1, coalesce=True,
                  misfire_grace_time=300)
    try:
        from apscheduler.triggers.cron import CronTrigger
        from .progress import weekly_report_job, monthly_best_job
        sched.add_job(weekly_report_job, CronTrigger.from_crontab(s.teacher.weekly_report_cron, timezone=s.schedule.timezone),
                      args=[s], id="teacher_weekly", max_instances=1, coalesce=True, misfire_grace_time=3600)
        sched.add_job(monthly_best_job, CronTrigger.from_crontab(s.teacher.monthly_best_cron, timezone=s.schedule.timezone),
                      args=[s], id="teacher_monthly", max_instances=1, coalesce=True, misfire_grace_time=3600)
    except ImportError:   # progress arrives in teacher step 5
        pass
