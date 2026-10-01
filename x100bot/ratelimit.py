"""The one gate for every call to a website, Open-Meteo, Telegram and Claude.

Buckets: web (all websites together), web:<domain> (pacing and cooldown per site), openmeteo, telegram,
claude (scheduled calls) and critique. Each has a minimum gap with jitter, an optional rolling window cap, an
optional daily cap (days roll over at midnight in the configured timezone) and a cooldown. All state lives in
SQLite, so a restart never resets a budget. Adapted from pddbot/ratelimit.py.
"""
from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class LimitError(Exception):
    pass


class BudgetExhausted(LimitError):
    pass


class InCooldown(LimitError):
    def __init__(self, bucket: str, until: float, reason: str):
        super().__init__(f"{bucket} in cooldown until {datetime.fromtimestamp(until):%Y-%m-%d %H:%M} ({reason})")
        self.bucket, self.until, self.reason = bucket, until, reason


@dataclass
class Rule:
    gap: tuple[float, float] = (0.0, 0.0)          # random wait since the last call, in seconds
    window: tuple[int, float] | None = None        # (max calls, seconds), rolling
    per_day: int | None = None
    cooldown_hours: float = 0                      # 0: failures never start a cooldown
    trip_after: int = 3                            # soft failures in a row that start a cooldown


@dataclass
class Ticket:
    id: int
    bucket: str
    started: float


@dataclass
class Trip:
    bucket: str
    until: float
    reason: str


def rules_from_settings(s, seed: bool = False) -> dict[str, Rule]:
    w, t, c = s.limits.web, s.limits.telegram, s.limits.claude
    return {
        # the seed is a one time build with its own page budget; normal days use max_requests_per_day
        "web": Rule(per_day=w.max_seed_pages if seed else w.max_requests_per_day),
        "web:*": Rule(gap=(w.per_domain_min_gap_seconds, w.per_domain_max_gap_seconds),
                      cooldown_hours=w.domain_cooldown_hours),
        "openmeteo": Rule(gap=(1.0, 2.0), per_day=s.limits.openmeteo.max_calls_per_day, cooldown_hours=6),
        "telegram": Rule(gap=(t.min_gap_seconds, t.min_gap_seconds + 0.3), window=(t.max_per_minute, 60.0)),
        "claude": Rule(per_day=c.max_scheduled_calls_per_day),
        "critique": Rule(per_day=c.max_critiques_per_day),
        "teacher_critique": Rule(per_day=s.limits.teacher.max_critiques_per_day),
        "teacher_followup": Rule(per_day=s.limits.teacher.max_followups_per_day),
        "teacher_question": Rule(per_day=s.limits.teacher.max_questions_per_day),
        "teacher_series": Rule(per_day=s.limits.teacher.max_series_per_day),
    }


class Limiter:
    def __init__(self, db: sqlite3.Connection, rules: dict[str, Rule], job: str = "", tz: str = "Asia/Singapore",
                 clock=time.time, sleep=time.sleep, rng: random.Random | None = None):
        self.db, self.rules, self.job, self.tz = db, rules, job, ZoneInfo(tz)
        self.clock, self._sleep, self.rng = clock, sleep, rng or random.Random()
        # ponytail: one lock per bucket inside this process; two processes can still race by a gap, rare (CLI next to serve)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def rule(self, bucket: str) -> Rule:
        if bucket in self.rules:
            return self.rules[bucket]
        if ":" in bucket and f"{bucket.split(':')[0]}:*" in self.rules:
            return self.rules[f"{bucket.split(':')[0]}:*"]
        raise KeyError(f"no rate limit rule for bucket {bucket}")

    def _lock(self, bucket: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(bucket, threading.Lock())

    # state ------------------------------------------------------------------------------------------
    def today(self, now: float | None = None) -> str:
        return datetime.fromtimestamp(self.clock() if now is None else now, self.tz).date().isoformat()

    def state(self, bucket: str) -> dict:
        """The bucket's row, with the day counter rolled over when the local day changed."""
        row = self.db.execute("SELECT * FROM rate_state WHERE bucket=?", (bucket,)).fetchone()
        st = dict(row) if row else {"bucket": bucket, "day": None, "day_count": 0, "last_call_at": None,
                                    "cooldown_until": None, "consecutive_failures": 0, "cooldown_reason": None}
        if st["day"] != self.today():
            st["day"], st["day_count"] = self.today(), 0
        return st

    def _save(self, st: dict) -> None:
        cols = list(st)
        self.db.execute(f"INSERT OR REPLACE INTO rate_state({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [st[c] for c in cols])

    def check(self, bucket: str) -> None:
        """Raise when the bucket cannot be used right now. Does not wait and does not count."""
        r, st = self.rule(bucket), self.state(bucket)
        if st["cooldown_until"] and st["cooldown_until"] > self.clock():
            raise InCooldown(bucket, st["cooldown_until"], st["cooldown_reason"] or "")
        if r.per_day is not None and st["day_count"] >= r.per_day:
            raise BudgetExhausted(f"{bucket}: daily budget of {r.per_day} reached")

    def remaining(self, bucket: str) -> int | None:
        r = self.rule(bucket)
        return None if r.per_day is None else max(0, r.per_day - self.state(bucket)["day_count"])

    # the gate -----------------------------------------------------------------------------------------
    def acquire(self, bucket: str, target: str = "") -> Ticket:
        """Wait until a call is allowed, count it and log it. Raises BudgetExhausted or InCooldown."""
        r = self.rule(bucket)
        # web:<domain> also spends the shared web budget, so check both before waiting
        parent = bucket.split(":")[0] if ":" in bucket and bucket.split(":")[0] in self.rules else None
        with self._lock(bucket):
            if parent:
                self.check(parent)
            self.check(bucket)
            st = self.state(bucket)
            if st["last_call_at"] is not None and r.gap[1] > 0:
                wait = st["last_call_at"] + self.rng.uniform(*r.gap) - self.clock()
                if wait > 0:
                    self._sleep(wait)
            if r.window:
                cap, secs = r.window
                while True:
                    rows = self.db.execute("SELECT at FROM requests_log WHERE bucket=? AND at>? ORDER BY at DESC",
                                           (bucket, self.clock() - secs)).fetchall()
                    if len(rows) < cap:
                        break
                    self._sleep(rows[cap - 1]["at"] + secs - self.clock() + 0.05)
            if parent:
                self.check(parent)
            self.check(bucket)  # the clock moved while we slept
            now = self.clock()
            for b in filter(None, (parent, bucket)):
                st = self.state(b)
                st["day_count"] += 1
                st["last_call_at"] = now
                self._save(st)
            cur = self.db.execute("INSERT INTO requests_log(job,bucket,at,target,outcome) VALUES(?,?,?,?,?)",
                                  (self.job, bucket, now, target.split("?")[0][:300], "pending"))
            return Ticket(cur.lastrowid, bucket, now)

    def report(self, ticket: Ticket, outcome: str, *, reason: str = "", status: int | None = None,
               retry_after: float | None = None) -> Trip | None:
        """outcome: ok, soft_fail, hard_fail or throttled. Returns a Trip when a cooldown started."""
        assert outcome in ("ok", "soft_fail", "hard_fail", "throttled"), outcome
        latency = int((self.clock() - ticket.started) * 1000)
        self.db.execute("UPDATE requests_log SET outcome=?, status=?, latency_ms=? WHERE id=?",
                        (f"{outcome}: {reason}"[:200] if reason else outcome, status, latency, ticket.id))
        st = self.state(ticket.bucket)
        rule = self.rule(ticket.bucket)
        if outcome == "ok":
            st["consecutive_failures"] = 0
        elif outcome == "soft_fail":
            st["consecutive_failures"] += 1
            if rule.cooldown_hours and st["consecutive_failures"] >= rule.trip_after:
                self._save(st)
                return self.trip(ticket.bucket, f"{rule.trip_after} failures in a row, last: {reason}")
        elif outcome == "hard_fail":
            self._save(st)
            return self.trip(ticket.bucket, reason, retry_after)
        # throttled is a request to slow down, not a failure: the caller sleeps retry_after itself
        self._save(st)
        return None

    def trip(self, bucket: str, reason: str, retry_after: float | None = None) -> Trip:
        """Start a cooldown of cooldown_hours, or longer when the server's Retry-After asks for longer."""
        st = self.state(bucket)
        until = self.clock() + max((self.rule(bucket).cooldown_hours or 24) * 3600, retry_after or 0)
        st.update(cooldown_until=until, consecutive_failures=0, cooldown_reason=reason)
        self._save(st)
        log.warning("%s: cooldown until %s: %s", bucket, datetime.fromtimestamp(until, self.tz), reason)
        return Trip(bucket, until, reason)

    def clear_cooldown(self, bucket: str) -> None:
        st = self.state(bucket)
        st.update(cooldown_until=None, consecutive_failures=0, cooldown_reason=None)
        self._save(st)

    def cooldowns(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM rate_state WHERE cooldown_until>?", (self.clock(),))]

    def sleep(self, seconds: float) -> None:
        """Backoff sleeps go through the injectable sleep so tests stay instant."""
        self._sleep(seconds)
