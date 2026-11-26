import pytest

from app.config import Settings
from app.notifications import RedisStore


REDIS_NAMES = ["KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
               "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_READ_ONLY_TOKEN", "KV_URL", "REDIS_URL"]


@pytest.fixture
def clean_redis_env(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in REDIS_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(("variables", "expected"), [
    ({}, ("", "")),
    ({"KV_REST_API_URL": " https://market.test ", "KV_REST_API_TOKEN": " market-token "},
     ("https://market.test", "market-token")),
    ({"UPSTASH_REDIS_REST_URL": " https://legacy.test ", "UPSTASH_REDIS_REST_TOKEN": " legacy-token "},
     ("https://legacy.test", "legacy-token")),
    ({"KV_REST_API_URL": "https://market.test", "KV_REST_API_TOKEN": "market-token",
      "UPSTASH_REDIS_REST_URL": "https://legacy.test", "UPSTASH_REDIS_REST_TOKEN": "legacy-token"},
     ("https://market.test", "market-token")),
    ({"KV_REST_API_URL": " ", "KV_REST_API_TOKEN": "", "UPSTASH_REDIS_REST_URL": "https://legacy.test",
      "UPSTASH_REDIS_REST_TOKEN": "legacy-token"}, ("https://legacy.test", "legacy-token")),
    ({"KV_REST_API_URL": "https://market.test", "UPSTASH_REDIS_REST_URL": "https://legacy.test",
      "UPSTASH_REDIS_REST_TOKEN": "legacy-token"}, ("https://market.test", "")),
    ({"KV_REST_API_TOKEN": "market-token", "UPSTASH_REDIS_REST_URL": "https://legacy.test",
      "UPSTASH_REDIS_REST_TOKEN": "legacy-token"}, ("", "market-token")),
    ({"KV_REST_API_URL": "https://market.test", "KV_REST_API_READ_ONLY_TOKEN": "readonly",
      "KV_URL": "rediss://fixture", "REDIS_URL": "rediss://fixture"}, ("https://market.test", "")),
])
def test_redis_environment_pairs(clean_redis_env, variables, expected):
    for name, value in variables.items():
        clean_redis_env.setenv(name, value)
    settings = Settings.from_env()
    assert (settings.redis_url, settings.redis_token) == expected
    assert RedisStore(settings).configured == bool(all(expected))
    if settings.redis_token:
        assert settings.redis_token not in repr(settings)
