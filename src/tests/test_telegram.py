import httpx
import pytest

from tests.conftest import Bot
from x100bot.telegram import RetryTooLong, Telegram, TelegramUnavailable, esc, esc_attr, preview_options


def tg_for(bot, limiter, settings):
    return Telegram("TOKEN", limiter, settings.limits.telegram, transport=httpx.MockTransport(bot.handler))


def test_429_is_honoured_and_retried(make_limiter, settings, clock):
    bot = Bot([("sendMessage", 429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}})])
    tg = tg_for(bot, make_limiter(), settings)
    t0 = clock.t
    assert tg.send("@channel", "x") == 101
    assert len(bot.calls) == 2
    assert 31 <= clock.t - t0 <= 33 + 2


def test_retry_after_over_limit_raises_without_retrying(make_limiter, settings):
    bot = Bot([("sendMessage", 429, {"ok": False, "parameters": {"retry_after": 3600}})])
    with pytest.raises(RetryTooLong):
        tg_for(bot, make_limiter(), settings).send("@channel", "x")
    assert len(bot.calls) == 1


def test_network_errors_retry_after_2_4_8(make_limiter, settings, clock):
    bot = Bot([("*", "reset", None)] * 4)
    with pytest.raises(TelegramUnavailable):
        tg_for(bot, make_limiter(), settings).send("@channel", "x")
    assert len(bot.calls) == 4
    assert [s for s in clock.sleeps if s in (2, 4, 8)] == [2, 4, 8]


def test_silent_and_preview_options_are_sent(make_limiter, settings):
    bot = Bot()
    tg = tg_for(bot, make_limiter(), settings)
    tg.send("@channel", "x", silent=True, preview_url="https://fujixweekly.com/r/", above=True)
    tg.send("@channel", "y", silent=False)
    a, b = bot.calls[0][1], bot.calls[1][1]
    assert a["disable_notification"] is True and a["parse_mode"] == "HTML"
    assert a["link_preview_options"] == {"url": "https://fujixweekly.com/r/", "prefer_large_media": True,
                                         "show_above_text": True}
    assert b["disable_notification"] is False and b["link_preview_options"] == {"is_disabled": True}


def test_delete_messages_batches_of_100(make_limiter, settings):
    bot = Bot()
    tg = tg_for(bot, make_limiter(), settings)
    assert tg.delete_many("@channel", list(range(1, 251))) == 3
    sizes = [len(b["message_ids"]) for m, b in bot.calls if m == "deleteMessages"]
    assert sizes == [100, 100, 50]


def test_poll_backs_off_and_keeps_offset(make_limiter, settings, conn, clock):
    import threading
    from x100bot.db import kv_get
    stop = threading.Event()
    bot = Bot([("getUpdates", 500, {"ok": False})] * 4 * 2 +
              [("getUpdates", 200, {"ok": True, "result": [{"update_id": 7, "message": {"text": "hi"}}]})])
    seen = []

    def handle(u):
        seen.append(u)
        stop.set()

    tg_for(bot, make_limiter(), settings).poll(conn, handle, stop)
    assert seen and kv_get(conn, "tg_offset") == "8"
    assert 5 in clock.sleeps and 10 in clock.sleeps   # backoff between failed polls, never a tight loop
    assert bot.calls[0][1]["timeout"] == settings.limits.telegram.poll_timeout_seconds


def test_esc():
    assert esc("a<b>&'c'") == "a&lt;b&gt;&amp;'c'"
    assert esc_attr('https://x/?a=1&b="2"') == "https://x/?a=1&amp;b=&quot;2&quot;"


def test_preview_disabled_without_url():
    assert preview_options(None, True) == {"is_disabled": True}
