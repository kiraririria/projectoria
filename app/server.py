import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from functools import lru_cache
from time import monotonic

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from app.config import ROOT, Settings
from app.diagnostics import ChatDiagnostics
from app.knowledge import Knowledge, load_knowledge, load_vectors
from app.openrouter import OpenRouter, ProviderError
from app.pipeline import Pipeline
from app.retrieval import Retriever
from app.schemas import MAX_BODY_BYTES, ChatRequest
from app.security import SecurityHeaders, require_chat_origin

logger = logging.getLogger(__name__)
STATIC = ROOT / "app/static"
MAX_REQUEST_SECONDS = 52
MAX_BODY_READ_SECONDS = 5


async def read_chat(request: Request) -> ChatRequest:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, "Отправьте вопрос в формате JSON.")
    size = request.headers.get("content-length")
    if size is not None:
        if not size.isascii() or not size.isdecimal() or len(size) > 10:
            raise HTTPException(400, "Некорректный размер запроса.")
        if int(size) > MAX_BODY_BYTES:
            raise HTTPException(413, "Запрос слишком большой.")
    body = bytearray()
    try:
        async with asyncio.timeout(MAX_BODY_READ_SECONDS):
            async for part in request.stream():
                if len(body) + len(part) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Запрос слишком большой.")
                body.extend(part)
    except TimeoutError:
        raise HTTPException(408, "Запрос передавался слишком долго. Попробуйте ещё раз.") from None
    except ClientDisconnect:
        raise HTTPException(400, "Передача запроса была прервана.") from None
    try:
        return ChatRequest.model_validate_json(body, strict=True)
    except ValidationError as exc:
        if exc.errors(include_input=False, include_url=False)[0]["type"] == "json_invalid":
            raise HTTPException(400, "Некорректный JSON.") from None
        raise HTTPException(422, "Введите вопрос длиной от 1 до 3000 символов. Допустимо до 8 предыдущих сообщений.") from None


def encode_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class ChatStreamingResponse(StreamingResponse):
    def __init__(self, events: AsyncGenerator[str, None], request_id: str):
        super().__init__(events, media_type="text/event-stream", headers={
            "X-Accel-Buffering": "no", "X-Request-ID": request_id,
        })
        self.events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def deliver():
            async with aclosing(self.events):
                try:
                    await self.stream_response(send)
                except (OSError, ClientDisconnect):
                    pass

        # Monitor receive even on ASGI 2.4+, when the next send may be a long provider call away.
        sender = asyncio.create_task(deliver())
        listener = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait((sender, listener), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            sender.cancel()
            listener.cancel()
            await asyncio.gather(sender, listener, return_exceptions=True)


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
               knowledge: Knowledge | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    application = FastAPI(title="Проектория", docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(SecurityHeaders)

    @lru_cache(maxsize=1)
    def load_pipeline():
        corpus = knowledge or load_knowledge(settings.index_path)
        vectors = load_vectors(settings.vectors_path, corpus, settings.embedding_model)
        return Pipeline(Retriever(corpus, vectors))

    def pipeline() -> Pipeline:
        try:
            return load_pipeline()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.error("Knowledge index unavailable (%s)", type(exc).__name__)
            raise HTTPException(503, "База знаний временно недоступна.") from None

    @application.get("/api/health")
    def health():
        service = pipeline()
        return {
            "status": "ok", "documents": len(service.knowledge.documents), "chunks": len(service.knowledge.chunks),
            "generation_configured": bool(settings.api_key), "semantic_index": bool(service.retriever.vectors),
            "chat_model": settings.chat_model,
        }

    @application.get("/api/documents")
    def documents():
        corpus = pipeline().knowledge
        return {"documents": [
            {"id": doc.id, "title": doc.title, "publisher": doc.publisher, "version": doc.version,
             "url": doc.url, "description": doc.description, "sections": [
                {"id": ch.id, "section": ch.section} for ch in corpus.chunks.values() if ch.document_id == doc.id
            ]} for doc in corpus.documents.values()
        ]}

    @application.get("/api/sources/{source_id}")
    def source(source_id: str):
        corpus = pipeline().knowledge
        chunk = corpus.chunks.get(source_id)
        if chunk is None:
            raise HTTPException(404, "Фрагмент не найден.")
        return corpus.source(chunk)

    @application.post("/api/chat")
    async def chat(request: Request):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_REQUEST_SECONDS
        diagnostics = ChatDiagnostics()
        phase = "admission"
        try:
            require_chat_origin(request)
            phase = "read_request"
            started = monotonic()
            chat_request = await read_chat(request)
            diagnostics.stage(phase, started, "ok")
            phase = "configuration"
            if not settings.api_key:
                raise HTTPException(503, "Модель пока не подключена. Материалы доступны в базе знаний.")
            phase = "knowledge"
            started = monotonic()
            async with asyncio.timeout_at(deadline):
                service = await asyncio.to_thread(pipeline)
            diagnostics.stage(phase, started, "ok")
        except HTTPException as exc:
            code = {"configuration": "provider_configuration", "knowledge": "knowledge_unavailable"}.get(phase)
            outcome = "error" if exc.status_code >= 500 else "rejected"
            diagnostics.finish(outcome, stage=phase, http_status=exc.status_code, code=code)
            raise
        except TimeoutError:
            diagnostics.finish("error", stage=phase, http_status=503, code="deadline_exceeded")
            raise HTTPException(503, "База знаний загружается слишком долго. Попробуйте ещё раз.") from None

        async def stream():
            outcome, fields = "cancelled", {}
            try:
                try:
                    router = OpenRouter(settings, transport, diagnostics=diagnostics)
                    async with router.http:
                        async with aclosing(service.events(chat_request, router, deadline)) as events:
                            while True:
                                if loop.time() >= deadline:
                                    raise TimeoutError
                                async with asyncio.timeout_at(deadline):
                                    item = await anext(events, None)
                                if item is None:
                                    raise RuntimeError("Chat stream ended without a terminal event")
                                name, data = item
                                if name in {"final", "error"}:
                                    break
                                yield encode_event(name, data)
                except ProviderError as exc:
                    name, data = "error", {"message": str(exc), **exc.fields()}
                except TimeoutError:
                    name, data = "error", {"code": "deadline_exceeded",
                                          "message": "Не удалось ответить вовремя. Попробуйте уточнить вопрос."}
                except Exception as exc:
                    logger.error("Chat failed (%s)", type(exc).__name__)
                    name, data = "error", {"code": "internal_error",
                                          "message": "Не удалось подготовить ответ. Попробуйте ещё раз."}

                yield encode_event(name, data)
                outcome = data["status"] if name == "final" else "error"
                fields = {"code": data.get("code"), "mode": data.get("retrieval", {}).get("mode")}
            finally:
                diagnostics.finish(outcome, http_status=200, **fields)

        return ChatStreamingResponse(stream(), diagnostics.request_id)

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    application.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return application


app = create_app()
