"""The research call: at most once a day, only when a library type is below min_stock or untagged recipes exist.
Everything Claude returns is verified deterministically before it enters the library."""
from __future__ import annotations

import json
import logging
import re

import yaml

from .claude import Claude, fill
from .library import people as people_lib
from .library import recipes as recipes_lib
from .library import tips as tips_lib
from .library.recipes import LIGHT_TAGS
from .ratelimit import LimitError
from .web import FetchError

log = logging.getLogger(__name__)


def stock(conn) -> dict:
    return {"recipe": conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0],
            "photographer": conn.execute("SELECT COUNT(*) FROM people WHERE verified_at IS NOT NULL").fetchone()[0],
            "camera_tip": conn.execute("SELECT COUNT(*) FROM tips WHERE verified_at IS NOT NULL").fetchone()[0],
            "video": conn.execute("SELECT COUNT(*) FROM videos WHERE verified_at IS NOT NULL").fetchone()[0]}


def untagged(conn) -> list[dict]:
    rows = conn.execute("SELECT name, film_simulation, author, source_url, light_tags_json FROM recipes").fetchall()
    return [{"name": r["name"], "film_simulation": r["film_simulation"], "author": r["author"], "source_url": r["source_url"]}
            for r in rows if json.loads(r["light_tags_json"] or '["any"]') == ["any"]]


def needed(s, conn) -> dict:
    st = stock(conn)
    return {k: max(0, v - st.get(k, 0)) for k, v in s.library.min_stock.items() if st.get(k, 0) < v}


def should_run(s, conn) -> bool:
    return bool(needed(s, conn)) or bool(untagged(conn))


def apply(x, data: dict) -> dict:
    """Verify and store what research returned. Anything that fails is dropped and logged."""
    s = x.s
    facts_file = str(s.path(s.camera.facts_file))
    out = {"photographers": 0, "recipes": 0, "tips": 0, "tags": 0, "dropped": []}
    for p in data.get("photographers", []):
        pid = people_lib.store(x, p)
        out["photographers" if pid else "dropped"] = out["photographers"] + 1 if pid else out["dropped"] + [f"photographer {p.get('name')}"]
    for r in data.get("recipes", []):
        try:
            page = x.web.get(r["source_url"], refresh="never")
        except (LimitError, FetchError) as ex:
            out["dropped"].append(f"recipe {r.get('name')}: {ex}")
            continue
        text = page.text.lower()
        values = [str(v).lower() for v in r.get("settings", {}).values()]
        if page.status >= 400 or r["film_simulation"].lower() not in text or sum(v in text for v in values) < 5:
            out["dropped"].append(f"recipe {r.get('name')}: page does not show the settings")
            continue
        sensor = r.get("sensor_generation", "other")
        if sensor not in ("X-Trans V", "X-Trans IV"):
            out["dropped"].append(f"recipe {r.get('name')}: sensor {sensor}")
            continue
        rec = recipes_lib.build(r["name"], r["author"], r["source_url"], sensor, r["settings"], facts_file)
        rec.light_tags = [t for t in r.get("light_tags", []) if t in LIGHT_TAGS] or ["any"]
        if recipes_lib.store(x.conn, rec, x.lim.clock()):
            out["recipes"] += 1
        else:
            out["dropped"].append(f"recipe {r.get('name')}: {'; '.join(rec.errors) or 'duplicate'}")
    for t in data.get("tips", []):
        t = dict(t)
        t.setdefault("menu_item", t["menu_path"].split(">")[-1].strip())
        if tips_lib.store(x, t):
            out["tips"] += 1
        else:
            out["dropped"].append(f"tip {t.get('title')}")
    for tag in data.get("recipe_tags", []):
        tags = [t for t in tag.get("light_tags", []) if t in LIGHT_TAGS]
        if tags:
            cur = x.conn.execute("UPDATE recipes SET light_tags_json=? WHERE source_url=?",
                                 (json.dumps(tags), tag.get("source_url")))
            out["tags"] += cur.rowcount
    return out


def run_research(x) -> dict | None:
    s = x.s
    if not should_run(s, x.conn):
        return None
    root = s.root
    weeks = yaml.safe_load(s.path(s.learning.curriculum_file).read_text(encoding="utf-8"))["weeks"]
    known = {"photographers": [r[0] for r in x.conn.execute("SELECT name FROM people")],
             "recipes": [r[0] for r in x.conn.execute("SELECT source_url FROM recipes")],
             "tips": [r[0] for r in x.conn.execute("SELECT title FROM tips")]}
    channels = [c["name"] if isinstance(c, dict) else c for c in s.sources.youtube_channels]
    need = needed(s, x.conn)
    stdin = {"needed": need, "themes": [w["theme"] for w in weeks], "known": known, "allowed_channels": channels,
             "untagged_recipes": untagged(x.conn)[:40]}
    brief = fill((root / "prompts" / "research_brief.md").read_text(encoding="utf-8"), date=x.lim.today(),
                 needed_json=json.dumps(need), themes_json=json.dumps(stdin["themes"]))
    claude = Claude(s, x.lim, x.lim.tz)
    res = claude.run_with_retry("claude", brief, stdin, alert=lambda t: x.alert("research_fallback", t),
                                schema=root / "prompts" / "research_schema.json",
                                timeout=s.claude.research.timeout_seconds,
                                system_file=root / "prompts" / "research_system.md",
                                allowed_tools="WebSearch,WebFetch",
                                disallowed_tools="Bash,Edit,Write,Read,Agent,NotebookEdit",
                                max_turns=s.claude.research.max_turns)
    if res is None:
        return {"status": "fallback"}
    out = apply(x, res.data)
    x.conn.execute("INSERT INTO runs(job, started_at, finished_at, status, cost_usd, claude_session_id, note) "
                   "VALUES('research', ?, ?, 'ok', ?, ?, ?)",
                   (x.lim.clock(), x.lim.clock(), res.cost_usd, res.session_id, json.dumps(out)[:2000]))
    log.info("research: %s", out)
    return out
