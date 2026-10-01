"""Load config.yaml and .env into pydantic settings, and refuse settings looser than the safety floors."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent

ITEM_TYPES = {"brief", "recipe", "camera_tip", "composition", "photographer", "video_lesson", "drill",
              "golden_hour", "learning_tip", "review", "weekly_recap"}


class ConfigError(Exception):
    pass


class TelegramCfg(BaseModel):
    chat_id: str
    owner_user_id: int = 0
    admin_chat_id: str = ""
    notify_types: list[str] = ["brief", "golden_hour"]
    preview_above_text: bool = True
    replace_previous_day: bool = False


class ScheduleCfg(BaseModel):
    timezone: str = "Asia/Singapore"
    sources_cron: str
    plan_cron: str
    firmware_cron: str
    post_minute: int = 0
    misfire_grace_minutes: int = 30
    slots: dict[str, str]
    sunday_overrides: dict[str, str] = {}


class LocationCfg(BaseModel):
    name: str
    latitude: float
    longitude: float


class CameraCfg(BaseModel):
    model: str
    facts_file: str
    menus_file: str


class LearningCfg(BaseModel):
    start_date: str
    level: str
    curriculum_file: str
    spots_file: str
    learning_topics: list[str]


class LibraryCfg(BaseModel):
    recipe_repeat_days: int
    photographer_repeat_days: int
    camera_tip_repeat_days: int
    lesson_repeat_days: int
    video_repeat: str = "never"
    min_stock: dict[str, int]


class RecipeIndex(BaseModel):
    name: str
    url: str
    sensor: str
    parser: str


class Feed(BaseModel):
    name: str
    url: str
    kind: str = "rss"


class SourcesCfg(BaseModel):
    recipe_indexes: list[RecipeIndex]
    feeds: list[Feed]
    youtube_channels: list[str | dict]
    manual_base: str
    firmware_page: str = ""


class WeatherCfg(BaseModel):
    rain_probability: int
    overcast_cloud_cover: int
    sunny_cloud_cover: int


class CallCfg(BaseModel):
    max_turns: int
    timeout_seconds: int
    max_long_edge_px: int = 2048


class ClaudeCfg(BaseModel):
    binary: str = "claude"
    model: str = "sonnet"
    fallback_model: str = "haiku"
    auth: Literal["oauth", "apikey"] = "oauth"
    max_retries: int = 4
    research: CallCfg
    compose: CallCfg
    critique: CallCfg
    no_tools: list[str]


class WebLimits(BaseModel):
    per_domain_min_gap_seconds: float
    per_domain_max_gap_seconds: float
    max_requests_per_day: int
    max_new_recipe_pages_per_day: int
    max_seed_pages: int
    retries: int
    backoff_seconds: list[float]
    domain_cooldown_hours: float
    link_check_cache_days: int
    respect_robots_txt: bool = True
    user_agent: str


class OpenMeteoLimits(BaseModel):
    max_calls_per_day: int


class TelegramLimits(BaseModel):
    min_gap_seconds: float
    max_per_minute: int
    max_retries: int
    max_retry_after_seconds: float
    poll_timeout_seconds: int


class ClaudeLimits(BaseModel):
    max_scheduled_calls_per_day: int
    max_critiques_per_day: int


class Limits(BaseModel):
    web: WebLimits
    openmeteo: OpenMeteoLimits
    telegram: TelegramLimits
    claude: ClaudeLimits


class RepairCfg(BaseModel):
    enabled: bool = False


class Settings(BaseModel):
    telegram: TelegramCfg
    schedule: ScheduleCfg
    location: LocationCfg
    camera: CameraCfg
    learning: LearningCfg
    library: LibraryCfg
    sources: SourcesCfg
    weather: WeatherCfg
    claude: ClaudeCfg
    limits: Limits
    repair: RepairCfg
    root: Path = ROOT          # where camera/, library/ and prompts/ live
    data_root: Path | None = None
    bot_token: str = ""

    @property
    def data_dir(self) -> Path:
        return self.data_root or self.root / "data"

    def path(self, rel: str) -> Path:
        return self.root / rel


FLOORS = [
    # key, is too loose, the rule
    ("limits.web.per_domain_min_gap_seconds", lambda s: s.limits.web.per_domain_min_gap_seconds < 3, "must be at least 3"),
    ("limits.web.max_requests_per_day", lambda s: s.limits.web.max_requests_per_day > 500, "must be at most 500"),
    ("limits.web.max_new_recipe_pages_per_day", lambda s: s.limits.web.max_new_recipe_pages_per_day > 30, "must be at most 30"),
    ("limits.telegram.min_gap_seconds", lambda s: s.limits.telegram.min_gap_seconds < 1.0, "must be at least 1.0"),
    ("limits.telegram.max_per_minute", lambda s: s.limits.telegram.max_per_minute > 20, "must be at most 20"),
    ("limits.claude.max_critiques_per_day", lambda s: s.limits.claude.max_critiques_per_day > 30, "must be at most 30"),
    ("limits.claude.max_scheduled_calls_per_day", lambda s: s.limits.claude.max_scheduled_calls_per_day > 6, "must be at most 6"),
]


def check(s: Settings) -> None:
    """Refuse to start when a limit is looser than its floor, naming the key."""
    for key, bad, why in FLOORS:
        if bad(s):
            raise ConfigError(f"Refusing to start: {key} {why}. The safety floors stop the bot being sped up by accident.")
    if s.limits.web.per_domain_max_gap_seconds < s.limits.web.per_domain_min_gap_seconds:
        raise ConfigError("Refusing to start: limits.web.per_domain_max_gap_seconds is below per_domain_min_gap_seconds.")
    for hh, t in {**s.schedule.slots, **s.schedule.sunday_overrides}.items():
        if t not in ITEM_TYPES or not (len(hh) == 2 and hh.isdigit() and 0 <= int(hh) <= 23):
            raise ConfigError(f"schedule.slots: '{hh}: {t}' is not an hour and a known item type")
    if s.telegram.admin_chat_id and s.telegram.admin_chat_id == s.telegram.chat_id:
        raise ConfigError("telegram.admin_chat_id must not be the channel. Alerts never go to the channel.")


def load_env(path: Path) -> None:
    """Minimal .env reader. Values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load(path: str | Path | None = None, env: str | Path | None = None) -> Settings:
    path = Path(path) if path else ROOT / "config.yaml"
    load_env(Path(env) if env else path.parent / ".env")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # in the container config.yaml is mounted at /app, next to camera/, library/ and prompts/
    s = Settings(**raw, root=path.parent.resolve(), bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    check(s)
    return s
