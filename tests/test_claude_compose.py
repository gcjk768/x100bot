import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.conftest import FIX, SGT
from x100bot import compose
from x100bot.claude import Claude, ClaudeError, UsageLimit, parse_reset

ROOT = FIX.parent.parent
SCHEMA = json.loads((ROOT / "prompts" / "compose_schema.json").read_text(encoding="utf-8"))


# claude result parsing ---------------------------------------------------------------------------------------------

class FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def claude(settings, make_limiter, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return claude.script.pop(0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    claude = Claude(settings, make_limiter(), SGT)
    claude.script, claude.calls = [], calls
    return claude


def ok_result(data):
    return FakeProc(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                                "structured_output": data, "session_id": "s1", "total_cost_usd": 0.12}))


def test_success_result_is_parsed(claude):
    claude.script = [ok_result({"slots": [], "run_note": "x"})]
    r = claude.run("claude", "brief", {"a": 1}, schema=ROOT / "prompts/compose_schema.json", timeout=10)
    assert r.data == {"slots": [], "run_note": "x"} and r.session_id == "s1" and r.cost_usd == 0.12
    argv = claude.calls[0]
    assert "--no-session-persistence" in argv and "--bare" not in argv   # oauth: never bare
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--fallback-model") + 1] == "haiku"


def test_session_limit_is_detected_with_reset_time(claude):
    claude.script = [FakeProc(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                          "result": "You've hit your session limit · resets 3:45pm"}))]
    with pytest.raises(UsageLimit) as ex:
        claude.run("claude", "b", {}, schema=ROOT / "prompts/compose_schema.json", timeout=10)
    assert ex.value.kind == "session" and not ex.value.retry_with_fallback
    assert ex.value.resets_at.strftime("%H:%M") == "15:45"


def test_sonnet_limit_retries_with_fallback_model(claude):
    claude.script = [FakeProc(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                                          "result": "You've hit your Sonnet limit"})),
                     ok_result({"slots": [], "run_note": ""})]
    r = claude.run_with_retry("claude", "b", {}, schema=ROOT / "prompts/compose_schema.json", timeout=10)
    assert r is not None
    assert claude.calls[1][claude.calls[1].index("--model") + 1] == "haiku"


def test_weekly_limit_gives_up_and_alerts(claude):
    claude.script = [FakeProc(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                                          "result": "You've hit your weekly limit · resets 9am"}))]
    alerts = []
    r = claude.run_with_retry("claude", "b", {}, alert=alerts.append, schema=ROOT / "prompts/compose_schema.json",
                              timeout=10)
    assert r is None and len(claude.calls) == 1 and "09:00" in alerts[0]


def test_timeout_then_fallback(claude, clock):
    def boom(argv, **kw):
        claude.calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 10)
    subprocess.run = boom
    alerts = []
    r = claude.run_with_retry("claude", "b", {}, alert=alerts.append, schema=ROOT / "prompts/compose_schema.json",
                              timeout=10)
    assert r is None and len(claude.calls) == 2 and 60 in clock.sleeps and alerts


def test_other_failure_retries_once(claude):
    claude.script = [FakeProc("", returncode=1, stderr="boom"), ok_result({"slots": [], "run_note": ""})]
    assert claude.run_with_retry("claude", "b", {}, schema=ROOT / "prompts/compose_schema.json", timeout=10)
    assert len(claude.calls) == 2


def test_parse_reset_variants():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=SGT)
    assert parse_reset("resets 3:45pm", now).hour == 15
    assert parse_reset("resets at 9am", now).day == 7   # already past today, so tomorrow
    assert parse_reset("no time here", now) is None


# compose guardrails ------------------------------------------------------------------------------------------------

def caps(settings):
    return compose.allowed_caps(str(settings.path(settings.camera.menus_file)), str(settings.path(settings.camera.facts_file)))


def slots():
    return [{"slot_id": "08", "type": "recipe", "facts": {"recipe_name": "Easy Reala Ace"}},
            {"slot_id": "12", "type": "photographer", "facts": {"name": "Saul Leiter"}},
            {"slot_id": "21", "type": "learning_tip", "facts": {"topic": "culling"}}]


def clean_output():
    return {"run_note": "", "slots": [
        {"slot_id": "08", "type": "recipe", "fields": {"best_for": "soft morning light on faces",
         "why": "Reala Ace keeps colours faithful with a touch of contrast. The look stays calm in mixed light.",
         "try_today": "Shoot the void deck at nine while the light is still soft."}},
        {"slot_id": "12", "type": "photographer", "fields": {"what_to_study": "Saul Leiter framed through windows and rain. He let colour carry the mood.",
         "moves": ["shoot through glass", "let a colour lead"], "try_it": "Stand under a shelter at Haji Lane and shoot through the rain on the glass with the EVF."}},
        {"slot_id": "21", "type": "learning_tip", "fields": {"title": "Cull in two passes", "body": "Flag quickly first. Then choose slowly.", "action": "Cull today down to five frames."}},
    ]}


def run(settings, output):
    return compose.validate(slots(), output, caps(settings), SCHEMA)


def test_clean_output_passes(settings):
    ok, bad = run(settings, clean_output())
    assert not bad and set(ok) == {"08", "12", "21"}


@pytest.mark.parametrize("slot_id,field,value,rule", [
    ("21", "body", "Read more at https://example.com today.", "link"),
    ("21", "body", "Flag first — then choose.", "dash"),
    ("21", "body", "Flag first - then choose.", "dash"),
    ("08", "why", "Set DR400 and you are done.", "restates a setting"),
    ("08", "why", "Use 5500K for warmth.", "restates a setting"),
    ("08", "why", "Push it to +2 for punch.", "restates a setting"),
    ("21", "body", "Open the SUPER SECRET MENU and turn it on.", "unknown capitalised word"),
    ("21", "body", "This also works on the X100V and X-T5.", "another camera"),
    ("12", "what_to_study", "Paul Leiter framed through windows. He waited for colour.", "wrong photographer name"),
])
def test_each_rule_fails(settings, slot_id, field, value, rule):
    out = clean_output()
    for sl in out["slots"]:
        if sl["slot_id"] == slot_id:
            sl["fields"][field] = value
    ok, bad = run(settings, out)
    assert slot_id in bad and any(rule in e for e in bad[slot_id]), bad


def test_missing_slot_fails(settings):
    out = clean_output()
    out["slots"] = out["slots"][:2]
    ok, bad = run(settings, out)
    assert bad == {"21": ["slot missing from the output"]}


def test_menu_names_and_short_forms_are_allowed(settings):
    out = clean_output()
    out["slots"][2]["fields"]["body"] = "Set FRAMING GUIDELINE to GRID 9, use the OVF, keep ISO on AUTO and IBIS on."
    ok, bad = run(settings, out)
    assert not bad, bad


def test_over_length_fails(settings):
    out = clean_output()
    out["slots"][2]["fields"]["title"] = "x" * 91
    ok, bad = run(settings, out)
    assert "21" in bad and "over 90" in bad["21"][0]
