"""One template per item type. The app prints every setting, number, time and link; Claude only fills text fields.
A line is left out when any value it uses is empty, so None, null or a dangling label never reaches the channel."""
from __future__ import annotations

import re
from string import Formatter

from .telegram import MAX_LEN, esc, esc_attr

# {name} is escaped text; {name:link} is used inside href and must be a URL. Lines are joined with newlines.
TEMPLATES: dict[str, list[str]] = {
    "recipe": [
        "#recipe  Film recipe for {light_label}",
        "<b>{recipe_name}</b>",
        "by {author}, {compat_label}",
        "{bank_note}",
        "",
        "Film Simulation: {film_simulation}",
        "Grain: {grain}",
        "Color Chrome Effect: {cce}",
        "Color Chrome FX Blue: {ccfxb}",
        "White Balance: {white_balance}",
        "Monochromatic Color: {mono_color}",
        "Dynamic Range: {dynamic_range}",
        "D Range Priority: {d_range_priority}",
        "Highlight: {highlight}   Shadow: {shadow}",
        "Color: {color}   Sharpness: {sharpness}",
        "Sharpness: {sharpness_only}",
        "High ISO NR: {nr}   Clarity: {clarity}",
        "ISO: {iso}",
        "Exposure Compensation: {exposure}",
        "{adaptation_note}",
        "",
        "Best for: {best_for}",
        "Why it works: {why}",
        "Try it today: {try_today}",
        "Save it: IMAGE QUALITY SETTING, EDIT/SAVE CUSTOM SETTING, then choose a CUSTOM slot.",
        '<a href="{source_url:link}">Full recipe and sample photos</a>',
    ],
    "brief": [
        "#today  {weekday} {date}",
        "<b>{headline}</b>",
        "Sunrise {sunrise}, golden hour {golden_evening_start} to {sunset}, blue hour until {blue_end}",
        "Morning {weather_morning}, afternoon {weather_afternoon}, evening {weather_evening}",
        "{forecast_note}",
        "Week {week_number}: {theme}",
        "Today's assignment: {assignment}",
        "Recipe of the day: {recipe_name}, {film_simulation}. {why_today}",
        "Recipe of the day: {recipe_name}, {film_simulation}.{no_why_today}",
        "Where to practise: {spot}, good for {spot_themes}",
    ],
    "composition": [
        "#composition #week{week_number}",
        "<b>{title}</b>",
        "{what_it_is}",
        "How to spot it: {how_to_see}",
        "With your X100VI: {camera_how}",
        "Exercise: {exercise}",
        "Common mistake: {mistake}",
        '<a href="{learn_more_url:link}">{learn_more_label}</a>',
    ],
    "photographer": [
        "#photographer  {name_tag}",
        "<b>{name}</b>, {identity}",
        "What to learn: {what_to_study}",
        "Composition moves: {moves}",
        "Try it with your X100VI: {try_it}",
        "Recipe to pair: {recipe_name}",
        '<a href="{official_url:link}">See their work</a>',
    ],
    "camera_tip": [
        "#x100vi  {topic}",
        "<b>{title}</b>",
        "Why: {why}",
        "How: {menu_path}",
        "{how_detail}",
        "When to use it: {when}",
        '<a href="{manual_url:link}">Official manual page</a>',
    ],
    "video_lesson": [
        "#lesson  {channel}",
        "<b>{video_title}</b>",
        "What to watch for: {watch_for}",
        "Then try: {then_try}",
        '<a href="{video_url:link}">Watch on YouTube</a>',
    ],
    "drill": [
        "#drill #week{week_number}",
        "<b>{title}</b>",
        "{steps}",
        "Settings to start with: {settings_hint}",
    ],
    "golden_hour": [
        "#goldenhour",
        "<b>Golden hour from {golden_evening_start}, sunset {sunset}</b>",
        "Plan: {plan}",
        "Look for: {look_for}",
        "Set up: {setup}",
        "Recipe: {recipe_name}",
        "Where: {spot}",
    ],
    "learning_tip": [
        "#learn  {topic}",
        "<b>{title}</b>",
        "{body}",
        "Try this week: {action}",
    ],
    "review": [
        "#review",
        "<b>{headline}</b>",
        "Pick your best three frames from today and ask:",
        "1. {q1}",
        "2. {q2}",
        "3. {q3}",
        "{culling_tip}",
        "Reply /done to log today's assignment.",
    ],
    "weekly_recap": [
        "#recap #week{week_number}",
        "<b>Week {week_number}: {theme}</b>",
        "Assignments done: {done_count} of 7",
        "{recap}",
        "Next week: {next_theme}. {next_week_hint}",
        "Next week: {next_theme}.{no_next_week_hint}",
        "{bank_plan}",
    ],
}

# which link previews the type shows: the recipe page, the official page, the video. Everything else: none
PREVIEW_KEY = {"recipe": "source_url", "photographer": "official_url", "video_lesson": "video_url",
               "composition": "learn_more_url"}


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or (isinstance(v, str) and v.strip().lower() in ("none", "null"))


def _text(v) -> str:
    if isinstance(v, list):
        return "\n".join(esc(x) for x in v if not _empty(x))
    return esc(v)


def render(item_type: str, values: dict) -> str:
    """Fill the type's template. Lines whose values are missing are dropped; blank separators collapse."""
    values = dict(values)
    # the brief and recap lines have a variant for when Claude's sentence is missing
    for text_key, alt in (("why_today", "no_why_today"), ("next_week_hint", "no_next_week_hint")):
        values[alt] = None if not _empty(values.get(text_key)) else " "
    out: list[str] = []
    for line in TEMPLATES[item_type]:
        names = [(f, spec) for _, f, spec, _ in Formatter().parse(line) if f]
        if any(_empty(values.get(f)) for f, _ in names):
            continue
        filled = line
        for f, spec in names:
            v = values[f]
            if spec == "link":
                if not re.match(r"^https?://", str(v)):
                    filled = None
                    break
                filled = filled.replace(f"{{{f}:link}}", esc_attr(v))
            else:
                filled = filled.replace(f"{{{f}}}", _text(v))
        if filled is not None:
            out.append(filled.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if len(text) > MAX_LEN:
        raise ValueError(f"{item_type} message is {len(text)} characters, over {MAX_LEN}")
    return text


def preview_url(item_type: str, values: dict) -> str | None:
    key = PREVIEW_KEY.get(item_type)
    v = values.get(key) if key else None
    return v if v and re.match(r"^https?://", v) else None


# helpers the planner uses to turn a recipe's settings into template values ---------------------------------------

def signed(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"+{v}" if v > 0 else str(v)


def recipe_values(settings: dict) -> dict:
    s = settings
    wb = s.get("white_balance", "")
    if wb == "COLOR TEMPERATURE" and s.get("wb_kelvin"):
        wb = f"{s['wb_kelvin']}K"
    if wb and s.get("wb_red") is not None:
        wb = f"{wb}, R {signed(s['wb_red'])}, B {signed(s['wb_blue'])}"
    grain = s.get("grain_roughness")
    if grain and grain != "OFF" and s.get("grain_size"):
        grain = f"{grain}, {s['grain_size']}"
    mc = s.get("monochromatic_color")
    has_color = s.get("color") is not None
    return {
        "film_simulation": s.get("film_simulation"), "grain": grain, "cce": s.get("color_chrome_effect"),
        "ccfxb": s.get("color_chrome_fx_blue"), "white_balance": wb,
        "mono_color": f"WC {signed(mc['wc'])}, MG {signed(mc['mg'])}" if mc else None,
        "dynamic_range": s.get("dynamic_range"), "d_range_priority": s.get("d_range_priority"),
        "highlight": signed(s.get("highlight")), "shadow": signed(s.get("shadow")),
        "color": signed(s.get("color")) if has_color else None,
        "sharpness": signed(s.get("sharpness")) if has_color else None,
        "sharpness_only": None if has_color else signed(s.get("sharpness")),
        "nr": signed(s.get("high_iso_nr")), "clarity": signed(s.get("clarity")),
        "iso": s.get("iso"), "exposure": s.get("exposure_compensation"),
    }
