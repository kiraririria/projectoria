import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHAT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_EMBEDDING_MODEL = "baai/bge-m3"
OPENROUTER_URL = "https://openrouter.ai/api/v1/"


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    chat_model: str = DEFAULT_CHAT_MODEL
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    rerank_model: str = ""
    max_tokens: int = 1100
    index_path: Path = ROOT / "knowledge/index.json"
    vectors_path: Path = ROOT / "knowledge/vectors.json"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            chat_model=os.getenv("OPENROUTER_CHAT_MODEL", DEFAULT_CHAT_MODEL).strip() or DEFAULT_CHAT_MODEL,
            embedding_model=os.getenv("OPENROUTER_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
            rerank_model=os.getenv("OPENROUTER_RERANK_MODEL", "").strip(),
        )
