import asyncio
import json

import httpx
import pytest

from app import diagnostics, server
from app.config import Settings
from evaluation.run import events


QUESTION = "За что отвечает Scrum Master?"
TEST_TOKEN = "fixture-authentication-value"
PRIVATE_VALUE = "fixture-private-provider-value"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def diagnostic_records(caplog):
    diagnostics.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        diagnostics.logger.removeHandler(caplog.handler)


def application(provider):
    return server.create_app(Settings(api_key=TEST_TOKEN, embedding_model=""), transport=httpx.MockTransport(provider))


async def chat(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://projectoria.test") as client:
        return await client.post("/api/chat", json={"message": QUESTION})


@pytest.mark.parametrize(("status", "retry_after", "event", "code"), [
    (401, None, "error", "provider_configuration"),
    (402, None, "error", "provider_budget_exhausted"),
    (402, "23", "error", "provider_rate_limited"),
    (403, None, "error", "provider_forbidden"),
    (404, None, "error", "provider_model_unavailable"),
    (429, "37", "error", "provider_rate_limited"),
    (429, PRIVATE_VALUE, "error", "provider_rate_limited"),
    (503, "19", "final", "provider_unavailable"),
])
async def test_provider_terminal_codes_and_retry_delays(status, retry_after, event, code, diagnostic_records):
    calls = []

    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(status, headers={"Retry-After": retry_after} if retry_after else {},
                              json={"error": {"message": PRIVATE_VALUE, "email": "private@example.test"}})

    response = await chat(application(provider))
    assert response.status_code == 200
    name, payload = list(events(iter(response.text.splitlines())))[-1]
    assert name == event and payload["code"] == code
    expected_delay = int(retry_after) if retry_after and retry_after.isdecimal() else None
    assert payload.get("retry_after_seconds") == expected_delay
    assert calls == ["/api/v1/chat/completions"]
    raw_logs = [record.getMessage() for record in diagnostic_records.records]
    rows = [json.loads(line) for line in raw_logs]
    provider_log = next(row for row in rows if row["event"] == "chat_provider_call")
    assert provider_log["http_status"] == status and provider_log["outcome"] == code
    assert rows[-1]["code"] == code and rows[-1]["provider_calls"] == 1
    assert all(PRIVATE_VALUE not in value and TEST_TOKEN not in value and "private@example.test" not in value
               for value in [response.text, *raw_logs])


@pytest.mark.parametrize("placement", ["top_level", "choice"])
async def test_http_success_can_contain_a_provider_error(placement, diagnostic_records):
    calls = []

    def provider(request):
        calls.append(request.url.path)
        error = {"code": 429, "message": PRIVATE_VALUE}
        choice = {"message": {"content": json.dumps({"status": "unrelated", "blocks": []})}}
        data = {"choices": [choice]}
        if placement == "choice":
            choice["error"] = error
        else:
            data["error"] = error
        return httpx.Response(200, headers={"Retry-After": "13"}, json=data)

    response = await chat(application(provider))
    responses = list(events(iter(response.text.splitlines())))
    assert responses[-1][0] == "error"
    assert responses[-1][1]["code"] == "provider_rate_limited"
    assert responses[-1][1]["retry_after_seconds"] == 13
    assert calls == ["/api/v1/chat/completions"]
    raw_logs = [record.getMessage() for record in diagnostic_records.records]
    rows = [json.loads(line) for line in raw_logs]
    provider_log = next(row for row in rows if row["event"] == "chat_provider_call")
    assert provider_log["http_status"] == 200 and provider_log["outcome"] == "provider_rate_limited"
    assert all(PRIVATE_VALUE not in value for value in [response.text, *raw_logs])


async def test_chat_stage_and_usage_logs_are_correlated_and_exclude_content(diagnostic_records):
    def provider(request):
        data = {"status": "unrelated", "blocks": [], "note": ""}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(data)}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 17, "provider_metadata": PRIVATE_VALUE},
        })

    response = await chat(application(provider))
    raw_logs = [record.getMessage() for record in diagnostic_records.records]
    rows = [json.loads(line) for line in raw_logs]
    assert {row["request_id"] for row in rows} == {response.headers["x-request-id"]}
    assert {row["stage"] for row in rows if row["event"] == "chat_stage"} == {
        "read_request", "knowledge", "retrieval", "verification",
    }
    assert all(row["duration_ms"] >= 0 for row in rows)
    logged_content = json.dumps(rows, ensure_ascii=False)
    assert all(value not in logged_content for value in (QUESTION, PRIVATE_VALUE, TEST_TOKEN))
    assert rows[-1] == {"event": "chat_total", "request_id": response.headers["x-request-id"],
                        "duration_ms": rows[-1]["duration_ms"], "outcome": "unrelated", "provider_calls": 1,
                        "prompt_tokens": 123, "completion_tokens": 17, "http_status": 200, "code": None,
                        "mode": "lexical"}


async def send_asgi_chat(app, disconnect, send):
    body = json.dumps({"message": QUESTION}).encode()
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        if message := next(messages, None):
            return message
        await disconnect.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "scheme": "https", "method": "POST", "path": "/api/chat", "query_string": b"",
             "headers": [(b"content-type", b"application/json")], "server": ("projectoria.test", 443)}
    await asyncio.wait_for(app(scope, receive, send), timeout=3)


async def test_asgi_disconnect_cancels_inflight_provider_call(diagnostic_records):
    disconnect = asyncio.Event()
    cancelled = asyncio.Event()
    frames = []

    async def provider(request):
        disconnect.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def send(message):
        if message["type"] == "http.response.body":
            frames.append(message["body"])

    await send_asgi_chat(application(provider), disconnect, send)
    assert cancelled.is_set()
    assert frames and not any(b"event: final" in frame or b"event: error" in frame for frame in frames)
    rows = [json.loads(record.getMessage()) for record in diagnostic_records.records]
    assert rows[-2]["event"] == "chat_provider_call" and rows[-2]["outcome"] == "cancelled"
    assert rows[-1]["event"] == "chat_total" and rows[-1]["outcome"] == "cancelled"


async def test_deadline_can_emit_terminal_event_after_slow_status_send(monkeypatch):
    app = application(lambda request: pytest.fail("The provider must not be called"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://projectoria.test") as client:
        assert (await client.get("/api/health")).status_code == 200
    monkeypatch.setattr(server, "MAX_REQUEST_SECONDS", 0.1)
    frames = []

    async def send(message):
        if message["type"] == "http.response.body":
            frames.append(message["body"])
            if message["body"].startswith(b"event: status"):
                await asyncio.sleep(0.15)

    await send_asgi_chat(app, asyncio.Event(), send)
    result = list(events(iter(b"".join(frames).decode().splitlines())))
    assert [name for name, _ in result] == ["status", "error"]
    assert result[-1][1]["code"] == "deadline_exceeded"
