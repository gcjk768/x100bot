"""Fuji X Weekly parser: recipe links from an index page, and the settings blocks from a recipe page.

The live markup (checked 2026-10-01): settings are one <p> of <strong> lines split by <br>, starting with
"Film Simulation:". The <h1> is the site name, so the recipe name comes from og:title. Some pages hold several
recipes; each block is then named by the next paragraph, "Example photographs ... using this <Name> Film
Simulation Recipe on my Fujifilm <camera>", which also tells the sensor. Sample photos are never downloaded.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

RECIPE_URL = re.compile(r"^https://fujixweekly\.com/\d{4}/\d{2}/\d{2}/[^#?]+/?$")
X_TRANS_V = ("X100VI", "X-T5", "X-H2", "X-H2S", "X-S20", "X-T50", "X-M5", "X-E5", "X-T30 III", "GFX100")
X_TRANS_IV = ("X100V", "X-Pro3", "X-T4", "X-S10", "X-E4", "X-T30 II")


def recipe_links(html: str) -> list[tuple[str, str]]:
    """(link text, url) for every dated post linked from the index page's content, in page order."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("div.entry-content") or soup
    out, seen = [], set()
    for a in content.select("a[href]"):
        url = a["href"].split("#")[0]
        if RECIPE_URL.match(url) and url not in seen:
            seen.add(url)
            out.append((a.get_text(" ", strip=True), url))
    return out


def sensor_from_text(text: str) -> str | None:
    """X-Trans V when a fifth generation camera or 'X-Trans V' is named, X-Trans IV when only fourth generation is."""
    t = re.sub(r"\s+", " ", text)
    if re.search(r"X-Trans V\b|X-Trans IV (&|and|/) V\b|fifth.generation|5th.gen", t, re.I) or \
            any(re.search(rf"\b{re.escape(c)}\b", t) for c in X_TRANS_V):
        return "X-Trans V"
    if re.search(r"X-Trans IV\b", t) or any(re.search(rf"\b{re.escape(c)}\b(?!I)", t) for c in X_TRANS_IV):
        return "X-Trans IV"
    return None


def extract(html: str) -> dict:
    """The one function to patch when the page layout changes.
    Returns {title, author, blocks: [{name, sensor, settings: {label: value}}]} with values as the page writes them."""
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:title")
    title = og["content"] if og else (soup.title.get_text(strip=True) if soup.title else "")
    author_el = soup.select_one("[rel=author], .byline .author a, .author a")
    content = soup.select_one("div.entry-content") or soup
    blocks = []
    for p in content.find_all("p"):
        flat = p.get_text(" ", strip=True)
        # current posts start with "Film Simulation:"; older ones put the film simulation alone on the first line
        if not (flat.startswith("Film Simulation:") or ("Dynamic Range:" in flat and "White Balance:" in flat)):
            continue
        for br in p.find_all("br"):
            br.replace_with("\n")
        settings = {}
        lines = [ln.replace("\xa0", " ").strip() for ln in p.get_text("").splitlines() if ln.strip()]
        if lines and ":" not in lines[0]:
            settings["Film Simulation"] = lines.pop(0)
        for line in lines:
            m = re.match(r"\s*([^:]+?)\s*:\s*(.+?)\s*$", line)
            if m:
                settings[re.sub(r"\s+", " ", m.group(1))] = re.sub(r"\s+", " ", m.group(2))
        caption = ""
        nxt = p.find_next_sibling("p")
        if nxt and "using this" in nxt.get_text():
            caption = nxt.get_text(" ", strip=True)
        named = re.search(r"using this (.+?) (?:film simulation )?recipe", caption, re.I)
        blocks.append({"name": named.group(1).strip(' "“”') if named else "",
                       "sensor": sensor_from_text(caption.split(" on ")[-1]) if caption else None,
                       "settings": settings})
    return {"title": title, "author": author_el.get_text(" ", strip=True) if author_el else "", "blocks": blocks}


def name_from_title(title: str) -> str:
    """'Easy Reala Ace — Fujifilm X100VI (X-Trans V) Film Simulation Recipe' -> 'Easy Reala Ace';
    'Fujifilm X100V Film Simulation Recipe: Fujicolor Superia 100' -> 'Fujicolor Superia 100'."""
    m = re.search(r"Recipe:\s*(.+)$", title)
    if m:
        return m.group(1).strip()
    return re.split(r"\s+[—–-]\s+|:\s", title)[0].strip()
