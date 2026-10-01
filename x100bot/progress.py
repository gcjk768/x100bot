"""Progress: the learning profile, the weekly report with its chart, the monthly best of, and the /progress, /best,
/history, /forget and /forgetall commands. The app computes every number; Claude only writes the words."""
from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import guardrails, teacher_render, vault
from .claude import ClaudeError, UsageLimit
from .ratelimit import LimitError
from .telegram import esc

log = logging.getLogger(__name__)
AREAS = teacher_render.AREAS
SCORED = "kind IN ('single','compare') AND status='done' AND used_fallback=0 AND scores_json IS NOT NULL"


def _rows(x, since: float, until: float | None = None) -> list[dict]:
    q = f"SELECT * FROM critiques WHERE {SCORED} AND created_at>=?" + (" AND created_at<?" if until else "") + " ORDER BY created_at"
    return [dict(r) for r in x.conn.execute(q, (since, until) if until else (since,))]


def averages(rows: list[dict]) -> dict:
    out = {}
    for a in AREAS:
        vals = [json.loads(r["scores_json"]).get(a) for r in rows]
        vals = [v for v in vals if isinstance(v, (int, float))]
        out[a] = round(sum(vals) / len(vals), 2) if vals else None
    ov = [r["overall"] for r in rows if r["overall"] is not None]
    out["overall"] = round(sum(ov) / len(ov), 2) if ov else None
    return out


def learning_profile(x) -> dict:
    now = x.lim.clock()
    last30 = _rows(x, now - 30 * 86400)
    prev30 = _rows(x, now - 60 * 86400, now - 30 * 86400)
    last7 = [r for r in last30 if r["created_at"] >= now - 7 * 86400]
    tags = Counter(t for r in last30 for t in json.loads(r["tags_json"] or "[]"))
    strengths = Counter(t for r in last30 for t in json.loads(r["strength_tags_json"] or "[]"))
    a30, a7, aprev = averages(last30), averages(last7), averages(prev30)
    scored = {a: v for a, v in a30.items() if a in AREAS and v is not None}
    improved = None
    if scored and prev30:
        deltas = {a: a30[a] - aprev[a] for a in AREAS if a30.get(a) is not None and aprev.get(a) is not None}
        if deltas:
            improved = max(deltas, key=deltas.get)
    return {"critiques": len(last30), "critiques_7d": len(last7), "issue_counts": dict(tags.most_common()),
            "strength_counts": dict(strengths.most_common()), "avg_30d": a30, "avg_7d": a7,
            "weakest": min(scored, key=scored.get) if scored else None, "strongest": max(scored, key=scored.get) if scored else None,
            "most_improved": improved, "top_issues": [t for t, _ in tags.most_common(3)]}


def refresh_profile(x) -> dict:
    prof = learning_profile(x)
    x.conn.execute("INSERT OR REPLACE INTO profile_snapshots(date, profile_json) VALUES(?,?)", (x.lim.today(), json.dumps(prof)))
    return prof


# weekly report ------------------------------------------------------------------------------------------------------

def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def weekly_numbers(x, now: datetime) -> dict:
    start, end = week_bounds(now)
    this = _rows(x, start.timestamp(), end.timestamp())
    last = _rows(x, (start - timedelta(days=7)).timestamp(), start.timestamp())
    a_this, a_last = averages(this), averages(last)
    change = round(a_this["overall"] - a_last["overall"], 2) if a_this["overall"] is not None and a_last["overall"] is not None else None
    deltas = {a: round(a_this[a] - a_last[a], 2) for a in AREAS if a_this.get(a) is not None and a_last.get(a) is not None}
    tags = Counter(t for r in this for t in json.loads(r["tags_json"] or "[]"))
    best = max(this, key=lambda r: r["overall"] or 0) if this else None
    scored = {a: v for a, v in a_this.items() if a in AREAS and v is not None}
    weeks = []
    for i in range(7, -1, -1):
        ws = start - timedelta(days=7 * i)
        rows = _rows(x, ws.timestamp(), (ws + timedelta(days=7)).timestamp())
        weeks.append({"week_start": ws.date().isoformat(), "n": len(rows), **averages(rows)})
    return {"week_start": start.date().isoformat(), "critiques": len(this), "avg_this": a_this, "avg_last": a_last,
            "overall_change": change, "top_issues": [t for t, _ in tags.most_common(3)],
            "most_improved": max(deltas, key=deltas.get) if deltas else None,
            "strongest": max(scored, key=scored.get) if scored else None,
            "weakest": min(scored, key=scored.get) if scored else None,
            "best": {"id": best["id"], "overall": best["overall"], "folder": best["folder"]} if best else None, "weeks": weeks}


def chart(numbers: dict, out: Path) -> Path:
    """Weekly average per area for the last 8 weeks. Light background, large labels, readable on a phone."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    weeks = numbers["weeks"]
    labels = [w["week_start"][5:] for w in weeks]
    fig, ax = plt.subplots(figsize=(8, 4.8), dpi=150)
    fig.patch.set_facecolor("white")
    colours = {"composition": "#1f77b4", "light": "#ff7f0e", "exposure": "#2ca02c", "focus": "#d62728", "colour": "#9467bd", "moment": "#8c564b"}
    for a in AREAS:
        ys = [w[a] for w in weeks]
        ax.plot(labels, [y if y is not None else float("nan") for y in ys], marker="o", linewidth=2.2, label=a.capitalize(), color=colours[a])
    ov = [w["overall"] for w in weeks]
    ax.plot(labels, [y if y is not None else float("nan") for y in ov], marker="s", linewidth=3, color="black", label="Overall")
    ax.set_ylim(0.8, 5.2)
    ax.set_yticks([1, 2, 3, 4, 5])
    ax.set_ylabel("Average score", fontsize=12)
    ax.set_xlabel("Week starting", fontsize=12)
    ax.set_title("Your weekly averages, last 8 weeks", fontsize=14)
    ax.grid(True, alpha=0.3)
    ax.tick_params(labelsize=11)
    ax.legend(loc="lower left", fontsize=9, ncol=4, frameon=False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out


def setup_hint(s, tag: str | None) -> str:
    """The camera setup change for the most common issue, from library/hints.yaml and the menu map."""
    if not tag:
        return ""
    rules = yaml.safe_load((s.root / "library" / "hints.yaml").read_text(encoding="utf-8"))["rules"]
    mm = teacher_render.menu_map(str(s.path("camera/settings_menu_map.yaml")))
    for r in rules:
        if r["tag"] == tag:
            hows = [mm[k]["how"] for k in r.get("menu", []) if k in mm]
            return r["advice"] + (" " + " ".join(hows) if hows else "")
    return ""


def render_weekly(n: dict, words: dict | None, hint: str) -> str:
    a, b = n["avg_this"], n["avg_last"]
    fmt = lambda v: "none" if v is None else f"{v:.1f}"
    lines = [f"📈 <b>WEEKLY PROGRESS</b> · week of {n['week_start']}", "",
             f"Critiques this week: <code>{n['critiques']}</code>"]
    if a["overall"] is not None:
        ch = n["overall_change"]
        arrow = "" if ch is None else (f" 🟢 ▲{ch:+.1f}" if ch > 0 else f" 🔴 ▼{ch:.1f}" if ch < 0 else " ⚪ unchanged")
        lines.append(f"Overall average: <code>{a['overall']:.1f}</code>{arrow}")
        lines.append(" · ".join(f"{ar.capitalize()} {fmt(a[ar])}" for ar in AREAS))
    if n["top_issues"]:
        lines.append("Recurring issues: " + ", ".join(t.replace("_", " ") for t in n["top_issues"]))
    if n["most_improved"]:
        lines.append(f"Most improved: {n['most_improved']}")
    if n["strongest"]:
        lines.append(f"Strongest area: {n['strongest']}")
    if words:
        lines += ["━━━━━━━━━━━━━━━━", esc(words.get("summary", ""))]
        for label, key in (("Biggest improvement", "biggest_improvement"), ("Focus for next week", "focus_next_week"),
                           ("Exercise", "exercise")):
            if words.get(key):
                lines.append(f"<b>{label}</b>: {esc(words[key])}")
        if words.get("encouragement"):
            lines.append(f"<i>{esc(words['encouragement'])}</i>")
    if hint:
        lines.append(f"<b>One setup change</b>: {esc(hint)}")
    return "\n".join(lines)


def progress_words(x, teacher, numbers: dict, hint: str) -> dict | None:
    stdin = {"critiques_this_week": numbers["critiques"], "avg_this_week": numbers["avg_this"], "avg_last_week": numbers["avg_last"],
             "top_issues": numbers["top_issues"], "most_improved": numbers["most_improved"], "strongest": numbers["strongest"],
             "setup_hint": hint, "level": teacher.level(), "style": teacher.style()}
    try:
        res = teacher.call("teacher_question", "Write this week's progress note from the numbers in stdin. Return the progress object.",
                           stdin, schema="progress_schema.json", folder=teacher.data, max_turns=2, allowed=None,
                           disallowed="Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit")
    except (ClaudeError, UsageLimit, LimitError) as ex:
        log.warning("progress words skipped: %s", ex)
        return None
    errs = guardrails.check_simple(res.data, facts_file=teacher.facts_file, menus_file=teacher.menus_file, recipe_names=set())
    if errs:
        log.warning("progress guardrails: %s", errs)
        return None
    return res.data


def weekly_report(x, chat, now: datetime | None = None, with_words: bool = True) -> dict:
    from .teacher import Teacher
    t = Teacher(x)
    now = now or datetime.now(x.lim.tz)
    n = weekly_numbers(x, now)
    if n["critiques"] == 0 and all(w["n"] == 0 for w in n["weeks"]):
        x.tg.send(chat, "No critiques yet this week, so there is no progress report. Send a photo when you are out shooting.", parse_mode=None)
        return n
    hint = setup_hint(x.s, n["top_issues"][0] if n["top_issues"] else None)
    png = chart(n, Path(x.s.data_dir) / "teacher" / "reports" / f"week_{n['week_start']}.png")
    t.send_photo(chat, png, f"Weekly averages per area, 8 weeks to {n['week_start']}")
    words = progress_words(x, t, n, hint) if with_words else None
    x.tg.send(chat, render_weekly(n, words, hint))
    if n["best"] and n["best"]["folder"] and (Path(n["best"]["folder"]) / "overlay.jpg").exists():
        t.send_photo(chat, Path(n["best"]["folder"]) / "overlay.jpg", f"Best photo of the week, {n['best']['overall']}/5")
    vault.log_event("📈", "weekly report", f"{n['critiques']} critiques, overall {n['avg_this']['overall']}")
    return n


def weekly_report_job(s) -> None:
    from .cli import Ctx
    x = Ctx(s, "teacher-weekly")
    if x.tg and s.telegram.admin_chat_id:
        try:
            weekly_report(x, s.telegram.admin_chat_id)
        except Exception:
            log.exception("weekly report failed")
    cleanup_old(x)


def best_of(x, since: float, limit: int = 5) -> list[dict]:
    rows = [r for r in _rows(x, since) if r["folder"] and (Path(r["folder"]) / "overlay.jpg").exists()]
    rows.sort(key=lambda r: r["overall"] or 0, reverse=True)
    return rows[:limit]


def send_album(x, chat, rows: list[dict], caption_for) -> None:
    """sendMediaGroup with local files via attach:// (multipart), through the telegram bucket."""
    media, files = [], {}
    for i, r in enumerate(rows):
        files[f"p{i}"] = (f"p{i}.jpg", open(Path(r["folder"]) / "overlay.jpg", "rb"), "image/jpeg")
        media.append({"type": "photo", "media": f"attach://p{i}", "caption": caption_for(r), "parse_mode": "HTML"})
    t = x.lim.acquire("telegram", target="sendMediaGroup")
    try:
        data = {"chat_id": str(chat), "media": json.dumps(media)}
        if x.tg.in_topic(chat):
            data["message_thread_id"] = str(x.tg.thread_id)
        r = x.tg.http.post("sendMediaGroup", data=data, files=files)
    finally:
        for f in files.values():
            f[1].close()
    x.lim.report(t, "ok" if r.status_code < 400 else "soft_fail", status=r.status_code)
    if not r.json().get("ok"):
        raise RuntimeError(f"sendMediaGroup: {r.json().get('description')}")


def monthly_best(x, chat, now: datetime | None = None) -> int:
    now = now or datetime.now(x.lim.tz)
    first_prev = (now.replace(day=1) - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = [r for r in best_of(x, first_prev.timestamp(), 50) if r["created_at"] < now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()][:5]
    if not rows:
        return 0
    strengths = Counter(t for r in rows for t in json.loads(r["strength_tags_json"] or "[]"))
    common = ", ".join(t.replace("_", " ") for t, _ in strengths.most_common(2)) or "a clear idea"
    send_album(x, chat, rows, lambda r: f"{r['overall']}/5 · {esc(json.loads(r['result_json']).get('summary', ''))[:150]}")
    x.tg.send(chat, f"🏆 <b>BEST OF {first_prev:%B}</b> · what these five share: {esc(common)}")
    return len(rows)


def monthly_best_job(s) -> None:
    from .cli import Ctx
    x = Ctx(s, "teacher-monthly")
    if x.tg and s.telegram.admin_chat_id:
        try:
            monthly_best(x, s.telegram.admin_chat_id)
        except Exception:
            log.exception("monthly best failed")


def cleanup_old(x) -> int:
    """Delete originals and folders older than retention_days."""
    cutoff = x.lim.clock() - x.s.teacher.retention_days * 86400
    n = 0
    for r in x.conn.execute("SELECT id, folder FROM critiques WHERE created_at<? AND folder IS NOT NULL", (cutoff,)).fetchall():
        if r["folder"] and Path(r["folder"]).exists():
            shutil.rmtree(r["folder"], ignore_errors=True)
            n += 1
        x.conn.execute("UPDATE critiques SET folder=NULL WHERE id=?", (r["id"],))
    return n


# forgetting -------------------------------------------------------------------------------------------------------

def forget(x, critique_id: int) -> bool:
    row = x.conn.execute("SELECT * FROM critiques WHERE id=?", (critique_id,)).fetchone()
    if not row:
        return False
    if row["folder"] and Path(row["folder"]).exists():
        shutil.rmtree(row["folder"], ignore_errors=True)
    if row["session_id"]:
        home = Path.home() / ".claude"
        for f in home.rglob(f"{row['session_id']}*"):
            try:
                f.unlink()
            except OSError:
                pass
    for table in ("followups", "teacher_messages"):
        x.conn.execute(f"DELETE FROM {table} WHERE critique_id=?", (critique_id,))
    x.conn.execute("DELETE FROM critiques WHERE id=? OR parent_id=?", (critique_id, critique_id))
    return True


def forget_all(x) -> int:
    ids = [r["id"] for r in x.conn.execute("SELECT id FROM critiques")]
    for i in ids:
        forget(x, i)
    x.conn.execute("DELETE FROM teacher_queue")
    x.conn.execute("DELETE FROM profile_snapshots")
    shutil.rmtree(Path(x.s.data_dir) / "teacher", ignore_errors=True)
    return len(ids)


# commands ----------------------------------------------------------------------------------------------------------

def command(x, cmd: str, arg: str, m: dict, intake) -> None:
    chat = m["chat"]["id"]
    if cmd == "/progress":
        prof = learning_profile(x)
        if not prof["critiques"]:
            return intake.reply(chat, "No scored critiques in the last 30 days yet.")
        fmt = lambda v: "none" if v is None else f"{v:.1f}"
        a7, a30 = prof["avg_7d"], prof["avg_30d"]
        lines = ["📈 <b>PROGRESS</b> · last 7 and 30 days", "",
                 f"Critiques: <code>{prof['critiques_7d']}</code> this week · <code>{prof['critiques']}</code> in 30 days",
                 f"Overall: <code>{fmt(a7['overall'])}</code> (7 d) · <code>{fmt(a30['overall'])}</code> (30 d)"]
        lines += [f"{a.capitalize()}: {fmt(a7[a])} · {fmt(a30[a])}" for a in AREAS]
        if prof["top_issues"]:
            lines.append("Recurring: " + ", ".join(f"{t.replace('_', ' ')} ({prof['issue_counts'][t]})" for t in prof["top_issues"]))
        if prof["weakest"]:
            lines.append(f"Weakest: {prof['weakest']} · Strongest: {prof['strongest']}" + (f" · Most improved: {prof['most_improved']}" if prof["most_improved"] else ""))
        intake.reply(chat, "\n".join(lines))
    elif cmd == "/best":
        rows = best_of(x, x.lim.clock() - 30 * 86400)
        if not rows:
            return intake.reply(chat, "No scored photos in the last 30 days yet.")
        send_album(x, chat, rows, lambda r: f"{r['overall']}/5 · {esc(json.loads(r['result_json']).get('summary', ''))[:150]}")
    elif cmd == "/history":
        rows = x.conn.execute("SELECT id, created_at, kind, overall, result_json, status FROM critiques ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            return intake.reply(chat, "No critiques yet.")
        lines = ["🗂 <b>HISTORY</b> · last 10", ""]
        for r in rows:
            when = datetime.fromtimestamp(r["created_at"], x.lim.tz).strftime("%d %b %H:%M")
            summary = json.loads(r["result_json"] or "{}").get("summary") or json.loads(r["result_json"] or "{}").get("verdict") or r["kind"]
            score = f"{r['overall']}/5" if r["overall"] else r["status"]
            lines.append(f"#{r['id']} · {when} · {r['kind']} · {score} · {esc(str(summary)[:70])}")
        lines.append("<i>/forget 12 deletes a critique, or reply /forget to one</i>")
        intake.reply(chat, "\n".join(lines))
    elif cmd == "/forget":
        cid = None
        if arg.strip().isdigit():
            cid = int(arg.strip())
        else:
            cid = intake.critique_for_message((m.get("reply_to_message") or {}).get("message_id"))
        if not cid:
            return intake.reply(chat, "Reply /forget to a critique, or give its number from /history.")
        intake.reply(chat, f"Forgotten critique #{cid}: files, rows and the Claude session are gone." if forget(x, cid)
                     else f"There is no critique #{cid}.")
    elif cmd == "/forgetall":
        if arg.strip() == "confirm":
            n = forget_all(x)
            return intake.reply(chat, f"Deleted everything the teacher stored ({n} critiques).")
        x.tg.call("sendMessage", chat_id=chat, text="Delete every critique, photo, follow up and session the teacher has stored? This cannot be undone.",
                  reply_markup={"inline_keyboard": [[{"text": "Yes, delete everything", "callback_data": "t:fa:0"},
                                                      {"text": "Cancel", "callback_data": "t:fx:0"}]]})
