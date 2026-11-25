import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import fakeredis
import httpx
import pytest

from app.config import Settings
from app.feedback import COOKIE, MAX_FEEDBACK_BYTES
from app.notifications import Notifications, RedisStore, CRITICAL
from app.server import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup():
    settings = Settings(telegram_chat_id="-5336363160", telegram_bot_token="123:fixture-telegram-secret",
                        telegram_alerts_enabled=True, telegram_feedback_enabled=True,
                        redis_url="https://redis.test", redis_token="private-redis-token", feedback_hash_secret="s" * 32,
                        embedding_model="")
    redis = fakeredis.FakeRedis(decode_responses=True)
    messages = []
    commands = []
    behavior = {"telegram": "ok", "redis": "ok"}

    def external(request):
        payload = json.loads(request.content)
        if request.url.host == "redis.test":
            commands.append(payload)
            if behavior["redis"] == "error":
                return httpx.Response(503)
            result = redis.execute_command(*payload)
            return httpx.Response(200, json={"result": result})
        assert request.url.host == "api.telegram.org"
        messages.append(payload)
        if behavior["telegram"] == "timeout":
            raise httpx.ReadTimeout("fixture-telegram-secret")
        if behavior["telegram"] == "429":
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 123}})
        if behavior["telegram"] == "error":
            return httpx.Response(403, json={"ok": False, "description": "fixture-telegram-secret"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(external)

    def app(config=settings, **kwargs):
        return create_app(config, notification_transport=transport, **kwargs)

    return settings, redis, messages, commands, behavior, app, transport


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://projectoria.test")


async def submit(client, data=None, key=None, **kwargs):
    if COOKIE not in client.cookies:
        await client.get("/api/feedback/config")
    return await client.post("/api/feedback", content=json.dumps(data if data is not None else {"rating": 3}),
                             headers={"Content-Type": "application/json", "Idempotency-Key": key or str(uuid4()),
                                      **kwargs.pop("headers", {})}, **kwargs)


@pytest.mark.parametrize("rating", [1, 3, 5])
@pytest.mark.parametrize("comment", ["", "Хочу больше примеров 👩‍💻 <script>alert(1)</script> *текст*"])
async def test_rating_plain_text_and_no_secrets(setup, rating, comment, caplog):
    settings, _, messages, commands, _, app, _ = setup
    caplog.set_level("INFO", logger="httpx")
    async with client(app()) as api:
        response = await submit(api, {"rating": rating, "comment": comment})
    assert response.status_code == 201
    assert len(messages) == 1 and "#feedback Projectoria" in messages[0]["text"]
    assert f"({rating}/5)" in messages[0]["text"] and comment in messages[0]["text"]
    assert "parse_mode" not in messages[0] and "entities" not in messages[0]
    assert len(messages[0]["text"]) < 4096
    assert all(value not in caplog.text + response.text + repr(settings)
               for value in [settings.telegram_bot_token, settings.redis_token, settings.feedback_hash_secret])
    assert all("127.0.0.1" not in json.dumps(command) and comment not in json.dumps(command)
               for command in commands if comment)


@pytest.mark.parametrize("payload", [
    {}, {"rating": 0}, {"rating": 6}, {"rating": 1.2}, {"rating": "3"}, {"rating": True},
    {"rating": 3, "comment": None}, {"rating": 3, "extra": "private"}, {"rating": 3, "comment": "x" * 1001},
    {"rating": 3, "comment": "\x00"}, {"rating": 3, "comment": "\ud800"}, {"rating": 3, "comment": "\u202e"},
])
async def test_invalid_payloads_stop_before_telegram(setup, payload):
    _, _, messages, commands, _, app, _ = setup
    async with client(app()) as api:
        response = await submit(api, payload)
    assert response.status_code in {400, 422} and not messages and not commands


async def test_content_type_invalid_json_body_limit_and_origin(setup):
    _, _, messages, _, _, app, _ = setup
    async with client(app()) as api:
        assert (await api.post("/api/feedback", content="test")).status_code == 415
        assert (await api.post("/api/feedback", content="{", headers={"Content-Type": "application/json"})).status_code == 400
        assert (await api.post("/api/feedback", content="x" * (MAX_FEEDBACK_BYTES + 1),
                               headers={"Content-Type": "application/json"})).status_code == 413
        for headers in [{"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}]:
            assert (await submit(api, headers=headers)).status_code == 403
        async def large():
            yield b" "
            yield b"x" * MAX_FEEDBACK_BYTES
        assert (await api.post("/api/feedback", content=large(), headers={"Content-Type": "application/json"})).status_code == 413
    assert not messages


async def test_cookie_and_idempotency_are_validated(setup):
    _, _, messages, _, _, app, _ = setup
    async with client(app()) as api:
        assert (await api.post("/api/feedback", json={"rating": 3})).status_code == 403
        config = await api.get("/api/feedback/config")
        assert "HttpOnly" in config.headers["set-cookie"] and "SameSite=strict" in config.headers["set-cookie"]
        assert (await api.post("/api/feedback", json={"rating": 3})).status_code == 422
    assert not messages


async def test_idempotency_across_instances_and_duplicate_content(setup):
    _, _, messages, _, _, app, _ = setup
    key = str(uuid4())
    async with client(app()) as first, client(app()) as second:
        await first.get("/api/feedback/config")
        second.cookies.update(first.cookies)
        replies = await asyncio.gather(submit(first, key=key), submit(second, key=key))
        assert sorted(r.status_code for r in replies) in ([200, 201], [201, 409])
        assert (await submit(first, key=key)).status_code == 200
        assert (await submit(first, {"rating": 5}, key=key)).status_code == 409
        assert (await submit(first)).status_code == 409
    assert len(messages) == 1


async def test_client_rate_limit_and_retry_after(setup):
    _, redis, messages, _, _, app, _ = setup
    async with client(app()) as api:
        for rating in [1, 3, 5]:
            assert (await submit(api, {"rating": rating})).status_code == 201
            redis.delete(*list(redis.scan_iter("projectoria:feedback:burst:*")))
        limited = await submit(api, {"rating": 2})
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert len(messages) == 3


async def test_shared_ip_limit_survives_new_clients_and_new_instances(setup):
    _, _, messages, _, _, app, _ = setup
    for _ in range(30):
        async with client(app()) as api:
            assert (await submit(api)).status_code == 201
    async with client(app()) as api:
        limited = await submit(api, headers={"X-Forwarded-For": "203.0.113.99",
                                             "X-Vercel-Forwarded-For": "203.0.113.98"})
    assert limited.status_code == 429 and len(messages) == 30


async def test_client_cooldown_and_tampered_cookie_do_not_send(setup):
    _, _, messages, _, _, app, _ = setup
    async with client(app()) as api:
        assert (await submit(api)).status_code == 201
        assert (await submit(api, {"rating": 5})).status_code == 429
        cookie = api.cookies.get(COOKIE)
        api.cookies.clear()
        response = await api.post("/api/feedback", json={"rating": 5}, headers={
            "Cookie": f"{COOKIE}={cookie[:-1]}{'1' if cookie[-1] != '1' else '0'}", "Idempotency-Key": str(uuid4())})
        assert response.status_code == 403
    assert len(messages) == 1


async def test_expected_rejections_do_not_alert_and_flags_are_independent(setup):
    settings, _, messages, _, _, app, _ = setup
    config = replace(settings, telegram_feedback_enabled=False, telegram_alerts_enabled=True)
    async with client(app(config)) as api:
        assert (await submit(api)).status_code == 503
        assert (await api.post("/api/chat", json={})).status_code == 422
        assert (await api.post("/api/chat", json={"message": "x"}, headers={"Origin": "https://evil.test"})).status_code == 403
    assert not messages
    config = replace(settings, telegram_feedback_enabled=True, telegram_alerts_enabled=False)
    async with client(app(config)) as api:
        assert (await api.post("/api/chat", json={"message": "x"})).status_code == 503
        assert (await submit(api)).status_code == 201
    assert len(messages) == 1 and messages[0]["text"].startswith("#feedback")


@pytest.mark.parametrize("mode", ["timeout", "error", "429"])
async def test_telegram_failure_is_not_reported_as_success(setup, mode):
    _, _, messages, _, behavior, app, _ = setup
    behavior["telegram"] = mode
    key = str(uuid4())
    async with client(app()) as api:
        assert (await submit(api, key=key)).status_code == 503
        second = await submit(api, key=key)
        assert second.status_code in {409, 429}
    assert len(messages) == 1 and "#error" not in messages[0]["text"]


@pytest.mark.parametrize("missing", ["telegram_bot_token", "redis_url", "feedback_hash_secret"])
async def test_missing_config_fails_closed_but_chat_and_library_work(setup, missing):
    settings, _, messages, _, _, app, _ = setup
    async with client(app(replace(settings, **{missing: ""}))) as api:
        assert (await api.get("/api/feedback/config")).json() == {"enabled": False}
        assert (await submit(api)).status_code == 503
        assert (await api.get("/api/health")).status_code == 200
        assert (await api.get("/api/documents")).status_code == 200
    assert not messages


async def test_redis_unavailable_does_not_send_feedback(setup):
    _, _, messages, _, behavior, app, _ = setup
    behavior["redis"] = "error"
    async with client(app()) as api:
        assert (await submit(api)).status_code == 503
    assert not messages


async def test_distributed_alert_dedup_threshold_and_independent_errors(setup):
    settings, redis, messages, _, _, _, transport = setup
    first = Notifications(settings, store=RedisStore(settings, transport), transport=transport)
    second = Notifications(settings, store=RedisStore(settings, transport), transport=transport)
    for _ in range(4):
        await first.alert("provider_timeout", uuid4().hex, "generation", 200)
    assert not messages
    await second.alert("provider_timeout", uuid4().hex, "generation", 200)
    assert len(messages) == 1 and "Повторений за 10 минут: 5" in messages[0]["text"]
    await first.alert("provider_timeout", uuid4().hex)
    # Different stage is a different fingerprint; use the same stage to test dedup.
    await first.alert("provider_timeout", uuid4().hex, "generation", 200)
    assert len(messages) == 1
    await first.alert("knowledge_unavailable", uuid4().hex)
    assert len(messages) == 2
    for code in CRITICAL:
        await first.alert(code, uuid4().hex)
    assert len(messages) == 5
    assert redis.ttl("projectoria:error:global") > 0


async def test_alerts_never_forward_chat_content_or_credentials(setup, caplog):
    settings, _, messages, _, _, app, _ = setup
    async with client(app(settings)) as api:
        response = await api.post("/api/chat", json={"message": "fixture-private-user-question"})
    assert response.status_code == 503 and len(messages) == 1
    text = messages[0]["text"] + caplog.text
    assert "#error Projectoria" in text and "provider_configuration" in text
    assert all(value not in text for value in ["fixture-private-user-question", settings.telegram_bot_token,
                                               settings.redis_token, settings.feedback_hash_secret])


async def test_telegram_outage_does_not_break_successful_chat(setup):
    settings, _, messages, _, behavior, app, _ = setup
    behavior["telegram"] = "timeout"
    def provider(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"unrelated","blocks":[]}'}}]})
    config = replace(settings, api_key="fixture-key")
    async with client(app(config, transport=httpx.MockTransport(provider))) as api:
        response = await api.post("/api/chat", json={"message": "Привет, как дела?"})
    assert "event: final" in response.text and not messages


async def test_cancelled_chat_does_not_notify(setup):
    from test_diagnostics import send_asgi_chat
    settings, _, messages, _, _, app, _ = setup
    disconnect = asyncio.Event()
    async def provider(request):
        disconnect.set()
        await asyncio.Future()
    async def send(message):
        pass
    await send_asgi_chat(app(replace(settings, api_key="fixture-key"), transport=httpx.MockTransport(provider)),
                         disconnect, send)
    assert not messages


async def test_notifications_bound_response_size_and_reject_invalid_redis_url(setup):
    settings, _, _, _, _, _, _ = setup
    notifications = Notifications(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 65537)))
    assert await notifications.send("test") == "unknown"
    assert not RedisStore(replace(settings, redis_url="https://[")).configured


async def test_unhandled_http_errors_and_sse_errors_are_notified(setup):
    settings, _, messages, _, _, app, _ = setup
    application = app()
    @application.get("/fixture-crash")
    def crash():
        raise RuntimeError("private-secret")
    async with client(application) as api:
        response = await api.get("/fixture-crash")
    assert response.status_code == 500 and len(messages) == 1
    assert "private-secret" not in messages[0]["text"] + response.text
    def provider(request):
        return httpx.Response(401, json={"error": {"message": "private-provider-secret"}})
    async with client(app(replace(settings, api_key="fixture-key"), transport=httpx.MockTransport(provider))) as api:
        response = await api.post("/api/chat", json={"message": "Что такое Scrum?"})
    assert "event: error" in response.text and len(messages) == 2
    assert "private-provider-secret" not in messages[-1]["text"]
