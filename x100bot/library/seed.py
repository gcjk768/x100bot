"""`x100bot seed`: build the starting library once, within limits.web.max_seed_pages and normal per domain pacing.
Recipes come from both index pages, tips from library/seed/tips.yaml (checked against the cached manual pages),
photographers from library/seed/people.yaml (official pages link checked and read), videos from the channel feeds.
A fresh install loads the seed YAML files without fetching anything but the recipe pages."""
from __future__ import annotations

import logging

from ..lock import job_lock
from ..ratelimit import BudgetExhausted, InCooldown
from ..web import FetchError
from .sources import refresh_recipes

log = logging.getLogger(__name__)
SEED_RECIPE_PAGES = 60   # of the max_seed_pages budget; the rest goes to people, videos and link checks


def run_seed(s, only: str | None = None) -> dict:
    from ..cli import Ctx
    out = {}
    with job_lock(s.data_dir, "seed"):
        x = Ctx(s, "seed", seed=True)
        steps = []
        if only in (None, "recipes"):
            steps.append(("recipes", lambda: refresh_recipes(x, max_new=SEED_RECIPE_PAGES)))
        if only in (None, "tips"):
            from .tips import load_seed as load_tips
            steps.append(("tips", lambda: load_tips(x)))
        if only in (None, "people"):
            from .people import load_seed as load_people
            steps.append(("people", lambda: load_people(x)))
        if only in (None, "videos"):
            from .videos import refresh_videos
            steps.append(("videos", lambda: refresh_videos(x, max_verify=25)))
        for name, step in steps:
            try:
                out[name] = step()
            except BudgetExhausted as ex:
                out[name] = f"stopped: {ex}"
                break
            except (InCooldown, FetchError) as ex:
                out[name] = f"skipped: {ex}"
            log.info("seed %s: %s", name, out[name])
    return out
