"""Await bounded notifications after response delivery; no detached serverless tasks."""
from uuid import uuid4
from starlette.requests import ClientDisconnect

from app.notifications import logger


class AlertMiddleware:
    def __init__(self, app, notifications):
        self.app, self.notifications = app, notifications

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 200
        started = False

        async def observe(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
            await send(message)

        try:
            await self.app(scope, receive, observe)
        except (OSError, ClientDisconnect):
            return
        except Exception as exc:
            status = 500
            logger.error("Unhandled server exception (%s)", type(exc).__name__)
            if not started:
                await send({"type": "http.response.start", "status": 500,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Internal server error"}'})
        alert = scope.get("projectoria.alert")
        if not alert and status >= 500 and not scope["path"].startswith("/api/feedback"):
            alert = {"code": "knowledge_unavailable" if status == 503 else "http_500",
                     "request_id": uuid4().hex, "stage": "server", "http_status": status}
        if alert:
            await self.notifications.alert(**alert)
