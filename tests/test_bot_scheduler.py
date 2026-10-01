import json
import threading
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import SGT, Bot as FakeServer
from x100bot import db, exif, scheduler
from x100bot.bot import Bot
from x100bot.telegram import Telegram


@pytest.fixture
def ctx(settings, conn, make_limiter):
    server = FakeServer()
    lim = make_limiter()
    tg = Telegram("T", lim, settings.limits.telegram, transport=httpx.MockTransport(server.handler), thread_id=396)
    alerts = []
    x = SimpleNamespace(s=settings, conn=conn, lim=lim, tg=tg, alert=lambda k, t: alerts.append((k, t)), alerts=alerts,
                        server=server)
    return x


def queue_row(conn, slot_at, status="pending"):
    conn.execute("INSERT INTO queue(slot_at, type, facts_json, fields_json, text_html, preview_url, notify, status) "
                 "VALUES(?,?,?,?,?,?,?,?)", (slot_at, "recipe", "{}", "{}", "<b>hi</b>", None, 0, status))


# scheduler ------------------------------------------------------------------------------------------------------

def patch_ctx(monkeypatch, ctx):
    import x100bot.cli
    monkeypatch.setattr(x100bot.cli, "Ctx", lambda s, job, **kw: ctx)


def test_misfire_older_than_grace_is_skipped(ctx, monkeypatch):
    patch_ctx(monkeypatch, ctx)
    now = datetime.now(SGT)
    old = (now.replace(minute=0, second=0, microsecond=0) - __import__("datetime").timedelta(hours=2))
    queue_row(ctx.conn, old.strftime("%Y-%m-%d %H:00"))
    out = scheduler.post_slot(ctx.s, old.strftime("%H"), old.strftime("%Y-%m-%d"))
    assert "skipped" in out
    assert ctx.conn.execute("SELECT status FROM queue").fetchone()[0] == "skipped"
    assert not [c for c in ctx.server.calls if c[0] == "sendMessage"]


def test_slot_is_never_sent_twice_when_two_posts_race(ctx, monkeypatch):
    patch_ctx(monkeypatch, ctx)
    now = datetime.now(SGT)
    slot = now.strftime("%Y-%m-%d %H:00")
    queue_row(ctx.conn, slot)
    results = []
    threads = [threading.Thread(target=lambda: results.append(scheduler.post_slot(ctx.s, now.strftime("%H"), force=True)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sends = [c for c in ctx.server.calls if c[0] == "sendMessage"]
    assert len(sends) == 1 and sum("posted" in r for r in results) == 1
    assert sends[0][1]["message_thread_id"] == 396 and sends[0][1]["disable_notification"] is True


def test_post_updates_repeat_window(ctx, monkeypatch):
    patch_ctx(monkeypatch, ctx)
    ctx.conn.execute("INSERT INTO recipes(id, name, source_url, settings_hash, film_simulation, settings_json) "
                     "VALUES(1,'r','https://x/','h','ACROS','{}')")
    now = datetime.now(SGT)
    ctx.conn.execute("INSERT INTO queue(slot_at, type, ref_type, ref_id, facts_json, fields_json, text_html, notify, status)"
                     " VALUES(?,?,?,?,?,?,?,?,?)", (now.strftime("%Y-%m-%d %H:00"), "recipe", "recipe", 1, "{}", "{}", "x", 0, "pending"))
    scheduler.post_slot(ctx.s, now.strftime("%H"), force=True)
    row = ctx.conn.execute("SELECT last_posted_at, times_posted FROM recipes WHERE id=1").fetchone()
    assert row["last_posted_at"] and row["times_posted"] == 1


# bot ------------------------------------------------------------------------------------------------------------

def msg(ctx, text=None, user=42, chat_type="private", **extra):
    m = {"message_id": 1, "chat": {"id": user, "type": chat_type}, "from": {"id": user}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


def test_messages_from_anyone_but_the_owner_are_ignored(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, "/help", user=7))
    bot.handle(msg(ctx, "/help", chat_type="supergroup"))
    assert not ctx.server.calls
    bot.handle(msg(ctx, "/help"))
    assert ctx.server.calls and "X100VI COACH" in ctx.server.calls[0][1]["text"]


def test_file_over_20mb_gets_the_right_reply(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, document={"file_id": "f", "file_name": "DSCF0001.JPG", "file_size": 25 * 1024 * 1024}))
    assert "20 MB" in ctx.server.calls[-1][1]["text"]
    bot.handle(msg(ctx, document={"file_id": "f", "file_name": "DSCF0001.RAF", "file_size": 100}))
    assert "RAF" in ctx.server.calls[-1][1]["text"]


def test_setc_and_slots_and_done(ctx):
    ctx.conn.execute("INSERT INTO recipes(id, name, source_url, settings_hash, film_simulation, settings_json) "
                     "VALUES(1,'Easy Reala Ace','https://x/','h','REALA ACE','{}')")
    bot = Bot(ctx)
    bot.handle(msg(ctx, "/setc 3 easy reala"))
    assert ctx.conn.execute("SELECT recipe_id FROM custom_banks WHERE bank=3").fetchone()[0] == 1
    bot.handle(msg(ctx, "/slots"))
    assert "C3 · <b>Easy Reala Ace</b>" in ctx.server.calls[-1][1]["text"]
    bot.handle(msg(ctx, "/done"))
    assert ctx.conn.execute("SELECT COUNT(*) FROM assignments WHERE done_at IS NOT NULL").fetchone()[0] == 1


def test_exif_mapping_on_fixture():
    raw = json.loads((__import__("tests.conftest", fromlist=["FIX"]).FIX / "exif" / "x100vi_sample.json").read_text())
    raw = {k: v for k, v in raw.items() if not exif.GPS_RE.search(k)}
    fields = exif.map_tags(raw)
    assert fields["model"] == "X100VI" and fields["film_simulation"] and fields["shutter_text"] == "1/250"
    assert not any("gps" in k.lower() for k in fields)
