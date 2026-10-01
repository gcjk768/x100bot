# x100bot

A small scheduled bot that teaches one photographer in Singapore to use a Fujifilm X100VI well. Every hour from 07:00 to 22:00 it posts one item to a Telegram channel: a film simulation recipe, a composition lesson, a photographer to learn from, an X100VI handling tip, a video lesson, a practice drill, or a daily plan built around Singapore's light and weather. It runs in Docker on a UGREEN NAS.

Build status: steps 1 to 3 of 7 are done (rate limits, web client, Telegram client, camera facts, recipe parser and validator, light, weather, planner, renderer, offline dry run). The Claude calls, scheduler, private chat commands, Docker files and the full setup guide come next.

## Try it

```
pip install -e ".[dev]"
python -m pytest -q
x100bot plan --date 2026-10-05 --dry-run --offline
```

The offline dry run uses only the library already in `data/` and the fallback text in `library/fallbacks.yaml`, with no web, weather or Claude calls.

## Why the limits exist

Every request to a website, Open-Meteo, Telegram or Claude goes through `x100bot/ratelimit.py`. Websites get one request at a time, 6 to 12 seconds apart per domain, robots.txt is obeyed, feeds and index pages are fetched at most once a day with conditional GET, recipe pages are cached forever, and the whole app makes at most 150 web requests a day. A 429 or 403, or three failures in a row, rests that domain for 24 hours. `config.py` refuses to start if anyone loosens a limit past its safety floor. A slow day is fine. Hammering a small blog is not.

## Where the live pages differ from the brief

Noted during the build, 1 and 2 October 2026:

* **fujifilm-x.com answers HTTP 403** to the bot's user agent, so the bot put it into a 24 hour cooldown and never retried it through a proxy. The facts the brief quotes from that specification page (X-Processor 5, 4 stop ND, 6.0 stop IBIS, 10 cm minimum focus, teleconverter framings) are kept with that URL. Every other fact in `camera/x100vi_facts.yaml` is also confirmed on the official manual at fujifilm-dsc.com, which the bot can read, and carries that URL too.
* **WHITE BALANCE shift and MONOCHROMATIC COLOR ranges** appear in the manual only as screenshots. The grids count 19 positions per axis for WB SHIFT (so -9 to +9) and 37 for MONOCHROMATIC COLOR (so -18 to +18). Both are stored with a note saying so.
* **The manual's menu headings use an icon font** (`<span class="fficn..">`), which `tools/build_menus.py` strips. `camera/menus.yaml` holds 230 menu items, each with its page and anchor.
* **Fuji X Weekly recipe pages**: the `<h1>` is the site name, so the recipe name comes from `og:title`. Some posts hold several recipes (for example Vibrant Arizona and Indoor Angouleme, each with an X-Trans V and an X-Trans IV version); each block is named by its caption line, the X-Trans V version is kept, and each gets a `#name` link on the same page. Older posts put the film simulation alone on the first line with no label, and title the post "...Recipe: Name". Some settings list a value per sensor ("Strong (X-Trans IV), Weak (X-Trans V)"); the X-Trans V value is used. All of this lives in one function, `extract()` in `x100bot/library/parsers/fujixweekly.py`.
* **Credit**: some recipes were created by readers and published by Ritchie Roesch (Easy Reala Ace thanks "Nathalie" in its text). The bot credits the page's byline author, because that is the only author the page states in a machine readable way. The link to the source page carries the full credit.
* **The recipe indexes are sorted by film simulation**, so the sources job takes new links in a date seeded shuffle to give the library variety.
* **The Fuji X Weekly feed carries full post bodies**, so a recipe published in the feed is parsed from the feed itself, with no extra page request.
* **astral** computes golden and blue hour on the UTC date, which puts the Singapore morning golden hour on the wrong day. `light.py` picks the event that falls on the local date.

## Test fixtures

Fixture pages in `tests/fixtures/fujixweekly/` are trimmed by `tools/trim_fixture.py` to the parts the parser reads: the title, the byline, the settings paragraphs and their caption lines, and index links. No article text or photos are kept in this repository.
