import json

import pytest

from tests.conftest import FIX
from x100bot.library import recipes
from x100bot.library.parsers import fujixweekly as fxw

FACTS = str(FIX.parent.parent / "camera" / "x100vi_facts.yaml")


def page(name):
    return (FIX / "fujixweekly" / f"{name}.html").read_text(encoding="utf-8")


def good():
    return {"film_simulation": "CLASSIC CHROME", "grain_roughness": "WEAK", "grain_size": "SMALL",
            "color_chrome_effect": "STRONG", "color_chrome_fx_blue": "WEAK", "white_balance": "DAYLIGHT",
            "wb_red": 2, "wb_blue": -4, "dynamic_range": "400%", "highlight": -1, "shadow": 1, "color": 2,
            "sharpness": -2, "high_iso_nr": -4, "clarity": 0, "iso": "Auto, up to ISO 6400",
            "exposure_compensation": "+1/3"}


def test_index_links():
    links = fxw.recipe_links(page("index_x_trans_v"))
    assert len(links) > 60
    assert ("Easy Reala Ace", "https://fujixweekly.com/2024/06/20/easy-reala-ace-fujifilm-x100vi-x-trans-v-film-simulation-recipe/") in links
    assert all(u.startswith("https://fujixweekly.com/20") for _, u in links)


def test_plain_x_trans_v_recipe():
    [r] = recipes.from_page(page("easy_reala_ace"), "https://fujixweekly.com/era/", "X-Trans V", FACTS)
    assert r.errors == []
    assert (r.name, r.author, r.compat_label) == ("Easy Reala Ace", "Ritchie Roesch", "Made for X-Trans V")
    s = r.settings
    assert (s["film_simulation"], s["dynamic_range"], s["white_balance"], s["wb_red"], s["wb_blue"]) == \
           ("REALA ACE", "400%", "AUTO", 0, 0)
    assert (s["highlight"], s["high_iso_nr"], s["iso"]) == (-1, -4, "Auto, up to ISO 6400")


def test_black_and_white_with_monochromatic_color():
    [r] = recipes.from_page(page("kodak_tmax_p3200"), "https://fujixweekly.com/tmax/", "X-Trans V", FACTS)
    assert r.errors == []
    assert r.settings["film_simulation"] == "ACROS"
    assert r.settings["monochromatic_color"] == {"wc": -1, "mg": -1}
    assert "color" not in r.settings   # no Color setting on monochrome, and none required
    assert (r.settings["white_balance"], r.settings["wb_kelvin"]) == ("COLOR TEMPERATURE", 5500)


def test_multi_recipe_page_with_d_range_priority():
    rs = recipes.from_page(page("wes_anderson_four"), "https://fujixweekly.com/wes/", "X-Trans V", FACTS)
    assert [r.name for r in rs] == ["Vibrant Arizona", "Indoor Angouleme"]   # X-Trans IV twins dropped
    va = rs[0]
    assert va.errors == []
    assert va.settings["d_range_priority"] == "STRONG" and "highlight" not in va.settings
    assert va.settings["color_chrome_fx_blue"] == "WEAK"   # the X-Trans V version
    assert va.source_url == "https://fujixweekly.com/wes/#vibrant-arizona"


@pytest.mark.parametrize("key,values", [
    ("grain_roughness", ["OFF", "WEAK", "STRONG"]), ("color_chrome_effect", ["OFF", "WEAK", "STRONG"]),
    ("color_chrome_fx_blue", ["OFF", "WEAK", "STRONG"]), ("dynamic_range", ["AUTO", "100%", "200%", "400%"]),
    ("white_balance", ["WHITE PRIORITY", "AUTO", "AMBIENCE PRIORITY", "CUSTOM 1", "DAYLIGHT", "SHADE",
                       "FLUORESCENT LIGHT-3", "INCANDESCENT", "UNDERWATER"]),
    ("highlight", [-2, -1.5, 0, 2.5, 4]), ("shadow", [-2, 4]), ("color", list(range(-4, 5))),
    ("sharpness", [-4, 4]), ("high_iso_nr", [-4, 4]), ("clarity", list(range(-5, 6))),
    ("wb_red", [-9, 9]), ("wb_blue", [-9, 9])])
def test_every_allowed_value_passes(key, values):
    for v in values:
        assert recipes.validate({**good(), key: v}, FACTS) == [], (key, v)


@pytest.mark.parametrize("key,value", [
    ("grain_roughness", "MEDIUM"), ("color_chrome_effect", "MAX"), ("dynamic_range", "800%"),
    ("white_balance", "TUNGSTEN"), ("highlight", 4.5), ("highlight", -2.5), ("highlight", 0.3), ("shadow", 5),
    ("color", 5), ("color", -5), ("sharpness", 4.5), ("high_iso_nr", -5), ("clarity", 6), ("wb_red", 10),
    ("wb_blue", -10), ("film_simulation", "KODACHROME"), ("exposure_compensation", "+6"), ("iso", "ISO 102400")])
def test_out_of_range_values_fail(key, value):
    assert recipes.validate({**good(), key: value}, FACTS)


def test_missing_required_setting_fails():
    s = good()
    del s["clarity"]
    assert "missing clarity" in recipes.validate(s, FACTS)


def test_monochromatic_color_range():
    mono = {**good(), "film_simulation": "ACROS+R FILTER", "monochromatic_color": {"wc": 18, "mg": -18}}
    del mono["color"]
    assert recipes.validate(mono, FACTS) == []
    assert recipes.validate({**mono, "monochromatic_color": {"wc": 19, "mg": 0}}, FACTS)


@pytest.mark.parametrize("sim", ["CLASSIC CHROME", "CLASSIC Neg.", "ETERNA/CINEMA", "ETERNA BLEACH BYPASS"])
def test_x_trans_iv_adaptation_lowers_fx_blue_for_the_four(sim):
    for before, after in (("STRONG", "WEAK"), ("WEAK", "OFF")):
        out = recipes.adapt({**good(), "film_simulation": sim, "color_chrome_fx_blue": before}, "X-Trans IV")
        assert out["color_chrome_fx_blue"] == after


@pytest.mark.parametrize("sim", ["PROVIA/STANDARD", "REALA ACE", "NOSTALGIC Neg.", "ACROS", "Velvia/VIVID"])
def test_adaptation_leaves_other_simulations_alone(sim):
    assert recipes.adapt({**good(), "film_simulation": sim, "color_chrome_fx_blue": "STRONG"}, "X-Trans IV") is None


def test_adaptation_only_for_x_trans_iv():
    assert recipes.adapt({**good(), "color_chrome_fx_blue": "STRONG"}, "X-Trans V") is None


def test_adapted_values_are_stored_separately(conn):
    raw = {"Film Simulation": "Classic Chrome", "Grain Effect": "Weak, Small", "Color Chrome Effect": "Strong",
           "Color Chrome FX Blue": "Strong", "White Balance": "Daylight, +2 Red & -4 Blue", "Dynamic Range": "DR400",
           "Highlight": "-1", "Shadow": "+1", "Color": "+2", "Sharpness": "-2", "High ISO NR": "-4", "Clarity": "0",
           "ISO": "Auto, up to ISO 6400", "Exposure Compensation": "+1/3"}
    r = recipes.build("Test", "A", "https://fujixweekly.com/t/", "X-Trans IV", raw, FACTS)
    assert r.compat_label == "Adapted from X-Trans IV"
    rid = recipes.store(conn, r, 0)
    row = conn.execute("SELECT * FROM recipes WHERE id=?", (rid,)).fetchone()
    assert json.loads(row["settings_json"])["color_chrome_fx_blue"] == "STRONG"
    assert json.loads(row["adapted_settings_json"])["color_chrome_fx_blue"] == "WEAK"


def test_duplicate_settings_are_stored_once(conn):
    [r] = recipes.from_page(page("easy_reala_ace"), "https://fujixweekly.com/era/", "X-Trans V", FACTS)
    assert recipes.store(conn, r, 0)
    r.name, r.source_url = "Same Look Other Name", "https://fujixweekly.com/other/"
    assert recipes.store(conn, r, 0) is None
    assert conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1


def test_invalid_recipe_is_not_stored(conn):
    r = recipes.build("Bad", "A", "https://x/", "X-Trans V", {"Film Simulation": "Classic Chrome"}, FACTS)
    assert r.errors and recipes.store(conn, r, 0) is None


def test_older_layout_x_trans_iv_recipe_is_adapted():
    [r] = recipes.from_page(page("retro_gold_x_trans_iv"), "https://fujixweekly.com/rg/", "X-Trans IV", FACTS)
    assert r.errors == [] and r.name == "Retro Gold" and r.compat_label == "Adapted from X-Trans IV"
    assert r.settings["film_simulation"] == "CLASSIC CHROME"
    assert r.settings["white_balance"] == "FLUORESCENT LIGHT-3" and (r.settings["wb_red"], r.settings["wb_blue"]) == (4, -6)
    assert r.settings["color_chrome_fx_blue"] == "STRONG" and r.adapted["color_chrome_fx_blue"] == "WEAK"


def test_per_sensor_value_takes_the_x_trans_v_one():
    s, _ = recipes.normalise({"Color Chrome FX Blue": "Strong (X-Trans IV), Weak (X-Trans V)"})
    assert s["color_chrome_fx_blue"] == "WEAK"
