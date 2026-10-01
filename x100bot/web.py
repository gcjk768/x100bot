"""Polite HTTP client for every website: robots.txt, conditional GET, per domain pacing, caching, link checks.

One request at a time across the whole app. Every request goes through the limiter's web:<domain> bucket,
which also spends the shared daily web budget.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .ratelimit import InCooldown, Limiter

log = logging.getLogger(__name__)
_ONE_AT_A_TIME = threading.Lock()


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """robots.txt disallows the URL."""


class Offline(FetchError):
    pass


@dataclass
class Page:
    url: str
    status: int
    text: str
    from_cache: bool = False


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Web:
    def __init__(self, s, conn, limiter: Limiter, transport: httpx.BaseTransport | None = None,
                 alert=None, offline: bool = False):
        self.s, self.conn, self.lim, self.offline = s, conn, limiter, offline
        self.cfg = s.limits.web
        self.alert = alert or (lambda kind, text: None)
        self.http = httpx.Client(headers={"User-Agent": self.cfg.user_agent}, follow_redirects=True,
                                 timeout=httpx.Timeout(30.0), transport=transport)
        self.pages_dir = Path(s.data_dir) / "pages"
        self._robots: dict[str, tuple[str, RobotFileParser]] = {}

    # the one request path --------------------------------------------------------------------------
    def request(self, method: str, url: str, headers: dict | None = None) -> httpx.Response:
        """Send one request with pacing, soft retries with backoff, and a domain cooldown on hard failures."""
        if self.offline:
            raise Offline(f"offline mode, no request to {url}")
        host = urlsplit(url).hostname or ""
        bucket = f"web:{host}"
        for attempt in range(self.cfg.retries + 1):
            with _ONE_AT_A_TIME:
                ticket = self.lim.acquire(bucket, target=urlsplit(url).path or "/")
                try:
                    r = self.http.request(method, url, headers=headers or {})
                    err = f"HTTP {r.status_code}" if r.status_code >= 500 else None
                except httpx.TransportError as ex:
                    r, err = None, type(ex).__name__
            if r is not None and r.status_code in (429, 403):
                trip = self.lim.report(ticket, "hard_fail", reason=f"HTTP {r.status_code} on {url}",
                                       status=r.status_code, retry_after=_retry_after(r))
                self.alert("cooldown", f"{host} answered HTTP {r.status_code}. No requests to it until "
                                       f"{self._fmt(trip.until)}.")
                raise InCooldown(bucket, trip.until, trip.reason)
            if err:
                trip = self.lim.report(ticket, "soft_fail", reason=err, status=r.status_code if r is not None else None)
                if trip:
                    self.alert("cooldown", f"{host} failed three times in a row ({err}). No requests to it until "
                                           f"{self._fmt(trip.until)}.")
                    raise InCooldown(bucket, trip.until, trip.reason)
                if attempt < self.cfg.retries:
                    backoff = self.cfg.backoff_seconds[min(attempt, len(self.cfg.backoff_seconds) - 1)]
                    self.lim.sleep(self.lim.rng.uniform(backoff, backoff * 1.5))
                continue
            self.lim.report(ticket, "ok", status=r.status_code)
            return r
        raise FetchError(f"{url}: {err} after {self.cfg.retries + 1} attempts")

    def _fmt(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.lim.tz).strftime("%Y-%m-%d %H:%M")

    # robots.txt -------------------------------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots_txt:
            return True
        parts = urlsplit(url)
        today = self.lim.today()
        cached = self._robots.get(parts.netloc)
        if not cached or cached[0] != today:
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            page = self.get(robots_url, refresh="daily", check_robots=False)
            rp = RobotFileParser()
            # a missing robots.txt allows everything; a server error is read as disallow all, to be safe
            rp.parse([] if page.status in (404, 410) else
                     ["User-agent: *", "Disallow: /"] if page.status >= 400 else page.text.splitlines())
            self._robots[parts.netloc] = cached = (today, rp)
        return cached[1].can_fetch(self.cfg.user_agent, url)

    # cached GET ---------------------------------------------------------------------------------------
    def _row(self, url: str):
        return self.conn.execute("SELECT * FROM pages WHERE url=?", (url,)).fetchone()

    def _body(self, row) -> str | None:
        if row and row["body_path"] and Path(row["body_path"]).exists():
            return Path(row["body_path"]).read_text(encoding="utf-8")
        return None

    def get(self, url: str, refresh: str = "never", force: bool = False, check_robots: bool = True) -> Page:
        """refresh='never': fetched once and cached forever (recipe and manual pages).
        refresh='daily': at most once per local day, as a conditional GET (feeds, index pages, robots.txt)."""
        row = self._row(url)
        body = self._body(row)
        if body is not None and not force:
            fresh_today = row["fetched_at"] and self.lim.today(row["fetched_at"]) == self.lim.today()
            if refresh == "never" or fresh_today:
                return Page(url, row["status"], body, from_cache=True)
        if check_robots and not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        headers = {}
        if refresh == "daily" and body is not None:
            if row["etag"]:
                headers["If-None-Match"] = row["etag"]
            if row["last_modified"]:
                headers["If-Modified-Since"] = row["last_modified"]
        r = self.request("GET", url, headers)
        now = self.lim.clock()
        if r.status_code == 304 and body is not None:
            self.conn.execute("UPDATE pages SET fetched_at=? WHERE url=?", (now, url))
            return Page(url, row["status"], body, from_cache=True)
        path = None
        if r.status_code < 400:
            self.pages_dir.mkdir(parents=True, exist_ok=True)
            path = self.pages_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")
            path.write_text(r.text, encoding="utf-8")
        self.conn.execute("INSERT OR REPLACE INTO pages(url, etag, last_modified, fetched_at, status, body_path) "
                          "VALUES(?,?,?,?,?,?)", (url, r.headers.get("etag"), r.headers.get("last-modified"), now,
                                                  r.status_code, str(path) if path else None))
        return Page(url, r.status_code, r.text if r.status_code < 400 else "", from_cache=False)

    # link checks ------------------------------------------------------------------------------------
    def link_ok(self, url: str) -> bool:
        """True when the URL answers below 400. Cached for link_check_cache_days, cached failures too."""
        row = self.conn.execute("SELECT * FROM link_checks WHERE url=?", (url,)).fetchone()
        if row and row["checked_at"] > self.lim.clock() - self.cfg.link_check_cache_days * 86400:
            return row["status"] is not None and row["status"] < 400
        if not url.startswith(("https://", "http://")):
            return False
        if not self.allowed(url):
            return False
        r = self.request("HEAD", url)
        if r.status_code in (405, 501):   # some servers refuse HEAD
            r = self.request("GET", url)
        self.conn.execute("INSERT OR REPLACE INTO link_checks(url, status, final_url, checked_at) VALUES(?,?,?,?)",
                          (url, r.status_code, str(r.url), self.lim.clock()))
        return r.status_code < 400
