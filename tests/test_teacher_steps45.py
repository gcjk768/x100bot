import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from tests.conftest import SGT, Bot as FakeServer
from tests.test_guardrails import good
from x100bot import progress, teacher as teacher_mod
from x100bot.bot import Bot
from x100bot.claude import Result
from x100bot.telegram import Telegram


@pytest.fixture
def ctx(settings, conn, make_limiter, monkeypatch, tmp_path):
    server = FakeServer()
    lim = make_limiter()
    tg = Telegram("T", lim, settings.limits.telegram, transport=httpx.MockTransport(server.handler))
    x = SimpleNamespace(s=settings, conn=conn, lim=lim, tg=tg, alert=lambda k, t: None, server=server)
    monkeypatch.setattr("x100bot.teacher_intake.TeacherIntake.kick", lambda self: None)
    img = tmp_path / "p.jpg"
    Image.new("RGB", (900, 600), (120, 130, 140)).save(img, "JPEG")
    monkeypatch.setattr(Telegram, "get_file_bytes", lambda self, fid: img.read_bytes())
    return x


def msg(text=None, user=42, **extra):
    m = {"message_id": extra.pop("message_id", 10), "chat": {"id": user, "type": "private"}, "from": {"id": user}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


PHOTO = [{"file_id": "big", "file_unique_id": "u2", "file_size": 900}]


def fake(data, calls=None):
    def call(self, bucket, prompt, stdin, **kw):
        if calls is not None:
            calls.append((bucket, kw.get("schema"), kw.get("resume"), stdin))
        return Result(data=data, session_id="s2", cost_usd=0.03, model="sonnet")
    return call


def seed_parent(ctx, monkeypatch):
    ctx.conn.execute("INSERT INTO recipes(id, name, source_url, settings_hash, film_simulation, settings_json) "
                     "VALUES(1,'Easy Reala Ace','https://fujixweekly.com/era/','h','REALA ACE','{}')")
    ctx.conn.execute("INSERT INTO people(name, kind, identity, official_url, facts_json, themes_json, verified_at) "
                     "VALUES('Fan Ho','master','x','https://fanhophotography.com/','[]','[]',1)")
    monkeypatch.setattr(teacher_mod.Teacher, "call", fake(good()))
    bot = Bot(ctx)
    bot.handle(msg(photo=PHOTO, caption="before"))
    teacher_mod.drain(ctx)
    row = ctx.conn.execute("SELECT id, session_id FROM critiques").fetchone()
    mids = [r[0] for r in ctx.conn.execute("SELECT message_id FROM teacher_messages")]
    return bot, row["id"], mids


def test_compare_resumes_the_parent_session_and_scores_the_after_photo(ctx, monkeypatch):
    bot, pid, mids = seed_parent(ctx, monkeypatch)
    calls = []
    cmp_result = {"verdict": "You applied the fix.", "fix_applied": True, "improved": ["clearer subject"], "still_to_fix": ["edges"],
                  "scores": {"composition": 4, "light": 4, "exposure": 3, "focus": 4, "colour": 4, "moment": 3},
                  "next_step": "Wait for a person.", "tags": [], "strength_tags": ["strong_subject"]}
    monkeypatch.setattr(teacher_mod.Teacher, "call", fake(cmp_result, calls))
    bot.handle(msg(photo=PHOTO, reply_to_message={"message_id": mids[0]}, message_id=30))
    assert teacher_mod.drain(ctx) == 1
    assert calls and calls[0][1] == "compare_schema.json" and calls[0][2] == "s2"
    rows = ctx.conn.execute("SELECT kind, parent_id, overall FROM critiques ORDER BY id").fetchall()
    assert [r["kind"] for r in rows] == ["single", "compare"] and rows[1]["parent_id"] == pid and rows[1]["overall"] == 3.7
    text = [b["text"] for m, b in ctx.server.calls if m == "sendMessage"][-1]
    assert "Before and after" in text and "Top fix applied: yes" in text and "Composition 4/5 (+2)" in text
    assert (Path(ctx.conn.execute("SELECT folder FROM critiques WHERE id=?", (pid,)).fetchone()[0]) / "after.jpg").exists()


def test_series_job_saves_numbered_frames_and_renders(ctx, monkeypatch):
    calls = []
    result = {"theme_seen": "A quiet morning market.", "strongest_index": 2, "strongest_why": "Best light.", "weakest_index": 1,
              "weakest_why": "No subject.", "order": [2, 3, 1], "per_image": [{"index": 1, "note": "empty"}, {"index": 2, "note": "good"}, {"index": 3, "note": "fine"}],
              "consistency": "Colour matches.", "missing_shot": "A close detail.", "exercise": "Shoot a detail."}
    monkeypatch.setattr(teacher_mod.Teacher, "call", fake(result, calls))
    bot = Bot(ctx)
    bot.teacher.enqueue(42, "series", {"chat_id": 42, "caption": "market", "parts": [{"file_id": f"f{i}", "source": "photo", "suffix": ".jpg"} for i in range(3)]})
    assert teacher_mod.drain(ctx) == 1
    assert calls[0][1] == "series_schema.json" and len(calls[0][3]["images"]) == 3
    folder = Path(ctx.conn.execute("SELECT folder FROM critiques WHERE kind='series'").fetchone()[0])
    assert all((folder / f"{i}.jpg").exists() for i in (1, 2, 3))
    text = [b["text"] for m, b in ctx.server.calls if m == "sendMessage"][-1]
    assert "Strongest</b>: frame 2" in text and "Order to show them</b>: 2, 3, 1" in text and "missing shot" in text.lower()


def test_ask_and_plan(ctx, monkeypatch):
    calls = []
    result = {"answer": "Zone focus at three metres.", "steps": ["Set M", "Turn the ring"], "settings": {"focus_mode": "M", "aperture": "f/8"},
              "recipe_name": "", "photographer_name": "", "plan": {"when": "Go at 18:20", "where_to_stand": "the corner", "look_for": "light", "backup": "the arcade"}}
    monkeypatch.setattr(teacher_mod.Teacher, "call", fake(result, calls))
    monkeypatch.setattr("x100bot.weather.fetch", lambda s, lim, transport=None: None)
    bot = Bot(ctx)
    bot.handle(msg("how do I zone focus?"))
    bot.handle(msg("/plan street portraits, Chinatown, tomorrow evening"))
    assert teacher_mod.drain(ctx) == 2
    assert calls[0][1] == "ask_schema.json" and "plan_request" not in calls[0][3]
    pr = calls[1][3]["plan_request"]
    assert pr["what"] == "street portraits" and pr["where"] == "Chinatown" and pr["light"]["sunset"]
    texts = [b["text"] for m, b in ctx.server.calls if m == "sendMessage"]
    assert "Zone focus" in texts[0] and "How to set it:" in texts[0] and "Focus" in texts[0]
    assert "Plan" in texts[1] and "Light on" in texts[1]


# progress -----------------------------------------------------------------------------------------------------

def seed_history(ctx, tmp_path):
    """Eight weeks of fixture critiques with rising scores and recurring tags."""
    now = datetime(2026, 10, 4, 20, 0, tzinfo=SGT)   # a Sunday
    base = tmp_path / "hist"
    for w in range(8):
        for k in range(3):
            at = now - timedelta(days=7 * w + k * 2 + 1)
            sc = {"composition": min(5, 2 + w // 3), "light": 3, "exposure": min(5, 2 + w // 2), "focus": 4, "colour": 3, "moment": 2 + (k % 2)}
            overall = progress.teacher_render.overall(sc, ctx.s.teacher.score_weights)
            folder = base / f"{w}_{k}"
            folder.mkdir(parents=True)
            Image.new("RGB", (300, 200), (100, 100, 100)).save(folder / "overlay.jpg", "JPEG")
            ctx.conn.execute("INSERT INTO critiques(created_at, kind, source, folder, result_json, scores_json, overall, tags_json,"
                             " strength_tags_json, used_fallback, status) VALUES(?,?,?,?,?,?,?,?,?,0,'done')",
                             (at.timestamp(), "single", "photo", str(folder), json.dumps({"summary": f"photo {w}-{k}"}), json.dumps(sc),
                              overall, json.dumps(["flat_light"] if k == 0 else ["tilted_horizon"]), json.dumps(["strong_light"])))
    return now


def test_weekly_numbers_match_fixture(ctx, tmp_path):
    now = seed_history(ctx, tmp_path)
    n = progress.weekly_numbers(ctx, now)
    assert n["week_start"] == "2026-09-28" and n["critiques"] == 3
    assert n["avg_this"]["composition"] == 2.0 and n["avg_this"]["focus"] == 4.0
    assert n["top_issues"][0] in ("flat_light", "tilted_horizon") and len(n["weeks"]) == 8
    assert n["best"]["overall"] >= 2.5 and n["best"]["folder"]
    prof = progress.learning_profile(ctx)
    assert prof["critiques"] == 12 and prof["issue_counts"]["tilted_horizon"] == 8


def test_chart_and_weekly_report(ctx, tmp_path, monkeypatch):
    now = seed_history(ctx, tmp_path)
    png = progress.chart(progress.weekly_numbers(ctx, now), tmp_path / "chart.png")
    assert png.exists() and Image.open(png).size[0] > 1000
    ctx.lim.clock.t = now.timestamp()
    n = progress.weekly_report(ctx, 42, now=now, with_words=False)
    methods = [m for m, _ in ctx.server.calls]
    assert methods.count("sendPhoto") == 2 and methods.count("sendMessage") == 1
    text = [b["text"] for m, b in ctx.server.calls if m == "sendMessage"][0]
    assert "WEEKLY PROGRESS" in text and "Critiques this week: <code>3</code>" in text and "One setup change" in text


def test_forget_removes_files_rows_and_session(ctx, monkeypatch, tmp_path):
    bot, cid, mids = seed_parent(ctx, monkeypatch)
    folder = Path(ctx.conn.execute("SELECT folder FROM critiques WHERE id=?", (cid,)).fetchone()[0])
    home = tmp_path / "home" / ".claude" / "projects" / "x"
    home.mkdir(parents=True)
    (home / "s2.jsonl").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    bot.handle(msg("/forget", reply_to_message={"message_id": mids[0]}))
    assert not folder.exists() and not (home / "s2.jsonl").exists()
    assert ctx.conn.execute("SELECT COUNT(*) FROM critiques").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM teacher_messages").fetchone()[0] == 0
    assert "Forgotten" in ctx.server.calls[-1][1]["text"]


def test_history_and_progress_commands(ctx, tmp_path):
    seed_history(ctx, tmp_path)
    bot = Bot(ctx)
    bot.handle(msg("/history"))
    bot.handle(msg("/progress"))
    texts = [b["text"] for m, b in ctx.server.calls if m == "sendMessage"]
    assert "HISTORY" in texts[0] and texts[0].count("#") >= 10 and "PROGRESS" in texts[1]
