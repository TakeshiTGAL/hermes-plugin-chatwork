import asyncio

import httpx
import pytest

from client import (ChatworkAccessError, ChatworkAuthError, ChatworkClient, ChatworkRateLimited,
                    ChatworkTransientError, RATE_RESERVE)

TOKEN = "t0ken-should-never-leak-123456"


def make_client(handler, now=1_000.0):
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ChatworkClient(TOKEN, http=http, sleep=fake_sleep, clock=lambda: now), sleeps


def run(coro):
    return asyncio.run(coro)


def test_token_sent_only_in_header():
    seen = {}

    def handler(req):
        seen["header"] = req.headers.get("x-chatworktoken")
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"account_id": 1, "name": "bot"})

    c, _ = make_client(handler)
    assert run(c.me())["account_id"] == 1
    assert seen["header"] == TOKEN and TOKEN not in seen["url"]


def test_429_waits_until_reset_then_retries():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"},
                                  json={"errors": ["Rate limit exceeded"]})
        return httpx.Response(200, json=[], headers={"x-ratelimit-remaining": "299", "x-ratelimit-reset": "1300"})

    c, sleeps = make_client(handler)
    assert run(c.rooms()) == []
    assert calls["n"] == 2
    assert sleeps and 30 <= sleeps[0] <= 32  # reset (1030) - now (1000) + 1


def test_per_room_post_limit_waits_10s():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"errors": ["Rate limit for message posting per room exceeded."]})
        return httpx.Response(200, json={"message_id": "42"})

    c, sleeps = make_client(handler)
    assert run(c.post_message("55", "hi")) == "42"
    assert sleeps == [10.0]


def test_persistent_429_raises_with_advice():
    def handler(req):
        return httpx.Response(429, json={"errors": ["Rate limit exceeded"]})

    c, sleeps = make_client(handler)
    with pytest.raises(ChatworkRateLimited) as ei:
        run(c.rooms())
    assert "CHATWORK_POLL_INTERVAL" in str(ei.value)
    assert len(sleeps) == 3


def test_low_budget_waits_before_polling_but_not_before_replying():
    def handler(req):
        return httpx.Response(200, json=[] if req.method == "GET" else {"message_id": "1"},
                              headers={"x-ratelimit-remaining": str(RATE_RESERVE), "x-ratelimit-reset": "1060"})

    c, sleeps = make_client(handler)
    run(c.rooms())  # learns remaining == reserve
    assert sleeps == []
    run(c.post_message("1", "reply"))  # replies may use the reserve
    assert sleeps == []
    run(c.rooms())  # polling must wait for the window
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_401_explains_what_to_do_and_hides_token():
    def handler(req):
        return httpx.Response(401, json={"errors": ["Invalid API token"]})

    c, _ = make_client(handler)
    with pytest.raises(ChatworkAuthError) as ei:
        run(c.me())
    msg = str(ei.value)
    assert "CHATWORK_API_TOKEN" in msg and "token.php" in msg and "restart" in msg
    assert TOKEN not in msg
    assert ei.value.retryable is False


def test_missing_token_is_auth_error():
    c = ChatworkClient("", http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(ChatworkAuthError) as ei:
        run(c.me())
    assert "not set" in str(ei.value)


def test_403_is_access_error():
    c, _ = make_client(lambda r: httpx.Response(403, json={"errors": ["You don't have permission"]}))
    with pytest.raises(ChatworkAccessError) as ei:
        run(c.post_message("5", "x"))
    assert ei.value.status == 403


def test_network_error_is_transient():
    def handler(req):
        raise httpx.ConnectError("boom")

    c, _ = make_client(handler)
    with pytest.raises(ChatworkTransientError):
        run(c.rooms())


def test_room_id_must_be_numeric():
    c, _ = make_client(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        run(c.messages("../me"))


def test_replies_never_sleep_more_than_a_minute():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1250"},
                              json={"errors": ["Rate limit exceeded"]})

    c, sleeps = make_client(handler)
    with pytest.raises(ChatworkRateLimited) as ei:
        run(c.post_message("1", "reply"))
    assert sleeps == [] and calls["n"] == 1  # handed back to the gateway instead of sleeping 251s
    assert ei.value.wait > 60
