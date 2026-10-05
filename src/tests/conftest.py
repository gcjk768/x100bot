import json
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from x100bot import config, db
from x100bot.ratelimit import Limiter, rules_from_settings

FIX = Path(__file__).parent / "fixtures"
SGT = ZoneInfo("Asia/Singapore")


class FakeClock:
    """time.time and time.sleep in one object. Sleeping advances the clock instantly."""

    def __init__(self, start: datetime):
        self.t = start.timestamp()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += max(0.0, s)


@pytest.fixture
def settings(tmp_path):
    s = config.load(config.ROOT / "config.yaml", env=tmp_path / "none.env")
    s.data_root = tmp_path / "data"
    s.telegram.chat_id = "@channel"
    s.telegram.admin_chat_id = "999"
    s.telegram.owner_user_id = 42
    return s


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 6, 9, 0, tzinfo=SGT))


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def make_limiter(settings, conn, clock):
    def make(job="test", seed=1, conn_=None):
        return Limiter(conn_ or conn, rules_from_settings(settings), job, clock=clock, sleep=clock.sleep,
                       rng=random.Random(seed))
    return make


class Bot:
    """A scripted Telegram server. script: list of (method, status, body) consumed in order; then ok."""

    def __init__(self, script=()):
        self.script, self.calls, self.next_id = list(script), [], 100

    def handler(self, req: httpx.Request) -> httpx.Response:
        method = req.url.path.rsplit("/", 1)[-1]
        try:
            body = json.loads(req.content) if req.content else {}
        except ValueError:   # multipart uploads such as sendPhoto
            body = {"multipart": True}
        self.calls.append((method, body))
        if self.script and self.script[0][0] in (method, "*"):
            _, status, resp = self.script.pop(0)
            if status == "reset":
                raise httpx.ConnectError("connection reset")
            return httpx.Response(status, json=resp)
        self.next_id += 1
        result = {"message_id": self.next_id} if method.startswith("send") else [] if method == "getUpdates" else True
        return httpx.Response(200, json={"ok": True, "result": result})
