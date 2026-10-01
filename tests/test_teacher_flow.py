import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from tests.conftest import Bot as FakeServer
from tests.test_guardrails import good
from x100bot import teacher as teacher_mod
from x100bot import teacher_queue as q
from x100bot.bot import Bot
from x100bot.claude import Result
from x100bot.telegram import Telegram


@pytest.fixture
def ctx(settings, conn, make_limiter, monkeypatch):
    server = FakeServer()
    lim = make_limiter()
    tg = Telegram("T", lim, settings.limits.telegram, transport=httpx.MockTransport(server.handler))
    x = SimpleNamespace(s=settings, conn=conn, lim=lim, tg=tg, alert=lambda k, t: None, server=server)
    # the intake drains in a thread; run it inline in tests
    monkeypatch.setattr("x100bot.teacher_intake.TeacherIntake.kick", lambda self: None)
    return x


def msg(ctx, text=None, user=42, **extra):
    m = {"message_id": extra.pop("message_id", 10), "chat": {"id": user, "type": "private"}, "from": {"id": user}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


PHOTO = [{"file_id": "small", "file_unique_id": "u1", "file_size": 100}, {"file_id": "big", "file_unique_id": "u2", "file_size": 900}]


def jobs(ctx):
    return [(r["kind"], json.loads(r["payload_json"])) for r in ctx.conn.execute("SELECT kind, payload_json FROM teacher_queue ORDER BY id")]


# routing --------------------------------------------------------------------------------------------------------

def test_photo_becomes_a_critique_job_with_the_largest_size(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, photo=PHOTO, caption="wanted it quiet #quick"))
    (kind, p), = jobs(ctx)
    assert kind == "critique" and p["file_id"] == "big" and p["source"] == "photo"
    assert p["flags"] == {"quick": True} and p["caption"] == "wanted it quiet"


def test_file_becomes_a_critique_job_with_source_file(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, document={"file_id": "f", "file_unique_id": "u", "file_name": "DSCF0001.JPG", "file_size": 5_000_000}))
    (kind, p), = jobs(ctx)
    assert kind == "critique" and p["source"] == "file" and p["suffix"] == ".jpg"


def test_settings_flag_uses_the_readout_not_the_teacher(ctx, monkeypatch):
    bot = Bot(ctx)
    monkeypatch.setattr(bot, "settings_readout", lambda file_id, name: ctx.server.calls.append(("readout", {})))
    bot.handle(msg(ctx, document={"file_id": "f", "file_name": "a.jpg", "file_size": 10}, caption="#settings"))
    assert jobs(ctx) == [] and ("readout", {}) in ctx.server.calls


def test_reply_to_teacher_message_routes_to_followup_and_photo_reply_to_compare(ctx):
    ctx.conn.execute("INSERT INTO critiques(id, created_at, kind, source, status) VALUES(7, 0, 'single', 'photo', 'done')")
    ctx.conn.execute("INSERT INTO teacher_messages(message_id, critique_id, role) VALUES(501, 7, 'teacher')")
    bot = Bot(ctx)
    bot.handle(msg(ctx, "why f/5.6?", reply_to_message={"message_id": 501}))
    bot.handle(msg(ctx, photo=PHOTO, reply_to_message={"message_id": 501}, message_id=11))
    kinds = [k for k, _ in jobs(ctx)]
    assert kinds == ["followup", "compare"]
    assert jobs(ctx)[0][1]["critique_id"] == 7 and jobs(ctx)[1][1]["parent_id"] == 7


def test_text_without_reply_is_a_question_and_non_owner_ignored(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, "how do I zone focus?", user=99))
    bot.handle(msg(ctx, "how do I zone focus?"))
    assert jobs(ctx) == [("question", {"chat_id": 42, "question": "how do I zone focus?", "reply_to": 10})]


def test_album_becomes_one_series_job(ctx, monkeypatch):
    bot = Bot(ctx)
    import threading
    timers = []
    monkeypatch.setattr(threading, "Timer", lambda *a, **k: timers.append((a, k)) or SimpleNamespace(cancel=lambda: None, start=lambda: None, daemon=True))
    for i in range(3):
        bot.handle(msg(ctx, photo=PHOTO, media_group_id="g1", message_id=20 + i, caption="my series" if i == 0 else None))
    assert jobs(ctx) == []
    bot.teacher.album_done("g1")
    (kind, p), = jobs(ctx)
    assert kind == "series" and len(p["parts"]) == 3 and p["caption"] == "my series"


def test_buttons_route_to_preset_followups(ctx):
    ctx.conn.execute("INSERT INTO critiques(id, created_at, kind, source, status) VALUES(3, 0, 'single', 'photo', 'done')")
    bot = Bot(ctx)
    bot.handle({"update_id": 2, "callback_query": {"id": "cq1", "from": {"id": 42}, "data": "t:s:3",
                                                   "message": {"message_id": 77, "chat": {"id": 42}}}})
    assert ctx.server.calls[0][0] == "answerCallbackQuery"
    (kind, p), = jobs(ctx)
    assert kind == "followup" and p["question"].startswith("Explain the top fix again")


def test_level_style_quick_commands(ctx):
    bot = Bot(ctx)
    bot.handle(msg(ctx, "/level advanced"))
    bot.handle(msg(ctx, "/style socratic"))
    bot.handle(msg(ctx, "/quick on"))
    t = teacher_mod.Teacher(ctx)
    assert (t.level(), t.style(), t.detail()) == ("advanced", "socratic", "quick")


# the pipeline with a fake Claude ----------------------------------------------------------------------------------

def test_critique_pipeline_end_to_end(ctx, monkeypatch, tmp_path):
    ctx.conn.execute("INSERT INTO recipes(id, name, source_url, settings_hash, film_simulation, settings_json) "
                     "VALUES(1,'Easy Reala Ace','https://fujixweekly.com/era/','h','REALA ACE','{}')")
    ctx.conn.execute("INSERT INTO people(name, kind, identity, official_url, facts_json, themes_json, verified_at) "
                     "VALUES('Fan Ho','master','x','https://fanhophotography.com/','[]','[]',1)")
    img = Image.new("RGB", (1200, 800), (120, 130, 140))
    buf = tmp_path / "p.jpg"
    img.save(buf, "JPEG")
    monkeypatch.setattr(Telegram, "get_file_bytes", lambda self, fid: buf.read_bytes())
    calls = []

    def fake_call(self, bucket, prompt, stdin, **kw):
        calls.append((bucket, kw.get("resume")))
        return Result(data=good(), session_id="sess1", cost_usd=0.05, model="sonnet")
    monkeypatch.setattr(teacher_mod.Teacher, "call", fake_call)
    bot = Bot(ctx)
    bot.handle(msg(ctx, photo=PHOTO, caption="quiet alley"))
    assert teacher_mod.drain(ctx) == 1
    methods = [c[0] for c in ctx.server.calls]
    assert "sendChatAction" in methods and "sendPhoto" in methods and methods.count("sendMessage") == 1
    guidance = [b for m, b in ctx.server.calls if m == "sendMessage"][0]
    assert guidance["reply_markup"]["inline_keyboard"] and "Top fix" in guidance["text"] and "unknown" in guidance["text"]
    row = ctx.conn.execute("SELECT * FROM critiques").fetchone()
    assert row["status"] == "done" and row["overall"] == 3.1 and row["session_id"] == "sess1" and not row["exif_json"]
    assert json.loads(row["tags_json"]) == ["no_clear_subject"]
    assert (ctx.s.data_dir / "teacher" / str(row["id"]) / "photo.jpg").exists()
    assert ctx.conn.execute("SELECT COUNT(*) FROM teacher_messages").fetchone()[0] == 2
    assert calls == [("teacher_critique", None)]


def test_usage_limit_requeues_and_tells_me(ctx, monkeypatch, tmp_path):
    from x100bot.claude import UsageLimit
    from datetime import datetime
    img = Image.new("RGB", (600, 400), (120, 130, 140))
    buf = tmp_path / "p.jpg"
    img.save(buf, "JPEG")
    monkeypatch.setattr(Telegram, "get_file_bytes", lambda self, fid: buf.read_bytes())
    reset = datetime(2026, 10, 6, 15, 45, tzinfo=__import__("tests.conftest", fromlist=["SGT"]).SGT)

    def limited(self, bucket, prompt, stdin, **kw):
        raise UsageLimit("weekly", reset, "hit your weekly limit")
    monkeypatch.setattr(teacher_mod.Teacher, "call", limited)
    bot = Bot(ctx)
    bot.handle(msg(ctx, photo=PHOTO))
    assert teacher_mod.drain(ctx) == 0
    row = ctx.conn.execute("SELECT status, not_before FROM teacher_queue").fetchone()
    assert row["status"] == "pending" and row["not_before"] == reset.timestamp()
    assert any("resting" in b.get("text", "") for m, b in ctx.server.calls if m == "sendMessage")


def test_two_failures_send_the_fallback_critique(ctx, monkeypatch, tmp_path):
    from x100bot.claude import ClaudeError
    img = Image.new("RGB", (600, 400), (255, 255, 255))   # blown out, so a hint fires
    buf = tmp_path / "p.jpg"
    img.save(buf, "JPEG")
    monkeypatch.setattr(Telegram, "get_file_bytes", lambda self, fid: buf.read_bytes())
    monkeypatch.setattr(teacher_mod.Teacher, "call", lambda self, *a, **k: (_ for _ in ()).throw(ClaudeError("boom")))
    bot = Bot(ctx)
    bot.handle(msg(ctx, photo=PHOTO))
    assert teacher_mod.drain(ctx, max_jobs=1) == 1
    caps = [b for m, b in ctx.server.calls if m == "sendPhoto"]
    texts = [b.get("text", "") for m, b in ctx.server.calls if m == "sendMessage"]
    assert caps and any("clipped" in t.lower() for t in texts)
    row = ctx.conn.execute("SELECT used_fallback, status FROM critiques").fetchone()
    assert row["used_fallback"] == 1 and row["status"] == "done"
    assert q.status(ctx).get("pending") == 1   # the full teacher will try again
