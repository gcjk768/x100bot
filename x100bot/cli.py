"""x100bot entry point: serve, plan, post, sources, seed, status, test-telegram, eval."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config, db
from .alerts import Alerts
from .ratelimit import Limiter, rules_from_settings
from .telegram import Telegram
from .web import Web


class Ctx:
    """Everything a job needs, wired once: database, limiter, web client, Telegram and alerts."""

    def __init__(self, s, job: str, offline: bool = False, seed: bool = False):
        self.s, self.job, self.offline = s, job, offline
        self.conn = db.connect(s.data_dir / "x100bot.db")
        self.lim = Limiter(self.conn, rules_from_settings(s, seed=seed), job, s.schedule.timezone)
        self.tg = Telegram(s.bot_token, self.lim, s.limits.telegram) if s.bot_token and not offline else None
        self.alert = Alerts(self.conn, self.tg, s.telegram.admin_chat_id, self.lim.today)
        self.web = Web(s, self.conn, self.lim, alert=self.alert, offline=offline)


def setup_logging(s) -> None:
    (s.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stderr),
              RotatingFileHandler(s.data_dir / "logs" / "x100bot.log", maxBytes=2_000_000, backupCount=5,
                                  encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines carry the bot token in the URL


def cmd_test_telegram(s) -> None:
    x = Ctx(s, "test-telegram")
    if not x.tg:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    mid = x.tg.send(s.telegram.chat_id, "x100bot test, this message deletes itself", silent=True)
    print(f"posted message {mid} to the channel")
    x.tg.call("deleteMessage", chat_id=s.telegram.chat_id, message_id=mid)
    print("deleted it again, so the bot can post and delete")
    if s.telegram.admin_chat_id:
        x.tg.send(s.telegram.admin_chat_id, "x100bot test, admin alerts arrive here", parse_mode=None)
        print("sent a test message to the admin chat")
    else:
        print("telegram.admin_chat_id is empty, alerts go to the log only")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="x100bot")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="plan a day")
    p.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    p.add_argument("--dry-run", action="store_true", help="print all rendered messages, store and post nothing")
    p.add_argument("--offline", action="store_true", help="library and fixtures only, no web, weather or Claude")
    po = sub.add_parser("post", help="post one queued slot now")
    po.add_argument("--slot", required=True, help="HH")
    po.add_argument("--date", default=None)
    for name in ("serve", "sources", "seed", "status", "test-telegram", "eval", "firmware"):
        sub.add_parser(name)
    a = ap.parse_args(argv)
    try:
        s = config.load(a.config)
    except config.ConfigError as ex:
        sys.exit(str(ex))
    setup_logging(s)

    if a.cmd == "test-telegram":
        cmd_test_telegram(s)
    elif a.cmd == "plan":
        from .planner import run_plan
        run_plan(s, date=a.date, dry_run=a.dry_run, offline=a.offline)
    elif a.cmd == "post":
        from .scheduler import post_slot
        print(post_slot(s, a.slot, date=a.date, force=True))
    elif a.cmd == "sources":
        from .library.sources import run_sources
        print(run_sources(s))
    elif a.cmd == "seed":
        from .library.seed import run_seed
        print(run_seed(s))
    elif a.cmd == "firmware":
        from .scheduler import firmware_job
        firmware_job(s)
    elif a.cmd == "serve":
        from .scheduler import serve
        serve(s)
    elif a.cmd == "status":
        from .status import report
        print(report(s))
    elif a.cmd == "eval":
        from .compose import run_eval
        sys.exit(0 if run_eval(s) else 1)


if __name__ == "__main__":
    main()
