"""The 05:00 sources job: refresh feeds, recipe indexes and YouTube feeds within budget, verify new entries,
top up the library. When the web budget runs out it stops; the plan job works from what the library has."""
from __future__ import annotations

import logging
import random
from datetime import datetime, time as dtime
from html import escape

import feedparser

from ..db import kv_set
from ..lock import job_lock
from ..ratelimit import BudgetExhausted, InCooldown
from ..web import Blocked, FetchError
from . import recipes
from .parsers import fujixweekly as fxw

log = logging.getLogger(__name__)
PARSERS = {"fujixweekly": fxw}


def recipe_pages_today(x) -> int:
    start = datetime.combine(datetime.fromtimestamp(x.lim.clock(), x.lim.tz).date(), dtime(0), x.lim.tz).timestamp()
    rows = x.conn.execute("SELECT url FROM pages WHERE fetched_at>=?", (start,)).fetchall()
    return sum(1 for r in rows if fxw.RECIPE_URL.match(r["url"]))


def parsed_urls(conn) -> set[str]:
    return {r["source_url"].split("#")[0] for r in conn.execute("SELECT source_url FROM recipes")} | \
           {r["value"] for r in conn.execute("SELECT value FROM kv WHERE key LIKE 'parsed:%'")}


def refresh_recipes(x, max_new: int | None = None) -> dict:
    s = x.s
    facts_file = str(s.path(s.camera.facts_file))
    stats = {"indexes": 0, "new_pages": 0, "stored": 0, "rejected": 0}
    cap = (max_new if max_new is not None else s.limits.web.max_new_recipe_pages_per_day) - recipe_pages_today(x)
    seen = parsed_urls(x.conn)
    cached = {r["url"] for r in x.conn.execute("SELECT url FROM pages WHERE body_path IS NOT NULL")}
    todo = []
    for idx in s.sources.recipe_indexes:
        page = x.web.get(idx.url, refresh="daily")
        stats["indexes"] += 1
        todo += [(url, idx) for _t, url in PARSERS[idx.parser].recipe_links(page.text) if url not in seen]
    # indexes are sorted by film simulation; a seeded shuffle gives the library variety, cached pages go first
    random.Random(x.lim.today()).shuffle(todo)
    todo.sort(key=lambda t: t[0] not in cached)
    for url, idx in todo:
        if url in seen:
            continue
        if url not in cached:   # a page already in the cache costs nothing
            if cap <= 0:
                log.info("max_new_recipe_pages_per_day reached, the rest waits for tomorrow")
                break
            cap -= 1
            stats["new_pages"] += 1
        seen.add(url)
        p = x.web.get(url)   # cached forever
        found = recipes.from_page(p.text, url, idx.sensor, facts_file)
        for r in found:
            if recipes.store(x.conn, r, x.lim.clock()):
                stats["stored"] += 1
            elif r.errors:
                stats["rejected"] += 1
        # only a page that gave valid recipes is done; others are re-read from the cache (no request) after a fix
        if found and not any(r.errors for r in found):
            kv_set(x.conn, f"parsed:{url}", url)
    return stats


def refresh_feeds(x) -> dict:
    """Recipes published in the feed are parsed from the feed body itself, so no extra page is fetched."""
    facts_file = str(x.s.path(x.s.camera.facts_file))
    stored = 0
    for feed in x.s.sources.feeds:
        page = x.web.get(feed.url, refresh="daily")
        for e in feedparser.parse(page.text).entries:
            body = (e.get("content") or [{"value": e.get("summary", "")}])[0]["value"]
            if "Film Simulation:" not in body:
                continue
            html = (f'<meta property="og:title" content="{escape(e.title)}">'
                    f'<a rel="author">{escape(e.get("author", ""))}</a><div class="entry-content">{body}</div>')
            for r in recipes.from_page(html, e.link, "X-Trans V", facts_file):
                stored += bool(recipes.store(x.conn, r, x.lim.clock()))
            kv_set(x.conn, f"parsed:{e.link}", e.link)
    return {"feed_recipes": stored}


def run_sources(s) -> dict:
    from ..cli import Ctx
    with job_lock(s.data_dir, "sources"):
        x = Ctx(s, "sources")
        out = {}
        steps = [("feeds", refresh_feeds), ("recipes", refresh_recipes)]
        try:
            from .videos import refresh_videos
            steps.append(("videos", refresh_videos))
        except ImportError:   # videos arrive with the seed step
            pass
        for name, step in steps:
            try:
                out[name] = step(x)
            except BudgetExhausted as ex:
                x.alert("budget", f"web budget reached during sources: {ex}")
                out[name] = f"stopped: {ex}"
                break
            except (InCooldown, Blocked, FetchError) as ex:
                log.warning("sources %s: %s", name, ex)
                out[name] = f"skipped: {ex}"
        return out
