from datetime import datetime

import pytest

from tests.conftest import SGT
from x100bot import config, db
from x100bot.ratelimit import BudgetExhausted, InCooldown


def test_per_domain_gap_is_random_between_min_and_max(make_limiter, clock):
    lim = make_limiter()
    times = []
    for _ in range(5):
        lim.report(lim.acquire("web:fujixweekly.com", "/x"), "ok", status=200)
        times.append(clock.t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(6 <= g <= 12 for g in gaps), gaps
    assert len({round(g, 3) for g in gaps}) > 1   # jittered


def test_domains_pace_independently(make_limiter, clock):
    lim = make_limiter()
    lim.report(lim.acquire("web:a.com"), "ok")
    t0 = clock.t
    lim.report(lim.acquire("web:b.com"), "ok")
    assert clock.t == t0   # a different domain does not wait


def test_daily_cap_and_rollover_at_midnight_singapore(make_limiter, clock):
    lim = make_limiter()
    lim.rules["web"].per_day = 3
    for _ in range(3):
        lim.report(lim.acquire("web:a.com"), "ok")
    with pytest.raises(BudgetExhausted):
        lim.acquire("web:b.com")   # the shared web budget covers every domain
    clock.t = datetime(2026, 10, 6, 23, 59, 50, tzinfo=SGT).timestamp()
    with pytest.raises(BudgetExhausted):
        lim.acquire("web:a.com")
    clock.t = datetime(2026, 10, 7, 0, 0, 5, tzinfo=SGT).timestamp()
    lim.report(lim.acquire("web:a.com"), "ok")


@pytest.mark.parametrize("status", [429, 403])
def test_hard_fail_starts_domain_cooldown(make_limiter, clock, status):
    lim = make_limiter()
    trip = lim.report(lim.acquire("web:a.com"), "hard_fail", reason=f"HTTP {status}", status=status)
    assert trip and trip.until == pytest.approx(clock.t + 24 * 3600)
    with pytest.raises(InCooldown):
        lim.acquire("web:a.com")
    lim.report(lim.acquire("web:b.com"), "ok")   # other domains are fine
    clock.t += 24 * 3600 + 1
    lim.report(lim.acquire("web:a.com"), "ok")


def test_retry_after_longer_than_cooldown_is_honoured(make_limiter, clock):
    lim = make_limiter()
    trip = lim.report(lim.acquire("web:a.com"), "hard_fail", reason="429", retry_after=48 * 3600)
    assert trip.until == pytest.approx(clock.t + 48 * 3600)


def test_three_soft_failures_in_a_row_start_cooldown(make_limiter):
    lim = make_limiter()
    assert lim.report(lim.acquire("web:a.com"), "soft_fail", reason="timeout") is None
    assert lim.report(lim.acquire("web:a.com"), "ok") is None   # an ok resets the run
    assert lim.report(lim.acquire("web:a.com"), "soft_fail") is None
    assert lim.report(lim.acquire("web:a.com"), "soft_fail") is None
    assert lim.report(lim.acquire("web:a.com"), "soft_fail") is not None
    with pytest.raises(InCooldown):
        lim.acquire("web:a.com")


def test_state_survives_a_restart(make_limiter, conn, tmp_path):
    lim = make_limiter()
    for _ in range(4):
        lim.report(lim.acquire("openmeteo"), "ok")
    lim.report(lim.acquire("web:a.com"), "hard_fail", reason="403")
    conn.close()
    lim2 = make_limiter(conn_=db.connect(tmp_path / "t.db"))
    assert lim2.state("openmeteo")["day_count"] == 4
    lim2.report(lim2.acquire("openmeteo"), "ok")
    lim2.report(lim2.acquire("openmeteo"), "ok")
    with pytest.raises(BudgetExhausted):
        lim2.acquire("openmeteo")   # max_calls_per_day 6
    with pytest.raises(InCooldown):
        lim2.acquire("web:a.com")


def test_telegram_gap_and_rolling_window(make_limiter, clock):
    lim = make_limiter()
    t0 = clock.t
    stamps = []
    for _ in range(25):
        lim.report(lim.acquire("telegram"), "ok")
        stamps.append(clock.t)
    assert all(b - a >= 1.2 for a, b in zip(stamps, stamps[1:]))
    for s in stamps:   # never more than 20 in any 60 s window
        assert sum(1 for x in stamps if s <= x < s + 60) <= 20
    assert clock.t - t0 >= 60


def test_throttled_is_not_a_failure(make_limiter):
    lim = make_limiter()
    for _ in range(5):
        assert lim.report(lim.acquire("telegram"), "throttled", reason="retry_after 3") is None
    assert lim.state("telegram")["consecutive_failures"] == 0


def test_claude_budgets(make_limiter):
    lim = make_limiter()
    for _ in range(3):
        lim.report(lim.acquire("claude"), "ok")
    with pytest.raises(BudgetExhausted):
        lim.acquire("claude")
    for _ in range(10):
        lim.report(lim.acquire("critique"), "ok")
    with pytest.raises(BudgetExhausted):
        lim.acquire("critique")


def test_requests_log_never_keeps_query_strings(make_limiter):
    lim = make_limiter()
    lim.report(lim.acquire("web:a.com", "/feed?token=secret"), "ok")
    assert lim.db.execute("SELECT target FROM requests_log").fetchone()["target"] == "/feed"


@pytest.mark.parametrize("key,value", [
    ("limits.web.per_domain_min_gap_seconds", 2.9), ("limits.web.max_requests_per_day", 501),
    ("limits.web.max_new_recipe_pages_per_day", 31), ("limits.telegram.min_gap_seconds", 0.9),
    ("limits.telegram.max_per_minute", 21), ("limits.claude.max_critiques_per_day", 31),
    ("limits.claude.max_scheduled_calls_per_day", 7)])
def test_safety_floors_refuse_looser_values(settings, key, value):
    obj = settings
    *path, last = key.split(".")
    for p in path:
        obj = getattr(obj, p)
    setattr(obj, last, value)
    with pytest.raises(config.ConfigError, match=key.replace(".", r"\.")):
        config.check(settings)


def test_default_config_passes_floors(settings):
    config.check(settings)
