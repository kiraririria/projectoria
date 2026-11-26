import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHAT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_EMBEDDING_MODEL = "baai/bge-m3"
OPENROUTER_URL = "https://openrouter.ai/api/v1/"
DEFAULT_MAX_INPUT_TOKENS = 6000


def env_flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"true", "1"}


def redis_credentials() -> tuple[str, str]:
    """Prefer the Marketplace pair; never combine credentials from different pairs."""
    url = os.getenv("KV_REST_API_URL", "").strip()
    token = os.getenv("KV_REST_API_TOKEN", "").strip()
    if url or token:
        return url, token
    return (os.getenv("UPSTASH_REDIS_REST_URL", "").strip(),
            os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip())


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    chat_model: str = DEFAULT_CHAT_MODEL
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    rerank_model: str = ""
    max_tokens: int = 1100
    index_path: Path = ROOT / "knowledge/index.json"
    vectors_path: Path = ROOT / "knowledge/vectors.json"
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    telegram_chat_id: str = ""
    telegram_bot_token: str = field(default="", repr=False)
    telegram_alerts_enabled: bool = False
    telegram_feedback_enabled: bool = False
    redis_url: str = ""
    redis_token: str = field(default="", repr=False)
    feedback_hash_secret: str = field(default="", repr=False)
    vercel: bool = False

    def __post_init__(self):
        if self.max_input_tokens <= 0:
            raise ValueError("OPENROUTER_MAX_INPUT_TOKENS must be positive")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        redis_url, redis_token = redis_credentials()
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            chat_model=os.getenv("OPENROUTER_CHAT_MODEL", DEFAULT_CHAT_MODEL).strip() or DEFAULT_CHAT_MODEL,
            embedding_model=os.getenv("OPENROUTER_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
            rerank_model=os.getenv("OPENROUTER_RERANK_MODEL", "").strip(),
            max_input_tokens=int(os.getenv("OPENROUTER_MAX_INPUT_TOKENS", str(DEFAULT_MAX_INPUT_TOKENS))),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_alerts_enabled=env_flag("TELEGRAM_ALERTS_ENABLED"),
            telegram_feedback_enabled=env_flag("TELEGRAM_FEEDBACK_ENABLED"),
            redis_url=redis_url,
            redis_token=redis_token,
            feedback_hash_secret=os.getenv("FEEDBACK_HASH_SECRET", "").strip(),
            vercel=os.getenv("VERCEL") == "1",
        )
