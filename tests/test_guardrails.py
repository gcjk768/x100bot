import json

import pytest

from tests.conftest import FIX
from x100bot import guardrails as g

ROOT = FIX.parent.parent
FACTS = str(ROOT / "camera" / "x100vi_facts.yaml")
MENUS = str(ROOT / "camera" / "menus.yaml")
SCHEMA = str(ROOT / "prompts" / "teacher_schema.json")
RECIPES = {"Easy Reala Ace", "Kodak T-Max P3200"}
PEOPLE = {"Saul Leiter", "Fan Ho"}


def good():
    sc = lambda n: {"score": n, "reason": "fine"}
    return {"seen": "A quiet alley with raking light on the left wall.", "intent_assumed": "", "summary": "Nice alley, no subject",
            "scores": {"composition": sc(2), "light": sc(4), "exposure": sc(3), "focus": sc(4), "colour": sc(4), "moment": sc(2)},
            "what_works": ["The side light gives the wall texture"],
            "top_fix": {"title": "Give the frame a subject", "why": "The lines lead to nothing.", "principle": "Leading lines need something to lead to.",
                        "steps": ["Stand where you stood", "Wait for someone to walk into the light"]},
            "composition_notes": "The drain line leads in well.", "light_notes": "Warm side light.", "timing_and_place": "Same time tomorrow.",
            "settings": {"mode": "A", "aperture": "f/5.6", "shutter": "", "shutter_type": "", "iso": "AUTO2, max 6400, min shutter 1/250",
                         "exposure_comp": "-2/3", "focus_mode": "M", "af_area": "SINGLE POINT", "nd_filter": "", "teleconverter": "50",
                         "film_simulation": "CLASSIC CHROME", "dynamic_range": "400%", "recipe_name": "Easy Reala Ace"},
            "settings_why": "Zone focus at four metres keeps the alley sharp.", "reshoot_steps": ["Go back at the same time"],
            "edit_suggestions": "", "crop": {"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6, "aspect": "3:2", "reason": "tighter"},
            "straighten_degrees": -1.5, "exercise": "Wait ten minutes at one spot.",
            "learn_from": {"photographer_name": "Fan Ho", "why": "He waits for people to complete the light."},
            "tags": ["no_clear_subject"], "strength_tags": ["strong_light"], "question": "What would make it feel quiet?",
            "assignment_met": None, "assignment_note": "", "cannot_tell": ""}


def run(result, exif=True, tilt=None):
    return g.check_critique(result, facts_file=FACTS, menus_file=MENUS, schema_file=SCHEMA, recipe_names=RECIPES,
                            photographer_names=PEOPLE, exif_available=exif, measured_tilt=tilt)


def test_clean_critique_passes():
    r, errs = run(good())
    assert errs == [] and r["crop"] and r["straighten_degrees"] == -1.5


@pytest.mark.parametrize("key,value", [
    ("mode", "Av"), ("aperture", "f/1.4"), ("aperture", "f/22"), ("shutter", "1/5000"), ("shutter", "1/333"),
    ("iso", "AUTO4"), ("iso", "AUTO2, max 200"), ("iso", "AUTO1, min shutter 1/4000"), ("iso", "150"),
    ("exposure_comp", "+6"), ("exposure_comp", "+0.5"), ("focus_mode", "Z"), ("af_area", "SPOT"), ("nd_filter", "AUTO"),
    ("teleconverter", "85"), ("film_simulation", "KODACHROME"), ("dynamic_range", "800%"), ("recipe_name", "Not A Recipe")])
def test_each_invalid_setting_fails(key, value):
    r = good()
    r["settings"][key] = value
    assert run(r)[1], (key, value)


@pytest.mark.parametrize("key,value", [
    ("aperture", "f/2"), ("aperture", "F16"), ("shutter", "1/4000"), ("shutter", "2 s"), ("shutter", "30"),
    ("iso", "6400"), ("iso", "AUTO3"), ("iso", "AUTO1, max 3200, min shutter 1/125"), ("exposure_comp", "+1 1/3"),
    ("exposure_comp", "0"), ("exposure_comp", "-5"), ("focus_mode", "C"), ("af_area", "WIDE/TRACKING"), ("nd_filter", "ON"),
    ("teleconverter", "70mm"), ("film_simulation", "ACROS+R FILTER"), ("dynamic_range", "AUTO"), ("recipe_name", "")])
def test_each_valid_setting_passes(key, value):
    r = good()
    r["settings"][key] = value
    assert run(r)[1] == [], (key, value)


def test_fast_shutter_needs_es():
    r = good()
    r["settings"]["shutter"] = "1/8000"
    assert run(r)[1]
    r["settings"]["shutter_type"] = "ES"
    assert run(r)[1] == []


@pytest.mark.parametrize("crop", [{"x": 0.5, "y": 0.5, "w": 0.6, "h": 0.6, "aspect": "3:2", "reason": "x"},
                                  {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.6, "aspect": "3:2", "reason": "x"},
                                  {"x": 0.1, "y": 0.1, "w": 0.6, "h": 0.6, "aspect": "2:1", "reason": "x"}])
def test_bad_crop_is_dropped_not_fatal(crop):
    r = good()
    r["crop"] = crop
    out, errs = run(r)
    assert errs == [] and out["crop"] is None


def test_straighten_rules():
    r = good()
    r["straighten_degrees"] = 12
    assert run(r)[0]["straighten_degrees"] is None
    r["straighten_degrees"] = 2.0
    tilt = {"tilt_degrees": -1.0, "confidence": "high", "tilted": True}
    assert run(r, tilt=tilt)[0]["straighten_degrees"] is None   # 3 degrees away from the measured tilt
    r["straighten_degrees"] = -0.5
    assert run(r, tilt=tilt)[0]["straighten_degrees"] == -0.5


def test_scores_and_tags():
    r = good()
    r["scores"]["light"]["score"] = 6
    assert any("score light" in e for e in run(r)[1])
    r = good()
    r["tags"] = ["invented_tag"]
    assert any("unknown tags" in e for e in run(r)[1])


@pytest.mark.parametrize("field,value,rule", [
    ("seen", "Shot on an X100V in Paris.", "another camera"), ("light_notes", "Set the SUPER MODE now.", "unknown capitalised"),
    ("exercise", "See https://example.com", "link"), ("exercise", "Wait — then shoot.", "dash"),
    ("exercise", "Use **bold** here", "markdown"), ("exercise", "Nice 📷 shot", "emoji")])
def test_text_rules(field, value, rule):
    r = good()
    r[field] = value
    assert any(rule in e for e in run(r)[1]), run(r)[1]


def test_wrong_photographer_fails():
    r = good()
    r["learn_from"]["photographer_name"] = "Someone Else"
    assert any("learn_from" in e for e in run(r)[1])


def test_made_up_used_settings_rejected_without_exif():
    r = good()
    r["settings_why"] = "You used f/2 which blurred the wall, so stop down."
    assert run(r, exif=True)[1] == []
    assert any("EXIF" in e for e in run(r, exif=False)[1])
