"""X100VI camera tips: a title, 2 to 5 facts, the menu path exactly as the manual writes it, and the manual page URL.
A tip is accepted only when its URL is under sources.manual_base and the page text contains the menu item name."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def page_text(x, url: str) -> str:
    """The manual page's text with icon glyphs removed. Cached forever, so a seed load after the build costs nothing."""
    page = x.web.get(url.split("#")[0], refresh="never")
    soup = BeautifulSoup(page.text, "lxml")
    for sp in soup.select('span[class^="fficn"]'):
        sp.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def verify(x, tip: dict) -> list[str]:
    """Problems with the tip, empty when it is acceptable."""
    errs = []
    url = tip.get("manual_url", "")
    if not url.startswith(x.s.sources.manual_base):
        errs.append(f"url not under the official manual: {url}")
        return errs
    if not 2 <= len(tip.get("facts", [])) <= 5:
        errs.append("needs 2 to 5 facts")
    menu_item = tip.get("menu_item") or tip["menu_path"].split(">")[-1].strip()
    text = page_text(x, url)
    if menu_item.lower() not in text.lower():
        errs.append(f"page does not contain the menu item '{menu_item}'")
    return errs


def store(x, tip: dict) -> int | None:
    errs = verify(x, tip)
    if errs:
        log.warning("tip rejected, %s: %s", tip.get("title"), "; ".join(errs))
        return None
    row = x.conn.execute("SELECT id FROM tips WHERE manual_url=? AND title=?", (tip["manual_url"], tip["title"])).fetchone()
    if row:
        return None
    import json
    cur = x.conn.execute("INSERT INTO tips(topic, title, facts_json, menu_path, manual_url, verified_at) VALUES(?,?,?,?,?,?)",
                         (tip["topic"], tip["title"], json.dumps(tip["facts"]), tip["menu_path"], tip["manual_url"],
                          x.lim.clock()))
    return cur.lastrowid


def load_seed(x, path: str | Path | None = None) -> dict:
    path = Path(path) if path else x.s.path("library/seed/tips.yaml")
    tips = yaml.safe_load(path.read_text(encoding="utf-8"))["tips"]
    stored = sum(1 for t in tips if store(x, t))
    return {"in_file": len(tips), "stored": stored, "total": x.conn.execute("SELECT COUNT(*) FROM tips").fetchone()[0]}
