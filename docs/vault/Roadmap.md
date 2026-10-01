---
tags: [active]
updated: 2026-10-02
---
# Roadmap

Build order from the brief. STOP HERE after 3 and after the first real compose call in 5.

1. Scaffold, config floors, db, ratelimit, web, lock, telegram, test-telegram. Done.
2. Camera facts, menus, recipe model and validator, Fuji X Weekly parser on fixtures. Done.
3. light, weather, planner, render with fallbacks, `plan --dry-run --offline`. Done, STOP HERE shown.
4. `x100bot seed`. Done: 57 recipes, 57 tips, 11 of 20+ photographers (rest need about pages, tomorrow's budget), 20 videos, channel ids resolved.
5. compose done with one real call (STOP HERE shown 2026-10-02). Research runs when stock is low or recipes are untagged (tomorrow). Basic critique dropped: the teacher replaces it. exif mapping waits for a real X100VI JPEG.
6. Done: scheduler, status, eval, bot.py private commands.
7. Done: Dockerfile, compose, README. Deployed 2026-10-02 as NAS stack /volume1/docker/x100bot (image x100bot:2a6ae78); first channel day is 2026-10-02 from 07:00 with fallback text (Claude budget was spent by the build), full compose from 2026-10-03.

Open items: `sources.firmware_page` URL (find during step 4), YouTube channel ids, `telegram.chat_id` and `owner_user_id` from the owner.

Then the teacher add on, steps 1 to 6 of [[Teacher Spec]], with its own two STOP HERE points.

Teacher: steps 1 to 6 done 2026-10-02 and deployed. Open: a real camera JPEG for the Fujifilm EXIF tag mapping (exif.py TAGS are exiftool's documented names, unconfirmed), 7 more photographers, research call on untagged recipes, first live weekly report on Sunday 2026-10-04 20:00.
