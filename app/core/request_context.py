"""Per-request context (client IP) so services can record it in the audit log
without every route passing the request through."""

from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)


def current_client_ip() -> str | None:
    return _client_ip.get()


class RequestContextMiddleware:
    """Pure ASGI middleware: stores the client address for the duration of the request.

    Behind nginx, uvicorn's --proxy-headers puts the original client address in scope["client"].
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        token = _client_ip.set(client[0] if client else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip.reset(token)
