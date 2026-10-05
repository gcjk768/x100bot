# x100bot

A small scheduled bot that teaches one photographer in Singapore to use a Fujifilm X100VI well. Every hour from 07:00 to 22:00 it posts one item to a Telegram channel or forum topic: a film simulation recipe, a composition lesson, a photographer to learn from, an X100VI handling tip, a video lesson, a practice drill, or a daily plan built around Singapore's light and weather. In the private chat it answers commands, reads the settings out of your JPEGs and (teacher add on) critiques your photos. It runs in Docker on a UGREEN NAS.

Everything it posts is verified: recipe settings are copied from the source page and printed by the app, camera facts and menu names come only from the official manual, photographers are described only from pages the bot opened, every link is checked, and nothing is ever downloaded or re uploaded (photos appear through Telegram's link preview of the original page). Claude writes only the short teaching text, in two or three batched calls a day, and deterministic guardrails check every word of it.

## Setup

1. **Telegram bot and token.** Talk to @BotFather, `/newbot`, keep the token. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
2. **Where to post.** Either a channel (`TELEGRAM_CHAT_ID=@yourchannel` or the numeric id) or a topic in a forum group (`TELEGRAM_CHAT_ID=-100...` plus `TELEGRAM_THREAD_ID=<topic id>`; open the topic in Telegram Web, the URL `t.me/c/2069000031/396` means chat `<TELEGRAM_CHAT_ID>`, topic `396`). Make the bot an **admin** of the channel or group with permission to post and delete messages.
3. **Your user id and the admin chat.** Send `/start` to the bot, then run `curl https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.from.id`. Put it in `.env` as `TELEGRAM_OWNER_USER_ID`, and the same id as `TELEGRAM_ADMIN_CHAT_ID` so alerts arrive in your private chat. The bot answers nobody else.
4. **Claude.** On your PC run `claude setup-token` and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. (Or set `ANTHROPIC_API_KEY` and `claude.auth: apikey` in config.yaml.)
5. **Build and check.**
   ```
   docker compose build
   docker compose run --rm x100bot x100bot test-telegram   # posts one silent message and deletes it, sends one admin alert
   docker compose run --rm x100bot x100bot seed            # builds the starting library, once, within its page budget
   docker compose run --rm x100bot x100bot status
   docker compose up -d
   ```

`config.yaml` on GitHub carries no private ids; `.env` overrides `telegram.chat_id`, `message_thread_id`, `owner_user_id` and `admin_chat_id`.

### Adding it to an existing compose file

If you already run pddbot (or any other bot) from one compose file, copy the `x100bot` service block from `docker-compose.yml` into it, keep `./data`, `./config.yaml` and `./data/claude_home` pointing at this folder, and give it its own `env_file`. Both share no state, so they can live in one stack.

## Reading `x100bot status`

```
Budgets today (2026-10-02):
  web        150/150        every website together, link checks included
  openmeteo  2/6
  telegram   31
  claude     3/3            scheduled calls: research, compose, one retry
  critique   0/10
Cooldowns: web:www.fujifilm-x.com until 2026-10-02 23:44 (HTTP 403 ...)
Library stock (min_stock):
  recipe         57 (40)
  photographer   11 (10)
  camera_tip     57 (20)
  video          20 (10)
Queue for 2026-10-02: 07:00 brief posted msg 3511, 08:00 recipe pending ...
Next sources: 2026-10-03 05:00 ...
```

A cooldown means a site answered 429 or 403, or failed three times in a row; the bot leaves it alone for 24 hours and tells you once. `LOW` next to a stock line means the next plan run may call research, and slots of that type are filled with a learning tip until stock returns.

## Changing the day

* **Hours and types**: `schedule.slots` in `config.yaml` maps `"HH": type`. `sunday_overrides` replaces types on Sundays (default: the 21:00 learning tip becomes the weekly recap). Types: brief, recipe, camera_tip, composition, photographer, video_lesson, drill, golden_hour, learning_tip, review, weekly_recap. Only `telegram.notify_types` post with a sound.
* **Places**: `library/spots.yaml`. The bot never suggests a place that is not in this file. Themes use the keys from the curriculum.
* **Curriculum**: `library/curriculum.yaml`, 12 weekly themes starting on `learning.start_date` (a Monday); after week 12 it repeats one level deeper.
* **Fallback copy**: `library/fallbacks.yaml` is what gets posted when Claude is unavailable, so the day is never empty.
* **Channels**: `sources.youtube_channels` with each channel's id. Videos are kept only when YouTube's oEmbed confirms the channel.

## Private chat commands

`/today`, `/recipe [sunny|overcast|rain|golden|night|indoor|bw]`, `/tip`, `/slots` and `/setc <1 to 7> <recipe name>` (then recipe posts say "You have this in C3"), `/favorites` (forward a channel post to the bot to save it), `/done`, `/status`, `/help`.

**Send files, not photos, for the settings readout.** Telegram strips EXIF from photos but keeps it in files. Send the original JPEG or HEIF as a file and the bot replies with the Fujifilm settings you shot and the closest library recipe. Files over 20 MB cannot be downloaded by bots; send a smaller JPEG. RAF files cannot be read.

## The photo teacher

Send a photo to the bot (private chat or the bot's topic) and it critiques it: an overlay with the thirds grid, the suggested crop and a level line, with scores for composition, light, exposure, focus, colour and the moment; then the guidance, with one top fix explained properly (what, why, the principle, concrete steps), composition and light notes, when and where to reshoot, a settings table (what you used from the EXIF against what to try, with the menu path for every change, from `camera/settings_menu_map.yaml`), a reshoot plan, an exercise, a recipe and a photographer from the library, and the measured values the advice rests on.

* **Send files for settings advice.** Telegram strips EXIF from photos but keeps it in files. Send the camera's original JPEG or HEIF as a file (not a gallery re-save, which drops the Fujifilm maker notes). Caption = your intent. Flags: `#quick` short critique, `#settings` readout only (no Claude call), `#assignment` grade against today's assignment and mark it done when it meets it.
* **Ask more**: reply to any teacher message with a question, or tap Simpler, More depth, Why these scores, Reshoot plan. Show crop sends the crop preview.
* **Reshoot and compare**: reply to a critique with the new photo.
* **Series**: send an album of 2 to 8 photos.
* **Any text** is a question. `/plan what, where, when` gives a shot plan with that day's light times and forecast.
* `/level`, `/style` (encouraging, direct, socratic), `/quick on|off`, `/progress`, `/best`, `/history`, `/forget` (reply, or `/forget 12`), `/forgetall` (asks for confirmation).
* Sundays 20:00: a weekly progress report with a chart of your averages per area over 8 weeks and the best photo of the week. The 1st of the month: the five best of the month as an album.

Every claim about exposure, tilt, sharpness or colour comes from the app's own measurements (`x100bot/measure.py`), every setting the teacher recommends is validated against the X100VI (`x100bot/guardrails.py`), and GPS and all other metadata are stripped before Claude sees a copy. Photos stay in `data/teacher/` on the NAS; `/forget` deletes a critique's files, rows and Claude session; folders older than `teacher.retention_days` are removed. The teacher has its own daily caps (`limits.teacher`), one Claude call at a time, and a persistent queue: when Claude's usage limit is hit, photos wait until the reset time instead of failing. Thresholds in `teacher.measurement_thresholds` were calibrated on the first real photos (see the comment in `config.yaml`).

## Why the limits exist

Every request to a website, Open-Meteo, Telegram and Claude goes through `x100bot/ratelimit.py`, with state in SQLite so a restart never resets a budget. Websites get one request at a time, 6 to 12 seconds apart per domain, robots.txt is obeyed, feeds and index pages are fetched at most once a day with conditional GET, recipe and manual pages are cached forever, link checks for 14 days, and the whole app makes at most 150 web requests a day. Telegram calls are 1.2 seconds apart and at most 20 a minute; a 429 is waited out, and an item whose wait would exceed 15 minutes fails for that hour rather than posting late in a burst. Claude is called at most three times a day for the channel. `x100bot/config.py` refuses to start if any limit is set looser than its floor. A slow day is fine. Hammering a small blog or the Telegram API is not.

## Where the live pages differ from the brief

Noted during the build, 1 and 2 October 2026:

* **fujifilm-x.com answers HTTP 403** to the bot's user agent (the specifications page and the firmware page both live there). The bot put the host in a 24 hour cooldown and never retried through a proxy. The facts the brief quotes from the specifications page are kept with that URL; every fact the manual at fujifilm-dsc.com also states is cross cited to the manual, which the bot can read. The Monday firmware job therefore usually ends in a log line; the manual's Firmware Updates page is the fallback source of truth.
* **WHITE BALANCE shift and MONOCHROMATIC COLOR ranges** appear in the manual only as screenshots. The grids count 19 positions per axis for WB SHIFT (so -9 to +9) and 37 for MONOCHROMATIC COLOR (so -18 to +18). Both are stored with a note saying so.
* **The manual's menu headings use an icon font** (`<span class="fficn..">`), which `tools/build_menus.py` strips. `camera/menus.yaml` holds 230 menu items with page and anchor, plus every capitalised option word from those pages (GRID 9, AUTO1, MECHANICAL SHUTTER) for the compose guardrail.
* **Fuji X Weekly recipe pages**: the `<h1>` is the site name, so the recipe name comes from `og:title`. Some posts hold several recipes (for example Vibrant Arizona and Indoor Angouleme, each in an X-Trans V and an X-Trans IV version); each block is named by its caption line, the X-Trans V version is kept, and each gets a `#name` link on the same page. Older posts put the film simulation alone on the first line and title the post "...Recipe: Name". Some settings list a value per sensor ("Strong (X-Trans IV), Weak (X-Trans V)"); the X-Trans V value is used. All of this lives in one function, `extract()` in `x100bot/library/parsers/fujixweekly.py`.
* **Credit**: some recipes were created by readers and published by Ritchie Roesch (Easy Reala Ace thanks "Nathalie" in its text). The bot credits the page's byline author, the only author the page states in a machine readable way; the link carries the full credit.
* **The recipe indexes are sorted by film simulation**, so the sources job takes new links in a date seeded shuffle for variety.
* **The Fuji X Weekly feed carries full post bodies**, so a recipe published in the feed is parsed from the feed itself.
* **astral** computes golden and blue hour on the UTC date, which puts the Singapore morning golden hour on the wrong day; `light.py` picks the event on the local date.
* **Claude Code CLI**: `--json-schema` takes the schema text, not a file path. On Windows the npm shim is a `.cmd`; the wrapper runs `bin/claude.exe` directly. In the container the native installer is used.
* **Photographer sites**: several official homepages are JavaScript rendered and give almost no text (Meyerowitz, Moriyama, Webb); their about pages are needed for notes. equinoxgallery.com blocks robots.txt with 403; valeriejardinphotography.com disallows crawling in robots.txt, so both are left out.

## Test fixtures

Fixture pages in `tests/fixtures/fujixweekly/` are trimmed by `tools/trim_fixture.py` to the parts the parser reads: the title, the byline, the settings paragraphs and their caption lines, and index links. No article text or photos are kept in this repository. `tests/fixtures/exif/x100vi_sample.json` is synthetic until a real X100VI file replaces it.

## Development

```
pip install -e ".[dev]"
python -m pytest -q
x100bot plan --date 2026-10-05 --dry-run --offline     # a full day from the library and fallbacks, no network
x100bot plan --date 2026-10-05 --dry-run               # same with weather and one Claude compose call
x100bot eval                                           # the compose guardrails on the golden fixtures
```

If `VAULT_DIR` is set (an Obsidian vault mounted into the container, NAS: `/volume1/<USER>/Obsidian/x100bot`), the bot keeps its movement log and memory there: one line per event in `Activity/YYYY/MM/YYYY-MM-DD.md` (posts, plans, sources runs, alerts, commands), one note per posted item in `Recipes/` and `Lessons/` with an append-only History, and `Home.md`. The daily compose call gets the newest Activity lines (capped at 4,000 characters) as memory so recipes and lessons are not repeated. Best effort only; the vault never breaks a run.
