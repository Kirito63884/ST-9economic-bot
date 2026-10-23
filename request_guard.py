"""Bounded application-side HTTP flood protection; Nginx is the first layer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import math
import os
import re
from functools import lru_cache
import time


@dataclass(frozen=True)
class Policy:
    rate: float
    burst: int


def _number(name, default, minimum, maximum):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class RequestGuard:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.enabled = os.getenv("REQUEST_GUARD_ENABLED", "true").lower() not in {
            "false",
            "0",
            "no",
        }
        self.ip = Policy(_number("GUARD_IP_RATE", 60, 5, 1000), 120)
        self.policies = {
            "read": Policy(250, 500),
            "write": Policy(40, 80),
            "auth": Policy(10, 20),
        }
        self.ip_policies = {"write": Policy(8, 32), "auth": Policy(1, 12)}
        self.slots = {"read": 32, "write": 16, "auth": 8}
        self.max_body = int(_number("GUARD_MAX_BODY_BYTES", 262144, 16384, 1048576))
        self.body_timeout = _number("GUARD_BODY_TIMEOUT", 10, 1, 60)
        self.max_keys = 8192
        self.log = logging.getLogger("strike_team9.request_guard")
        self.reset()

    def reset(self):
        self.buckets = {}
        self.global_buckets = {}
        self.inflight = dict.fromkeys(self.slots, 0)
        self.rejected = {}
        self.last_cleanup = self.last_log = self.clock()

    @staticmethod
    def kind(scope):
        path = scope.get("path", "")
        if path.rstrip("/") in {"/login", "/auth/callback", "/auth/link"}:
            return "auth"
        return (
            "read"
            if scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS"}
            else "write"
        )

    def _cleanup(self, now):
        if now - self.last_cleanup < 1:
            return
        # A full bucket can be discarded without granting extra quota.
        for key, (tokens, updated, policy) in list(self.buckets.items()):
            if tokens + max(0, now - updated) * policy.rate >= policy.burst:
                del self.buckets[key]
        self.last_cleanup = now

    def admit(self, peer, kind):
        now = self.clock()
        self._cleanup(now)
        specs = [
            (self.buckets, ("all", peer), self.ip),
            (self.global_buckets, kind, self.policies[kind]),
        ]
        if kind in self.ip_policies:
            specs.insert(1, (self.buckets, (kind, peer), self.ip_policies[kind]))
        computed = []
        wait = 0
        missing = 0
        for storage, key, policy in specs:
            if key not in storage and storage is self.buckets:
                missing += 1
            tokens, updated, _ = storage.get(key, (policy.burst, now, policy))
            tokens = min(policy.burst, tokens + max(0, now - updated) * policy.rate)
            if tokens < 1:
                wait = max(wait, (1 - tokens) / policy.rate)
            computed.append((storage, key, tokens, policy))
        if wait:
            return 429, max(1, math.ceil(wait))
        if (
            len(self.buckets) + missing > self.max_keys
            or self.inflight[kind] >= self.slots[kind]
        ):
            return 503, 1
        # Rejected requests consume neither another client's global allowance
        # nor new cache entries. Admission is synchronous on the worker loop.
        for storage, key, tokens, policy in computed:
            storage[key] = (tokens - 1, now, policy)
        self.inflight[kind] += 1
        return None

    def rejection(self, reason):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        now = self.clock()
        if now - self.last_log >= 30:
            self.log.warning(
                "HTTP protection rejected requests in the last interval: %s",
                self.rejected,
            )
            self.rejected = {}
            self.last_log = now


class RequestGuardMiddleware:
    def __init__(self, app, guard, routes=None):
        self.app, self.guard = app, guard
        self.routes = []
        if routes:
            for path, methods in routes().items():
                pattern = re.escape(path)
                pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", pattern)
                allowed = {
                    method.upper()
                    for method in methods
                    if method.upper()
                    in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                }
                if "GET" in allowed:
                    allowed.add("HEAD")
                allowed.add("OPTIONS")
                self.routes.append((re.compile(pattern), allowed))
            for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
                self.routes.append(
                    (re.compile(re.escape(path)), {"GET", "HEAD", "OPTIONS"})
                )

    @lru_cache(maxsize=512)
    def allowed_methods(self, path):
        if not self.routes:
            return None
        alternate = (
            path.rstrip("/") if path.endswith("/") and path != "/" else path + "/"
        )
        for candidate in (path, alternate):
            for pattern, methods in self.routes:
                if pattern.fullmatch(candidate):
                    return methods
        return set()

    async def reject(self, scope, send, status, message, retry=None):
        self.guard.rejection(str(status))
        body = json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]
        if retry is not None:
            headers.append((b"retry-after", str(retry).encode()))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"" if scope.get("method") == "HEAD" else body,
            }
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.guard.enabled:
            return await self.app(scope, receive, send)
        if len(scope.get("raw_path", b"")) + len(scope.get("query_string", b"")) > 8192:
            return await self.reject(scope, send, 414, "Адрес запроса слишком длинный.")
        headers = scope.get("headers", [])
        if sum(len(key) + len(value) + 4 for key, value in headers) > 32768:
            return await self.reject(
                scope, send, 431, "Заголовки запроса слишком большие."
            )
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        chunked = any(key.lower() == b"transfer-encoding" for key, value in headers)
        if lengths and chunked:
            return await self.reject(scope, send, 400, "Неоднозначный размер запроса.")
        try:
            length = int(lengths[0]) if lengths else None
            if len(lengths) > 1 or length is not None and length < 0:
                raise ValueError()
        except ValueError:
            return await self.reject(scope, send, 400, "Некорректный размер запроса.")
        max_body = (
            1_000_000
            if scope.get("method") == "POST"
            and re.fullmatch(r"/api/admin/award-design/[a-z_]+", scope.get("path", ""))
            else self.guard.max_body
        )
        if length is not None and length > max_body:
            return await self.reject(
                scope, send, 413, "Размер запроса превышает допустимый."
            )
        peer = str((scope.get("client") or ("unknown", 0))[0])[:128]
        # Do not parse user-controlled forwarding headers here. The ASGI server
        # and Nginx must supply the client address using their trusted proxies.
        kind = self.guard.kind(scope)
        denied = self.guard.admit(peer, kind)
        if denied:
            status, retry = denied
            message = (
                "Слишком много запросов. Подождите немного и повторите."
                if status == 429
                else "Сайт временно перегружен. Повторите через несколько секунд."
            )
            return await self.reject(scope, send, status, message, retry)
        try:
            methods = self.allowed_methods(scope.get("path", ""))
            if methods is not None and not methods:
                return await self.reject(scope, send, 404, "Страница не найдена.")
            if methods is not None and scope.get("method") not in methods:
                return await self.reject(
                    scope, send, 405, "Этот метод запроса не поддерживается."
                )
            body_needed = (
                scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
                or bool(length)
                or chunked
            )
            if body_needed:
                body = bytearray()

                async def read_body():
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return "disconnect"
                        if event["type"] != "http.request":
                            continue
                        chunk = event.get("body", b"")
                        if len(body) + len(chunk) > max_body:
                            return "large"
                        body.extend(chunk)
                        if not event.get("more_body", False):
                            return "ok"

                try:
                    outcome = await asyncio.wait_for(
                        read_body(), timeout=self.guard.body_timeout
                    )
                except asyncio.TimeoutError:
                    return await self.reject(
                        scope, send, 408, "Превышено время загрузки запроса."
                    )
                if outcome == "disconnect":
                    return
                if outcome == "large":
                    return await self.reject(
                        scope, send, 413, "Размер запроса превышает допустимый."
                    )
                if length is not None and length != len(body):
                    return await self.reject(
                        scope, send, 400, "Размер запроса не совпадает с заголовком."
                    )
                replayed = False

                async def buffered_receive():
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {
                            "type": "http.request",
                            "body": bytes(body),
                            "more_body": False,
                        }
                    return await receive()

                await self.app(scope, buffered_receive, send)
            else:
                await self.app(scope, receive, send)
        finally:
            self.guard.inflight[kind] -= 1
