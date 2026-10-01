"""The photo teacher: the pipeline for one photo (download, EXIF, measurements, Claude critique, guardrails,
render, send, store), the queue handler, and the Claude calls. Follow ups, compare, series, ask and progress build
on the same pieces."""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import exif as exifmod
from . import guardrails, measure, teacher_queue, teacher_render, vault
from .claude import Claude, ClaudeError, UsageLimit, fill
from .db import kv_get
from .ratelimit import LimitError

log = logging.getLogger(__name__)
TOOLS_NONE = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"
TOOLS_READ_ONLY = "Bash,Edit,Write,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"
BUTTON_QUESTIONS = {"s": "Explain the top fix again in simpler words, with fewer terms",
                    "d": "Go deeper on composition and light, as for an advanced photographer",
                    "w": "Explain each score in one sentence against the rubric",
                    "r": "Give me a step by step plan to reshoot this tomorrow"}


def setting(x, key: str, default: str) -> str:
    row = x.conn.execute("SELECT value FROM teacher_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(x, key: str, value: str) -> None:
    x.conn.execute("INSERT OR REPLACE INTO teacher_settings(key, value) VALUES(?,?)", (key, value))


class Teacher:
    def __init__(self, x):
        self.x, self.s = x, x.s
        self.cfg = x.s.teacher
        self.root = x.s.root
        self.data = Path(x.s.data_dir) / "teacher"
        self.claude = Claude(x.s, x.lim, x.lim.tz)
        self.facts_file = str(x.s.path(x.s.camera.facts_file))
        self.menus_file = str(x.s.path(x.s.camera.menus_file))
        self.map_file = str(x.s.path("camera/settings_menu_map.yaml"))

    # context ----------------------------------------------------------------------------------------------------
    def level(self) -> str:
        return setting(self.x, "level", self.cfg.level)

    def style(self) -> str:
        return setting(self.x, "style", self.cfg.style)

    def detail(self) -> str:
        return setting(self.x, "detail", self.cfg.default_detail)

    def camera_facts(self) -> list[str]:
        f = yaml.safe_load(Path(self.facts_file).read_text(encoding="utf-8"))
        return [x["text"] for x in f["facts"]] + ["Film simulations: " + ", ".join(f["film_simulations"]["color"]
                                                                                   + f["film_simulations"]["monochrome"])]

    def menu_names(self) -> list[str]:
        m = yaml.safe_load(Path(self.menus_file).read_text(encoding="utf-8"))
        return sorted({i["name"] for i in m["items"]})

    def recipe_list(self, light: str | None = None, limit: int = 40) -> list[dict]:
        rows = [dict(r) for r in self.x.conn.execute("SELECT name, film_simulation, light_tags_json, source_url FROM recipes")]
        for r in rows:
            r["light_tags"] = json.loads(r.pop("light_tags_json") or '["any"]')
        rows.sort(key=lambda r: (light not in r["light_tags"]) if light else 0)
        return [{"name": r["name"], "film_simulation": r["film_simulation"], "light_tags": r["light_tags"]} for r in rows[:limit]]

    def photographer_list(self, limit: int = 30) -> list[dict]:
        return [{"name": r["name"], "themes": json.loads(r["themes_json"] or "[]")} for r in
                self.x.conn.execute("SELECT name, themes_json FROM people WHERE verified_at IS NOT NULL LIMIT ?", (limit,))]

    def week_theme(self) -> str:
        from .planner import curriculum_week
        week, _, _ = curriculum_week(self.s, datetime.now(self.x.lim.tz).date())
        return week["theme"]

    def todays_assignment(self) -> str | None:
        today = self.x.lim.today()
        row = self.x.conn.execute("SELECT fields_json FROM queue WHERE slot_at LIKE ? AND type='brief'", (f"{today}%",)).fetchone()
        return json.loads(row["fields_json"] or "{}").get("assignment") if row else None

    def profile(self) -> dict:
        try:
            from .progress import learning_profile
            return learning_profile(self.x)
        except ImportError:
            return {"critiques": 0}

    def light_hint(self, m: dict) -> str | None:
        lm = m.get("luma_mean", 128)
        if lm < 60:
            return "night"
        if m.get("colour", {}).get("cast") == "warm" and lm < 150:
            return "golden_hour"
        return "overcast" if m.get("contrast", 60) < 45 else "sunny"

    # the critique ---------------------------------------------------------------------------------------------
    def context(self, caption: str, exif: dict | None, m: dict, flags: dict) -> dict:
        return {"level": self.level(), "style": self.style(), "detail": "quick" if flags.get("quick") else self.detail(),
                "caption": caption or "", "exif_available": exif is not None, "exif": exif or {},
                "measurements": m, "learning_profile": self.profile(), "week_theme": self.week_theme(),
                "assignment": self.todays_assignment() if flags.get("assignment") else None,
                "recipes": self.recipe_list(self.light_hint(m)), "photographers": self.photographer_list(),
                "camera_facts": self.camera_facts(), "menu_names": self.menu_names(),
                "allowed_tags": sorted(guardrails.allowed_tags(str(self.root / "prompts" / "teacher_schema.json"))[0]),
                "allowed_strength_tags": sorted(guardrails.allowed_tags(str(self.root / "prompts" / "teacher_schema.json"))[1])}

    def brief(self, flags: dict) -> str:
        return fill((self.root / "prompts" / "teacher_brief.md").read_text(encoding="utf-8"),
                    detail="quick" if flags.get("quick") else self.detail(), level=self.level(), style=self.style(),
                    theme=self.week_theme(),
                    assignment_line=" Check it against today's assignment and fill assignment_met." if flags.get("assignment") else "")

    def call(self, bucket: str, prompt: str, stdin: dict, *, schema: str, folder: Path, max_turns: int, allowed: str | None,
             disallowed: str, resume: str | None = None, model: str | None = None):
        return self.claude.run(bucket, prompt, stdin, schema=self.root / "prompts" / schema, timeout=self.cfg.timeout_seconds,
                               model=model or self.cfg.model, cwd=folder, system_file=None if resume else
                               self.root / "prompts" / schema.replace("_schema.json", "_system.md"),
                               allowed_tools=allowed, disallowed_tools=disallowed, max_turns=max_turns, resume=resume,
                               persist_session=True)

    def critique_call(self, folder: Path, stdin: dict, flags: dict, measured_tilt: dict, recipe_names: set, people: set):
        """Claude, then guardrails; one retry with the errors appended. Returns (clean result, Result) or raises."""
        prompt = self.brief(flags)
        res = self.call("teacher_critique", prompt, stdin, schema="teacher_schema.json", folder=folder, max_turns=6,
                        allowed="Read", disallowed=TOOLS_READ_ONLY)
        clean, errs = guardrails.check_critique(res.data, facts_file=self.facts_file, menus_file=self.menus_file,
                                                 schema_file=str(self.root / "prompts" / "teacher_schema.json"),
                                                 recipe_names=recipe_names, photographer_names=people,
                                                 exif_available=stdin["exif_available"], measured_tilt=measured_tilt)
        if not errs:
            return clean, res
        log.warning("teacher guardrails: %s", errs)
        retry = prompt + "\n\nYour previous reply had these problems; fix them and return the whole object again:\n" + "\n".join(errs)
        res2 = self.call("teacher_critique", retry, stdin, schema="teacher_schema.json", folder=folder, max_turns=6,
                         allowed="Read", disallowed=TOOLS_READ_ONLY, resume=res.session_id)
        clean, errs = guardrails.check_critique(res2.data, facts_file=self.facts_file, menus_file=self.menus_file,
                                                 schema_file=str(self.root / "prompts" / "teacher_schema.json"),
                                                 recipe_names=recipe_names, photographer_names=people,
                                                 exif_available=stdin["exif_available"], measured_tilt=measured_tilt)
        if errs:
            raise ClaudeError("guardrails failed twice: " + "; ".join(errs))
        return clean, res2

    def hints_for(self, m: dict, exif: dict | None) -> list[dict]:
        rules = yaml.safe_load((self.root / "library" / "hints.yaml").read_text(encoding="utf-8"))["rules"]
        th = self.cfg.measurement_thresholds
        flat = {"highlights_clipped_pct": m.get("highlights_clipped_pct"), "shadows_crushed_pct": m.get("shadows_crushed_pct"),
                "luma_mean": m.get("luma_mean"), "tilt.tilted": m.get("tilt", {}).get("tilted"),
                "sharpness.blur_suspect": m.get("sharpness", {}).get("blur_suspect"), "colour.cast": m.get("colour", {}).get("cast"),
                "exif.shutter": (exif or {}).get("shutter"), "exif.iso": (exif or {}).get("iso"), "exif.aperture": (exif or {}).get("aperture")}
        out = []
        for rule in rules:
            ok = True
            for c in rule["when"]:
                v = flat.get(c["metric"])
                target = c.get("value")
                if target == "threshold":
                    target = th.get(c["metric"].split(".")[-1])
                op = c["op"]
                if v is None or (op in ("gt", "lt", "ge", "le") and target is None):
                    ok = False
                elif op == "true":
                    ok = bool(v)
                elif op == "false":
                    ok = not v
                elif op == "eq":
                    ok = str(v) == str(target)
                elif op == "gt":
                    ok = v > target
                elif op == "lt":
                    ok = v < target
                elif op == "ge":
                    ok = v >= target
                elif op == "le":
                    ok = v <= target
                if not ok:
                    break
            if ok:
                out.append(rule)
        return out

    def run_critique_job(self, job: dict):
        """The queue handler for a single photo. Returns 'done', 'failed' or a not_before time."""
        x = self.x
        p = json.loads(job["payload_json"])
        chat, caption, flags = p["chat_id"], p.get("caption", ""), p.get("flags", {})
        cap_reason = teacher_queue.can_run(x, "critique")
        if cap_reason:
            x.tg.send(chat, f"The teacher has used today's critiques ({cap_reason}). It will look at this photo tomorrow.", parse_mode=None)
            return self._midnight()
        cid = x.conn.execute("INSERT INTO critiques(created_at, kind, source, file_unique_id, caption, flags_json, status) "
                             "VALUES(?,?,?,?,?,?,'running')", (x.lim.clock(), "single", p["source"], p.get("file_unique_id"),
                                                               caption, json.dumps(flags))).lastrowid
        folder = self.data / str(cid)
        folder.mkdir(parents=True, exist_ok=True)
        x.tg.call("sendChatAction", chat_id=chat, action="upload_photo")
        # download
        data = x.tg.get_file_bytes(p["file_id"])
        original = folder / ("original" + p.get("suffix", ".jpg"))
        original.write_bytes(data)
        exif = None
        if p["source"] == "file" and exifmod.available():
            try:
                exif = exifmod.read(original)
            except Exception as ex:   # noqa: BLE001
                log.warning("exiftool failed: %s", ex)
        prep = measure.prepare(original, folder, self.cfg, self.cfg.measurement_thresholds)
        m = prep.measurements
        stdin = self.context(caption, exif, m, flags)
        recipe_names = {r["name"] for r in x.conn.execute("SELECT name FROM recipes")}
        people = {r["name"] for r in x.conn.execute("SELECT name FROM people")}
        used_fallback, result, res = False, None, None
        try:
            result, res = self.critique_call(folder, stdin, flags, m["tilt"], recipe_names, people)
        except UsageLimit as ex:
            if ex.retry_with_fallback:
                try:
                    res = self.call("teacher_critique", self.brief(flags), stdin, schema="teacher_schema.json", folder=folder,
                                    max_turns=6, allowed="Read", disallowed=TOOLS_READ_ONLY, model=self.cfg.fallback_model)
                    result, errs = guardrails.check_critique(res.data, facts_file=self.facts_file, menus_file=self.menus_file,
                                                             schema_file=str(self.root / "prompts" / "teacher_schema.json"),
                                                             recipe_names=recipe_names, photographer_names=people,
                                                             exif_available=exif is not None, measured_tilt=m["tilt"])
                    if errs:
                        result = None
                except ClaudeError:
                    result = None
            if result is None:
                when = ex.resets_at or (datetime.now(x.lim.tz) + timedelta(hours=1))
                x.tg.send(chat, f"The teacher is resting (Claude {ex.kind} limit). It will answer at about {when:%H:%M}.", parse_mode=None)
                x.conn.execute("UPDATE critiques SET status='waiting' WHERE id=?", (cid,))
                return when.timestamp()
        except (ClaudeError, LimitError) as ex:
            log.warning("teacher critique fell back: %s", ex)
            used_fallback = True
            result = teacher_render.fallback_result(m, exif, self.hints_for(m, exif))
        self.send_critique(cid, chat, p.get("message_id"), result, exif, m, prep, used_fallback, res, flags)
        if used_fallback:
            # the full teacher tries again later with a fresh job
            teacher_queue.enqueue(x, "critique", {**p, "retry_of": cid})
            return "done"
        return "done"

    def _midnight(self) -> float:
        now = datetime.now(self.x.lim.tz)
        return (now.replace(hour=0, minute=1, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    def send_critique(self, cid, chat, reply_to, result, exif, m, prep, used_fallback, res, flags):
        x = self.x
        weights = self.cfg.score_weights
        overall = teacher_render.overall(result["scores"], weights)
        avg = x.conn.execute("SELECT AVG(overall) FROM critiques WHERE status='done' AND created_at>=? AND id<>?",
                             (x.lim.clock() - 30 * 86400, cid)).fetchone()[0]
        trend = f" (your 30 day average is {avg:.1f})" if avg else ""
        folder = prep.folder
        ov = measure.overlay(prep.overlay_base, result.get("crop"), m.get("tilt"))
        ov_path = measure.save_stripped(ov, folder / "overlay.jpg", quality=88)
        cap = teacher_render.caption(result, overall, trend)
        msg1 = self.send_photo(chat, ov_path, cap, reply_to)
        recipe = None
        rn = (result.get("settings") or {}).get("recipe_name")
        if rn:
            r = x.conn.execute("SELECT name, source_url FROM recipes WHERE name=?", (rn,)).fetchone()
            recipe = dict(r) if r else None
        photographer = None
        lf = result.get("learn_from")
        if lf and lf.get("photographer_name"):
            r = x.conn.execute("SELECT name, official_url FROM people WHERE name=?", (lf["photographer_name"],)).fetchone()
            photographer = dict(r) if r else None
        closest = None
        if exif and exif.get("film_simulation"):
            best, _ = exifmod.closest_recipe(x.conn, exifmod.to_recipe_like(exif))
            closest = best["name"] if best else None
        quick = flags.get("quick") or self.detail() == "quick"
        socratic = None
        if self.style() == "socratic" and not quick:
            socratic = [result.get("question") or "What was this photo meant to say?",
                        f"Where does your eye land first, and is that where {result['top_fix']['title'].lower()} would send it?"]
        parts = teacher_render.join(teacher_render.guidance(result, exif=exif, exif_available=exif is not None, measurements=m,
                                                            map_path=self.map_file, recipe=recipe, photographer=photographer,
                                                            closest_recipe=closest, quick=quick, socratic_questions=socratic))
        ids = [msg1]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            mid = x.tg.call("sendMessage", chat_id=chat, text=part, parse_mode="HTML", reply_to_message_id=msg1,
                            link_preview_options={"is_disabled": True},
                            **({"reply_markup": teacher_render.buttons(cid, bool(result.get("crop")))} if last else {}))["message_id"]
            ids.append(mid)
        for mid in ids:
            x.conn.execute("INSERT OR REPLACE INTO teacher_messages(message_id, critique_id, role) VALUES(?,?,?)", (mid, cid, "teacher"))
        scores = {a: (v["score"] if isinstance(v, dict) else v) for a, v in result["scores"].items()}
        x.conn.execute("UPDATE critiques SET folder=?, exif_json=?, measurements_json=?, result_json=?, scores_json=?, overall=?,"
                       " tags_json=?, strength_tags_json=?, session_id=?, model=?, cost_usd=?, used_fallback=?, status='done' WHERE id=?",
                       (str(folder), json.dumps(exif) if exif else None, json.dumps(m), json.dumps(result), json.dumps(scores), overall,
                        json.dumps(result.get("tags", [])), json.dumps(result.get("strength_tags", [])),
                        res.session_id if res else None, res.model if res else None, res.cost_usd if res else 0,
                        int(used_fallback), cid))
        if flags.get("assignment") and result.get("assignment_met"):
            today = x.lim.today()
            x.conn.execute("INSERT INTO assignments(date, text, done_at) VALUES(?,?,?) ON CONFLICT(date) DO UPDATE SET done_at=excluded.done_at",
                           (today, self.todays_assignment(), x.lim.clock()))
        try:
            from .progress import refresh_profile
            refresh_profile(x)
        except ImportError:
            pass
        vault.log_event("🎓", "critique", f"#{cid} overall {overall}" + (" fallback" if used_fallback else ""))
        return cid

    def send_photo(self, chat, path: Path, caption: str, reply_to=None) -> int:
        """sendPhoto with a local file through the telegram bucket (multipart, not JSON)."""
        x = self.x
        t = x.lim.acquire("telegram", target="sendPhoto")
        with open(path, "rb") as f:
            data = {"chat_id": str(chat), "caption": caption, "parse_mode": "HTML"}
            if reply_to:
                data["reply_to_message_id"] = str(reply_to)
            if x.tg.in_topic(chat):
                data["message_thread_id"] = str(x.tg.thread_id)
            r = x.tg.http.post("sendPhoto", data=data, files={"photo": ("overlay.jpg", f, "image/jpeg")})
        x.lim.report(t, "ok" if r.status_code < 400 else "soft_fail", status=r.status_code)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"sendPhoto: {body.get('description')}")
        return body["result"]["message_id"]

    # follow ups (teacher step 4) -----------------------------------------------------------------------------------
    def run_followup_job(self, job: dict):
        x = self.x
        p = json.loads(job["payload_json"])
        c = x.conn.execute("SELECT * FROM critiques WHERE id=?", (p["critique_id"],)).fetchone()
        if not c:
            return "failed"
        reason = teacher_queue.can_run(x, "followup")
        if reason:
            x.tg.send(p["chat_id"], f"Follow ups are used up for today ({reason}). Ask again tomorrow.", parse_mode=None)
            return "done"
        folder = Path(c["folder"])
        question = p["question"]
        stdin = {"question": question, "level": self.level(), "style": self.style(), "camera_facts": self.camera_facts(),
                 "menu_names": self.menu_names(), "earlier_critique": json.loads(c["result_json"] or "{}")}
        prompt = fill((self.root / "prompts" / "followup_brief.md").read_text(encoding="utf-8"), question=question)
        fresh = not c["session_id"] or (x.lim.clock() - c["created_at"]) > self.cfg.session_days * 86400
        try:
            try:
                if fresh:
                    raise ClaudeError("no session to resume")
                res = self.call("teacher_followup", prompt, stdin, schema="followup_schema.json", folder=folder, max_turns=4,
                                allowed="Read", disallowed=TOOLS_READ_ONLY, resume=c["session_id"])
            except ClaudeError as ex:
                if isinstance(ex, UsageLimit):
                    raise
                log.info("followup resume failed (%s), starting a fresh session", ex)
                res = self.call("teacher_followup", prompt, stdin, schema="followup_schema.json", folder=folder, max_turns=4,
                                allowed="Read", disallowed=TOOLS_READ_ONLY)
        except UsageLimit as ex:
            when = ex.resets_at or (datetime.now(x.lim.tz) + timedelta(hours=1))
            x.tg.send(p["chat_id"], f"The teacher is resting (Claude {ex.kind} limit). It will answer at about {when:%H:%M}.", parse_mode=None)
            return when.timestamp()
        except (ClaudeError, LimitError) as ex:
            x.tg.send(p["chat_id"], "The teacher could not answer that just now. Try again in a little while.", parse_mode=None)
            log.warning("followup failed: %s", ex)
            return "done"
        recipe_names = {r["name"] for r in x.conn.execute("SELECT name FROM recipes")}
        errs = guardrails.check_simple(res.data, facts_file=self.facts_file, menus_file=self.menus_file, recipe_names=recipe_names)
        answer = res.data.get("answer", "")
        if errs:
            log.warning("followup guardrails: %s", errs)
            answer = guardrails_strip(answer)
        text = teacher_render.esc(answer)
        if res.data.get("steps"):
            text += "\n" + teacher_render.numbered(res.data["steps"])
        settings = {k: v for k, v in (res.data.get("settings") or {}).items() if v}
        if settings and not any("setting" in e for e in errs):
            text += "\n" + teacher_render.settings_table(settings, json.loads(c["exif_json"] or "null"), bool(c["exif_json"]))
            how = teacher_render.how_to_set(settings, self.map_file)
            if how:
                text += f"\nHow to set it: {teacher_render.esc(how)}"
        mid = x.tg.call("sendMessage", chat_id=p["chat_id"], text=text, parse_mode="HTML", reply_to_message_id=p.get("reply_to"),
                        link_preview_options={"is_disabled": True})["message_id"]
        x.conn.execute("INSERT OR REPLACE INTO teacher_messages(message_id, critique_id, role) VALUES(?,?,?)", (mid, c["id"], "teacher"))
        x.conn.execute("INSERT INTO followups(critique_id, at, question, answer_json, cost_usd) VALUES(?,?,?,?,?)",
                       (c["id"], x.lim.clock(), question, json.dumps(res.data), res.cost_usd))
        if res.session_id and (fresh or not c["session_id"]):
            x.conn.execute("UPDATE critiques SET session_id=? WHERE id=?", (res.session_id, c["id"]))
        return "done"


def guardrails_strip(text: str) -> str:
    """Last resort cleanup for a follow up that tripped a text rule: remove links and dashes rather than drop it."""
    import re
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[‒–—―]| - |--", ", ", text)


# queue entry points ------------------------------------------------------------------------------------------------

def handle_job(x, job: dict):
    t = Teacher(x)
    kind = job["kind"]
    if kind == "critique":
        return t.run_critique_job(job)
    if kind == "followup":
        return t.run_followup_job(job)
    for name in ("compare", "series", "question", "plan", "crop"):
        if kind == name:
            fn = getattr(t, f"run_{name}_job", None)
            if fn:
                return fn(job)
            p = json.loads(job["payload_json"])
            x.tg.send(p["chat_id"], f"That part of the teacher ({name}) is not built yet. Single photo critiques and "
                                    f"follow up replies work now.", parse_mode=None)
            return "done"
    log.warning("unknown teacher job kind %s", kind)
    return "failed"


def drain(x, max_jobs: int = 5) -> int:
    return teacher_queue.drain(x, lambda job: handle_job(x, job), max_jobs=max_jobs)


def drain_job(s) -> None:
    from .cli import Ctx
    x = Ctx(s, "teacher")
    if x.tg:
        drain(x)
