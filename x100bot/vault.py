"""Obsidian vault (VAULT_DIR): the bot's movement log and long-term memory, per the NAS vault standard.

Layout under VAULT_DIR (NAS: /volume1/James/Obsidian/x100bot):
* ``Activity/YYYY/MM/YYYY-MM-DD.md``: one line per event, ``- HH:MM emoji **what** · detail · [[entity]]`` (SGT)
* ``Recipes/<name>.md`` and ``Lessons/<title>.md``: one note per posted item, append-only ``## History``
* ``Home.md``: what this is, the current month folder, the latest day notes

Memory: ``recent`` returns the newest Activity lines (capped) so the daily compose call never repeats a recipe or
lesson. Best effort throughout: any error is logged and ignored, the vault never breaks a run or loses a post.
Never writes secrets. An older flat ``Activity/YYYY-MM-DD.md`` is moved into ``YYYY/MM/`` by ``migrate`` (never deleted).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Singapore")
MEMORY_CHARS = 4000
MEMORY_DAYS = 30
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_DAY_NOTE = re.compile(r"^(\d{4})-(\d\d)-\d\d\.md$")
_TAG = re.compile(r"<[^>]+>")
_LINE = re.compile(r"^- (\d\d:\d\d) \S+ \*\*(.+?)\*\*(?: · (.*))?$")


def root() -> Path | None:
    r = os.environ.get("VAULT_DIR")
    return Path(r) if r else None


def note_name(title: str) -> str:
    """A filename and wikilink safe note title."""
    return re.sub(r"\s+", " ", _BAD_NAME.sub(" ", str(title))).strip(" .")[:120] or "Untitled"


def _field(text) -> str:
    return " ".join(str(text).split()).replace("·", "-")


def _day_path(r: Path, day: date) -> Path:
    return r / "Activity" / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.md"


def _write(path: Path, text: str) -> None:
    """Atomic (tmp + rename); files stay editable by James (uid 1000 runs the container, 664 for the group)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass


def migrate() -> None:
    """Moves flat Activity/YYYY-MM-DD.md notes into Activity/YYYY/MM/ (never deletes). Call once at start."""
    r = root()
    if r is None or not (r / "Activity").is_dir():
        return
    try:
        for old in sorted((r / "Activity").glob("*.md")):
            m = _DAY_NOTE.match(old.name)
            if not m:
                continue
            new = r / "Activity" / m[1] / m[2] / old.name
            if not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, new)
                log.info("vault: moved %s to Activity/%s/%s", old.name, m[1], m[2])
    except Exception as ex:   # noqa: BLE001
        log.warning("vault migration stopped: %s", ex)


def log_event(emoji: str, what: str, detail: str = "", entity: str | None = None) -> None:
    """Appends one line to today's Activity note."""
    r = root()
    if r is None:
        return
    try:
        now = datetime.now(TZ)
        day = _day_path(r, now.date())
        day.parent.mkdir(parents=True, exist_ok=True)
        if not day.exists():
            day.write_text(f"---\ntags: [log]\nupdated: {now:%Y-%m-%d}\n---\n# {now:%a %d %b %Y}\n\n", encoding="utf-8")
            os.chmod(day, 0o664)
        line = f"- {now:%H:%M} {emoji} **{_field(what)}**" + (f" · {_field(detail)}" if detail else "") + \
            (f" · [[{note_name(entity)}]]" if entity else "")
        with day.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as ex:   # noqa: BLE001
        log.warning("vault write skipped: %s", ex)


def entity_of(row) -> tuple[str, str]:
    """(folder, note name) for a queue row: recipes by recipe name, everything else by its title or topic."""
    facts = json.loads(row["facts_json"] or "{}")
    fields = json.loads(row["fields_json"] or "{}")
    if row["type"] == "recipe":
        return "Recipes", note_name(facts.get("recipe_name") or fields.get("title") or "recipe")
    title = fields.get("title") or fields.get("headline") or facts.get("topic") or facts.get("name") or \
        facts.get("title") or f"{row['type']} {row['slot_at']}"
    return "Lessons", note_name(f"{title}")


def posted(row, message_id) -> None:
    """A queued slot went out: write or update its note (append-only History), log it, refresh Home."""
    r = root()
    if r is None:
        return
    folder, name = "Lessons", str(row["type"])
    try:
        folder, name = entity_of(row)
        now = datetime.now(TZ)
        path = r / folder / f"{name}.md"
        history: list[str] = []
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if "## History\n" in old:
                history = [ln for ln in old.split("## History\n", 1)[1].splitlines() if ln.strip()]
        history.append(f"- {now:%Y-%m-%d %H:%M} posted as {row['type']} at {row['slot_at']} (message {message_id})")
        facts = json.loads(row["facts_json"] or "{}")
        front = ["---", "tags: [posted]", f"updated: {now:%Y-%m-%d}", f"type: {row['type']}",
                 f"title: {json.dumps(name, ensure_ascii=False)}", f"posted: {now:%Y-%m-%d}"]
        if row["type"] == "recipe":
            front += [f"author: {json.dumps(facts.get('author') or '', ensure_ascii=False)}",
                      f"film_simulation: {json.dumps(facts.get('film_simulation') or '', ensure_ascii=False)}",
                      f"source: {json.dumps(facts.get('source_url') or '')}"]
        body = [f"# {name}", "", f"- **Type:** {row['type']} · **Posted:** {now:%Y-%m-%d %H:%M} SGT · [[{now:%Y-%m-%d}]]",
                "", "## Post", html.unescape(_TAG.sub("", row["text_html"] or "")).strip(), "", "## History", *history]
        _write(path, "\n".join(front + ["---"] + body) + "\n")
    except Exception as ex:   # noqa: BLE001
        log.warning("vault note skipped: %s", ex)
    log_event("📨", f"posted {row['type']}", f"{row['slot_at']} · {name} · message {message_id}", name)
    write_home()


def write_home() -> None:
    r = root()
    if r is None:
        return
    try:
        now = datetime.now(TZ)
        latest = [d for d in (now.date() - timedelta(days=i) for i in range(60)) if _day_path(r, d).is_file()][:7]
        lines = ["---", "tags: [active]", f"updated: {now:%Y-%m-%d}", "---", "# x100bot", "",
                 "Written by the X100VI learning bot (@jameskoh_x100vi_bot, James Channel topic 396). "
                 "`Activity/YYYY/MM/` holds one note per day (the movement log), `Recipes/` one note per posted film "
                 "simulation recipe, `Lessons/` one note per posted lesson, tip or brief.", "",
                 f"- **This month:** `Activity/{now:%Y/%m}/` · today [[{now:%Y-%m-%d}]]",
                 "- **Latest notes:** " + (", ".join(f"[[{d.isoformat()}]]" for d in latest) or "-"), ""]
        for folder in ("Recipes", "Lessons"):
            notes = sorted((r / folder).glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:10] \
                if (r / folder).is_dir() else []
            lines += [f"## Latest {folder.lower()}"] + [f"- [[{p.stem}]]" for p in notes] + [""]
        _write(r / "Home.md", "\n".join(lines))
    except Exception as ex:   # noqa: BLE001
        log.warning("vault home skipped: %s", ex)


def recent(max_chars: int = MEMORY_CHARS, days: int = MEMORY_DAYS) -> str:
    """'YYYY-MM-DD HH:MM what · detail' lines from the Activity notes, newest first, at most max_chars."""
    r = root()
    if r is None:
        return ""
    try:
        out: list[str] = []
        size = 0
        today = datetime.now(TZ).date()
        for back in range(days):
            day = today - timedelta(days=back)
            path = _day_path(r, day)
            if not path.is_file():
                path = r / "Activity" / f"{day.isoformat()}.md"   # flat note the migration could not move
                if not path.is_file():
                    continue
            for ln in reversed(path.read_text(encoding="utf-8").splitlines()):
                m = _LINE.match(ln)
                if not m:
                    continue
                line = f"{day.isoformat()} {m[1]} {m[2]}" + (f" · {m[3]}" if m[3] else "")
                if size + len(line) + 1 > max_chars:
                    return "\n".join(out)
                out.append(line)
                size += len(line) + 1
        return "\n".join(out)
    except Exception as ex:   # noqa: BLE001
        log.warning("vault read skipped: %s", ex)
        return ""


MEMORY_HEADER = ("\n\nWhat you already posted and learned (from the vault, newest first). Never repeat a recipe, "
                 "lesson, tip or drill listed here; pick something new or a clearly different angle:\n")


def memory_block() -> str:
    """The memory excerpt for a prompt, or '' when the vault is off or empty."""
    text = recent()
    return MEMORY_HEADER + text if text else ""
