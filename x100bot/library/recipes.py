"""Recipe model: normalise a source's settings to the manual's names, validate them against
camera/x100vi_facts.yaml, adapt X-Trans IV recipes, and store each look once."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)
LIGHT_TAGS = ("sunny", "overcast", "rain", "golden_hour", "night", "indoor", "any")
# Fuji X Weekly, X-Trans V recipes page: for X-Trans IV recipes that use these, reduce Color Chrome FX Blue by one
ADAPT_SIMS = {"CLASSIC CHROME", "CLASSIC Neg.", "ETERNA/CINEMA", "ETERNA BLEACH BYPASS"}
STEP_DOWN = {"STRONG": "WEAK", "WEAK": "OFF", "OFF": "OFF"}


@lru_cache
def facts(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


SIM_NAMES = [   # (pattern on the source's wording, the manual's name); order matters
    (r"eterna bleach bypass", "ETERNA BLEACH BYPASS"), (r"eterna|cinema", "ETERNA/CINEMA"),
    (r"provia|standard", "PROVIA/STANDARD"), (r"velvia|vivid", "Velvia/VIVID"), (r"astia|soft", "ASTIA/SOFT"),
    (r"classic chrome", "CLASSIC CHROME"), (r"reala ace", "REALA ACE"), (r"pro neg\.? ?hi", "PRO Neg. Hi"),
    (r"pro neg\.? ?std", "PRO Neg. Std"), (r"classic neg", "CLASSIC Neg."), (r"nostalgic neg", "NOSTALGIC Neg."),
    (r"sepia", "SEPIA")]
FILTERS = {"y": "Ye", "ye": "Ye", "yellow": "Ye", "r": "R", "red": "R", "g": "G", "green": "G"}
WB_MODES = [(r"white priority", "WHITE PRIORITY"), (r"ambi[ae]nce priority", "AMBIENCE PRIORITY"),
            (r"^auto", "AUTO"), (r"daylight|sunny|fine", "DAYLIGHT"), (r"shade", "SHADE"),
            (r"fluorescent\s*(?:light)?\s*-?\s*([123])", "FLUORESCENT LIGHT-{0}"), (r"incandescent", "INCANDESCENT"),
            (r"underwater", "UNDERWATER"), (r"custom\s*([123])", "CUSTOM {0}")]


def film_simulation(text: str) -> str | None:
    t = text.split("(")[0].strip().lower()
    m = re.match(r"(acros|monochrome)\s*(?:\+\s*(\w+))?", t)
    if m:
        base = m.group(1).upper()
        f = FILTERS.get((m.group(2) or "").lower())
        return f"{base}+{f} FILTER" if f else base
    for pat, name in SIM_NAMES:
        if re.search(pat, t):
            return name
    return None


def _for_x_trans_v(v: str) -> str:
    """'Strong (X-Trans IV), Weak (X-Trans V)' -> 'Weak'. Values without per sensor labels are unchanged."""
    m = re.search(r"([^,()]+?)\s*\(X-Trans V\)", v)
    return m.group(1).strip() if m else v


def _level(v: str | None) -> str | None:
    return v.strip().upper() if v else None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    m = re.fullmatch(r"\s*([+-−]?\s*\d+(?:\.\d+)?)\s*(?:\(.*\))?\s*", v)
    if not m:
        return None
    n = float(m.group(1).replace("−", "-").replace(" ", ""))
    return int(n) if n.is_integer() else n


def _fractions(text: str) -> list[Fraction]:
    """'+2/3 to + 1 1/3 (typically)' -> [2/3, 4/3]."""
    out = []
    for sign, whole, frac in re.findall(r"([+-]?)\s*(\d+(?:\s+\d+/\d+)?|\d+/\d+)()", text.split("(")[0]):
        parts = whole.split()
        val = sum(Fraction(p) for p in parts)
        out.append(-val if sign == "-" else val)
    return out


@dataclass
class Recipe:
    name: str
    author: str
    source_url: str
    sensor: str
    settings: dict
    adapted: dict | None = None
    light_tags: list[str] = field(default_factory=lambda: ["any"])
    errors: list[str] = field(default_factory=list)

    @property
    def compat_label(self) -> str:
        return "Made for X-Trans V" if self.sensor == "X-Trans V" else "Adapted from X-Trans IV"

    @property
    def for_camera(self) -> dict:
        return self.adapted or self.settings

    @property
    def settings_hash(self) -> str:
        core = {k: v for k, v in self.for_camera.items() if k != "raw"}
        return hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()


def normalise(raw: dict[str, str]) -> tuple[dict, list[str]]:
    """Map the source's labels and wording to the manual's names. Numbers are kept exactly as written."""
    by = {re.sub(r"\s*\(.*?\)", "", k).strip().lower(): _for_x_trans_v(v) for k, v in raw.items()}
    s: dict = {"raw": raw}
    errs: list[str] = []
    if "film simulation" in by:
        s["film_simulation"] = film_simulation(by["film simulation"])
    if "grain effect" in by:
        parts = [p.strip() for p in by["grain effect"].split(",")]
        s["grain_roughness"] = _level(parts[0])
        s["grain_size"] = _level(parts[1]) if len(parts) > 1 else None
    s["color_chrome_effect"] = _level(by.get("color chrome effect"))
    s["color_chrome_fx_blue"] = _level(by.get("color chrome fx blue") or by.get("color chrome effect blue"))
    wb = by.get("white balance")
    if wb:
        mode_txt = wb.split(",")[0].strip()
        k = re.fullmatch(r"(\d{4,5})\s*K", mode_txt, re.I)
        if k:
            s["white_balance"], s["wb_kelvin"] = "COLOR TEMPERATURE", int(k.group(1))
        else:
            for pat, name in WB_MODES:
                m = re.search(pat, mode_txt, re.I)
                if m:
                    s["white_balance"] = name.format(*m.groups())
                    break
        red = re.search(r"([+-]?\d+)\s*Red", wb)
        blue = re.search(r"([+-]?\d+)\s*Blue", wb)
        s["wb_red"] = int(red.group(1)) if red else None
        s["wb_blue"] = int(blue.group(1)) if blue else None
    dr = by.get("dynamic range")
    if dr:
        m = re.search(r"DR-?P\s*(\w+)|D-?Range Priority\s*(\w+)", dr, re.I)
        if m:
            s["d_range_priority"] = _level(m.group(1) or m.group(2))
        elif re.search(r"auto", dr, re.I):
            s["dynamic_range"] = "AUTO"
        elif re.search(r"(\d{3})", dr):
            s["dynamic_range"] = re.search(r"(\d{3})", dr).group(1) + "%"
    if "d-range priority" in by or "d range priority" in by:
        s["d_range_priority"] = _level(by.get("d-range priority") or by.get("d range priority"))
    for key, label in (("highlight", "highlight"), ("shadow", "shadow"), ("color", "color"),
                       ("sharpness", "sharpness"), ("high_iso_nr", "high iso nr"), ("clarity", "clarity")):
        v = by.get(label) or (by.get("noise reduction") if key == "high_iso_nr" else None) \
            or (by.get("sharpening") if key == "sharpness" else None)
        if v is not None:
            s[key] = _num(v)
            if s[key] is None:
                errs.append(f"{label}: cannot read '{v}'")
    mono = by.get("monochromatic color")
    if mono:
        wc, mg = re.search(r"WC\s*([+-]?\d+)", mono), re.search(r"MG\s*([+-]?\d+)", mono)
        s["monochromatic_color"] = {"wc": int(wc.group(1)) if wc else None, "mg": int(mg.group(1)) if mg else None}
    s["iso"] = by.get("iso")
    s["exposure_compensation"] = by.get("exposure compensation")
    return {k: v for k, v in s.items() if v is not None}, errs


def validate(s: dict, facts_file: str) -> list[str]:
    """Every value must be in the manual's allowed list or range. Returns the problems, empty when valid."""
    f = facts(facts_file)
    st, errs = f["settings"], []
    sims = f["film_simulations"]["color"] + f["film_simulations"]["monochrome"]
    sim = s.get("film_simulation")
    mono = sim in f["film_simulations"]["monochrome"]
    drp = s.get("d_range_priority")
    required = ["film_simulation", "grain_roughness", "color_chrome_effect", "color_chrome_fx_blue",
                "white_balance", "sharpness", "high_iso_nr", "clarity", "iso", "exposure_compensation"]
    if not drp or drp == "OFF":   # D RANGE PRIORITY replaces DYNAMIC RANGE and the tone curve
        required += ["dynamic_range", "highlight", "shadow"]
    if not mono:
        required.append("color")
    errs += [f"missing {k}" for k in required if s.get(k) is None]
    if sim is not None and sim not in sims:
        errs.append(f"film simulation '{sim}' is not an X100VI film simulation")
    for key in ("grain_roughness", "color_chrome_effect", "color_chrome_fx_blue", "dynamic_range", "d_range_priority"):
        if s.get(key) is not None and s[key] not in st[key]["values"]:
            errs.append(f"{key} '{s[key]}' not in {st[key]['values']}")
    if s.get("grain_roughness") not in (None, "OFF") and s.get("grain_size") not in st["grain_size"]["values"]:
        errs.append(f"grain_size '{s.get('grain_size')}' not in {st['grain_size']['values']}")
    if s.get("white_balance") is not None:
        if s["white_balance"] not in st["white_balance"]["values"]:
            errs.append(f"white balance '{s['white_balance']}' is not an X100VI option")
        k = s.get("wb_kelvin")
        if k is not None and not st["white_balance"]["kelvin"]["min"] <= k <= st["white_balance"]["kelvin"]["max"]:
            errs.append(f"color temperature {k}K out of range")
        for axis, key in (("red", "wb_red"), ("blue", "wb_blue")):
            r = st["wb_shift"][axis]
            if s.get(key) is None:
                errs.append(f"missing {key}")
            elif not r["min"] <= s[key] <= r["max"]:
                errs.append(f"{key} {s[key]} out of {r['min']}..{r['max']}")
    for key in ("highlight", "shadow", "color", "sharpness", "high_iso_nr", "clarity"):
        v = s.get(key)
        if v is None:
            continue
        r = st[key]
        if not r["min"] <= v <= r["max"] or (v / r["step"]) % 1:
            errs.append(f"{key} {v} out of {r['min']}..{r['max']} in steps of {r['step']}")
    mc = s.get("monochromatic_color")
    if mc:
        if not mono:
            errs.append("monochromatic color on a color film simulation")
        for k, axis in (("wc", "warm_cool"), ("mg", "magenta_green")):
            r = st["monochromatic_color"][axis]
            if mc.get(k) is None or not r["min"] <= mc[k] <= r["max"]:
                errs.append(f"monochromatic color {k} {mc.get(k)} out of {r['min']}..{r['max']}")
    if s.get("exposure_compensation") is not None:
        vals = _fractions(s["exposure_compensation"])
        if not vals or any(abs(v) > st["exposure_compensation"]["max"] for v in vals):
            errs.append(f"exposure compensation '{s['exposure_compensation']}' unreadable or out of range")
    if s.get("iso") is not None:
        iso = st["iso"]
        nums = [int(n) for n in re.findall(r"\d+", s["iso"])]
        if not nums and "auto" not in s["iso"].lower():
            errs.append(f"ISO '{s['iso']}' unreadable")
        for n in nums:
            if not (iso["standard"]["min"] <= n <= iso["standard"]["max"] or n in iso["extended"]):
                errs.append(f"ISO {n} out of range")
    return errs


def adapt(settings: dict, sensor: str) -> dict | None:
    """X-Trans IV recipes on CLASSIC CHROME, CLASSIC Neg., ETERNA or ETERNA BLEACH BYPASS: COLOR CHROME FX BLUE one
    step lower. Returns the adapted copy, or None when nothing changes."""
    if sensor != "X-Trans IV" or settings.get("film_simulation") not in ADAPT_SIMS:
        return None
    before = settings.get("color_chrome_fx_blue")
    if before in (None, "OFF"):
        return None
    return {**settings, "color_chrome_fx_blue": STEP_DOWN[before]}


def build(name: str, author: str, url: str, sensor: str, raw: dict, facts_file: str) -> Recipe:
    settings, errs = normalise(raw)
    errs += validate(settings, facts_file)
    r = Recipe(name=name, author=author, source_url=url, sensor=sensor, settings=settings, errors=errs)
    r.adapted = adapt(settings, sensor) if not errs else None
    return r


def store(conn, r: Recipe, now: float) -> int | None:
    """Insert a valid recipe once. The same settings under another name are skipped (settings_hash is unique)."""
    if r.errors:
        log.warning("recipe rejected, %s (%s): %s", r.name, r.source_url, "; ".join(r.errors))
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO recipes(name, author, source_url, sensor, compat_label, film_simulation, settings_json,"
        " adapted_settings_json, settings_hash, light_tags_json, fetched_at, times_posted) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
        (r.name, r.author, r.source_url, r.sensor, r.compat_label, r.for_camera["film_simulation"],
         json.dumps(r.settings), json.dumps(r.adapted) if r.adapted else None, r.settings_hash,
         json.dumps(r.light_tags), now))
    if not cur.rowcount:
        log.info("recipe %s already stored (same URL or same settings)", r.name)
        return None
    return cur.lastrowid


def from_page(html: str, url: str, index_sensor: str, facts_file: str) -> list[Recipe]:
    """Every recipe on a Fuji X Weekly page that suits the X100VI. On a page with an X-Trans V and an X-Trans IV
    version of the same look, only the X-Trans V one is kept. Pages for older sensors give nothing."""
    from .parsers import fujixweekly as fxw
    page = fxw.extract(html)
    author = re.sub(r"^by\s*", "", page["author"]).strip() or "Fuji X Weekly"
    page_sensor = fxw.sensor_from_text(page["title"]) or index_sensor
    blocks = page["blocks"]
    multi = len(blocks) > 1
    out: list[Recipe] = []
    names_v = {b["name"] for b in blocks if b["sensor"] == "X-Trans V"}
    for b in blocks:
        sensor = b["sensor"] or page_sensor
        name = b["name"] if multi and b["name"] else fxw.name_from_title(page["title"])
        if sensor not in ("X-Trans V", "X-Trans IV"):
            continue
        if multi and sensor == "X-Trans IV" and name in names_v:
            continue
        if multi and not b["name"]:
            continue   # an unnamed block on a page of several cannot be credited correctly
        link = url if not multi else f"{url}#{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}"
        out.append(build(name, author, link, sensor, b["settings"], facts_file))
    return out
