"""Bounded Telegram delivery and distributed admission; never log message content or URLs."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("projectoria.chat")


class RedactBotURL(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r"https://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot[redacted]",
                            record.getMessage())
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(RedactBotURL())


async def bounded_post(client, url, **kwargs):
    async with client.stream("POST", url, **kwargs) as response:
        body = bytearray()
        async for part in response.aiter_bytes(chunk_size=8192):
            if len(body) + len(part) > 65536:
                raise ValueError("Oversized notification service response")
            body.extend(part)
        return response.status_code, json.loads(body)

FEEDBACK_ADMIT = """-- feedback_admit
local old = redis.call('GET', KEYS[1])
if old then
  if string.sub(old, 1, 64) ~= ARGV[1] then return {'conflict', 0} end
  return {string.sub(old, 66), math.max(1, redis.call('TTL', KEYS[1]))}
end
if redis.call('EXISTS', KEYS[2]) == 1 then return {'duplicate', 0} end
if redis.call('EXISTS', KEYS[7]) == 1 then return {'limited', redis.call('TTL', KEYS[7])} end
local limits = {3, 30, 1, 100}
local ttl = {86400, 86400, 60, 86400}
for i=3,6 do
  if tonumber(redis.call('GET', KEYS[i]) or '0') >= limits[i-2] then
    return {'limited', math.max(1, redis.call('TTL', KEYS[i]))}
  end
end
for i=3,6 do
  local n = redis.call('INCR', KEYS[i])
  if n == 1 then redis.call('EXPIRE', KEYS[i], ttl[i-2]) end
end
redis.call('SET', KEYS[1], ARGV[1] .. ':pending', 'EX', 86400)
redis.call('SET', KEYS[2], '1', 'EX', 86400)
return {'new', 0}
"""
FEEDBACK_FINISH = """-- feedback_finish
if ARGV[2] == 'sent' then
  redis.call('SET', KEYS[1], ARGV[1] .. ':sent', 'EX', 86400)
elseif ARGV[2] == 'rejected' then
  redis.call('DEL', KEYS[1], KEYS[2])
end
return 1
"""
ALERT_ADMIT = """-- alert_admit
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 600) end
if n < tonumber(ARGV[1]) then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[4]) == 1 then return 0 end
local total = tonumber(redis.call('GET', KEYS[3]) or '0')
if total >= 5 then return 0 end
redis.call('SET', KEYS[2], '1', 'EX', 900)
if redis.call('INCR', KEYS[3]) == 1 then redis.call('EXPIRE', KEYS[3], 60) end
return n
"""
CRITICAL = {
    "internal_error": "Внутренняя ошибка приложения.",
    "http_500": "Сервер вернул HTTP 500.",
    "knowledge_unavailable": "База знаний недоступна.",
    "provider_configuration": "Не настроено подключение к модели.",
    "provider_budget_exhausted": "Исчерпан бюджет провайдера.",
    "provider_model_unavailable": "Выбранная модель недоступна.",
    "provider_forbidden": "Провайдер отклонил запрос.",
}
TRANSIENT = {
    "provider_unavailable", "provider_timeout", "provider_invalid_response", "provider_rejected",
    "answer_unverified", "deadline_exceeded",
}
STAGES = {"admission", "configuration", "knowledge", "generation", "retry", "repair", "chat", "server"}


class StoreUnavailable(Exception):
    pass


class RedisStore:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    @property
    def configured(self):
        try:
            parts = urlsplit(self.settings.redis_url)
        except ValueError:
            return False
        return bool(parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                    and not parts.query and not parts.fragment and self.settings.redis_token)

    async def command(self, *command):
        try:
            async with asyncio.timeout(0.7):
                async with httpx.AsyncClient(transport=self.transport, timeout=0.6, follow_redirects=False) as client:
                    status, data = await bounded_post(client, self.settings.redis_url, json=list(command),
                                                      headers={"Authorization": f"Bearer {self.settings.redis_token}"})
                if status != 200:
                    raise StoreUnavailable
                if not isinstance(data, dict) or "error" in data or "result" not in data:
                    raise StoreUnavailable
                return data["result"]
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
            raise StoreUnavailable from None

    async def script(self, script, keys, *args):
        if not self.configured:
            raise StoreUnavailable
        return await self.command("EVAL", script, len(keys), *keys, *args)


def timestamp():
    return datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M UTC+5")


class Notifications:
    def __init__(self, settings, *, store=None, transport=None):
        self.settings = settings
        self.store = store or RedisStore(settings)
        self.transport = transport

    @property
    def configured(self):
        return bool(re.fullmatch(r"-?[0-9]{1,20}", self.settings.telegram_chat_id)
                    and re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.settings.telegram_bot_token))

    @property
    def feedback_ready(self):
        return bool(self.configured and self.settings.telegram_feedback_enabled and self.store.configured
                    and len(self.settings.feedback_hash_secret) >= 32)

    def digest(self, value):
        return hmac.new(self.settings.feedback_hash_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    async def send(self, text):
        """One attempt only; unknown delivery must not be retried automatically."""
        try:
            async with asyncio.timeout(1.1):
                async with httpx.AsyncClient(transport=self.transport, timeout=1, follow_redirects=False) as client:
                    status, payload = await bounded_post(client,
                        f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                        json={"chat_id": self.settings.telegram_chat_id, "text": text[:3800],
                              "link_preview_options": {"is_disabled": True}},
                    )
                if status == 200 and payload.get("ok") is True:
                    return "sent"
                if status == 429 or payload.get("error_code") == 429:
                    parameters = payload.get("parameters")
                    delay = parameters.get("retry_after", 60) if isinstance(parameters, dict) else 60
                    delay = min(86400, max(1, delay)) if type(delay) is int else 60
                    try:
                        await self.store.command("SET", "projectoria:telegram:cooldown", "1", "EX", delay)
                    except StoreUnavailable:
                        pass
                return "rejected" if payload.get("ok") is False else "unknown"
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
            return "unknown"

    async def alert(self, code, request_id, stage="server", http_status=500):
        if not self.settings.telegram_alerts_enabled or not self.configured or not self.store.configured:
            return
        if code not in CRITICAL and code not in TRANSIENT:
            return
        stage = stage if stage in STAGES else "server"
        request_id = request_id if re.fullmatch(r"[a-f0-9]{32}", request_id) else "unavailable"
        fingerprint = hashlib.sha256(f"error:server:{code}:{stage}".encode()).hexdigest()
        try:
            async with asyncio.timeout(1.9):
                count = await self.store.script(ALERT_ADMIT, [f"projectoria:error:count:{fingerprint}",
                    f"projectoria:error:dedup:{fingerprint}", "projectoria:error:global", "projectoria:telegram:cooldown"],
                    1 if code in CRITICAL else 5)
                if not count:
                    return
                result = await self.send(
                    f"#error Projectoria\nУровень: {'CRITICAL' if code in CRITICAL else 'ERROR'}\n"
                    f"Компонент: Server / Chat\nКод: {code}\nВремя: {timestamp()}\nRequest ID: {request_id}\n"
                    f"HTTP: {int(http_status)}\nОписание: {CRITICAL.get(code, 'Повторяющийся технический сбой.')}\n"
                    f"Стадия: {stage}\nПовторений за 10 минут: {int(count)}"
                )
                logger.info(json.dumps({"event": "telegram_alert", "request_id": request_id, "outcome": result}))
        except (StoreUnavailable, TimeoutError, ValueError, TypeError):
            logger.warning('{"event":"telegram_alert","outcome":"unavailable"}')
