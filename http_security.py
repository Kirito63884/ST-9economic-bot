"""Browser response hardening without weakening the existing rate/body guard."""


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
                    ),
                    (b"referrer-policy", b"same-origin"),
                    (
                        b"permissions-policy",
                        b"camera=(), microphone=(), geolocation=()",
                    ),
                ]
                existing = {key.lower() for key, _ in headers}
                headers.extend(
                    (key, value) for key, value in defaults if key not in existing
                )
                path = scope.get("path", "")
                if not path.startswith(
                    ("/medal-assets/", "/award-images/", "/favicon")
                ):
                    cache = next(
                        (v for k, v in headers if k.lower() == b"cache-control"), b""
                    )
                    if b"no-store" not in cache.lower():
                        headers = [
                            (k, v) for k, v in headers if k.lower() != b"cache-control"
                        ]
                        headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secure_send)
