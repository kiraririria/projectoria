import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic

import httpx

from app.config import OPENROUTER_URL, Settings
from app.diagnostics import ChatDiagnostics
from app.knowledge import unit_vector
from app.token_budget import estimate_input_tokens

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PROVIDER_PRIVACY = {"data_collection": "deny"}
TRANSIENT_STATUS = {408, 500, 502, 503, 504, 520, 522, 524, 529}
MODEL_ENDPOINTS = {"embeddings", "rerank", "chat/completions"}


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str = "provider_invalid_response", retry_after_seconds: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds

    def fields(self) -> dict:
        result = {"code": self.code}
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = self.retry_after_seconds
        return result


class TemporaryProviderError(ProviderError):
    pass


class InvalidCompletion(ProviderError):
    pass


class InputLimitExceeded(ProviderError):
    def __init__(self):
        super().__init__("Вопрос и материалы получились слишком длинными. Сократите вопрос и повторите.",
                         code="input_token_limit")


def retry_delay(header: str | None) -> int | None:
    if not header or len(header) > 64:
        return None
    if header.isascii() and header.isdecimal():
        seconds = int(header)
    else:
        try:
            resume = parsedate_to_datetime(header)
            if resume.tzinfo is None:
                resume = resume.replace(tzinfo=UTC)
            seconds = math.ceil((resume - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if 1 <= seconds <= 86_400 else None


def rejected(path: str, status: int, retry_after: str | None) -> ProviderError:
    if status == 402:
        if (delay := retry_delay(retry_after)) is not None:
            return ProviderError("Сервис генерации временно ограничен. Попробуйте позже.", code="provider_rate_limited",
                                 retry_after_seconds=delay)
        return ProviderError("Сервис генерации временно недоступен. Попробуйте позже.", code="provider_budget_exhausted")
    if status == 401:
        return ProviderError("Сервис генерации временно недоступен. Попробуйте позже.", code="provider_configuration")
    if status == 403:
        return ProviderError("Сервис генерации отклонил запрос.", code="provider_forbidden")
    if status == 404 and path in MODEL_ENDPOINTS:
        return ProviderError("Выбранная модель временно недоступна.", code="provider_model_unavailable")
    if status == 429:
        return ProviderError("Достигнут лимит запросов к сервису генерации. Попробуйте позже.", code="provider_rate_limited",
                             retry_after_seconds=retry_delay(retry_after))
    if status in TRANSIENT_STATUS:
        return TemporaryProviderError("OpenRouter временно недоступен.", code="provider_unavailable",
                                      retry_after_seconds=retry_delay(retry_after))
    return ProviderError("OpenRouter не смог обработать запрос. Попробуйте позже.", code="provider_rejected")


class OpenRouter:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, *,
                 diagnostics: ChatDiagnostics | None = None):
        self.settings = settings
        self.diagnostics = diagnostics
        self.http = httpx.AsyncClient(
            base_url=OPENROUTER_URL,
            headers={"Authorization": f"Bearer {settings.api_key}", "X-Title": "Proektoriya",
                     "Accept-Encoding": "identity"},
            follow_redirects=False,
            transport=transport,
        )

    async def post(self, path: str, payload: dict, timeout: float, *, stage: str) -> dict:
        started = monotonic()
        outcome, status, usage, actual_model = "internal_error", None, None, None
        estimated = estimate_input_tokens(payload["messages"]) if path == "chat/completions" else None
        operation = {"generation": "answer", "expansion": "expanded_search", "expansion_repair": "repair"}.get(stage, stage)
        try:
            if estimated is not None and estimated > self.settings.max_input_tokens:
                if self.diagnostics:
                    self.diagnostics.stage("input_budget", started, "blocked", code="input_token_limit",
                                           operation=operation, estimated_input_tokens=estimated)
                raise InputLimitExceeded()
            try:
                async with asyncio.timeout(timeout):
                    limits = httpx.Timeout(timeout, connect=min(4.0, timeout))
                    async with self.http.stream("POST", path, json=payload, timeout=limits) as response:
                        status = response.status_code
                        retry_after = response.headers.get("retry-after")
                        if not response.is_success:
                            raise rejected(path, status, retry_after)
                        body = bytearray()
                        async for part in response.aiter_bytes(chunk_size=64 * 1024):
                            if len(body) + len(part) > MAX_RESPONSE_BYTES:
                                raise ValueError("oversized provider response")
                            body.extend(part)
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError("invalid provider envelope")
                    usage = result.get("usage")
                    actual_model = result.get("model")
                    error = result.get("error")
                    choices = result.get("choices") if path == "chat/completions" else None
                    if not error and isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        error = choices[0].get("error")
                    if error:
                        error_status = error.get("code") if isinstance(error, dict) else None
                        if type(error_status) is int and 400 <= error_status <= 599:
                            raise rejected(path, error_status, retry_after)
                        raise ValueError("invalid provider error envelope")
            except (httpx.TimeoutException, TimeoutError):
                raise TemporaryProviderError("OpenRouter отвечает слишком долго.", code="provider_timeout") from None
            except httpx.TransportError:
                raise TemporaryProviderError("Не удалось связаться с OpenRouter.", code="provider_unavailable") from None
            except (httpx.HTTPError, ValueError, RecursionError):
                raise ProviderError("Не удалось получить ответ от OpenRouter. Попробуйте позже.") from None
            outcome = "ok"
            return result
        except ProviderError as exc:
            outcome = exc.code
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if self.diagnostics:
                if status is not None or outcome != "input_token_limit":
                    self.diagnostics.provider_call(
                        stage, started, outcome, status, usage, operation=operation,
                        requested_model=payload.get("model"), model=actual_model, estimated_input_tokens=estimated,
                    )

    async def embed(self, texts: list[str], *, timeout: float = 3.5) -> list[tuple[float, ...]]:
        data = await self.post("embeddings", {
            "model": self.settings.embedding_model, "input": texts, "encoding_format": "float",
            "provider": PROVIDER_PRIVACY,
        }, timeout, stage="embedding")
        try:
            by_position = {item["index"]: unit_vector(item["embedding"]) for item in data["data"]}
            if len(data["data"]) != len(texts) or set(by_position) != set(range(len(texts))):
                raise ValueError("incomplete embedding batch")
            return [by_position[i] for i in range(len(texts))]
        except (ValueError, KeyError, TypeError, OverflowError):
            raise ProviderError("Не удалось получить эмбеддинги OpenRouter.") from None

    async def rerank(self, query: str, documents: list[str], *, timeout: float = 2.5) -> list[int]:
        data = await self.post("rerank", {
            "model": self.settings.rerank_model, "query": query, "documents": documents,
            "top_n": len(documents), "provider": PROVIDER_PRIVACY,
        }, timeout, stage="rerank")
        try:
            order = [item["index"] for item in data["results"]]
            if (not order or any(type(index) is not int or not 0 <= index < len(documents) for index in order)
                    or len(set(order)) != len(order)):
                raise ValueError("invalid reranker indices")
            return order
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Реранкер OpenRouter недоступен.") from None

    async def chat(self, messages: list[dict], *, timeout: float = 20, stage: str = "generation") -> str:
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
        data = await self.post("chat/completions", payload, timeout, stage=stage)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") in {"length", "content_filter", "error"} or not isinstance(content, str) or not content.strip():
                raise ValueError("incomplete completion")
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise InvalidCompletion("Модель вернула незавершённый ответ.") from None
