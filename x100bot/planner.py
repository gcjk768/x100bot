"""The slot planner: weather and light for the day, then verified library material for every slot.
Each slot becomes one queue row whose facts are the only material the compose call may use."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date as Date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from . import light as lightmod
from . import render, weather

log = logging.getLogger(__name__)
# a slot with no verified material becomes a learning tip on a topic not used yet today, so nothing repeats
SUBSTITUTE = {"recipe": "learning_tip", "photographer": "learning_tip", "camera_tip": "learning_tip",
              "video_lesson": "learning_tip"}
LIGHT_WORDS = {"sunny": "sunny weather", "overcast": "overcast skies", "rain": "a rainy day",
               "partly cloudy": "mixed sun and cloud", "golden_hour": "golden hour", "night": "blue hour and night",
               "indoor": "indoor light", "any": "today's light"}
# which recipe light tags suit each hour label
TAG_FIT = {"sunny": {"sunny"}, "partly cloudy": {"sunny", "overcast"}, "overcast": {"overcast", "rain"},
           "rain": {"rain", "overcast"}, "golden_hour": {"golden_hour"}, "night": {"night", "indoor"}, "any": set()}


@lru_cache
def _yaml(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@dataclass
class Slot:
    hour: str
    type: str
    facts: dict = field(default_factory=dict)
    ref_type: str | None = None
    ref_id: int | None = None
    planned_type: str = ""


@dataclass
class Day:
    date: Date
    light: lightmod.Light
    hours: dict[int, str]
    periods: dict[str, str]
    week: dict
    cycle: int
    next_week: dict
    slots: list[Slot]


def curriculum_week(s, day: Date) -> tuple[dict, int, dict]:
    weeks = _yaml(str(s.path(s.learning.curriculum_file)))["weeks"]
    n = max(0, (day - Date.fromisoformat(s.learning.start_date)).days // 7)
    return weeks[n % len(weeks)], n // len(weeks) + 1, weeks[(n + 1) % len(weeks)]


def slot_types(s, day: Date) -> dict[str, str]:
    slots = dict(s.schedule.slots)
    if day.weekday() == 6:
        slots.update(s.schedule.sunday_overrides)
    return dict(sorted(slots.items()))


def hour_label(day: Day, hour: int, typ_hint: str = "") -> str:
    """golden_hour at 18:00 and night at 20:00 per the spec; otherwise the light, then the forecast."""
    special = lightmod.hour_light(day.light, hour)
    if special:
        return special
    return day.hours.get(hour, "any")


class Picker:
    """Chooses library rows. Everything chosen today is remembered so nothing repeats inside the day."""

    def __init__(self, conn, s, day: Date):
        self.conn, self.s, self.day = conn, s, day
        self.now = datetime.combine(day, datetime.min.time()).timestamp()
        self.used: dict[str, set[int]] = {}
        self.last_sim: str | None = None

    def _cutoff(self, days: int) -> float:
        return self.now - days * 86400

    def _take(self, kind: str, rid: int) -> None:
        self.used.setdefault(kind, set()).add(rid)

    def recipe(self, label: str):
        rows = self.conn.execute(
            "SELECT * FROM recipes WHERE last_posted_at IS NULL OR last_posted_at<?",
            (self._cutoff(self.s.library.recipe_repeat_days),)).fetchall()
        rows = [r for r in rows if r["id"] not in self.used.get("recipe", set())]
        fit = TAG_FIT.get(label, set())

        def score(r):
            tags = set(json.loads(r["light_tags_json"] or '["any"]'))
            return (0 if tags & fit else 1 if "any" in tags else 2,
                    0 if r["sensor"] == "X-Trans V" else 1, r["last_posted_at"] or 0, r["id"])
        rows.sort(key=score)
        # night and golden hour slots need a matching tag; untagged recipes count as any
        if label in ("golden_hour", "night"):
            rows = [r for r in rows if score(r)[0] < 2]
        pick = next((r for r in rows if r["film_simulation"] != self.last_sim), None)
        if pick:
            self._take("recipe", pick["id"])
            self.last_sim = pick["film_simulation"]
        return pick

    def person(self, kind: str, theme_key: str):
        rows = self.conn.execute(
            "SELECT * FROM people WHERE verified_at IS NOT NULL AND (last_posted_at IS NULL OR last_posted_at<?)",
            (self._cutoff(self.s.library.photographer_repeat_days),)).fetchall()
        rows = [r for r in rows if r["id"] not in self.used.get("person", set())]
        rows.sort(key=lambda r: (r["kind"] != kind, theme_key not in json.loads(r["themes_json"] or "[]"),
                                 r["last_posted_at"] or 0))
        if rows:
            self._take("person", rows[0]["id"])
        return rows[0] if rows else None

    def tip(self):
        rows = self.conn.execute(
            "SELECT * FROM tips WHERE verified_at IS NOT NULL AND (last_posted_at IS NULL OR last_posted_at<?) "
            "ORDER BY COALESCE(last_posted_at, 0), id", (self._cutoff(self.s.library.camera_tip_repeat_days),)).fetchall()
        rows = [r for r in rows if r["id"] not in self.used.get("tip", set())]
        if rows:
            self._take("tip", rows[0]["id"])
        return rows[0] if rows else None

    def video(self, theme: str):
        rows = self.conn.execute("SELECT * FROM videos WHERE verified_at IS NOT NULL AND last_posted_at IS NULL "
                                 "ORDER BY published_at DESC").fetchall()
        rows = [r for r in rows if r["id"] not in self.used.get("video", set())]
        words = {w.lower().strip(":,") for w in theme.split() if len(w) > 4}
        rows.sort(key=lambda r: not any(w in f"{r['title']} {r['description']}".lower() for w in words))
        if rows:
            self._take("video", rows[0]["id"])
        return rows[0] if rows else None

    def spot(self, theme_key: str, exclude: set[str]):
        spots = _yaml(str(self.s.path(self.s.learning.spots_file)))["spots"]
        recent = set()
        for r in self.conn.execute("SELECT facts_json FROM queue WHERE slot_at>=? AND slot_at<? AND type IN "
                                   "('brief','golden_hour')", ((self.day - timedelta(days=7)).isoformat(),
                                                                self.day.isoformat())):
            recent.add(json.loads(r["facts_json"] or "{}").get("spot"))
        ranked = sorted(spots, key=lambda sp: (sp["name"] in exclude, sp["name"] in recent,
                                               theme_key not in sp["themes"]))
        return ranked[0] if ranked else None

    def learning_topic(self, prefer: str | None = None) -> str:
        topics = self.s.learning.learning_topics
        last = {r["concept_title"]: r["posted_at"] for r in
                self.conn.execute("SELECT concept_title, MAX(posted_at) posted_at FROM lessons "
                                  "WHERE type='learning_tip' GROUP BY concept_title")}
        used = self.used.setdefault("topic_names", set())
        if prefer in topics and prefer not in used:
            used.add(prefer)
            return prefer
        fresh = [t for t in topics if t not in used]
        if not fresh:   # ponytail: only an almost empty library gets here; topics repeat until the seed fills it
            log.warning("more substitute slots than learning topics, repeating a topic")
            used.clear()
            fresh = list(topics)
        topic = sorted(fresh, key=lambda t: (last.get(t) or 0, topics.index(t)))[0]
        used.add(topic)
        return topic

    def recent_lessons(self) -> list[str]:
        return [r["concept_title"] for r in self.conn.execute(
            "SELECT concept_title FROM lessons WHERE posted_at>=? AND type IN ('composition','drill')",
            (self._cutoff(self.s.library.lesson_repeat_days),))]


def bank_note(conn, recipe_id: int) -> str | None:
    row = conn.execute("SELECT bank FROM custom_banks WHERE recipe_id=?", (recipe_id,)).fetchone()
    return f"You have this in C{row['bank']}" if row else None


def recipe_facts(conn, r, label: str) -> dict:
    settings = json.loads(r["adapted_settings_json"] or r["settings_json"])
    original = json.loads(r["settings_json"])
    note = None
    if r["adapted_settings_json"]:
        note = (f"Adapted from X-Trans IV: Color Chrome FX Blue lowered from {original['color_chrome_fx_blue']} to "
                f"{settings['color_chrome_fx_blue']}, as Fuji X Weekly advises for this film simulation.")
    return {"light_label": LIGHT_WORDS.get(label, label), "light_tag": label, "recipe_name": r["name"],
            "author": r["author"], "compat_label": r["compat_label"], "source_url": r["source_url"],
            "adaptation_note": note, "bank_note": bank_note(conn, r["id"]),
            "settings": {k: v for k, v in settings.items() if k != "raw"}, **render.recipe_values(settings)}


def plan_day(x, day: Date, offline: bool = False, weather_data: dict | None = None) -> Day:
    s = x.s
    lt = lightmod.compute(s.location, day, s.schedule.timezone)
    if weather_data is None and not offline:
        weather_data = weather.fetch(s, x.lim)
    hours = weather.hours_for(weather_data, day.isoformat(), s.weather)
    week, cycle, next_week = curriculum_week(s, day)
    d = Day(day, lt, hours, weather.periods(hours), week, cycle, next_week, [])
    pk = Picker(x.conn, s, day)
    theme, key = week["theme"], week["key"]
    week_number = week["week"]
    base = {"week_number": week_number, "theme": theme, "theme_key": key, "cycle": cycle}
    fb = _yaml(str(s.path("library/fallbacks.yaml")))
    types = slot_types(s, day)
    stock_low: list[str] = []

    # recipe slots first, so the brief, golden hour and photographer posts can point at the day's recipes
    recipes_by_hour = {}
    for hh, t in types.items():
        if t == "recipe":
            label = hour_label(d, int(hh))
            r = pk.recipe(label)
            recipes_by_hour[hh] = (r, label)
    main = weather.main_condition(hours)
    of_day = next(((r, lb) for r, lb in recipes_by_hour.values() if r and lb == main), None) or \
        next(((r, lb) for r, lb in recipes_by_hour.values() if r), (None, None))
    spot = pk.spot(key, set())
    gh_spot = pk.spot("night" if key == "night" else key, {spot["name"]} if spot else set())
    comp_n = 0
    for hh, t in types.items():
        sl = Slot(hh, t, dict(base), planned_type=t)
        if t == "recipe":
            r, label = recipes_by_hour[hh]
            if r:
                sl.ref_type, sl.ref_id = "recipe", r["id"]
                sl.facts.update(recipe_facts(x.conn, r, label))
            else:
                stock_low.append("recipe")
        elif t == "brief":
            r = of_day[0]
            sl.facts.update({
                "weekday": day.strftime("%A"), "date": day.strftime("%d %B %Y").lstrip("0"),
                "sunrise": lt.hhmm("sunrise"), "sunset": lt.hhmm("sunset"),
                "golden_evening_start": lt.hhmm("golden_evening_start"), "blue_end": lt.hhmm("blue_end"),
                **{f"weather_{k}": weather.WORDS.get(v, v) if hours else None for k, v in d.periods.items()},
                "forecast_note": None if hours else "Forecast unavailable today, so plan for the light alone.",
                "main_condition": main, "recipe_name": r["name"] if r else None,
                "film_simulation": (json.loads(r["adapted_settings_json"] or r["settings_json"])["film_simulation"]
                                    if r else None),
                "spot": spot["name"] if spot else None,
                "spot_themes": ", ".join(spot["themes"]).replace("_", " ") if spot else None})
        elif t == "photographer":
            kind = "master" if day.toordinal() % 2 == 0 else "educator"
            p = pk.person(kind, key)
            if p:
                sl.ref_type, sl.ref_id = "person", p["id"]
                sl.facts.update({"name": p["name"], "name_tag": p["name"], "kind": p["kind"], "identity": p["identity"],
                                 "official_url": p["official_url"], "notes": json.loads(p["facts_json"]),
                                 "recipe_name": of_day[0]["name"] if of_day[0] else None})
            else:
                stock_low.append("photographer")
        elif t == "camera_tip":
            fw = x.conn.execute("SELECT * FROM firmware WHERE found_at>=? ORDER BY found_at DESC LIMIT 1",
                                (pk.now - 7 * 86400,)).fetchone()
            if fw and hh == "09" and not x.conn.execute("SELECT 1 FROM kv WHERE key=?", (f"fw_posted:{fw['version']}",)).fetchone():
                sl.ref_type = "firmware"
                sl.facts.update({"topic": "firmware", "title": f"Firmware {fw['version']} is out",
                                 "menu_path": "Check the version in the camera, then follow the official update steps.",
                                 "manual_url": fw["url"], "tip_facts": [f"Fujifilm lists firmware version {fw['version']} for the X100VI."],
                                 "firmware_version": fw["version"]})
            else:
                tip = pk.tip()
                if tip:
                    sl.ref_type, sl.ref_id = "tip", tip["id"]
                    sl.facts.update({"topic": tip["topic"], "tip_title": tip["title"], "menu_path": tip["menu_path"],
                                     "manual_url": tip["manual_url"], "tip_facts": json.loads(tip["facts_json"])})
                else:
                    stock_low.append("camera_tip")
        elif t == "video_lesson":
            v = pk.video(theme)
            if v:
                sl.ref_type, sl.ref_id = "video", v["id"]
                sl.facts.update({"channel": v["channel"], "video_title": v["title"], "video_url": v["url"],
                                 "description": (v["description"] or "")[:600]})
            else:
                stock_low.append("video")
        elif t == "golden_hour":
            r18 = next((r for h, (r, lb) in recipes_by_hour.items() if r and lb == "golden_hour"), None)
            sl.facts.update({"golden_evening_start": lt.hhmm("golden_evening_start"), "sunset": lt.hhmm("sunset"),
                             "golden_evening_end": lt.hhmm("golden_evening_end"),
                             "weather_evening": weather.WORDS.get(d.periods.get("evening"), d.periods.get("evening")),
                             "recipe_name": r18["name"] if r18 else None, "spot": gh_spot["name"] if gh_spot else None})
        elif t in ("composition", "drill"):
            sl.facts.update({"lesson_index": comp_n if t == "composition" else 0,
                             "evening": int(hh) >= 18, "recent_lessons": pk.recent_lessons(),
                             "weather": d.periods})
            comp_n += t == "composition"
        elif t == "learning_tip":
            sl.facts["topic"] = pk.learning_topic()
        elif t == "weekly_recap":
            monday = day - timedelta(days=day.weekday())
            done = x.conn.execute("SELECT COUNT(*) FROM assignments WHERE date>=? AND date<=? AND done_at IS NOT NULL",
                                  (monday.isoformat(), day.isoformat())).fetchone()[0]
            sl.facts.update({"done_count": done, "next_theme": next_week["theme"]})
        # no verified material: the slot becomes a type that needs none, never a repeat inside its window
        if sl.type in SUBSTITUTE and sl.ref_type is None:
            sl.type = SUBSTITUTE[sl.type]
            sl.facts["topic"] = pk.learning_topic(prefer="choosing a recipe for the light" if t == "recipe" else None)
        sl.facts["hour_label"] = hour_label(d, int(hh))
        d.slots.append(sl)
    if stock_low:
        x.alert("stock_low", f"library stock is low for {', '.join(sorted(set(stock_low)))}; those slots were "
                             f"replaced on {day.isoformat()}")
    return d


def fallback_fields(s, sl: Slot) -> dict:
    """Text for a slot when compose is skipped or failed: the facts alone, plus the build time fallback copy."""
    fb = _yaml(str(s.path("library/fallbacks.yaml")))
    f = sl.facts
    th = fb["themes"].get(f.get("theme_key"), {})
    t = sl.type
    if t == "brief":
        drill = th.get("drill", {})
        return {"headline": f"{f['weekday']}, week {f['week_number']}: {f['theme'].split(':')[0]}",
                "assignment": th.get("composition", [{}])[0].get("exercise")}
    if t == "composition":
        lessons = th.get("composition", [])
        return dict(lessons[f.get("lesson_index", 0) % len(lessons)]) if lessons else {}
    if t == "drill":
        return dict(th.get("drill", {}))
    if t == "review":
        return {"headline": "Look back at today", **th.get("review", {})}
    if t == "golden_hour":
        return dict(fb["golden_hour"])
    if t == "learning_tip":
        return dict(fb["learning_topics"].get(f.get("topic"), {}))
    if t == "camera_tip":
        tf = f.get("tip_facts") or []
        return {"title": f.get("tip_title") or f.get("title"), "why": tf[0] if tf else None,
                "how_detail": " ".join(tf[1:]) if len(tf) > 1 else None}
    if t == "photographer":
        return {"what_to_study": " ".join(n["note"] for n in (f.get("notes") or [])[:2])}
    if t == "weekly_recap":
        return {}
    return {}


def render_slot(sl: Slot, fields: dict) -> tuple[str, str | None]:
    values = {**{k: v for k, v in sl.facts.items() if not isinstance(v, (dict,))}, **fields}
    if sl.type == "camera_tip" and fields.get("title") is None:
        values["title"] = sl.facts.get("tip_title") or sl.facts.get("title")
    return render.render(sl.type, values), render.preview_url(sl.type, values)


def run_plan(s, date: str | None = None, dry_run: bool = False, offline: bool = False) -> list[dict]:
    """Plan a day, render all 16 messages, and store them in the queue (or print them with --dry-run)."""
    import sys
    from .cli import Ctx
    from .lock import job_lock
    x = Ctx(s, "plan", offline=offline)
    day = Date.fromisoformat(date) if date else datetime.now(x.lim.tz).date()
    with job_lock(s.data_dir, "plan"):
        d = plan_day(x, day, offline=offline)
        composed = {}
        if not offline:
            from .compose import compose_day
            try:
                composed = compose_day(x, d)
            except Exception as ex:   # noqa: BLE001 - the day must never be empty because compose failed
                log.exception("compose failed, fallback templates for every slot")
                x.alert("compose_fallback", f"compose crashed ({type(ex).__name__}: {str(ex)[:120]}); fallback templates used")
                composed = {}
        out = []
        for sl in d.slots:
            fields = composed.get(sl.hour)
            used_fallback = fields is None
            if used_fallback:
                fields = fallback_fields(s, sl)
            text, preview = render_slot(sl, fields)
            row = {"slot_at": f"{day.isoformat()} {sl.hour}:00", "type": sl.type, "planned_type": sl.planned_type,
                   "ref_type": sl.ref_type, "ref_id": sl.ref_id, "facts": sl.facts, "fields": fields, "text": text,
                   "preview_url": preview, "notify": sl.type in s.telegram.notify_types, "used_fallback": used_fallback}
            out.append(row)
        if dry_run:
            sys.stdout.reconfigure(encoding="utf-8")
            for r in out:
                flag = f" (planned {r['planned_type']}, no stock)" if r["planned_type"] != r["type"] else ""
                print(f"===== {r['slot_at']}  {r['type']}{flag}  {'sound' if r['notify'] else 'silent'}  "
                      f"preview: {r['preview_url'] or 'off'}  {len(r['text'])} chars")
                print(r["text"])
                print()
            return out
        store_day(x, out)
        return out


def store_day(x, rows: list[dict]) -> None:
    """Write the day's rows. A slot that is already sending, posted, failed or skipped is never replaced."""
    for r in rows:
        x.conn.execute(
            "INSERT INTO queue(slot_at, type, ref_type, ref_id, facts_json, fields_json, text_html, preview_url, notify,"
            " status, used_fallback) VALUES(?,?,?,?,?,?,?,?,?,'pending',?) "
            "ON CONFLICT(slot_at) DO UPDATE SET type=excluded.type, ref_type=excluded.ref_type, ref_id=excluded.ref_id,"
            " facts_json=excluded.facts_json, fields_json=excluded.fields_json, text_html=excluded.text_html,"
            " preview_url=excluded.preview_url, notify=excluded.notify, used_fallback=excluded.used_fallback "
            "WHERE queue.status='pending'",
            (r["slot_at"], r["type"], r["ref_type"], r["ref_id"], json.dumps(r["facts"]), json.dumps(r["fields"]),
             r["text"], r["preview_url"], int(r["notify"]), int(r["used_fallback"])))
