"""The daily compose call: one claude -p for all of today's slots, then deterministic guardrails per slot.
A slot that fails after one retry falls back to the facts only template (planner.fallback_fields)."""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .claude import Claude, fill

log = logging.getLogger(__name__)

REQUIRED = {
    "brief": ["headline", "assignment", "why_today"],
    "recipe": ["best_for", "why", "try_today"],
    "composition": ["title", "what_it_is", "how_to_see", "camera_how", "exercise", "mistake"],
    "photographer": ["what_to_study", "moves", "try_it"],
    "camera_tip": ["title", "why", "how_detail", "when"],
    "video_lesson": ["watch_for", "then_try"],
    "drill": ["title", "steps", "settings_hint"],
    "golden_hour": ["plan", "look_for", "setup"],
    "learning_tip": ["title", "body", "action"],
    "review": ["headline", "q1", "q2", "q3", "culling_tip"],
    "weekly_recap": ["recap", "next_week_hint"],
}
SHORT_FORMS = {"ISO", "OVF", "EVF", "ND", "IBIS", "AF", "MF", "JPEG", "HEIF", "RAW", "SGT", "NAS", "HDB", "MRT",
               "EV", "X100VI", "LCD", "USB", "SD", "OK"}
OTHER_CAMERAS = re.compile(r"\b(X100[VFTS]\b|X100VI(?!\b)|X-?T\d+\b|X-?Pro\d\b|X-?E\d\b|X-?H\d\b|X-?S\d+\b|X-?M\d\b|"
                           r"GFX\s?\d+|Leica|Ricoh|GR III|Sony|Canon|Nikon)", re.I)
URL_RE = re.compile(r"https?://|www\.|\.com\b|\.org\b", re.I)
DASH_RE = re.compile(r"[‒–—―]| - |--|^-|\s-\s")
SETTING_RE = re.compile(r"\bDR\s?-?\d|\b\d{3,5}\s?K\b|[+\-−]\s?\d")
CAPS_RE = re.compile(r"\b[A-Z][A-Z0-9./+()\-]{1,}\b")


@lru_cache
def allowed_caps(menus_file: str, facts_file: str) -> set[str]:
    """Every capitalised word in the manual's menu names and the camera facts, plus the allowed short forms."""
    words = set(SHORT_FORMS)
    menus = yaml.safe_load(Path(menus_file).read_text(encoding="utf-8"))
    for item in menus["items"]:
        words.update(w.strip("().,") for w in item["name"].split())
    words.update(menus.get("option_words", []))
    facts = Path(facts_file).read_text(encoding="utf-8")
    words.update(m.group(0).strip("().,") for m in CAPS_RE.finditer(facts))
    return {w for w in words if w}


def texts_of(fields: dict):
    for k, v in fields.items():
        if isinstance(v, str):
            yield k, v
        elif isinstance(v, list):
            for i, x in enumerate(v):
                if isinstance(x, str):
                    yield f"{k}[{i}]", x


def check_slot(slot: dict, out: dict, caps: set[str], schema_props: dict) -> list[str]:
    """Problems with one slot's output, empty when it passes."""
    errs = []
    fields = out.get("fields") or {}
    typ = slot["type"]
    if out.get("type") != typ:
        errs.append(f"type {out.get('type')} does not match slot type {typ}")
    for k in REQUIRED.get(typ, []):
        v = fields.get(k)
        if v in (None, "", []):
            errs.append(f"missing {k}")
    for k, text in texts_of(fields):
        base = k.split("[")[0]
        limit = (schema_props.get(base) or {}).get("maxLength") or (schema_props.get(base) or {}).get("items", {}).get("maxLength")
        if limit and len(text) > limit:
            errs.append(f"{k} is {len(text)} characters, over {limit}")
        if URL_RE.search(text):
            errs.append(f"{k} contains a link")
        if DASH_RE.search(text):
            errs.append(f"{k} contains a dash")
        if typ == "recipe" and SETTING_RE.search(text):
            errs.append(f"{k} restates a setting value")
        if OTHER_CAMERAS.search(text):
            errs.append(f"{k} names another camera")
        for m in CAPS_RE.finditer(text):
            w = m.group(0).strip("().,")
            if len(w) >= 2 and w not in caps and not w.isdigit():
                errs.append(f"{k} has an unknown capitalised word {w}")
    if typ == "photographer":
        name = slot["facts"].get("name", "")
        for k, text in texts_of(fields):
            for cand in re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+(?: [A-Z][a-z]+)?)\b", text):
                if cand != name and cand.split()[0] not in text.replace(cand, "") and cand.split()[-1] == name.split()[-1]:
                    errs.append(f"{k} uses a wrong photographer name {cand}")
    return errs


def validate(slots: list[dict], result: dict, caps: set[str], schema: dict) -> tuple[dict, dict]:
    """Returns ({hour: fields} for slots that pass, {hour: [errors]} for slots that fail)."""
    props = schema["properties"]["slots"]["items"]["properties"]["fields"]["properties"]
    by_id = {o.get("slot_id"): o for o in result.get("slots", [])}
    ok, bad = {}, {}
    for sl in slots:
        out = by_id.get(sl["slot_id"])
        if out is None:
            bad[sl["slot_id"]] = ["slot missing from the output"]
            continue
        errs = check_slot(sl, out, caps, props)
        (bad if errs else ok)[sl["slot_id"]] = errs or out["fields"]
    extra = set(by_id) - {sl["slot_id"] for sl in slots}
    for e in extra:
        bad[e] = ["unknown slot id in the output"]
    return ok, bad


def slot_inputs(d) -> list[dict]:
    """The slot facts Claude may use. Recipe settings go without the raw block."""
    out = []
    for sl in d.slots:
        facts = {k: v for k, v in sl.facts.items() if k not in ("bank_note",)}
        if "settings" in facts:
            facts["settings"] = {k: v for k, v in facts["settings"].items() if k != "raw"}
        out.append({"slot_id": sl.hour, "type": sl.type, "facts": facts})
    return out


def compose_day(x, d) -> dict:
    """{hour: fields} for every slot the guardrails accepted. Missing hours use the fallback template."""
    s = x.s
    root = s.root
    schema_path = root / "prompts" / "compose_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    caps = allowed_caps(str(s.path(s.camera.menus_file)), str(s.path(s.camera.facts_file)))
    facts = yaml.safe_load(s.path(s.camera.facts_file).read_text(encoding="utf-8"))
    slots = slot_inputs(d)
    stdin = {"date": d.date.isoformat(), "light": d.light.as_text(), "weather": d.periods,
             "week": {"number": d.week["week"], "theme": d.week["theme"], "cycle": d.cycle}, "level": s.learning.level,
             "camera_facts": [f["text"] for f in facts["facts"]] + ["Film simulations: " + ", ".join(
                 facts["film_simulations"]["color"] + facts["film_simulations"]["monochrome"])],
             "slots": slots}
    brief = fill((root / "prompts" / "compose_brief.md").read_text(encoding="utf-8"), date=d.date.isoformat(),
                 week_number=d.week["week"], theme=d.week["theme"], cycle=d.cycle, level=s.learning.level,
                 slot_count=len(slots))
    claude = Claude(s, x.lim, x.lim.tz)
    kw = dict(schema=schema_path, timeout=s.claude.compose.timeout_seconds,
              system_file=root / "prompts" / "compose_system.md", disallowed_tools=",".join(s.claude.no_tools),
              max_turns=s.claude.compose.max_turns)
    res = claude.run_with_retry("claude", brief, stdin, alert=lambda t: x.alert("compose_fallback", t), **kw)
    if res is None:
        return {}
    ok, bad = validate(slots, res.data, caps, schema)
    x.conn.execute("INSERT INTO runs(job, started_at, finished_at, status, cost_usd, claude_session_id, note) "
                   "VALUES('compose', ?, ?, ?, ?, ?, ?)", (x.lim.clock(), x.lim.clock(), "ok" if not bad else "partial",
                                                           res.cost_usd, res.session_id, json.dumps(bad)[:2000]))
    if bad:
        log.warning("compose guardrails failed for %s, retrying once with the errors", sorted(bad))
        retry_prompt = brief + "\n\nYour previous reply had these problems; fix them and return all slots again:\n" + \
            "\n".join(f"{k}: {'; '.join(v)}" for k, v in bad.items())
        try:
            res2 = claude.run("claude", retry_prompt, stdin, **kw)
            ok2, bad2 = validate(slots, res2.data, caps, schema)
            for k in bad:   # keep the first pass where it was fine, take the retry where it now passes
                if k in ok2:
                    ok[k] = ok2[k]
            bad = {k: v for k, v in bad2.items() if k not in ok}
        except Exception as ex:   # noqa: BLE001 - the retry is best effort, the fallback covers the rest
            log.warning("compose retry failed: %s", ex)
        if bad:
            x.alert("compose_fallback", f"compose guardrails failed for slots {', '.join(sorted(bad))}; fallback "
                                        f"templates used there")
    return ok


def run_eval(s) -> bool:
    """Run the guardrails on tests/fixtures/compose_golden/*.json and print pass or fail per rule."""
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    schema = json.loads((s.root / "prompts" / "compose_schema.json").read_text(encoding="utf-8"))
    caps = allowed_caps(str(s.path(s.camera.menus_file)), str(s.path(s.camera.facts_file)))
    all_ok = True
    for f in sorted((s.root / "tests" / "fixtures" / "compose_golden").glob("*.json")):
        case = json.loads(f.read_text(encoding="utf-8"))
        ok, bad = validate(case["slots"], case["output"], caps, schema)
        failed = bool(bad)
        passed = failed == (case["expect"] == "fail")
        all_ok &= passed
        detail = "; ".join(f"{k}: {v[0]}" for k, v in bad.items())
        print(f"{'PASS' if passed else 'FAIL'}  {case['rule']:40} {detail[:100]}")
    return all_ok
