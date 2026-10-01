---
tags: [active]
updated: 2026-10-02
---
# Changelog

## 2026-10-02
- Posts restyled to the shared Telegram card style (emoji header, <code> settings, divider, <blockquote expandable> hint, short link labels); James chose this over the brief's plain templates. Preview image stays above the text. SECTION_TITLES constant in `x100bot/render.py`. First real card posted to topic 396.
- Bot @jameskoh_x100vi_bot ("X100VI Coach") created via BotFather; admin in James Channel, posts to topic 396 (chat -1002069000031); private ids moved to .env (TELEGRAM_CHAT_ID, THREAD_ID, OWNER_USER_ID, ADMIN_CHAT_ID); forum topic support added to telegram.py; test-telegram passed (msg 3500 posted and deleted, admin alert received).
- Steps 1 to 3 built: config with safety floors, db, ratelimit, lock, web, telegram, alerts, cli; camera facts and 230 menu items from the official manual; Fuji X Weekly parser (new and old layouts, multi recipe pages, per sensor values) and recipe validator; light, weather, planner, renderer, fallback copy; `plan --dry-run --offline` prints a full day. 111 tests pass.
- Library: 21 verified recipes from 2 days of sources runs (14 web requests used on 2 Oct).
- Fixtures trimmed to parser relevant parts (`tools/trim_fixture.py`), no article text in the repo.
- Public repo created: github.com/gcjk768/x100bot.
