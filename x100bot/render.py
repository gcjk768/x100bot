"""One template per item type. The app prints every setting, number, time and link; Claude only fills text fields.
A line is left out when any value it uses is empty, so None, null or a dangling label never reaches the channel."""
from __future__ import annotations

import re
from string import Formatter

from .telegram import MAX_LEN, esc, esc_attr

# Card style shared by all of James's bots: emoji + <b>TITLE</b> · subtitle header, one block per item with emoji
# led detail lines, copyable values in <code>, hints in <i>, a divider between groups, short link labels, and
# secondary detail in <blockquote expandable>. One fixed emoji per section type, defined once here.
SECTION_TITLES = {
    "recipe": "🎞 <b>RECIPE</b>", "brief": "🌅 <b>TODAY</b>", "composition": "🖼 <b>COMPOSITION</b>",
    "photographer": "👤 <b>PHOTOGRAPHER</b>", "camera_tip": "📷 <b>X100VI TIP</b>", "video_lesson": "▶️ <b>VIDEO LESSON</b>",
    "drill": "⏱ <b>DRILL</b>", "golden_hour": "🌇 <b>GOLDEN HOUR</b>", "learning_tip": "📚 <b>LEARN</b>",
    "review": "🔍 <b>REVIEW</b>", "weekly_recap": "📆 <b>WEEKLY RECAP</b>",
}
DIVIDER = "━━━━━━━━━━━━━━━━"
T = SECTION_TITLES

# {name} is escaped text; {name:link} is used inside href and must be a URL. {no_name} is a single space when
# {name} is empty, so a line can have a variant without the Claude sentence. Lines are joined with newlines.
TEMPLATES: dict[str, list[str]] = {
    "recipe": [
        f"{T['recipe']} · {{light_label}}",
        "",
        "🎞 <b>{recipe_name}</b> · by {author}, {compat_label}",
        "🔖 <i>{bank_note}</i>",
        "🎬 Film Simulation <code>{film_simulation}</code>",
        "⚙️ Grain <code>{grain}</code> · Color Chrome <code>{cce}</code> · FX Blue <code>{ccfxb}</code>",
        "🌡 White Balance <code>{white_balance}</code>",
        "🎨 Monochromatic Color <code>{mono_color}</code>",
        "📈 Dynamic Range <code>{dynamic_range}</code> · Highlight <code>{highlight}</code> · Shadow <code>{shadow}</code>",
        "📈 D Range Priority <code>{d_range_priority}</code>",
        "🎨 Color <code>{color}</code> · Sharpness <code>{sharpness}</code> · Clarity <code>{clarity}</code> · NR <code>{nr}</code>",
        "🎨 Sharpness <code>{sharpness_only}</code> · Clarity <code>{clarity}</code> · NR <code>{nr}</code>",
        "📷 ISO <code>{iso}</code> · Exposure <code>{exposure}</code>",
        DIVIDER,
        "✨ Best for: {best_for}",
        "💡 Why it works: {why}",
        "🎯 Try it today: {try_today}",
        '<a href="{source_url:link}">Full recipe and sample photos</a>  ·  #recipe',
        "<blockquote expandable>Save it: IMAGE QUALITY SETTING, EDIT/SAVE CUSTOM SETTING, then choose a CUSTOM slot. "
        "{adaptation_note}</blockquote>",
        "<blockquote expandable>Save it: IMAGE QUALITY SETTING, EDIT/SAVE CUSTOM SETTING, then choose a CUSTOM slot."
        "{no_adaptation_note}</blockquote>",
    ],
    "brief": [
        f"{T['brief']} · {{weekday}} {{date}}",
        "",
        "📝 <b>{headline}</b>",
        "☀️ Sunrise <code>{sunrise}</code> · golden hour <code>{golden_evening_start}</code> to <code>{sunset}</code>"
        " · blue hour until <code>{blue_end}</code>",
        "🌤 Morning {weather_morning} · afternoon {weather_afternoon} · evening {weather_evening}",
        "<i>{forecast_note}</i>",
        "📚 Week {week_number} · {theme}",
        "🎯 Assignment: {assignment}",
        "🎞 Recipe of the day: <b>{recipe_name}</b> · {film_simulation}. {why_today}",
        "🎞 Recipe of the day: <b>{recipe_name}</b> · {film_simulation}.{no_why_today}",
        "📍 Where: <b>{spot}</b> · good for {spot_themes}",
        "#today",
    ],
    "composition": [
        f"{T['composition']} · week {{week_number}}",
        "",
        "🖼 <b>{title}</b>",
        "{what_it_is}",
        "👀 How to spot it: {how_to_see}",
        "📷 With your X100VI: {camera_how}",
        "🎯 Exercise: {exercise}",
        "⚠️ Common mistake: {mistake}",
        '<a href="{learn_more_url:link}">{learn_more_label}</a>',
        "#composition #week{week_number}",
    ],
    "photographer": [
        f"{T['photographer']} · {{kind}}",
        "",
        "👤 <b>{name}</b> · {identity}",
        "📖 What to learn: {what_to_study}",
        "🧭 Composition moves: {moves}",
        "🎯 Try it with your X100VI: {try_it}",
        "🎞 Recipe to pair: <b>{recipe_name}</b>",
        '<a href="{official_url:link}">See their work</a>  ·  #photographer',
    ],
    "camera_tip": [
        f"{T['camera_tip']} · {{topic}}",
        "",
        "📷 <b>{title}</b>",
        "💡 Why: {why}",
        "🔧 How: <code>{menu_path}</code>",
        "{how_detail}",
        "⏰ When to use it: {when}",
        '<a href="{manual_url:link}">Official manual page</a>  ·  #x100vi',
    ],
    "video_lesson": [
        f"{T['video_lesson']} · {{channel}}",
        "",
        "▶️ <b>{video_title}</b>",
        "👀 What to watch for: {watch_for}",
        "🎯 Then try: {then_try}",
        '<a href="{video_url:link}">Watch on YouTube</a>  ·  #lesson',
    ],
    "drill": [
        f"{T['drill']} · week {{week_number}}, ten minutes",
        "",
        "⏱ <b>{title}</b>",
        "{steps}",
        "⚙️ Settings to start with: {settings_hint}",
        "#drill #week{week_number}",
    ],
    "golden_hour": [
        f"{T['golden_hour']} · from <code>{{golden_evening_start}}</code>, sunset <code>{{sunset}}</code>",
        "",
        "🗺 Plan: {plan}",
        "👀 Look for: {look_for}",
        "📷 Set up: {setup}",
        "🎞 Recipe: <b>{recipe_name}</b>",
        "📍 Where: <b>{spot}</b>",
        "#goldenhour",
    ],
    "learning_tip": [
        f"{T['learning_tip']} · {{topic}}",
        "",
        "📚 <b>{title}</b>",
        "{body}",
        "🎯 Try this week: {action}",
        "#learn",
    ],
    "review": [
        f"{T['review']} · pick your best three frames from today",
        "",
        "🔍 <b>{headline}</b>",
        "1️⃣ {q1}",
        "2️⃣ {q2}",
        "3️⃣ {q3}",
        "✂️ {culling_tip}",
        "<i>Reply /done to log today's assignment.</i>",
        "#review",
    ],
    "weekly_recap": [
        f"{T['weekly_recap']} · week {{week_number}}",
        "",
        "📆 <b>Week {week_number}: {theme}</b>",
        "✅ Assignments done: <code>{done_count}</code> of 7",
        "{recap}",
        "➡️ Next week: <b>{next_theme}</b>. {next_week_hint}",
        "➡️ Next week: <b>{next_theme}</b>.{no_next_week_hint}",
        "{bank_plan}",
        "#recap #week{week_number}",
    ],
}

# which link previews the type shows: the recipe page, the official page, the video. Everything else: none
PREVIEW_KEY = {"recipe": "source_url", "photographer": "official_url", "video_lesson": "video_url",
               "composition": "learn_more_url"}


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or (isinstance(v, str) and v.strip().lower() in ("none", "null"))


def _text(v, key: str = "") -> str:
    if isinstance(v, list):
        items = [esc(x) for x in v if not _empty(x)]
        if key == "steps":
            return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))
        return " · ".join(items)
    return esc(v)


def render(item_type: str, values: dict) -> str:
    """Fill the type's template. Lines whose values are missing are dropped; blank separators collapse."""
    values = dict(values)
    out: list[str] = []
    for line in TEMPLATES[item_type]:
        names = [(f, spec) for _, f, spec, _ in Formatter().parse(line) if f]
        # {no_x} is the variant marker: present (and rendered as nothing) exactly when {x} is empty
        for f, _ in names:
            if f.startswith("no_"):
                values[f] = "" if _empty(values.get(f[3:])) else None
        if any(_empty(values.get(f)) and not (f.startswith("no_") and values.get(f) == "") for f, _ in names):
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
                filled = filled.replace(f"{{{f}}}", _text(v, f))
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
