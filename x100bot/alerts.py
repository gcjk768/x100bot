"""Plain text alerts to admin_chat_id, at most one per kind per day. Never to the channel."""
from __future__ import annotations

import logging

from .db import kv_get, kv_set

log = logging.getLogger("x100bot.alerts")
KINDS = {"cooldown", "budget", "stock_low", "research_fallback", "compose_fallback", "post_failed", "firmware",
         "critique_fallback"}


class Alerts:
    def __init__(self, conn, tg, admin_chat_id: str, today):
        self.conn, self.tg, self.admin, self.today = conn, tg, admin_chat_id, today

    def __call__(self, kind: str, text: str) -> None:
        assert kind in KINDS, kind
        key = f"alert:{kind}:{self.today()}"
        if kv_get(self.conn, key):
            log.info("alert %s already sent today: %s", kind, text)
            return
        kv_set(self.conn, key, "1")
        log.warning("ALERT %s: %s", kind, text)
        if self.admin and self.tg:
            try:
                self.tg.send(self.admin, f"x100bot: {text}", parse_mode=None)
            except Exception as ex:   # an alert must never break a job
                log.error("alert not delivered: %s", ex)
