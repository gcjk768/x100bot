"""Photographers: name, kind (master or educator), identity, official URL, extra URLs, 3 to 6 factual notes each with
the URL it came from, and the curriculum themes they suit. Valid only when every URL passes the link check and the
official page's text contains the person's name."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from ..ratelimit import LimitError
from ..web import FetchError

log = logging.getLogger(__name__)


def _name_in(text: str, name: str) -> bool:
    parts = [p for p in re.split(r"[\s\-]+", name) if len(p) > 2]
    low = text.lower()
    return all(p.lower() in low for p in parts)


def verify(x, p: dict) -> list[str]:
    """Problems with the entry, empty when valid. The official page is fetched once: a 200 proves the link and
    gives the text for the name check; other URLs get a link check (cached for link_check_cache_days)."""
    errs = []
    if p.get("kind") not in ("master", "educator"):
        errs.append("kind must be master or educator")
    if not 3 <= len(p.get("facts", [])) <= 6:
        errs.append("needs 3 to 6 facts")
    try:
        page = x.web.get(p["official_url"], refresh="never")
    except (LimitError, FetchError) as ex:
        return errs + [f"official page: {ex}"]
    if page.status >= 400:
        return errs + [f"official page answered HTTP {page.status}"]
    text = re.sub(r"\s+", " ", BeautifulSoup(page.text, "lxml").get_text(" ", strip=True))
    if not _name_in(text, p["name"]):
        errs.append("official page does not contain the name")
    others = [u for u in dict.fromkeys(list(p.get("extra_urls", [])) + [f["url"] for f in p.get("facts", [])])
              if u != p["official_url"]]
    for u in others:
        try:
            if not x.web.link_ok(u):
                errs.append(f"link check failed: {u}")
        except (LimitError, FetchError) as ex:
            errs.append(f"link check error {u}: {ex}")
    return errs


def store(x, p: dict) -> int | None:
    if x.conn.execute("SELECT 1 FROM people WHERE name=?", (p["name"],)).fetchone():
        return None
    errs = verify(x, p)
    if errs:
        log.warning("photographer rejected, %s: %s", p["name"], "; ".join(errs))
        return None
    cur = x.conn.execute(
        "INSERT INTO people(name, kind, identity, official_url, urls_json, facts_json, themes_json, verified_at) "
        "VALUES(?,?,?,?,?,?,?,?)", (p["name"], p["kind"], p["identity"], p["official_url"],
                                    json.dumps(p.get("extra_urls", [])), json.dumps(p["facts"]),
                                    json.dumps(p.get("themes", [])), x.lim.clock()))
    return cur.lastrowid


def load_seed(x, path: str | Path | None = None) -> dict:
    path = Path(path) if path else x.s.path("library/seed/people.yaml")
    people = yaml.safe_load(path.read_text(encoding="utf-8"))["people"]
    stored = sum(1 for p in people if store(x, p))
    return {"in_file": len(people), "stored": stored,
            "total": x.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]}
