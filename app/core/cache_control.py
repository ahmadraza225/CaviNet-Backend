"""API responses are not cached (NFR-2, NFR-3): they carry patient data, and a browser or a
proxy must not keep a copy. Endpoints that choose their own caching (e.g. preview images,
"private") keep it. nginx adds the other security headers for every response."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class NoStoreMiddleware:
    """Pure ASGI middleware: adds `Cache-Control: no-store` where the endpoint set none."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)
