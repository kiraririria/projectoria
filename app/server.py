import asyncio
import json
import logging
from functools import lru_cache

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import ROOT, Settings
from app.knowledge import Knowledge, load_knowledge, load_vectors
from app.openrouter import OpenRouter, ProviderError
from app.pipeline import Pipeline
from app.retrieval import Retriever
from app.schemas import MAX_BODY_BYTES, ChatRequest

logger = logging.getLogger(__name__)
STATIC = ROOT / "app/static"
MAX_REQUEST_SECONDS = 52


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
    async for part in request.stream():
        if len(body) + len(part) > MAX_BODY_BYTES:
            raise HTTPException(413, "Запрос слишком большой.")
        body.extend(part)
    try:
        return ChatRequest.model_validate_json(body, strict=True)
    except ValidationError as exc:
        if exc.errors(include_input=False, include_url=False)[0]["type"] == "json_invalid":
            raise HTTPException(400, "Некорректный JSON.") from None
        raise HTTPException(422, "Введите вопрос длиной от 1 до 3000 символов. Допустимо до 8 предыдущих сообщений.") from None


def encode_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def create_app(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
               knowledge: Knowledge | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    application = FastAPI(title="Проектория", docs_url=None, redoc_url=None, openapi_url=None)

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
        chat_request = await read_chat(request)
        if not settings.api_key:
            raise HTTPException(503, "Модель пока не подключена. Материалы доступны в базе знаний.")
        service = await asyncio.to_thread(pipeline)

        async def stream():
            router = OpenRouter(settings, transport)
            try:
                async with asyncio.timeout(MAX_REQUEST_SECONDS):
                    async for name, data in service.events(chat_request, router):
                        yield encode_event(name, data)
            except ProviderError as exc:
                yield encode_event("error", {"message": str(exc)})
            except TimeoutError:
                yield encode_event("error", {"message": "Не удалось ответить вовремя. Попробуйте уточнить вопрос."})
            except Exception as exc:
                logger.error("Chat failed (%s)", type(exc).__name__)
                yield encode_event("error", {"message": "Не удалось подготовить ответ. Попробуйте ещё раз."})
            finally:
                await router.http.aclose()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
        })

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    application.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return application


app = create_app()
