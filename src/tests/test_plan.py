import json
import re
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import FIX, SGT
from x100bot import light, planner, render, weather

WEATHER = json.loads((FIX / "openmeteo_2026-10-02.json").read_text())
DASHES = re.compile(r"[‒–—―]| - |--")


# light and weather ------------------------------------------------------------------------------------------------

def test_astral_times_for_a_fixed_date(settings):
    lt = light.compute(settings.location, date(2026, 10, 2), "Asia/Singapore")
    t = lt.as_text()
    assert (t["sunrise"], t["sunset"]) == ("06:51", "18:56")
    assert lt.golden_evening_start < lt.sunset < lt.golden_evening_end <= lt.blue_end
    assert lt.golden_morning_start < lt.sunrise < lt.golden_morning_end
    assert light.hour_light(lt, 18) == "golden_hour" and light.hour_light(lt, 20) == "night"
    assert light.hour_light(lt, 12) is None


def test_hour_labels_from_saved_response(settings):
    hours = weather.hours_for(WEATHER, "2026-10-02", settings.weather)
    assert len(hours) == 24 and set(hours.values()) <= {"rain", "overcast", "sunny", "partly cloudy"}
    assert hours[16] == "rain" and hours[9] == "overcast"
    assert weather.periods(hours) == {"morning": "overcast", "afternoon": "rain", "evening": "rain"}


@pytest.mark.parametrize("cloud,rain,expected", [(10, 80, "rain"), (90, 10, "overcast"), (20, 0, "sunny"),
                                                 (50, 0, "partly cloudy"), (70, 59, "overcast"), (30, 60, "rain")])
def test_label_thresholds(settings, cloud, rain, expected):
    assert weather.label(cloud, rain, settings.weather) == expected


def test_weather_call_through_the_openmeteo_bucket(settings, make_limiter):
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json=WEATHER)
    lim = make_limiter()
    assert weather.fetch(settings, lim, transport=httpx.MockTransport(handler)) == WEATHER
    assert seen["hourly"] == "cloud_cover,precipitation_probability,weather_code" and seen["forecast_days"] == "2"
    assert lim.state("openmeteo")["day_count"] == 1


def test_weather_failure_returns_none(settings, make_limiter):
    assert weather.fetch(settings, make_limiter(), transport=httpx.MockTransport(lambda r: httpx.Response(500))) is None


# planner ----------------------------------------------------------------------------------------------------------

def settings_for(sim):
    return {"film_simulation": sim, "grain_roughness": "WEAK", "grain_size": "SMALL", "color_chrome_effect": "OFF",
            "color_chrome_fx_blue": "OFF", "white_balance": "AUTO", "wb_red": 0, "wb_blue": 0,
            "dynamic_range": "400%", "highlight": 0, "shadow": 0, "color": 0, "sharpness": 0, "high_iso_nr": -4,
            "clarity": 0, "iso": "Auto, up to ISO 6400", "exposure_compensation": "0"}


def add_recipe(conn, name, sim, tags=("any",), posted=None, sensor="X-Trans V"):
    conn.execute("INSERT INTO recipes(name, author, source_url, sensor, compat_label, film_simulation, settings_json,"
                 " settings_hash, light_tags_json, last_posted_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (name, "A", f"https://fujixweekly.com/{name}/", sensor, "Made for X-Trans V", sim,
                  json.dumps(settings_for(sim)), name, json.dumps(list(tags)), posted))


@pytest.fixture
def ctx(settings, conn, make_limiter):
    alerts = []
    return SimpleNamespace(s=settings, conn=conn, lim=make_limiter(), alert=lambda k, t: alerts.append((k, t)),
                           alerts=alerts)


DAY = date(2026, 10, 5)


def plan(ctx, data=WEATHER, day=DAY):
    data = json.loads(json.dumps(data).replace("2026-10-02", day.isoformat())) if data else None
    return planner.plan_day(ctx, day, weather_data=data)


def recipe_slots(d):
    return [sl for sl in d.slots if sl.type == "recipe"]


def test_no_recipe_within_repeat_days(ctx):
    recent = datetime(2026, 9, 20, tzinfo=SGT).timestamp()   # 15 days before
    old = datetime(2026, 7, 1, tzinfo=SGT).timestamp()       # 96 days before
    for i, sim in enumerate(["REALA ACE", "CLASSIC CHROME", "ACROS", "CLASSIC Neg.", "PROVIA/STANDARD"]):
        add_recipe(ctx.conn, f"recent{i}", sim, posted=recent)
    for i, sim in enumerate(["REALA ACE", "CLASSIC CHROME", "ACROS", "CLASSIC Neg.", "PROVIA/STANDARD", "Velvia/VIVID"]):
        add_recipe(ctx.conn, f"ok{i}", sim, posted=old if i % 2 else None, tags=("any", "golden_hour", "night"))
    names = [sl.facts["recipe_name"] for sl in recipe_slots(plan(ctx))]
    assert len(names) == 5 and not any(n.startswith("recent") for n in names)
    assert len(set(names)) == 5


def test_never_the_same_film_simulation_twice_in_a_row(ctx):
    for i in range(6):
        add_recipe(ctx.conn, f"reala{i}", "REALA ACE", tags=("any", "golden_hour", "night"))
    for i in range(3):
        add_recipe(ctx.conn, f"chrome{i}", "CLASSIC CHROME", tags=("any", "golden_hour", "night"))
    sims = [sl.facts["film_simulation"] for sl in recipe_slots(plan(ctx))]
    assert all(a != b for a, b in zip(sims, sims[1:])), sims


def test_golden_hour_and_night_slots_get_matching_tags(ctx):
    add_recipe(ctx.conn, "warm", "CLASSIC Neg.", tags=("golden_hour",))
    add_recipe(ctx.conn, "neon", "CLASSIC CHROME", tags=("night",))
    for i, sim in enumerate(["REALA ACE", "PROVIA/STANDARD", "ASTIA/SOFT", "Velvia/VIVID"]):
        add_recipe(ctx.conn, f"day{i}", sim, tags=("overcast", "sunny"))
    by_hour = {sl.hour: sl for sl in plan(ctx).slots}
    assert by_hour["18"].facts["recipe_name"] == "warm" and by_hour["18"].facts["light_label"] == "golden hour"
    assert by_hour["20"].facts["recipe_name"] == "neon"
    assert by_hour["17"].facts["recipe_name"] == "warm"   # the golden hour plan points at the 18:00 recipe


def test_x_trans_v_preferred(ctx):
    add_recipe(ctx.conn, "iv", "REALA ACE", sensor="X-Trans IV")
    add_recipe(ctx.conn, "v", "CLASSIC CHROME")
    assert recipe_slots(plan(ctx))[0].facts["recipe_name"] == "v"


def add_person(conn, name, kind, themes=("light",)):
    conn.execute("INSERT INTO people(name, kind, identity, official_url, urls_json, facts_json, themes_json,"
                 " verified_at) VALUES(?,?,?,?,?,?,?,1)",
                 (name, kind, "photographer", f"https://{name}.example/", "[]",
                  json.dumps([{"note": "Composes with layers.", "url": f"https://{name}.example/"}]), json.dumps(themes)))


def test_photographer_alternates_master_and_educator_by_day(ctx):
    for i in range(3):
        add_person(ctx.conn, f"master{i}", "master")
        add_person(ctx.conn, f"educator{i}", "educator")
    kinds = []
    for day in (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)):
        sl = next(s for s in plan(ctx, day=day).slots if s.hour == "12")
        kinds.append(sl.facts["kind"])
    assert kinds[0] != kinds[1] and kinds[1] != kinds[2] and kinds[2] != kinds[3]


def test_missing_stock_becomes_distinct_learning_tips_and_alerts(ctx):
    d = plan(ctx)
    subs = [sl for sl in d.slots if sl.planned_type != sl.type]
    assert subs and all(sl.type == "learning_tip" for sl in subs)
    topics = [sl.facts["topic"] for sl in d.slots if sl.type == "learning_tip"]
    assert len(set(topics)) == min(len(topics), len(ctx.s.learning.learning_topics))
    assert ctx.alerts and ctx.alerts[0][0] == "stock_low"


def test_plan_works_when_the_weather_call_fails(ctx):
    add_recipe(ctx.conn, "a", "REALA ACE", tags=("any", "golden_hour", "night"))
    d = plan(ctx, data=None)
    brief = d.slots[0]
    assert brief.type == "brief" and brief.facts["forecast_note"].startswith("Forecast unavailable")
    text, _ = planner.render_slot(brief, planner.fallback_fields(ctx.s, brief))
    assert "Forecast unavailable" in text and "Sunrise" in text


def test_sixteen_slots_and_sunday_recap(ctx):
    assert len(plan(ctx).slots) == 16
    sunday = plan(ctx, day=date(2026, 10, 11))
    assert {sl.hour: sl.type for sl in sunday.slots}["21"] == "weekly_recap"


# renderer ---------------------------------------------------------------------------------------------------------

def full_day(ctx):
    for i, sim in enumerate(["REALA ACE", "CLASSIC CHROME", "ACROS", "CLASSIC Neg.", "PROVIA/STANDARD", "Velvia/VIVID"]):
        add_recipe(ctx.conn, f"r{i}", sim, tags=("any", "golden_hour", "night"))
    add_person(ctx.conn, "p1", "master")
    add_person(ctx.conn, "p2", "educator")
    d = plan(ctx)
    return [(sl, *planner.render_slot(sl, planner.fallback_fields(ctx.s, sl))) for sl in d.slots]


def test_renderer_never_prints_none_and_stays_short(ctx):
    for sl, text, _ in full_day(ctx):
        assert "None" not in text and "null" not in text, text
        assert len(text) < 4096
        dangling = [ln for ln in text.splitlines() if ln.rstrip().endswith(":") and not ln.endswith("and ask:")]
        assert not dangling, text   # no label left without its value


def test_no_dashes_in_prose_lines(ctx):
    for sl, text, _ in full_day(ctx):
        for line in text.splitlines():
            if "href=" in line or line.startswith("━"):
                continue
            assert not DASHES.search(line), line


def test_one_message_per_item_with_its_own_preview(ctx):
    rows = full_day(ctx)
    assert len(rows) == 16
    for sl, text, preview in rows:
        if sl.type == "recipe":
            assert preview == sl.facts["source_url"] and text.count("<b>") == 2   # section title and recipe name
        elif sl.type == "photographer":
            assert preview == sl.facts["official_url"]
        elif sl.type in ("brief", "camera_tip", "drill", "learning_tip", "review", "weekly_recap", "golden_hour"):
            assert preview is None


def test_render_drops_empty_lines_and_escapes():
    text = render.render("learning_tip", {"topic": "a & b", "title": "<script>", "body": None, "action": ""})
    assert text == "📚 <b>LEARN</b> · a &amp; b\n\n📚 <b>&lt;script&gt;</b>\n#learn"


def test_recipe_lines_print_settings_exactly(ctx):
    vals = render.recipe_values({**settings_for("CLASSIC CHROME"), "white_balance": "COLOR TEMPERATURE",
                                 "wb_kelvin": 5500, "wb_red": 4, "wb_blue": -7, "highlight": -1.5})
    assert vals["white_balance"] == "5500K, R +4, B -7" and vals["highlight"] == "-1.5" and vals["clarity"] == "0"
