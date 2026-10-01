---
tags: [active]
updated: 2026-10-02
---
# App Overview

One item per hourly Telegram message, 07:00 to 22:00 SGT, 16 a day. Claude is called in batches at 05:30, never per post.

## Flow
- 05:00 sources (`x100bot/library/sources.py`): feeds, recipe indexes, YouTube feeds, within budget.
- 05:30 plan (`x100bot/planner.py`): light (`x100bot/light.py`), weather (`x100bot/weather.py`), pick library material per slot, compose, render (`x100bot/render.py`), queue.
- Hourly post (`x100bot/scheduler.py`, step 6): claim one queue row, send.

## The gate
Every website, Open-Meteo, Telegram and Claude call goes through `x100bot/ratelimit.py` (buckets `web`, `web:<domain>`, `openmeteo`, `telegram`, `claude`, `critique`; state in SQLite). Websites only through `x100bot/web.py` (robots.txt, conditional GET, cache forever, link checks). Safety floors in `x100bot/config.py`.

## Library
- Recipes: `x100bot/library/recipes.py` (normalise, validate against `camera/x100vi_facts.yaml`, adapt X-Trans IV, hash) and `x100bot/library/parsers/fujixweekly.py` (`extract()` is the one function to patch).
- Menus for the compose guardrail: `camera/menus.yaml`, built by `tools/build_menus.py` from cached manual pages.
- Curriculum `library/curriculum.yaml`, spots `library/spots.yaml`, fallback copy `library/fallbacks.yaml`.

## Data
SQLite at `data/x100bot.db` (WAL), page cache in `data/pages/`, locks in `data/locks/`. Schema in `x100bot/db.py`.

## Message style
Global card style (see `~/.claude/CLAUDE.md`), one fixed emoji per type in `SECTION_TITLES` (`x100bot/render.py`). Hashtags at the end of each card. `{no_x}` placeholders give a line variant for when Claude's text is missing.

## Planner rules worth knowing
- Recipe of the day in the brief is the day's recipe post that best fits the main condition, so it also gets its own card.
- A slot with no verified material becomes a learning tip on an unused topic (never a repeat inside a repeat window) and raises one `stock_low` alert.
- 18:00 is tagged golden_hour and 20:00 night; untagged recipes count as `any` until research tags them.
