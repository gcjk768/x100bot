"""Teacher steps 4 and 5: compare (before and after), series review, crop preview, ask and plan.
Mixed into Teacher so the queue handler finds run_<kind>_job methods."""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import exif as exifmod
from . import guardrails, light as lightmod, measure, teacher_queue, teacher_render, vault, weather
from .claude import ClaudeError, UsageLimit, fill
from .ratelimit import LimitError
from .telegram import esc

log = logging.getLogger(__name__)
TOOLS_NONE = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"
TOOLS_READ_ONLY = "Bash,Edit,Write,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _resting(x, chat, ex: UsageLimit):
    when = ex.resets_at or (datetime.now(x.lim.tz) + timedelta(hours=1))
    x.tg.send(chat, f"The teacher is resting (Claude {ex.kind} limit). It will answer at about {when:%H:%M}.", parse_mode=None)
    return when.timestamp()


class TeacherMore:
    """Methods added to Teacher. self.x, self.s, self.cfg, self.root, self.data, self.call, self.context helpers exist."""

    # compare -----------------------------------------------------------------------------------------------------
    def run_compare_job(self, job: dict):
        x = self.x
        p = json.loads(job["payload_json"])
        chat = p["chat_id"]
        parent = x.conn.execute("SELECT * FROM critiques WHERE id=?", (p.get("parent_id"),)).fetchone()
        if not parent or not parent["folder"] or not Path(parent["folder"]).exists():
            x.tg.send(chat, "I no longer have the earlier photo for that critique, so I will treat this as a new one.", parse_mode=None)
            teacher_queue.enqueue(x, "critique", {k: v for k, v in p.items() if k != "parent_id"})
            return "done"
        reason = teacher_queue.can_run(x, "compare")
        if reason:
            x.tg.send(chat, f"The teacher has used today's critiques ({reason}). It will compare this tomorrow.", parse_mode=None)
            return self._midnight()
        from .teacher import ChatAction
        with ChatAction(x, chat, "upload_photo") as action:
            folder = Path(parent["folder"])
            data = x.tg.get_file_bytes(p["file_id"])
            original = folder / ("after_original" + p.get("suffix", ".jpg"))
            original.write_bytes(data)
            exif = None
            if p["source"] == "file" and exifmod.available():
                try:
                    exif = exifmod.read(original)
                except Exception as ex:   # noqa: BLE001
                    log.warning("exiftool failed: %s", ex)
            prep = measure.prepare(original, folder, self.cfg, self.cfg.measurement_thresholds, name="after.jpg")
            m = prep.measurements
            earlier = json.loads(parent["result_json"] or "{}")
            stdin = {"exif_available": exif is not None, "exif": exif or {}, "measurements": m, "level": self.level(),
                     "style": self.style(), "earlier_critique": earlier,
                     "before_measurements": json.loads(parent["measurements_json"] or "{}"),
                     "before_exif": json.loads(parent["exif_json"] or "null"), "caption": p.get("caption", "")}
            action.set("typing")
            prompt = (self.root / "prompts" / "compare_brief.md").read_text(encoding="utf-8")
            try:
                try:
                    if not parent["session_id"]:
                        raise ClaudeError("no session")
                    res = self.call("teacher_critique", prompt, stdin, schema="compare_schema.json", folder=folder,
                                    max_turns=6, allowed="Read", disallowed=TOOLS_READ_ONLY, resume=parent["session_id"])
                except ClaudeError as ex:
                    if isinstance(ex, UsageLimit):
                        raise
                    log.info("compare resume failed (%s), fresh session", ex)
                    res = self.call("teacher_critique", prompt, stdin, schema="compare_schema.json", folder=folder,
                                    max_turns=6, allowed="Read", disallowed=TOOLS_READ_ONLY)
            except UsageLimit as ex:
                return _resting(x, chat, ex)
            except (ClaudeError, LimitError) as ex:
                log.warning("compare failed: %s", ex)
                x.tg.send(chat, "The teacher could not compare these two just now. It will try again later.", parse_mode=None)
                return x.lim.clock() + 1800
        r = res.data
        errs = guardrails.check_simple(r, facts_file=self.facts_file, menus_file=self.menus_file, recipe_names=set())
        scores = {a: int(r["scores"][a]) for a in teacher_render.AREAS if a in r.get("scores", {})}
        if len(scores) != 6 or any(not 1 <= v <= 5 for v in scores.values()) or errs:
            log.warning("compare guardrails: %s", errs)
            if len(scores) != 6:
                x.tg.send(chat, "The teacher's comparison did not pass the checks. Try again later.", parse_mode=None)
                return "done"
        overall = teacher_render.overall(scores, self.cfg.score_weights)
        before = json.loads(parent["scores_json"] or "{}")
        cid = x.conn.execute(
            "INSERT INTO critiques(created_at, kind, source, file_unique_id, folder, caption, flags_json, exif_json, measurements_json,"
            " result_json, scores_json, overall, tags_json, strength_tags_json, session_id, parent_id, model, cost_usd, used_fallback, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,'done')",
            (x.lim.clock(), "compare", p["source"], p.get("file_unique_id"), str(folder), p.get("caption", ""), "{}",
             json.dumps(exif) if exif else None, json.dumps(m), json.dumps(r), json.dumps(scores), overall,
             json.dumps(r.get("tags", [])), json.dumps(r.get("strength_tags", [])), res.session_id, parent["id"], res.model,
             res.cost_usd)).lastrowid
        text = self.render_compare(r, scores, before, overall, parent["overall"])
        mid = x.tg.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_to_message_id=p.get("message_id"),
                        link_preview_options={"is_disabled": True})["message_id"]
        x.conn.execute("INSERT OR REPLACE INTO teacher_messages(message_id, critique_id, role) VALUES(?,?,?)", (mid, cid, "teacher"))
        try:
            from .progress import refresh_profile
            refresh_profile(x)
        except ImportError:
            pass
        vault.log_event("🔁", "compare", f"#{cid} after #{parent['id']}: {overall}")
        return "done"

    def render_compare(self, r: dict, scores: dict, before: dict, overall: float, before_overall) -> str:
        def delta(a):
            b = before.get(a)
            if b is None:
                return ""
            d = scores[a] - b
            return f" ({'+' if d > 0 else ''}{d})" if d else " (=)"
        lines = ["<b>Before and after</b>", esc(r.get("verdict", "")),
                 "Top fix applied: " + ("yes" if r.get("fix_applied") else "not yet")]
        if r.get("improved"):
            lines.append("<b>Improved</b>\n" + teacher_render.bullets(r["improved"]))
        if r.get("still_to_fix"):
            lines.append("<b>Still to fix</b>\n" + teacher_render.bullets(r["still_to_fix"]))
        lines.append(f"Composition {scores['composition']}/5{delta('composition')} · Light {scores['light']}/5{delta('light')} · "
                     f"Exposure {scores['exposure']}/5{delta('exposure')}\nFocus {scores['focus']}/5{delta('focus')} · "
                     f"Colour {scores['colour']}/5{delta('colour')} · Moment {scores['moment']}/5{delta('moment')}")
        ov = f"Overall {overall}/5" + (f" (was {before_overall})" if before_overall else "")
        lines.append(ov)
        if r.get("next_step"):
            lines.append(f"<b>Next step</b>\n{esc(r['next_step'])}")
        return "\n".join(lines)

    # series ------------------------------------------------------------------------------------------------------
    def run_series_job(self, job: dict):
        x = self.x
        p = json.loads(job["payload_json"])
        chat = p["chat_id"]
        reason = teacher_queue.can_run(x, "series")
        if reason:
            x.tg.send(chat, f"Series reviews are used up for today ({reason}). Send it again tomorrow.", parse_mode=None)
            return "done"
        parts = p["parts"][: self.cfg.series_max_images]
        cid = x.conn.execute("INSERT INTO critiques(created_at, kind, source, caption, flags_json, status) VALUES(?,?,?,?,?,'running')",
                             (x.lim.clock(), "series", parts[0]["source"], p.get("caption", ""), "{}")).lastrowid
        folder = self.data / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        from .teacher import ChatAction
        with ChatAction(x, chat, "upload_photo") as action:
            images = []
            for i, part in enumerate(parts, 1):
                data = x.tg.get_file_bytes(part["file_id"])
                original = folder / f"original_{i}{part.get('suffix', '.jpg')}"
                original.write_bytes(data)
                exif = None
                if part["source"] == "file" and exifmod.available():
                    try:
                        exif = exifmod.read(original)
                    except Exception as ex:   # noqa: BLE001
                        log.warning("exiftool failed: %s", ex)
                prep = measure.prepare(original, folder, self.cfg, self.cfg.measurement_thresholds, name=f"{i}.jpg")
                images.append({"index": i, "exif_available": exif is not None, "exif": exif or {}, "measurements": prep.measurements})
            stdin = {"caption": p.get("caption", ""), "level": self.level(), "style": self.style(), "images": images,
                     "week_theme": self.week_theme()}
            action.set("typing")
            prompt = f"Review the series of {len(images)} photos in this folder, named 1.jpg to {len(images)}.jpg. Return the series object."
            try:
                res = self.call("teacher_series", prompt, stdin, schema="series_schema.json", folder=folder, max_turns=8,
                                allowed="Read", disallowed=TOOLS_READ_ONLY)
            except UsageLimit as ex:
                x.conn.execute("UPDATE critiques SET status='waiting' WHERE id=?", (cid,))
                return _resting(x, chat, ex)
            except (ClaudeError, LimitError) as ex:
                log.warning("series failed: %s", ex)
                x.conn.execute("UPDATE critiques SET status='failed' WHERE id=?", (cid,))
                x.tg.send(chat, "The teacher could not review the series just now. Send it again later.", parse_mode=None)
                return "done"
        r = res.data
        errs = guardrails.check_simple(r, facts_file=self.facts_file, menus_file=self.menus_file, recipe_names=set())
        if errs:
            log.warning("series guardrails: %s", errs)
        n = len(images)
        text = self.render_series(r, n)
        mid = x.tg.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})["message_id"]
        x.conn.execute("INSERT OR REPLACE INTO teacher_messages(message_id, critique_id, role) VALUES(?,?,?)", (mid, cid, "teacher"))
        x.conn.execute("UPDATE critiques SET folder=?, measurements_json=?, result_json=?, session_id=?, model=?, cost_usd=?, status='done' WHERE id=?",
                       (str(folder), json.dumps(images), json.dumps(r), res.session_id, res.model, res.cost_usd, cid))
        vault.log_event("🖼", "series review", f"#{cid}, {n} frames")
        return "done"

    def render_series(self, r: dict, n: int) -> str:
        def frame(i):
            return f"frame {i}" if isinstance(i, int) and 1 <= i <= n else "a frame"
        lines = ["<b>Series review</b>", esc(r.get("theme_seen", "")),
                 f"<b>Strongest</b>: {frame(r.get('strongest_index'))}. {esc(r.get('strongest_why', ''))}",
                 f"<b>Weakest</b>: {frame(r.get('weakest_index'))}. {esc(r.get('weakest_why', ''))}"]
        order = [i for i in r.get("order", []) if isinstance(i, int) and 1 <= i <= n]
        if order:
            lines.append("<b>Order to show them</b>: " + ", ".join(str(i) for i in order))
        notes = [f"{pi['index']}. {esc(pi['note'])}" for pi in r.get("per_image", []) if isinstance(pi.get("index"), int)]
        if notes:
            lines.append("<b>Frame by frame</b>\n" + "\n".join(notes))
        for title, key in (("Consistency", "consistency"), ("The missing shot", "missing_shot"), ("Exercise", "exercise")):
            if r.get(key):
                lines.append(f"<b>{title}</b>\n{esc(r[key])}")
        return "\n".join(lines)

    # crop preview --------------------------------------------------------------------------------------------
    def run_crop_job(self, job: dict):
        x = self.x
        p = json.loads(job["payload_json"])
        c = x.conn.execute("SELECT * FROM critiques WHERE id=?", (p["critique_id"],)).fetchone()
        if not c or not c["folder"]:
            return "done"
        crop = (json.loads(c["result_json"] or "{}")).get("crop")
        src = Path(c["folder"]) / "photo.jpg"
        if not crop or not src.exists():
            x.tg.send(p["chat_id"], "There is no crop suggestion stored for that critique.", parse_mode=None)
            return "done"
        im = measure.crop_preview(measure.load(src), crop)
        out = measure.save_stripped(im, Path(c["folder"]) / "crop.jpg")
        mid = self.send_photo(p["chat_id"], out, f"Suggested crop ({esc(crop.get('aspect', ''))}): {esc(crop.get('reason', ''))}", p.get("reply_to"))
        x.conn.execute("INSERT OR REPLACE INTO teacher_messages(message_id, critique_id, role) VALUES(?,?,?)", (mid, c["id"], "teacher"))
        return "done"

    # ask and plan ------------------------------------------------------------------------------------------------
    def parse_plan(self, text: str) -> tuple[str, str, str, datetime]:
        """'street portraits, Chinatown, tomorrow evening' -> what, where, when text, the day."""
        parts = [t.strip() for t in text.split(",")]
        what = parts[0] if parts else text
        where = parts[1] if len(parts) > 1 else ""
        when = parts[2] if len(parts) > 2 else (parts[1] if len(parts) == 2 and re.search(r"today|tomorrow|day|\d", parts[1]) else "")
        now = datetime.now(self.x.lim.tz)
        day = now
        low = when.lower()
        if "tomorrow" in low:
            day = now + timedelta(days=1)
        else:
            for i, name in enumerate(WEEKDAYS):
                if name[:3] in low:
                    ahead = (i - now.weekday()) % 7
                    day = now + timedelta(days=ahead or 7)
                    break
            m = re.search(r"(\d{4}-\d{2}-\d{2})", when)
            if m:
                day = datetime.fromisoformat(m.group(1)).replace(tzinfo=self.x.lim.tz)
        return what, where, when, day

    def light_and_forecast(self, day: datetime) -> dict:
        s = self.s
        lt = lightmod.compute(s.location, day.date(), s.schedule.timezone)
        out = {"date": day.date().isoformat(), "light": lt.as_text(), "forecast": None}
        row = self.x.conn.execute("SELECT weather_json FROM light_days WHERE date=?", (day.date().isoformat(),)).fetchone()
        data = json.loads(row["weather_json"]) if row and row["weather_json"] else None
        if data is None and (day.date() - datetime.now(self.x.lim.tz).date()).days <= 1:
            data = weather.fetch(s, self.x.lim)
            if data:
                self.x.conn.execute("INSERT OR REPLACE INTO light_days(date, sunrise, sunset, golden_morning_end, golden_evening_start,"
                                    " blue_end, weather_json) VALUES(?,?,?,?,?,?,?)",
                                    (day.date().isoformat(), lt.hhmm("sunrise"), lt.hhmm("sunset"), lt.hhmm("golden_morning_end"),
                                     lt.hhmm("golden_evening_start"), lt.hhmm("blue_end"), json.dumps(data)))
        if data:
            hours = weather.hours_for(data, day.date().isoformat(), s.weather)
            out["forecast"] = {"periods": weather.periods(hours), "hours": {str(h): v for h, v in hours.items()}}
        return out

    def run_question_job(self, job: dict):
        return self._ask(job, plan=False)

    def run_plan_job(self, job: dict):
        return self._ask(job, plan=True)

    def _ask(self, job: dict, plan: bool):
        x = self.x
        p = json.loads(job["payload_json"])
        chat, question = p["chat_id"], p["question"]
        reason = teacher_queue.can_run(x, "question")
        if reason:
            x.tg.send(chat, f"Questions are used up for today ({reason}). Ask again tomorrow.", parse_mode=None)
            return "done"
        stdin = {"question": question, "level": self.level(), "style": self.style(), "learning_profile": self.profile(),
                 "camera_facts": self.camera_facts(), "menu_names": self.menu_names(), "recipes": self.recipe_list(),
                 "photographers": self.photographer_list(), "week_theme": self.week_theme(), "spots": self.spots()}
        if plan:
            what, where, when, day = self.parse_plan(question)
            stdin["plan_request"] = {"what": what, "where": where, "when": when, **self.light_and_forecast(day)}
            prompt = f"Make a shot plan for my student: {question}. Stdin holds the light times and forecast for that day. Return the ask object with the plan filled."
        else:
            prompt = f"My student asks: {question}. Return the ask object."
        from .teacher import ChatAction
        with ChatAction(x, chat, "typing"):
            try:
                res = self.call("teacher_question", prompt, stdin, schema="ask_schema.json", folder=self.data, max_turns=3,
                                allowed=None, disallowed=TOOLS_NONE)
            except UsageLimit as ex:
                return _resting(x, chat, ex)
            except (ClaudeError, LimitError) as ex:
                log.warning("ask failed: %s", ex)
                x.tg.send(chat, "The teacher could not answer just now. Try again in a little while.", parse_mode=None)
                return "done"
        r = res.data
        recipe_names = {row["name"] for row in x.conn.execute("SELECT name FROM recipes")}
        errs = guardrails.check_simple(r, facts_file=self.facts_file, menus_file=self.menus_file, recipe_names=recipe_names)
        if errs:
            log.warning("ask guardrails: %s", errs)
        text = esc(r.get("answer", ""))
        if r.get("steps"):
            text += "\n" + teacher_render.numbered(r["steps"])
        pl = r.get("plan") if plan else None
        if pl:
            text += "\n<b>Plan</b>"
            for label, key in (("When", "when"), ("Where to stand", "where_to_stand"), ("Look for", "look_for"), ("If the weather turns", "backup")):
                if pl.get(key):
                    text += f"\n{label}: {esc(pl[key])}"
            lf = stdin.get("plan_request", {})
            if lf.get("light"):
                lt = lf["light"]
                text += (f"\nLight on {lf['date']}: sunrise {lt['sunrise']}, golden hour {lt['golden_evening_start']} to {lt['sunset']},"
                         f" blue hour until {lt['blue_end']}")
            if lf.get("forecast"):
                per = lf["forecast"]["periods"]
                text += f"\nForecast: morning {per.get('morning')}, afternoon {per.get('afternoon')}, evening {per.get('evening')}"
        settings = {k: v for k, v in (r.get("settings") or {}).items() if v}
        if settings and not any("setting" in e or "ISO" in e or "aperture" in e for e in errs):
            text += "\n" + teacher_render.settings_table(settings, None, False).replace("unknown", "")
            how = teacher_render.how_to_set(settings, self.map_file)
            if how:
                text += f"\nHow to set it: {esc(how)}"
        if r.get("recipe_name") in recipe_names:
            row = x.conn.execute("SELECT source_url FROM recipes WHERE name=?", (r["recipe_name"],)).fetchone()
            text += f"\nRecipe: {esc(r['recipe_name'])}, <a href=\"{esc(row['source_url'])}\">full recipe</a>"
        if r.get("photographer_name"):
            row = x.conn.execute("SELECT official_url FROM people WHERE name=?", (r["photographer_name"],)).fetchone()
            if row:
                text += f"\nLearn from: {esc(r['photographer_name'])}, <a href=\"{esc(row['official_url'])}\">see their work</a>"
        for part in teacher_render.join([text]):
            x.tg.call("sendMessage", chat_id=chat, text=part, parse_mode="HTML", reply_to_message_id=p.get("reply_to"),
                      link_preview_options={"is_disabled": True})
        vault.log_event("❓", "plan" if plan else "question", question[:80])
        return "done"

    def spots(self) -> list[dict]:
        import yaml
        return yaml.safe_load(self.s.path(self.s.learning.spots_file).read_text(encoding="utf-8"))["spots"]
