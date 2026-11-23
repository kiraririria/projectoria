import asyncio
import json
import logging

import httpx

from app.config import OPENROUTER_URL, Settings
from app.knowledge import unit_vector

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PROVIDER_PRIVACY = {"data_collection": "deny"}
TRANSIENT_STATUS = {408, 500, 502, 503, 504, 520, 522, 524, 529}


class ProviderError(Exception):
    pass


class TemporaryProviderError(ProviderError):
    pass


class InvalidCompletion(ProviderError):
    pass


class OpenRouter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=OPENROUTER_URL,
            headers={"Authorization": f"Bearer {settings.api_key}", "X-Title": "Proektoriya",
                     "Accept-Encoding": "identity"},
            follow_redirects=False,
            transport=transport,
        )

    async def post(self, path: str, payload: dict, timeout: float) -> dict:
        try:
            async with asyncio.timeout(timeout):
                limits = httpx.Timeout(timeout, connect=min(4.0, timeout))
                async with self.http.stream("POST", path, json=payload, timeout=limits) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for part in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(part) > MAX_RESPONSE_BYTES:
                            raise ValueError("oversized provider response")
                        body.extend(part)
                result = json.loads(body)
                if not isinstance(result, dict) or result.get("error"):
                    raise ValueError("invalid provider envelope")
                return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.info("OpenRouter %s returned HTTP %s", path, status)
            if status in {401, 402, 403}:
                raise ProviderError("Сервис генерации временно недоступен. Попробуйте позже.") from None
            if status == 429:
                raise ProviderError("Сервис перегружен. Попробуйте через минуту.") from None
            if status in TRANSIENT_STATUS:
                raise TemporaryProviderError("OpenRouter временно недоступен.") from None
            raise ProviderError("OpenRouter не смог обработать запрос. Попробуйте позже.") from None
        except (httpx.TimeoutException, TimeoutError):
            raise TemporaryProviderError("OpenRouter отвечает слишком долго.") from None
        except httpx.TransportError:
            raise TemporaryProviderError("Не удалось связаться с OpenRouter.") from None
        except (httpx.HTTPError, ValueError, RecursionError):
            raise ProviderError("Не удалось получить ответ от OpenRouter. Попробуйте позже.") from None

    async def embed(self, texts: list[str], *, timeout: float = 3.5) -> list[tuple[float, ...]]:
        data = await self.post("embeddings", {
            "model": self.settings.embedding_model, "input": texts, "encoding_format": "float",
            "provider": PROVIDER_PRIVACY,
        }, timeout)
        try:
            by_position = {item["index"]: unit_vector(item["embedding"]) for item in data["data"]}
            if len(data["data"]) != len(texts) or set(by_position) != set(range(len(texts))):
                raise ValueError("incomplete embedding batch")
            return [by_position[i] for i in range(len(texts))]
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ProviderError("Не удалось получить эмбеддинги OpenRouter.") from None

    async def rerank(self, query: str, documents: list[str]) -> list[int]:
        data = await self.post("rerank", {
            "model": self.settings.rerank_model, "query": query, "documents": documents,
            "top_n": len(documents), "provider": PROVIDER_PRIVACY,
        }, 2.5)
        try:
            order = [item["index"] for item in data["results"]]
            if (not order or any(type(index) is not int or not 0 <= index < len(documents) for index in order)
                    or len(set(order)) != len(order)):
                raise ValueError("invalid reranker indices")
            return order
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Реранкер OpenRouter недоступен.") from None

    async def chat(self, messages: list[dict], *, timeout: float = 20) -> str:
        payload = {
            "model": self.settings.chat_model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_object"},
            "provider": {"require_parameters": True, "sort": "latency", **PROVIDER_PRIVACY},
        }
        if self.settings.chat_model.startswith("deepseek/deepseek-v4"):
            payload["reasoning"] = {"enabled": False}
        data = await self.post("chat/completions", payload, timeout)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") in {"length", "content_filter", "error"} or not isinstance(content, str) or not content.strip():
                raise ValueError("incomplete completion")
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise InvalidCompletion("Модель вернула незавершённый ответ.") from None
