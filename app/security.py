from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
}


class SecurityHeaders:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.update(RESPONSE_HEADERS)
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store, no-transform"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def require_chat_origin(request: Request) -> None:
    """Reject cross-origin browser use while allowing API clients without browser headers."""
    site = request.headers.get("sec-fetch-site")
    if site in {"same-site", "cross-site"}:
        raise HTTPException(403, "Отправьте вопрос со страницы Проектории.")
    origin = request.headers.get("origin")
    if origin is None:
        return

    default_ports = {"http": 80, "https": 443}
    try:
        browser = urlsplit(origin)
        target = urlsplit(str(request.url))
        # Fetch Metadata comes from the browser. The ASGI scheme can describe the proxy hop.
        target_scheme = browser.scheme if site == "same-origin" else target.scheme
        origin_port = browser.port if browser.port is not None else default_ports.get(browser.scheme)
        target_port = target.port if target.port is not None else default_ports.get(target_scheme)
        same_origin = (
            browser.scheme in default_ports
            and browser.scheme == target_scheme
            and bool(browser.hostname)
            and browser.hostname == target.hostname
            and origin_port == target_port
            and not (browser.path or browser.query or browser.fragment or browser.username is not None)
        )
    except ValueError:
        same_origin = False
    if not same_origin:
        raise HTTPException(403, "Отправьте вопрос со страницы Проектории.")
