"""Video lessons: each configured channel's RSS feed once a day, each kept video verified with YouTube oEmbed
(the call must succeed and author_name must match the channel). Nothing is downloaded."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

import feedparser

from ..ratelimit import LimitError
from ..web import FetchError

log = logging.getLogger(__name__)
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={id}"
OEMBED = "https://www.youtube.com/oembed?url={url}&format=json"
# preferred subjects, from the brief: composition, light, street, or the X100VI
KEYWORDS = re.compile(r"compos|light|street|x100|frame|framing|layer|shadow|golden|colou?r|lines|minimal|negative space|"
                      r"portrait|night|blue hour|photo", re.I)


def channels(s) -> list[dict]:
    """[{name, id}] from config; plain strings without an id are skipped (they were starting points only)."""
    out = []
    for c in s.sources.youtube_channels:
        if isinstance(c, dict) and c.get("id"):
            out.append({"name": c["name"], "id": c["id"]})
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def verify(x, url: str, channel: str) -> bool:
    """YouTube oEmbed must answer and name the same channel."""
    page = x.web.get(OEMBED.format(url=quote(url, safe="")), refresh="never", check_robots=False)
    if page.status != 200:
        return False
    try:
        data = json.loads(page.text)
    except ValueError:
        return False
    return _norm(data.get("author_name", "")) == _norm(channel)


def refresh_videos(x, max_verify: int = 10, per_channel: int = 3) -> dict:
    """Read every channel feed (once a day), keep new videos about composition, light, street or the X100VI,
    verify at most max_verify of them with oEmbed (each is one request), newest first."""
    stats = {"channels": 0, "candidates": 0, "verified": 0, "rejected": 0}
    have = {r["url"] for r in x.conn.execute("SELECT url FROM videos")}
    tried = {r["value"] for r in x.conn.execute("SELECT value FROM kv WHERE key LIKE 'video_tried:%'")}
    budget = max_verify
    for ch in channels(x.s):
        try:
            feed = x.web.get(FEED.format(id=ch["id"]), refresh="daily", check_robots=False)
        except (LimitError, FetchError) as ex:
            log.warning("feed for %s skipped: %s", ch["name"], ex)
            continue
        stats["channels"] += 1
        kept = 0
        for e in feedparser.parse(feed.text).entries:
            url = e.get("link", "")
            title = e.get("title", "")
            desc = (e.get("media_description") or e.get("summary") or "")
            if not url or url in have or url in tried:
                continue
            if not KEYWORDS.search(f"{title} {desc}"):
                continue
            stats["candidates"] += 1
            if budget <= 0 or kept >= per_channel:
                continue
            budget -= 1
            x.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (f"video_tried:{url}", url))
            if verify(x, url, ch["name"]):
                x.conn.execute("INSERT OR IGNORE INTO videos(url, channel, title, description, published_at, verified_at)"
                               " VALUES(?,?,?,?,?,?)", (url, ch["name"], title, desc[:1000], e.get("published", ""),
                                                        x.lim.clock()))
                stats["verified"] += 1
                kept += 1
            else:
                stats["rejected"] += 1
    return stats
