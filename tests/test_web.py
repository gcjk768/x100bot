import httpx
import pytest

from x100bot.ratelimit import InCooldown
from x100bot.web import Blocked, Web


class Site:
    def __init__(self, robots="User-agent: *\nDisallow: /private/\n", routes=None):
        self.robots, self.routes, self.calls = robots, routes or {}, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, dict(req.headers)))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        route = self.routes.get(req.url.path)
        if callable(route):
            return route(req)
        if route is None:
            return httpx.Response(404, text="nope")
        return httpx.Response(200, text=route, headers={"ETag": '"v1"', "Last-Modified": "Mon, 05 Oct 2026 00:00:00 GMT"})

    def paths(self):
        return [p for _, p, _ in self.calls]


@pytest.fixture
def make_web(settings, conn, make_limiter):
    def make(site, alerts=None):
        return Web(settings, conn, make_limiter(), transport=httpx.MockTransport(site.handler),
                   alert=(lambda k, t: alerts.append((k, t))) if alerts is not None else None)
    return make


def test_robots_disallow_is_obeyed(make_web):
    site = Site(routes={"/private/x": "secret", "/ok": "fine"})
    web = make_web(site)
    with pytest.raises(Blocked):
        web.get("https://a.com/private/x")
    assert web.get("https://a.com/ok").text == "fine"
    assert "/private/x" not in site.paths()
    assert site.paths().count("/robots.txt") == 1   # read once a day per domain


def test_user_agent_is_sent(make_web, settings):
    site = Site(routes={"/ok": "fine"})
    make_web(site).get("https://a.com/ok")
    assert all(h["user-agent"] == settings.limits.web.user_agent for _, _, h in site.calls)


def test_conditional_get_sends_etag_and_if_modified_since(make_web, clock):
    seen = {}

    def feed(req):
        seen.update(req.headers)
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="<rss/>", headers={"ETag": '"v1"', "Last-Modified": "Mon, 05 Oct 2026 00:00:00 GMT"})

    site = Site(routes={"/feed/": feed})
    web = make_web(site)
    assert web.get("https://a.com/feed/", refresh="daily").text == "<rss/>"
    assert web.get("https://a.com/feed/", refresh="daily").from_cache   # same day: no request at all
    assert site.paths().count("/feed/") == 1
    clock.t += 86400
    page = web.get("https://a.com/feed/", refresh="daily")
    assert page.text == "<rss/>" and page.from_cache
    assert seen["if-none-match"] == '"v1"' and seen["if-modified-since"] == "Mon, 05 Oct 2026 00:00:00 GMT"


def test_cached_recipe_pages_are_never_refetched(make_web, clock):
    site = Site(routes={"/recipe/": "settings"})
    web = make_web(site)
    web.get("https://a.com/recipe/")
    clock.t += 400 * 86400
    assert web.get("https://a.com/recipe/").from_cache
    assert site.paths().count("/recipe/") == 1


def test_403_starts_cooldown_and_alerts(make_web):
    alerts = []
    site = Site(routes={"/x": lambda r: httpx.Response(403)})
    web = make_web(site, alerts)
    with pytest.raises(InCooldown):
        web.get("https://a.com/x")
    with pytest.raises(InCooldown):
        web.get("https://a.com/y")
    assert alerts and alerts[0][0] == "cooldown"


def test_soft_failures_retry_with_backoff(make_web, clock):
    n = {"x": 0}

    def flaky(req):
        n["x"] += 1
        return httpx.Response(503) if n["x"] < 3 else httpx.Response(200, text="ok")

    site = Site(routes={"/x": flaky})
    assert make_web(site).get("https://a.com/x").text == "ok"
    assert any(30 <= s <= 45 for s in clock.sleeps) and any(120 <= s <= 180 for s in clock.sleeps)


def test_link_check_is_cached(make_web, clock):
    site = Site(routes={"/page": "hi"})
    web = make_web(site)
    assert web.link_ok("https://a.com/page")
    assert not web.link_ok("https://a.com/missing")
    assert web.link_ok("https://a.com/page")
    assert site.paths().count("/page") == 1
    clock.t += 15 * 86400
    web.link_ok("https://a.com/page")
    assert site.paths().count("/page") == 2


def test_offline_makes_no_requests(settings, conn, make_limiter):
    from x100bot.web import Offline
    web = Web(settings, conn, make_limiter(), offline=True)
    with pytest.raises(Offline):
        web.get("https://a.com/x", check_robots=False)
