import asyncio
import json

import httpx
import pytest

import app.openrouter as openrouter
import app.server as server
from app.config import ROOT, Settings
from app.schemas import MAX_BODY_BYTES
from app.security import RESPONSE_HEADERS
from evaluation.run import events

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def api(provider, *, api_key="fixture-key"):
    app = server.create_app(Settings(api_key=api_key, embedding_model=""), transport=httpx.MockTransport(provider))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://projectoria.test")


def no_provider(request):
    pytest.fail("Invalid requests must stop before OpenRouter")


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site", "Origin": "https://alt.projectoria.test"},
    {"Origin": "https://unrelated.test"},
    {"Origin": "https://unrelated.test", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://projectoria.test"},
    {"Origin": "null"},
])
async def test_cross_origin_request_stops_before_openrouter(headers):
    async with api(no_provider) as client:
        response = await client.post("/api/chat", json={"message": "Что такое Scrum?"}, headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://projectoria.test:443"},
                                      {"Origin": "https://projectoria.test", "Sec-Fetch-Site": "same-origin"}])
async def test_same_origin_and_regular_api_clients_pass_origin_gate(headers):
    async with api(no_provider, api_key="") as client:
        response = await client.post("/api/chat", json={"message": "Что такое Scrum?"}, headers=headers)
    assert response.status_code == 503
    assert "Модель пока не подключена" in response.json()["detail"]


async def test_cors_preflight_has_no_permission():
    async with api(no_provider) as client:
        response = await client.options("/api/chat", headers={
            "Origin": "https://unrelated.test", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        })
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


async def test_security_headers_on_html_assets_api_and_errors():
    async with api(no_provider, api_key="") as client:
        for url in ("/", "/static/app.js", "/api/health", "/api/sources/missing"):
            response = await client.get(url)
            for key, value in RESPONSE_HEADERS.items():
                assert response.headers[key] == value
            if url.startswith("/api/"):
                assert response.headers["Cache-Control"] == "no-store, no-transform"
        for url in ("/.env", "/static/.env", "/app/config.py", "/knowledge/vectors.json"):
            assert (await client.get(url)).status_code == 404

    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    edge_headers = {entry["key"]: entry["value"] for entry in config["headers"][0]["headers"]}
    assert edge_headers == RESPONSE_HEADERS


async def test_slow_chunked_body_times_out_before_openrouter(monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY_READ_SECONDS", 0.01)

    async def request_body():
        yield b'{"message":'
        await asyncio.sleep(0.1)
        yield b'"Scrum"}'

    async with api(no_provider) as client:
        response = await client.post("/api/chat", content=request_body(), headers={"Content-Type": "application/json"})
    assert response.status_code == 408


async def test_chunked_request_without_content_length_is_bounded():
    async def request_body():
        yield b' '
        yield b' ' * MAX_BODY_BYTES

    async with api(no_provider) as client:
        response = await client.post("/api/chat", content=request_body(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


async def test_chat_deadline_returns_terminal_sse_error(monkeypatch):
    monkeypatch.setattr(server, "MAX_REQUEST_SECONDS", 0.1)

    async def slow_provider(request):
        await asyncio.sleep(2)
        pytest.fail("Response should have been cancelled by the chat deadline")

    async with api(slow_provider) as client:
        await client.get("/api/health")  # Warm the corpus before testing the streaming deadline.
        response = await client.post("/api/chat", json={"message": "Что такое Scrum?"})
    rows = list(events(iter(response.text.splitlines())))
    assert rows[0][0] == "status" and rows[-1][0] == "error"
    assert "вовремя" in rows[-1][1]["message"]
    assert response.headers["cache-control"] == "no-store, no-transform"


async def test_provider_oversized_response_is_bounded_and_sanitized(monkeypatch):
    monkeypatch.setattr(openrouter, "MAX_RESPONSE_BYTES", 32)

    def provider(request):
        return httpx.Response(200, json={"private_mock_value": "x" * 64})

    async with api(provider) as client:
        response = await client.post("/api/chat", json={"message": "Что такое Scrum?"})
    rows = list(events(iter(response.text.splitlines())))
    assert rows[-1][0] == "error"
    assert "private_mock_value" not in response.text


async def test_provider_redirect_is_not_followed():
    targets = []

    def provider(request):
        targets.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://unrelated.test/"})

    async with api(provider) as client:
        response = await client.post("/api/chat", json={"message": "Что такое Scrum?"})
    assert list(events(iter(response.text.splitlines())))[-1][0] == "error"
    assert targets == ["openrouter.ai"]
