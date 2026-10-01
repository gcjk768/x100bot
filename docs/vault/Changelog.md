---
tags: [active]
updated: 2026-10-02
---
# Changelog

## 2026-10-02
- Steps 1 to 3 built: config with safety floors, db, ratelimit, lock, web, telegram, alerts, cli; camera facts and 230 menu items from the official manual; Fuji X Weekly parser (new and old layouts, multi recipe pages, per sensor values) and recipe validator; light, weather, planner, renderer, fallback copy; `plan --dry-run --offline` prints a full day. 111 tests pass.
- Library: 21 verified recipes from 2 days of sources runs (14 web requests used on 2 Oct).
- Fixtures trimmed to parser relevant parts (`tools/trim_fixture.py`), no article text in the repo.
- Public repo created: github.com/gcjk768/x100bot.
