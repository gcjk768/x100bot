"""Telegram Bot API client: send, delete, getUpdates long polling, 429 handling, HTML escaping.
Every call goes through the telegram bucket. Adapted from pddbot/telegram.py."""
from __future__ import annotations

import html
import logging
import threading

import httpx

from .db import kv_get, kv_set
from .ratelimit import Limiter

log = logging.getLogger(__name__)
MAX_LEN = 4096
DELETE_BATCH = 100
POLL_BACKOFF = (5, 10, 30, 60)


def esc(value) -> str:
    """Escape &, < and > in any value from a source or from Claude."""
    return html.escape(str(value), quote=False)


def esc_attr(value) -> str:
    """For values inside href="...": quotes are escaped too."""
    return html.escape(str(value), quote=True)


class TelegramError(Exception):
    """Telegram answered ok=false, for example a message that can no longer be deleted."""


class RetryTooLong(Exception):
    """Telegram asked to wait longer than max_retry_after_seconds, or kept asking. The item fails for this hour."""

    def __init__(self, retry_after: float):
        super().__init__(f"Telegram asked to wait {retry_after:.0f} s")
        self.retry_after = retry_after


class TelegramUnavailable(Exception):
    pass


def preview_options(preview_url: str | None, above: bool) -> dict:
    """Link previews show the source's photos, so nothing is ever downloaded or uploaded again."""
    if not preview_url:
        return {"is_disabled": True}
    return {"url": preview_url, "prefer_large_media": True, "show_above_text": above}


class Telegram:
    def __init__(self, token: str, limiter: Limiter, limits, transport: httpx.BaseTransport | None = None,
                 thread_id: int | None = None, group_chat_id: str | int | None = None):
        self.lim, self.limits, self.thread_id = limiter, limits, thread_id
        self.group_chat_id = str(group_chat_id) if group_chat_id not in (None, "") else None
        # long polls hold the connection for poll_timeout_seconds, so the read timeout must be longer
        self.http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}/", transport=transport,
                                 timeout=httpx.Timeout(limits.poll_timeout_seconds + 15))

    def in_topic(self, chat_id) -> bool:
        """True when chat_id is the configured group, so messages there belong in the configured topic."""
        return bool(self.thread_id and self.group_chat_id and str(chat_id) == self.group_chat_id)

    def call(self, method: str, **params):
        # anything sent into the group chat lands in the configured topic, whichever module sends it
        if method.startswith("send") and "message_thread_id" not in params and self.in_topic(params.get("chat_id")):
            params["message_thread_id"] = self.thread_id
        fails = throttles = 0
        while True:
            t = self.lim.acquire("telegram", target=method)
            try:
                r = self.http.post(method, json=params)
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                err = f"HTTP {r.status_code}" if r.status_code >= 500 else None
            except httpx.TransportError as ex:
                r, data, err = None, {}, type(ex).__name__
            if r is not None and r.status_code == 429:
                retry_after = float(data.get("parameters", {}).get("retry_after", 5))
                self.lim.report(t, "throttled", reason=f"retry_after {retry_after:.0f}", status=429)
                throttles += 1
                if retry_after > self.limits.max_retry_after_seconds or throttles > self.limits.max_retries:
                    raise RetryTooLong(retry_after)
                self.lim.sleep(retry_after + self.lim.rng.uniform(1, 3))
                continue
            if err:
                self.lim.report(t, "soft_fail", reason=err, status=r.status_code if r is not None else None)
                fails += 1
                if fails > 3:   # retried after 2, 4 and 8 s, all failed
                    raise TelegramUnavailable(f"{method}: {err}, four attempts in a row")
                self.lim.sleep(2 ** fails)
                continue
            self.lim.report(t, "ok", status=r.status_code)
            if not data.get("ok"):
                raise TelegramError(f"{method}: {data.get('description', r.status_code)}")
            return data["result"]

    def send(self, chat_id, text: str, *, silent: bool = False, preview_url: str | None = None,
             above: bool = True, parse_mode: str | None = "HTML", topic: bool = False) -> int:
        """One message. Returns its message_id. topic=True posts into the configured forum topic."""
        assert len(text) <= MAX_LEN, f"message is {len(text)} characters"
        params = dict(chat_id=chat_id, text=text, disable_notification=silent,
                      link_preview_options=preview_options(preview_url, above))
        if topic and self.thread_id:
            params["message_thread_id"] = self.thread_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("sendMessage", **params)["message_id"]

    def delete_many(self, chat_id, message_ids: list[int]) -> int:
        """deleteMessages takes up to 100 ids per call. Returns how many batches were sent."""
        batches = 0
        for i in range(0, len(message_ids), DELETE_BATCH):
            try:
                self.call("deleteMessages", chat_id=chat_id, message_ids=message_ids[i:i + DELETE_BATCH])
            except TelegramError as ex:   # older than 48 hours or already gone: nothing more to do
                log.warning("deleteMessages failed: %s", ex)
            batches += 1
        return batches

    def get_file_bytes(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        t = self.lim.acquire("telegram", target="file")
        r = self.http.get(f"{str(self.http.base_url).replace('/bot', '/file/bot')}{info['file_path']}")
        self.lim.report(t, "ok" if r.status_code < 400 else "soft_fail", status=r.status_code)
        r.raise_for_status()
        return r.content

    def poll(self, conn, handle, stop: threading.Event | None = None) -> None:
        """getUpdates long polling with the offset kept in SQLite. Backs off 5, 10, 30, then 60 s after errors."""
        stop = stop or threading.Event()
        errors = 0
        while not stop.is_set():
            offset = int(kv_get(conn, "tg_offset", "0"))
            try:
                updates = self.call("getUpdates", offset=offset, timeout=self.limits.poll_timeout_seconds,
                                    allowed_updates=["message", "callback_query"])
                errors = 0
            except Exception as ex:
                wait = POLL_BACKOFF[min(errors, len(POLL_BACKOFF) - 1)]
                errors += 1
                log.warning("getUpdates failed (%s), waiting %d s", ex, wait)
                self.lim.sleep(wait)
                continue
            for u in updates:
                kv_set(conn, "tg_offset", u["update_id"] + 1)   # advance first, so one bad update can't loop forever
                try:
                    handle(u)
                except Exception:
                    log.exception("update %s failed", u.get("update_id"))
