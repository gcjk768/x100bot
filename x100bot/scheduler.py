"""APScheduler jobs for the container: sources, plan, firmware, the hourly post, and the private chat listener.
The post job claims its queue row inside a transaction, so an item can never be sent twice, and an item older
than misfire_grace_minutes is skipped rather than posted late in a burst."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import vault
from .db import kv_get, kv_set, tx
from .lock import LockBusy, job_lock
from .ratelimit import LimitError
from .telegram import RetryTooLong, TelegramError, TelegramUnavailable
from .web import FetchError

log = logging.getLogger(__name__)
JOB = dict(max_instances=1, coalesce=True)


def triggers(s) -> dict[str, CronTrigger]:
    tz = s.schedule.timezone
    hours = ",".join(sorted({h for h in s.schedule.slots} | set(s.schedule.sunday_overrides)))
    t = {"sources": CronTrigger.from_crontab(s.schedule.sources_cron, timezone=tz),
         "plan": CronTrigger.from_crontab(s.schedule.plan_cron, timezone=tz),
         "firmware": CronTrigger.from_crontab(s.schedule.firmware_cron, timezone=tz),
         "post": CronTrigger(hour=hours, minute=s.schedule.post_minute, timezone=tz)}
    if s.telegram.replace_previous_day:
        t["delete_yesterday"] = CronTrigger(hour=6, minute=55, timezone=tz)
    return t


# posting ------------------------------------------------------------------------------------------------------

def mark_posted_material(x, row) -> None:
    """Update repeat windows and the lessons table after a successful post."""
    now = x.lim.clock()
    table = {"recipe": "recipes", "person": "people", "tip": "tips", "video": "videos"}.get(row["ref_type"] or "")
    if table and row["ref_id"]:
        extra = ", times_posted=COALESCE(times_posted,0)+1" if table == "recipes" else ""
        x.conn.execute(f"UPDATE {table} SET last_posted_at=?{extra} WHERE id=?", (now, row["ref_id"]))
    if row["ref_type"] == "firmware":
        facts = json.loads(row["facts_json"] or "{}")
        kv_set(x.conn, f"fw_posted:{facts.get('firmware_version')}", "1")
    if row["type"] in ("composition", "drill", "learning_tip"):
        fields = json.loads(row["fields_json"] or "{}")
        facts = json.loads(row["facts_json"] or "{}")
        concept = fields.get("title") or facts.get("topic") or row["type"]
        x.conn.execute("INSERT INTO lessons(type, concept_title, theme, posted_at) VALUES(?,?,?,?)",
                       (row["type"], concept, facts.get("theme"), now))


def post_slot(s, hour: str, date_str: str | None = None, force: bool = False) -> str:
    """Claim and send one queued slot. Returns a short status line."""
    from .cli import Ctx
    x = Ctx(s, "post")
    now = datetime.now(x.lim.tz)
    day = date_str or now.date().isoformat()
    slot_at = f"{day} {hour}:00"
    with job_lock(s.data_dir, "post"):
        with tx(x.conn):
            row = x.conn.execute("SELECT * FROM queue WHERE slot_at=? AND status='pending'", (slot_at,)).fetchone()
            if not row:
                return f"{slot_at}: nothing pending"
            slot_time = datetime.fromisoformat(slot_at).replace(tzinfo=x.lim.tz)
            if not force and now - slot_time > timedelta(minutes=s.schedule.misfire_grace_minutes):
                x.conn.execute("UPDATE queue SET status='skipped' WHERE id=?", (row["id"],))
                log.warning("%s skipped, %d minutes late", slot_at, (now - slot_time).seconds // 60)
                return f"{slot_at}: skipped, too late"
            x.conn.execute("UPDATE queue SET status='sending' WHERE id=?", (row["id"],))
        if not x.tg:
            x.conn.execute("UPDATE queue SET status='pending' WHERE id=?", (row["id"],))
            return "TELEGRAM_BOT_TOKEN is not set"
        try:
            mid = x.tg.send(s.telegram.chat_id, row["text_html"], silent=not row["notify"], preview_url=row["preview_url"],
                            above=s.telegram.preview_above_text, topic=True)
        except (RetryTooLong, TelegramUnavailable, TelegramError, LimitError) as ex:
            x.conn.execute("UPDATE queue SET status='failed' WHERE id=?", (row["id"],))
            x.alert("post_failed", f"slot {slot_at} ({row['type']}) failed to post: {ex}")
            vault.log_event("🔴", "post failed", f"{slot_at} {row['type']}: {ex}")
            return f"{slot_at}: failed, {ex}"
        x.conn.execute("UPDATE queue SET status='posted', message_id=?, posted_at=? WHERE id=?",
                       (mid, x.lim.clock(), row["id"]))
        mark_posted_material(x, row)
        vault.log_event("📨", "posted", f"{slot_at} {row['type']} message {mid}")
        return f"{slot_at}: posted message {mid} ({row['type']})"


def post_job(s) -> None:
    hour = datetime.now().astimezone().strftime("%H")
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo(s.schedule.timezone)).strftime("%H")
        log.info(post_slot(s, hour))
    except LockBusy as ex:
        log.warning("post skipped: %s", ex)
    except Exception:
        log.exception("post job failed")


def delete_yesterday(s) -> None:
    """replace_previous_day: delete yesterday's posts at 06:55 (bots can delete only posts younger than 48 hours)."""
    from .cli import Ctx
    x = Ctx(s, "delete")
    if not x.tg:
        return
    y = (datetime.now(x.lim.tz).date() - timedelta(days=1)).isoformat()
    rows = x.conn.execute("SELECT id, message_id FROM queue WHERE slot_at LIKE ? AND status='posted' AND message_id "
                          "IS NOT NULL", (f"{y}%",)).fetchall()
    if rows:
        x.tg.delete_many(s.telegram.chat_id, [r["message_id"] for r in rows])
        vault.log_event("🗑", "deleted yesterday's posts", f"{len(rows)} messages")


# other jobs -------------------------------------------------------------------------------------------------------

def plan_job(s) -> None:
    from .planner import run_plan
    try:
        rows = run_plan(s)
        log.info("plan finished: %d slots, %d with fallback", len(rows), sum(r["used_fallback"] for r in rows))
        vault.log_event("🗓", "day planned", f"{len(rows)} slots, {sum(r['used_fallback'] for r in rows)} fallback")
    except LockBusy as ex:
        log.warning("plan skipped: %s", ex)
    except Exception:
        log.exception("plan failed")


def sources_job(s) -> None:
    from .library.sources import run_sources
    try:
        out = run_sources(s)
        log.info("sources finished: %s", out)
        vault.log_event("📚", "sources refreshed", json.dumps(out)[:200])
    except LockBusy as ex:
        log.warning("sources skipped: %s", ex)
    except Exception:
        log.exception("sources failed")


VERSION_RE = re.compile(r"X100VI[^0-9]{0,80}?Ver\.?\s*([0-9]+\.[0-9]+)", re.I | re.S)


def firmware_job(s) -> None:
    """Mondays 05:15: read the official firmware page once. fujifilm-x.com blocks the bot's user agent, so this
    usually ends in a cooldown and a log line; the manual's own firmware page is the fallback source of truth."""
    from .cli import Ctx
    if not s.sources.firmware_page:
        return
    x = Ctx(s, "firmware")
    try:
        with job_lock(s.data_dir, "firmware"):
            page = x.web.get(s.sources.firmware_page, refresh="daily")
    except (LimitError, FetchError, LockBusy) as ex:
        log.warning("firmware check skipped: %s", ex)
        return
    m = VERSION_RE.search(page.text)
    if not m:
        log.info("firmware page gave no X100VI version")
        return
    version = m.group(1)
    if x.conn.execute("SELECT 1 FROM firmware WHERE version=?", (version,)).fetchone():
        return
    x.conn.execute("INSERT INTO firmware(version, found_at, url) VALUES(?,?,?)", (version, x.lim.clock(), s.sources.firmware_page))
    x.alert("firmware", f"Fujifilm lists X100VI firmware {version}. The 09:00 tip will cover it this week.")
    vault.log_event("🆕", "firmware found", version)


def serve(s) -> None:
    """The container command: all jobs plus the private chat listener, blocking."""
    from .bot import listen
    sched = BlockingScheduler(timezone=s.schedule.timezone)
    t = triggers(s)
    grace = s.schedule.misfire_grace_minutes * 60
    sched.add_job(sources_job, t["sources"], args=[s], id="sources", misfire_grace_time=3600, **JOB)
    sched.add_job(plan_job, t["plan"], args=[s], id="plan", misfire_grace_time=3600, **JOB)
    sched.add_job(firmware_job, t["firmware"], args=[s], id="firmware", misfire_grace_time=3600, **JOB)
    sched.add_job(post_job, t["post"], args=[s], id="post", misfire_grace_time=grace, **JOB)
    if "delete_yesterday" in t:
        sched.add_job(delete_yesterday, t["delete_yesterday"], args=[s], id="delete_yesterday", misfire_grace_time=600, **JOB)
    try:
        from .teacher_queue import add_jobs as add_teacher_jobs
        add_teacher_jobs(sched, s)
    except ImportError:
        pass
    stop = threading.Event()
    threading.Thread(target=listen, args=(s, stop), name="bot", daemon=True).start()
    log.info("serving: %s", {k: str(v) for k, v in t.items()})
    try:
        sched.start()
    finally:
        stop.set()
