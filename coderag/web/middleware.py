"""
Plain-ASGI middleware for production (SSE answer streams pass through untouched),
plus the logging setup.

  SecurityHeaders   CSP (the inline-script hashes are computed from the frontend's
                    HTML at start-up, so they never go stale), nosniff, Referrer-Policy,
                    X-Frame-Options / frame-ancestors 'none', HSTS when PUBLIC_URL is https
  BodyLimit         413 for request bodies over MAX_BODY bytes (declared or streamed)
  OriginCheck       403 for POST/PUT/PATCH/DELETE whose Origin (or Referer) isn't PUBLIC_URL
  configure_logging INFO with timestamps; uvicorn's access log without query strings
"""

import base64
import glob
import hashlib
import json
import logging
import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException

MAX_BODY = 64 * 1024
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
CDN = "https://cdn.jsdelivr.net"
INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def inline_script_hashes(frontend_dir):
    """'sha256-...' of every inline <script> (no attributes) in the frontend's HTML files, sorted."""
    hashes = set()
    for path in glob.glob(os.path.join(frontend_dir or "", "*.html")):
        with open(path, encoding="utf-8") as f:
            for body in INLINE_SCRIPT.findall(f.read()):
                digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
                hashes.add(f"'sha256-{digest}'")
    return sorted(hashes)


def content_security_policy(frontend_dir):
    scripts = " ".join(["'self'", CDN, *inline_script_hashes(frontend_dir)])
    return "; ".join([
        "default-src 'self'",
        f"script-src {scripts}",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",   # inline style= set by the JS
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: https://avatars.githubusercontent.com",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "form-action 'self'",
    ])


def origin_of(url):
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def _header(scope, name):
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _reject(send, status, message):
    body = json.dumps({"error": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SecurityHeaders:
    def __init__(self, app, frontend_dir=None, public_url=""):
        self.app = app
        self.headers = [
            (b"content-security-policy", content_security_policy(frontend_dir).encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"x-frame-options", b"DENY"),
        ]
        if public_url.startswith("https://"):
            self.headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
        self.names = {name for name, _ in self.headers}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in self.names]
                message = {**message, "headers": kept + self.headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyTooLarge(HTTPException):
    """Raised from receive() for a streamed body past the limit: FastAPI re-raises it, the app renders 413."""

    def __init__(self, limit):
        super().__init__(413, f"Request body too large (limit {limit // 1024} KB)")


class BodyLimit:
    """413 when Content-Length is over the limit, or when a streamed body grows past it."""

    def __init__(self, app, limit=MAX_BODY):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.limit
            except ValueError:
                return await _reject(send, 400, "Invalid Content-Length")
            if too_big:
                return await _reject(send, 413, f"Request body too large (limit {self.limit // 1024} KB)")
        received = 0
        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise BodyTooLarge(self.limit)
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await _reject(send, 413, f"Request body too large (limit {self.limit // 1024} KB)")


class OriginCheck:
    """
    State-changing requests from browsers must come from PUBLIC_URL's origin
    (defence in depth on top of the SameSite=Lax session cookie). Requests with
    neither Origin nor Referer (curl, server-to-server) are not browser CSRF and pass.
    """

    def __init__(self, app, public_url=""):
        self.app = app
        self.allowed = origin_of(public_url)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.allowed and scope.get("method") in UNSAFE_METHODS:
            origin = _header(scope, b"origin")
            if origin is not None and origin != "null":
                ok = origin.lower().rstrip("/") == self.allowed
            elif origin == "null":
                ok = False
            else:
                referer = _header(scope, b"referer")
                ok = referer is None or origin_of(referer) == self.allowed
            if not ok:
                return await _reject(send, 403, "Cross-site request refused")
        return await self.app(scope, receive, send)


# ── logging ──────────────────────────────────────────────────────────────────
class DropQueryStrings(logging.Filter):
    """uvicorn's access log without query strings (OAuth codes, questions in ?q=, file paths)."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging():
    """Timestamps and INFO for coderag's loggers (once); the access-log filter on uvicorn.access."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("coderag").setLevel(logging.INFO)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DropQueryStrings) for f in access.filters):
        access.addFilter(DropQueryStrings())
