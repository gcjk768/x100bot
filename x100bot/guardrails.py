"""Deterministic checks on the teacher's output: schema shape, scores, tags, crop, straighten, every recommended
setting against the X100VI, names, and the text rules. Anything else never reaches Telegram."""
from __future__ import annotations

import json
import re
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import yaml

from .compose import APERTURE_RE, CAPS_RE, DASH_RE, OTHER_CAMERAS, URL_RE, allowed_caps

SHORT_FORMS = {"ISO", "OVF", "EVF", "ND", "IBIS", "AF", "MF", "EV", "JPEG", "HEIF", "RAW", "SGT", "HDB", "MRT", "EXIF", "LCD"}
MARKDOWN_RE = re.compile(r"\*\*|__|^#+\s|`|\[[^\]]+\]\([^)]+\)", re.M)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF]")
# a positive claim about the settings used: the phrase, then a value within the sentence, and no negation just before
USED_CLAIM_RE = re.compile(r"\b(you (used|shot (at|with)|chose|set|were (at|on)|had)|your (aperture|shutter( speed)?|iso|"
                           r"exposure( compensation)?|settings?)( was| were| of| at|:)?)\b[^.]{0,40}?(\d|f/|AUTO)", re.I)
NEGATED_RE = re.compile(r"\b(not|no|cannot|can't|unknown|without|unclear|missing|unable)\b[^.]{0,30}$", re.I)


def claims_used(text: str) -> bool:
    for m in USED_CLAIM_RE.finditer(text or ""):
        if not NEGATED_RE.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False
AREAS = ("composition", "light", "exposure", "focus", "colour", "moment")
MECH_SPEEDS = ["4000", "3200", "2500", "2000", "1600", "1250", "1000", "800", "640", "500", "400", "320", "250", "200",
               "160", "125", "100", "80", "60", "50", "40", "30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "3"]
ES_SPEEDS = ["5000", "6400", "8000", "10000", "12800", "16000", "20000", "25600", "32000", "40000", "51200", "64000",
             "80000", "102400", "128000", "160000", "180000"]
SECONDS = ["30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "3", "2.5", "2", "1.6", "1.3", "1", "0.8", "0.6", "0.5",
           "0.4", "0.3"]
ISO_VALUES = [64, 80, 100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200, 4000, 5000,
              6400, 8000, 10000, 12800, 25600, 51200]
AF_AREAS = {"SINGLE POINT", "ZONE", "WIDE/TRACKING", "ALL"}
CROP_ASPECTS = {"3:2", "4:5", "1:1", "16:9", "free"}


@lru_cache
def facts(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@lru_cache
def allowed_tags(schema_path: str) -> tuple[set[str], set[str]]:
    sch = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    p = sch["properties"]
    return set(p["tags"]["items"]["enum"]), set(p["strength_tags"]["items"]["enum"])


# settings -----------------------------------------------------------------------------------------------------------

def _frac(text: str) -> Fraction | None:
    t = text.replace("−", "-").replace("EV", "").strip()
    m = re.fullmatch(r"([+-]?)\s*(\d+(?:\s+\d+/\d+)?|\d+/\d+|\d+\.\d+)", t)
    if not m:
        return None
    val = sum(Fraction(p) for p in m.group(2).split())
    return -val if m.group(1) == "-" else val


def ec_text(fr: Fraction) -> str:
    """-4/3 -> '-1 1/3', 2/3 -> '+2/3', 0 -> '0'."""
    if fr == 0:
        return "0"
    sign = "-" if fr < 0 else "+"
    fr = abs(fr)
    whole, rem = divmod(fr.numerator, fr.denominator)
    parts = ([str(whole)] if whole else []) + ([f"{rem}/{fr.denominator}"] if rem else [])
    return sign + " ".join(parts)


def check_setting(key: str, value: str, settings: dict, f: dict) -> str | None:
    """None when the value is valid for the X100VI, else the problem. Empty means keep what was used."""
    v = (value or "").strip()
    if not v:
        return None
    up = v.upper()
    if key == "mode":
        return None if up in ("P", "A", "S", "M") else f"mode {v} is not P, A, S or M"
    if key == "aperture":
        m = re.fullmatch(r"[fF]/?\s*(\d+(?:\.\d+)?)", v)
        rng = f["settings"]["aperture"]
        if not m or not rng["min"] <= float(m.group(1)) <= rng["max"]:
            return f"aperture {v} is outside f/{rng['min']:g} to f/{rng['max']:g}"
        return None
    if key == "shutter":
        m = re.fullmatch(r"1/(\d+)\s*s?", v)
        if m:
            n = m.group(1)
            if n in MECH_SPEEDS:
                return None
            if n in ES_SPEEDS:
                return None if (settings.get("shutter_type") or "").upper() in ("ES", "ELECTRONIC SHUTTER", "ELECTRONIC") \
                    else f"shutter {v} needs shutter_type ES"
            return f"shutter {v} is not a speed the camera offers"
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|\")?", v)
        if m and m.group(1) in SECONDS:
            return None
        return f"shutter {v} is not a speed the camera offers"
    if key == "shutter_type":
        return None if up in ("MS", "ES", "MECHANICAL SHUTTER", "ELECTRONIC SHUTTER", "MECHANICAL + ELECTRONIC") \
            else f"shutter_type {v} unknown"
    if key == "iso":
        m = re.fullmatch(r"(?:ISO\s*)?(\d+)", up)
        if m:
            return None if int(m.group(1)) in ISO_VALUES else f"ISO {v} is not a value the camera offers"
        if "AUTO" not in up:
            return f"ISO {v} is not a number or AUTO1 to AUTO3"
        bank = re.search(r"AUTO\s?([123])", up)
        mx = re.search(r"MAX(?:\.|IMUM)?(?:\s*SENSITIVITY)?(?:\s*ISO)?\s*(\d+)|(?:UP TO|TO)\s*(?:ISO\s*)?(\d+)", up)
        mn = re.search(r"MIN(?:\.|IMUM)?\s*SHUTTER(?:\s*SPEED)?\s*(?:OF\s*)?(1/\d+|\d+\s*S(?:EC)?|AUTO)", up)
        if not bank and not (mx or mn):
            return f"ISO {v} needs AUTO1, AUTO2 or AUTO3, or a max sensitivity and minimum shutter speed"
        max_v = int(next(g for g in mx.groups() if g)) if mx else None
        if max_v is not None and not 400 <= max_v <= 12800:
            return f"ISO AUTO max sensitivity {max_v} is outside 400 to 12800"
        min_s = mn.group(1).replace(" ", "") if mn else None
        if min_s and min_s != "AUTO":
            ok = (min_s.startswith("1/") and min_s[2:] in MECH_SPEEDS and int(min_s[2:]) <= 2000) or                  (re.fullmatch(r"\d+S(EC)?", min_s) and re.sub(r"S(EC)?$", "", min_s) in SECONDS)
            if not ok:
                return f"ISO AUTO min shutter {min_s} is outside 1/2000 to 30 seconds"
        parts = [f"AUTO{bank.group(1)}" if bank else "AUTO"]
        if max_v:
            parts.append(f"max {max_v}")
        if min_s:
            parts.append(f"min shutter {min_s.lower() if min_s != 'AUTO' else 'AUTO'}")
        settings[key] = ", ".join(parts)
        return None
    if key == "exposure_comp":
        fr = _frac(v)
        if fr is None or abs(fr) > 5:
            return f"exposure_comp {v} is not within -5 to +5 in thirds"
        thirds = round(float(fr) * 3)
        if abs(float(fr) * 3 - thirds) > 0.16:   # 0.3, 0.33, 0.67 and 0.7 are thirds written as decimals
            return f"exposure_comp {v} is not a third of a stop"
        settings[key] = ec_text(Fraction(thirds, 3))
        return None
    if key == "focus_mode":
        return None if up in ("S", "C", "M", "AF-S", "AF-C", "MF") else f"focus_mode {v} is not S, C or M"
    if key == "af_area":
        return None if up in AF_AREAS else f"af_area {v} is not SINGLE POINT, ZONE, WIDE/TRACKING or ALL"
    if key == "nd_filter":
        return None if up in ("ON", "OFF") else f"nd_filter {v} is not ON or OFF"
    if key == "teleconverter":
        m = re.fullmatch(r"(35|50|70)\s*(mm)?", v.lower())
        return None if m else f"teleconverter {v} is not 35, 50 or 70"
    if key == "film_simulation":
        sims = f["film_simulations"]["color"] + f["film_simulations"]["monochrome"]
        return None if v in sims else f"film_simulation {v} is not one of the 20"
    if key == "dynamic_range":
        m = re.fullmatch(r"(?:DR\s*-?\s*)?(AUTO|100|200|400)\s*%?", up)   # DR200, DR 400, 200% all mean the same
        if not m:
            return f"dynamic_range {v} is not AUTO, 100%, 200% or 400%"
        settings[key] = m.group(1) if m.group(1) == "AUTO" else m.group(1) + "%"
        return None
    if key == "recipe_name":
        return None   # checked against the library by the caller
    return f"unknown setting {key}"


def check_settings(settings: dict, facts_file: str, recipe_names: set[str]) -> list[str]:
    f = facts(facts_file)
    errs = [e for k, v in (settings or {}).items() if (e := check_setting(k, v, settings, f))]
    rn = (settings or {}).get("recipe_name", "")
    if rn and rn not in recipe_names:
        errs.append(f"recipe_name {rn} is not a library recipe")
    return errs


# text -------------------------------------------------------------------------------------------------------------

def check_text(key: str, text: str, caps: set[str]) -> list[str]:
    errs = []
    if URL_RE.search(text):
        errs.append(f"{key} contains a link")
    if MARKDOWN_RE.search(text):
        errs.append(f"{key} contains markdown")
    if EMOJI_RE.search(text):
        errs.append(f"{key} contains an emoji")
    if DASH_RE.search(text):
        errs.append(f"{key} contains a dash")
    if OTHER_CAMERAS.search(text):
        errs.append(f"{key} names another camera")
    for m in CAPS_RE.finditer(text):
        w = m.group(0).strip("().,")
        if len(w) >= 2 and not w.isdigit() and not APERTURE_RE.match(w) and w not in caps and w not in SHORT_FORMS:
            errs.append(f"{key} has an unknown capitalised word {w}")
    return errs


def texts(obj, prefix="") -> list[tuple[str, str]]:
    out = []
    if isinstance(obj, str):
        out.append((prefix, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("settings", "tags", "strength_tags"):
                continue
            out += texts(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += texts(v, f"{prefix}[{i}]")
    return out


# the whole critique ------------------------------------------------------------------------------------------------

def check_critique(result: dict, *, facts_file: str, menus_file: str, schema_file: str, recipe_names: set[str],
                   photographer_names: set[str], exif_available: bool, measured_tilt: dict | None) -> tuple[dict, list[str]]:
    """Returns (cleaned result, hard errors). Soft problems (a bad crop or straighten) are dropped from the result
    and not counted as errors, as the brief asks."""
    errs: list[str] = []
    r = json.loads(json.dumps(result))   # a copy we may trim
    caps = allowed_caps(menus_file, facts_file)
    scores = r.get("scores") or {}
    for a in AREAS:
        sc = scores.get(a, {})
        v = sc.get("score") if isinstance(sc, dict) else sc
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 5:
            errs.append(f"score {a} must be an integer from 1 to 5")
    tags, strengths = allowed_tags(schema_file)
    bad = [t for t in r.get("tags", []) if t not in tags]
    if bad:
        errs.append(f"unknown tags {bad}")
    bad = [t for t in r.get("strength_tags", []) if t not in strengths]
    if bad:
        errs.append(f"unknown strength tags {bad}")
    crop = r.get("crop")
    if crop:
        ok = all(isinstance(crop.get(k), (int, float)) for k in "xywh") and 0 <= crop["x"] < 1 and 0 <= crop["y"] < 1 \
            and 0.35 <= crop["w"] <= 1 and 0.35 <= crop["h"] <= 1 and crop["x"] + crop["w"] <= 1.0001 \
            and crop["y"] + crop["h"] <= 1.0001 and crop.get("aspect") in CROP_ASPECTS
        if not ok:
            r["crop"] = None
    st = r.get("straighten_degrees")
    if st is not None:
        keep = isinstance(st, (int, float)) and abs(st) <= 10
        if keep and measured_tilt and measured_tilt.get("confidence") in ("medium", "high") and measured_tilt.get("tilted"):
            keep = abs(st - measured_tilt["tilt_degrees"]) <= 2
        if not keep:
            r["straighten_degrees"] = None
    errs += check_settings(r.get("settings") or {}, facts_file, recipe_names)
    lf = r.get("learn_from")
    if lf and lf.get("photographer_name") and lf["photographer_name"] not in photographer_names:
        errs.append(f"learn_from {lf['photographer_name']} is not a library photographer")
    for key, text in texts(r):
        errs += check_text(key, text, caps)
    if not exif_available and claims_used(r.get("settings_why", "") or ""):
        errs.append("settings_why claims what was used, but no EXIF was available")
    return r, errs


def check_simple(result: dict, *, facts_file: str, menus_file: str, recipe_names: set[str]) -> list[str]:
    """For follow up, compare, series, ask and progress outputs: text rules plus any settings offered."""
    caps = allowed_caps(menus_file, facts_file)
    errs = []
    for key, text in texts(result):
        errs += check_text(key, text, caps)
    if isinstance(result.get("settings"), dict):
        errs += check_settings(result["settings"], facts_file, recipe_names)
    return errs
