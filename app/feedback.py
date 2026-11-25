import asyncio
import hmac
import ipaddress
import json
import re
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import ClientDisconnect

from app.notifications import FEEDBACK_ADMIT, FEEDBACK_FINISH, StoreUnavailable, timestamp
from app.schemas import valid_text
from app.security import require_chat_origin

COOKIE = "projectoria_feedback_client"
MAX_FEEDBACK_BYTES = 8192
UNAVAILABLE = "Отзывы временно недоступны. Попробуйте позже."


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)

    @field_validator("comment")
    @classmethod
    def safe_comment(cls, value):
        valid_text(value)
        # Disallow invisible direction controls and NUL-like text tricks; keep emoji ZWJ.
        if any(0x202A <= ord(char) <= 0x202E or 0x2066 <= ord(char) <= 0x2069 for char in value):
            raise ValueError("Недопустимый символ")
        return value.strip()


async def read_feedback(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Отправьте отзыв в формате JSON.")
    size = request.headers.get("content-length")
    if size is not None:
        if not size.isascii() or not size.isdecimal() or len(size) > 10:
            raise HTTPException(400, "Некорректный размер запроса.")
        if int(size) > MAX_FEEDBACK_BYTES:
            raise HTTPException(413, "Отзыв слишком большой.")
    body = bytearray()
    try:
        async with asyncio.timeout(3):
            async for part in request.stream():
                if len(body) + len(part) > MAX_FEEDBACK_BYTES:
                    raise HTTPException(413, "Отзыв слишком большой.")
                body.extend(part)
    except TimeoutError:
        raise HTTPException(408, "Отзыв передавался слишком долго.") from None
    except ClientDisconnect:
        raise HTTPException(400, "Передача отзыва прервана.") from None
    try:
        return Feedback.model_validate_json(body, strict=True)
    except ValidationError as exc:
        code = 400 if exc.errors(include_input=False)[0]["type"] == "json_invalid" else 422
        raise HTTPException(code, "Выберите оценку от 1 до 5. Комментарий — до 1000 символов.") from None


def client_id(request, notifications):
    cookie = request.cookies.get(COOKIE, "")
    if re.fullmatch(r"[a-f0-9]{32}\.[a-f0-9]{64}", cookie):
        identity, signature = cookie.split(".")
        if hmac.compare_digest(signature, notifications.digest("client:" + identity)):
            return identity
    return None


def source_ip(request, settings):
    # Only Vercel's platform-overwritten header is trusted, and only on Vercel.
    raw = request.headers.get("x-vercel-forwarded-for", "") if settings.vercel else ""
    raw = raw or (request.client.host if request.client else "unknown")
    try:
        address = ipaddress.ip_address(raw)
        return str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)
    except ValueError:
        return "unknown"


def register_feedback(application, notifications):
    @application.get("/api/feedback/config")
    async def feedback_config(request: Request):
        require_chat_origin(request)
        response = JSONResponse({"enabled": notifications.feedback_ready})
        if notifications.feedback_ready and not client_id(request, notifications):
            identity = uuid4().hex
            response.set_cookie(COOKIE, identity + "." + notifications.digest("client:" + identity),
                                max_age=31536000, httponly=True, secure=request.url.scheme == "https",
                                samesite="strict", path="/api/feedback")
        return response

    @application.post("/api/feedback")
    async def feedback(request: Request):
        require_chat_origin(request)
        data = await read_feedback(request)
        if not notifications.feedback_ready:
            raise HTTPException(503, UNAVAILABLE)
        identity = client_id(request, notifications)
        if identity is None:
            raise HTTPException(403, "Откройте форму отзыва на сайте и повторите попытку.")
        idem = request.headers.get("idempotency-key", "")
        try:
            valid_key = len(idem) == 36 and str(UUID(idem)) == idem.lower()
        except ValueError:
            valid_key = False
        if not valid_key:
            raise HTTPException(422, "Не удалось распознать отправку. Откройте форму заново.")
        digest = notifications.digest(json.dumps(data.model_dump(), ensure_ascii=False, sort_keys=True))
        client = notifications.digest("client:" + identity)
        keys = ["projectoria:feedback:" + notifications.digest("idem:" + identity + idem),
                "projectoria:feedback:" + notifications.digest("duplicate:" + client + digest),
                "projectoria:feedback:client:" + client,
                "projectoria:feedback:ip:" + notifications.digest("ip:" + source_ip(request, notifications.settings)),
                "projectoria:feedback:burst:" + client, "projectoria:feedback:global", "projectoria:telegram:cooldown"]
        try:
            async with asyncio.timeout(5):
                status, retry = await notifications.store.script(FEEDBACK_ADMIT, keys, digest)
                if status == "sent":
                    return {"status": "sent", "duplicate": True}
                if status in {"pending", "conflict", "duplicate"}:
                    detail = ("Статус предыдущей отправки не подтверждён. Повтор заблокирован, чтобы не отправить отзыв дважды."
                              if status == "pending" else "Этот отзыв уже отправлялся или данные отправки изменились.")
                    raise HTTPException(409, detail)
                if status == "limited":
                    raise HTTPException(429, "Достигнут лимит отзывов. Попробуйте позже.",
                                        headers={"Retry-After": str(max(1, int(retry)))})
                if status != "new":
                    raise StoreUnavailable
                text = (f"#feedback Projectoria\nОценка: {'★' * data.rating}{'☆' * (5 - data.rating)} ({data.rating}/5)\n"
                        f"Время: {timestamp()}\nКомментарий:\n{data.comment or 'Без комментария'}")
                outcome = await notifications.send(text)
                await notifications.store.script(FEEDBACK_FINISH, keys[:2], digest, outcome)
                if outcome != "sent":
                    raise HTTPException(503, "Отправка не подтверждена. Повторите позже; повторные отправки защищены от дублирования.")
                return JSONResponse({"status": "sent"}, status_code=201)
        except (StoreUnavailable, TimeoutError, ValueError, TypeError):
            raise HTTPException(503, UNAVAILABLE) from None
