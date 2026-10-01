"""The teacher's Telegram messages: the overlay caption (under 1024 characters), the guidance (split once at a section
boundary if over 4000), the aligned settings table, and the fallback critique text. Every value is escaped; empty
lines and rows are left out; "keep" and "unknown" are printed where the brief says so."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .telegram import esc

CAPTION_MAX = 1024
GUIDANCE_MAX = 4000
AREAS = ("composition", "light", "exposure", "focus", "colour", "moment")
AREA_LABEL = {"composition": "Composition", "light": "Light", "exposure": "Exposure", "focus": "Focus",
              "colour": "Colour", "moment": "Moment"}
TABLE_ROWS = [("Mode", "mode"), ("Aperture", "aperture"), ("Shutter", "shutter"), ("ISO", "iso"),
              ("Exposure comp", "exposure_comp"), ("Focus", "focus_mode"), ("AF area", "af_area"),
              ("ND filter", "nd_filter"), ("Teleconverter", "teleconverter"), ("Film simulation", "film_simulation"),
              ("Dynamic range", "dynamic_range")]
# EXIF field for each table row (the "You used" column)
EXIF_FOR = {"mode": "exposure_mode_text", "aperture": "aperture_text", "shutter": "shutter_text", "iso": "iso",
            "exposure_comp": "exposure_compensation_text", "focus_mode": "focus_mode", "af_area": "af_area",
            "nd_filter": None, "teleconverter": "teleconverter", "film_simulation": "film_simulation",
            "dynamic_range": "dynamic_range"}
BUTTONS = [("Simpler", "s"), ("More depth", "d"), ("Why these scores", "w"), ("Reshoot plan", "r")]


@lru_cache
def menu_map(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["settings"]


def overall(scores: dict, weights: dict) -> float:
    total = sum(weights.get(a, 0) * (scores[a]["score"] if isinstance(scores.get(a), dict) else scores.get(a, 0))
                for a in AREAS)
    from decimal import ROUND_HALF_UP, Decimal
    return float(Decimal(str(total / max(sum(weights.values()), 1e-9))).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _s(scores: dict, a: str):
    v = scores.get(a)
    return v["score"] if isinstance(v, dict) else v


def caption(result: dict, overall_score: float, trend_note: str = "") -> str:
    sc = result["scores"]
    tf = result["top_fix"]
    lines = [f"<b>{esc(result['summary'])}</b>",
             f"Composition {_s(sc, 'composition')}/5 · Light {_s(sc, 'light')}/5 · Exposure {_s(sc, 'exposure')}/5",
             f"Focus {_s(sc, 'focus')}/5 · Colour {_s(sc, 'colour')}/5 · Moment {_s(sc, 'moment')}/5",
             f"Overall {overall_score}/5{esc(trend_note)}",
             f"Top fix: <b>{esc(tf['title'])}</b>", esc(tf["why"])]
    text = "\n".join(lines)
    if len(text) > CAPTION_MAX:
        text = text[:CAPTION_MAX - 1].rsplit(" ", 1)[0] + "…"
    return text


def exif_display(exif: dict | None) -> dict:
    """Readable strings for the You used column from the mapped EXIF fields."""
    if not exif:
        return {}
    d = dict(exif)
    if "aperture" in d and d.get("aperture") not in (None, ""):
        try:
            d["aperture_text"] = f"f/{float(d['aperture']):g}"
        except (TypeError, ValueError):
            d["aperture_text"] = str(d["aperture"])
    if "exposure_compensation" in d:
        try:
            v = float(d["exposure_compensation"])
            d["exposure_compensation_text"] = f"{v:+.1f}".rstrip("0").rstrip(".") if v else "0"
        except (TypeError, ValueError):
            d["exposure_compensation_text"] = str(d["exposure_compensation"])
    prog = d.get("exposure_mode")
    d["exposure_mode_text"] = {1: "M", 2: "P", 3: "A", 4: "S"}.get(prog, str(prog) if prog not in (None, "") else "")
    return d


def settings_table(settings: dict, exif: dict | None, exif_available: bool) -> str:
    """Aligned <pre> table. A row is left out when nothing is known and nothing is suggested."""
    disp = exif_display(exif)
    rows = []
    for label, key in TABLE_ROWS:
        used = disp.get(EXIF_FOR.get(key) or "") if exif_available else None
        used_text = str(used) if used not in (None, "") else ("unknown" if not exif_available else "")
        try_text = (settings or {}).get(key, "") or ""
        if not used_text and not try_text:
            continue
        rows.append((label, used_text or "unknown", try_text or "keep"))
    if not rows:
        return ""
    w1 = max(len(r[0]) for r in rows + [("", "", "")]) + 2
    w2 = max(len(r[1]) for r in rows + [("", "You used", "")]) + 2
    out = [f"{'':<{w1}}{'You used':<{w2}}Try"]
    out += [f"{esc(a):<{w1}}{esc(b):<{w2}}{esc(c)}" for a, b, c in rows]
    return "<pre>" + "\n".join(out) + "</pre>"


def how_to_set(settings: dict, map_path: str) -> str:
    """One 'How to set it' line from camera/settings_menu_map.yaml for every setting the teacher changes."""
    mm = menu_map(map_path)
    parts = []
    for label, key in TABLE_ROWS + [("Recipe", "recipe_name")]:
        v = (settings or {}).get(key)
        if not v:
            continue
        k = "iso_auto" if key == "iso" and str(v).upper().startswith("AUTO") else key
        if k in mm:
            parts.append(mm[k]["how"])
    return " ".join(dict.fromkeys(parts))


def numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {esc(x)}" for i, x in enumerate(items or [], 1) if x)


def bullets(items: list[str]) -> str:
    return "\n".join(f"• {esc(x)}" for x in items or [] if x)


def measured_line(m: dict) -> str:
    if not m:
        return ""
    parts = [f"highlights clipped {m.get('highlights_clipped_pct', 0)}%", f"shadows crushed {m.get('shadows_crushed_pct', 0)}%"]
    t = m.get("tilt") or {}
    if t.get("lines"):
        parts.append(f"tilt {t.get('tilt_degrees', 0)}°" + ("" if t.get("tilted") else " (level)"))
    s = m.get("sharpness") or {}
    if s.get("sharpest_cell"):
        parts.append(f"sharpest area {s['sharpest_cell']}")
    c = m.get("colour") or {}
    if c.get("cast"):
        parts.append(f"colour {c['cast']}")
    return "Measured: " + ", ".join(parts)


def guidance(result: dict, *, exif: dict | None, exif_available: bool, measurements: dict, map_path: str,
             recipe: dict | None = None, photographer: dict | None = None, closest_recipe: str | None = None,
             quick: bool = False, socratic_questions: list[str] | None = None) -> list[str]:
    """The guidance message as a list of sections; join() splits once at a boundary when too long."""
    r = result
    tf = r.get("top_fix") or {}
    sections: list[str] = []
    if socratic_questions:
        sections.append("<b>Before you read on</b>\n" + numbered(socratic_questions))
    if not quick:
        if r.get("seen"):
            sections.append(f"<b>What I see</b>\n{esc(r['seen'])}")
        if r.get("what_works"):
            sections.append(f"<b>What works</b>\n{bullets(r['what_works'])}")
    top = [f"<b>Top fix: {esc(tf.get('title', ''))}</b>", esc(tf.get("why", ""))]
    if tf.get("principle"):
        top.append(f"Why it matters: {esc(tf['principle'])}")
    if tf.get("steps"):
        top.append(numbered(tf["steps"]))
    sections.append("\n".join(x for x in top if x))
    if not quick:
        for title, key in (("Composition", "composition_notes"), ("Light", "light_notes"),
                           ("When and where to reshoot", "timing_and_place")):
            if r.get(key):
                sections.append(f"<b>{title}</b>\n{esc(r[key])}")
    table = settings_table(r.get("settings") or {}, exif, exif_available)
    if table or r.get("settings_why"):
        block = ["<b>Settings</b>"]
        if table:
            block.append(table)
        if r.get("settings_why"):
            block.append(esc(r["settings_why"]))
        how = how_to_set(r.get("settings") or {}, map_path)
        if how:
            block.append(f"How to set it: {esc(how)}")
        sections.append("\n".join(block))
    if not quick:
        if r.get("reshoot_steps"):
            sections.append("<b>Reshoot it like this</b>\n" + numbered(r["reshoot_steps"]))
        if r.get("edit_suggestions"):
            sections.append(f"<b>If you edit</b>\n{esc(r['edit_suggestions'])}")
    if r.get("exercise"):
        sections.append(f"<b>Exercise</b>\n{esc(r['exercise'])}")
    tail = []
    if recipe:
        tail.append(f"Recipe to try: {esc(recipe['name'])}, <a href=\"{esc(recipe['source_url'])}\">full recipe</a>")
    lf = r.get("learn_from")
    if photographer and lf:
        tail.append(f"Learn from: {esc(photographer['name'])}, {esc(lf.get('why', ''))} "
                    f"<a href=\"{esc(photographer['official_url'])}\">see their work</a>")
    if not quick:
        ml = measured_line(measurements)
        if ml:
            tail.append(esc(ml))
        if closest_recipe:
            tail.append(f"Closest recipe to what you shot: {esc(closest_recipe)}")
        if r.get("cannot_tell"):
            tail.append(f"Cannot tell from this image: {esc(r['cannot_tell'])}")
        if r.get("question"):
            tail.append(f"<i>{esc(r['question'])}</i>")
    if tail:
        sections.append("\n".join(tail))
    return sections


def join(sections: list[str]) -> list[str]:
    """One message, or two split at a section boundary when the whole would pass GUIDANCE_MAX."""
    text = "\n\n".join(s for s in sections if s)
    if len(text) <= GUIDANCE_MAX:
        return [text]
    first, rest = [], list(sections)
    while rest and len("\n\n".join(first + [rest[0]])) <= GUIDANCE_MAX:
        first.append(rest.pop(0))
    if not first:   # a single giant section: hard cut
        return [text[:GUIDANCE_MAX], text[GUIDANCE_MAX:GUIDANCE_MAX * 2]]
    return ["\n\n".join(first), "\n\n".join(rest)[:GUIDANCE_MAX]]


def buttons(critique_id: int, has_crop: bool) -> dict:
    row = [{"text": label, "callback_data": f"t:{code}:{critique_id}"} for label, code in BUTTONS]
    if has_crop:
        row.append({"text": "Show crop", "callback_data": f"t:c:{critique_id}"})
    return {"inline_keyboard": [row[:3], row[3:]] if len(row) > 3 else [row]}


def fallback_result(measurements: dict, exif: dict | None, hints: list[dict]) -> dict:
    """A critique shaped result built only from measurements and hints, for when Claude failed twice."""
    top = hints[0] if hints else {"advice": "The full teacher could not look at this photo yet. The measured values "
                                            "are below, and it will try again later.", "settings": {}, "tag": ""}
    settings = {}
    for h in hints:
        for k, v in (h.get("settings") or {}).items():
            settings.setdefault(k, v)
    return {"summary": "Quick check, the full teacher will try again later",
            "scores": {a: {"score": 3, "reason": "not scored in a quick check"} for a in AREAS},
            "top_fix": {"title": top.get("title") or "What the measurements say", "why": top["advice"],
                        "principle": "", "steps": [h["advice"] for h in hints[1:3]]},
            "settings": settings, "settings_why": "", "exercise": "", "tags": [h["tag"] for h in hints if h.get("tag")],
            "strength_tags": [], "what_works": [], "seen": "", "question": "", "crop": None, "straighten_degrees": None}
