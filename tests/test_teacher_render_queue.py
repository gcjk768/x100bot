import json
from types import SimpleNamespace

import pytest

from tests.conftest import FIX
from tests.test_guardrails import good
from x100bot import teacher_queue as q
from x100bot import teacher_render as tr

ROOT = FIX.parent.parent
MAP = str(ROOT / "camera" / "settings_menu_map.yaml")
EXIF = {"model": "X100VI", "aperture": 2.0, "shutter_text": "1/125", "iso": 125, "exposure_compensation": 0.0,
        "exposure_mode": 3, "focus_mode": "Auto", "af_area": "Single Point", "film_simulation": "Classic Chrome",
        "dynamic_range": "Auto"}
MEAS = {"highlights_clipped_pct": 0.4, "shadows_crushed_pct": 1.1, "tilt": {"tilt_degrees": 0.3, "lines": 6, "tilted": False},
        "sharpness": {"sharpest_cell": "middle centre"}, "colour": {"cast": "warm"}}


def test_overall_is_weighted_one_decimal(settings):
    r = good()
    assert tr.overall(r["scores"], settings.teacher.score_weights) == 3.1


def test_caption_under_1024_and_has_scores(settings):
    r = good()
    r["top_fix"]["why"] = "x" * 2000
    c = tr.caption(r, 3.1, " (your 30 day average is 2.9)")
    assert len(c) <= 1024 and "Composition 2/5 · Light 4/5" in c and "Overall 3.1/5 (your 30 day average is 2.9)" in c


def test_settings_table_aligned_keep_and_unknown():
    r = good()
    t = tr.settings_table(r["settings"], EXIF, True)
    lines = t.replace("<pre>", "").replace("</pre>", "").split("\n")
    head = lines[0]
    col = head.index("Try")
    assert all(ln[col:].strip() == ln[col:].strip() and len(ln) > col for ln in lines[1:]), lines
    assert any(ln.startswith("Shutter") and ln.rstrip().endswith("keep") for ln in lines)
    assert any(ln.startswith("Aperture") and "f/2" in ln and "f/5.6" in ln for ln in lines)
    assert not any(ln.startswith("ND filter") for ln in lines)   # neither known nor suggested: row omitted
    t2 = tr.settings_table(r["settings"], None, False)
    assert "unknown" in t2 and "None" not in t2


def test_guidance_sections_and_split(settings):
    r = good()
    secs = tr.guidance(r, exif=EXIF, exif_available=True, measurements=MEAS, map_path=MAP,
                       recipe={"name": "Easy Reala Ace", "source_url": "https://fujixweekly.com/era/"},
                       photographer={"name": "Fan Ho", "official_url": "https://fanhophotography.com/"})
    text = tr.join(secs)
    assert len(text) == 1 and "How to set it:" in text[0] and "AF/MF SETTING &gt; AF MODE" in text[0]
    assert "Measured: highlights clipped 0.4%" in text[0] and "None" not in text[0]
    r["composition_notes"] = "y" * 3000
    r["light_notes"] = "z" * 2000
    parts = tr.join(tr.guidance(r, exif=EXIF, exif_available=True, measurements=MEAS, map_path=MAP))
    assert len(parts) == 2 and all(len(p) <= 4000 for p in parts)
    assert parts[1].startswith("<b>")   # split at a section boundary


def test_quick_mode_is_short(settings):
    r = good()
    parts = tr.join(tr.guidance(r, exif=EXIF, exif_available=True, measurements=MEAS, map_path=MAP, quick=True))
    assert "What I see" not in parts[0] and "Top fix" in parts[0] and "Exercise" in parts[0]


def test_buttons_and_fallback():
    b = tr.buttons(12, True)
    assert b["inline_keyboard"][1][-1]["callback_data"] == "t:c:12" and all(len(x["callback_data"]) < 64 for row in b["inline_keyboard"] for x in row)
    fb = tr.fallback_result(MEAS, EXIF, [{"advice": "Clipped highlights.", "settings": {"exposure_comp": "-2/3"}, "tag": "highlights_clipped"}])
    assert fb["summary"].startswith("Quick check") and fb["settings"]["exposure_comp"] == "-2/3"


# queue --------------------------------------------------------------------------------------------------------

@pytest.fixture
def qx(settings, conn, make_limiter):
    return SimpleNamespace(s=settings, conn=conn, lim=make_limiter())


def test_queue_caps_and_order(qx):
    for i in range(qx.s.limits.teacher.queue_max):
        q.enqueue(qx, "critique", {"i": i})
    with pytest.raises(q.QueueFull):
        q.enqueue(qx, "critique", {})
    seen = []
    q.drain(qx, lambda job: seen.append(json.loads(job["payload_json"])["i"]) or "done", max_jobs=3)
    assert seen == [0, 1, 2]
    assert q.status(qx)["done"] == 3


def test_usage_limit_requeues_with_not_before(qx, clock):
    q.enqueue(qx, "critique", {})
    reset = clock.t + 3600
    assert q.drain(qx, lambda job: reset) == 0
    row = qx.conn.execute("SELECT status, not_before FROM teacher_queue").fetchone()
    assert row["status"] == "pending" and row["not_before"] == reset
    assert q.claim(qx) is None          # not due yet
    clock.t = reset + 1
    assert q.claim(qx) is not None


def test_plan_job_pauses_the_queue(qx, tmp_path):
    q.enqueue(qx, "critique", {})
    lock = qx.s.data_dir / "locks" / "plan.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1")
    assert q.drain(qx, lambda job: "done") == 0
    lock.unlink()
    assert q.drain(qx, lambda job: "done") == 1


def test_job_never_processed_twice(qx):
    q.enqueue(qx, "critique", {})
    j1 = q.claim(qx)
    assert q.claim(qx) is None
    q.finish(qx, j1["id"])
    assert q.claim(qx) is None


def test_daily_caps_hold(qx):
    for _ in range(qx.s.limits.teacher.max_series_per_day):
        qx.lim.report(qx.lim.acquire("teacher_series"), "ok")
    assert q.can_run(qx, "series") and q.can_run(qx, "critique") is None
