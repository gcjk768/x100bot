"""Build camera/menus.yaml from the official manual pages already in the page cache (data/pages).
Run after fetching the manual pages through the bot's own web client. Every h2 with an anchor on a menu page
is a menu item; icon font glyphs in front of some names are stripped."""
import re
import sqlite3
import sys
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://fujifilm-dsc.com/en/manual/x100vi/"
MENU_PAGES = ["menu_shooting/image_quality_setting/", "menu_shooting/af_mf_setting/", "menu_shooting/shooting_setting/",
              "menu_shooting/flash_setting/", "menu_shooting/movie_setting_still_photography/",
              "menu_shooting/movie_setting/", "menu_shooting/image_quality_setting_movie/",
              "menu_shooting/af_mf_setting_movie/", "menu_shooting/audio_setting/",
              "menu_shooting/time_code_setting/", "playback/menu_playback/", "connections/network_usb_menu/",
              "menu_setup/user_setting/", "menu_setup/sound_set-up/", "menu_setup/screen_set-up/",
              "menu_setup/button-dial_setting/", "menu_setup/power_management/", "menu_setup/save_data_set-up/",
              "shortcuts/my_menu/", "shortcuts/quick_menu/", "shortcuts/function_buttons/"]


def clean(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def strip_icons(soup) -> None:
    """The manual draws menu icons with an icon font: <span class="fficn63">x</span>. They are not text."""
    for span in soup.select('span[class^="fficn"]'):
        span.decompose()


def main(db_path=ROOT / "data" / "x100bot.db"):
    conn = sqlite3.connect(db_path)
    menus, seen = [], set()
    for rel in MENU_PAGES:
        url = BASE + rel
        row = conn.execute("SELECT body_path FROM pages WHERE url=?", (url,)).fetchone()
        if not row:
            sys.exit(f"not cached yet: {url}")
        soup = BeautifulSoup(Path(row[0]).read_text(encoding="utf-8"), "lxml")
        strip_icons(soup)
        h1 = clean(soup.find("h1").get_text(" ", strip=True))
        for h in soup.find_all("h2"):
            name = clean(h.get_text(" ", strip=True))
            if not h.get("id") or name != name.upper() or not re.search("[A-Z]", name):
                continue
            key = (name, h1)
            if key in seen:
                continue
            seen.add(key)
            menus.append({"name": name, "section": h1, "url": f"{url}#{h['id']}"})
    # every capitalised word in the manual's menu pages (option values such as GRID 9, AUTO1, MECHANICAL SHUTTER),
    # so the compose guardrail can tell a manual term from an invented one
    option_words = set()
    for rel in MENU_PAGES:
        url = BASE + rel
        row = conn.execute("SELECT body_path FROM pages WHERE url=?", (url,)).fetchone()
        soup = BeautifulSoup(Path(row[0]).read_text(encoding="utf-8"), "lxml")
        strip_icons(soup)
        main = soup.select_one("main") or soup.body
        for w in re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9./+()-]+(?![A-Za-z0-9])", main.get_text(" ")):
            option_words.add(w.strip("().,"))
    out = ROOT / "camera" / "menus.yaml"
    header = ("# Every menu item name from the official X100VI manual, one entry per item, with the page and anchor.\n"
              "# Built by tools/build_menus.py from the cached manual pages. The compose guardrail reads it.\n")
    out.write_text(header + yaml.safe_dump({"source": BASE, "items": menus, "option_words": sorted(w for w in option_words if w)},
                                  sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    print(f"{len(menus)} menu items written to {out}")


if __name__ == "__main__":
    main()
