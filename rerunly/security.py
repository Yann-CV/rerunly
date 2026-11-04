"""Who may reach the dashboard. It can read history and start agents, so requests are checked here.

Loopback connections must come from this user (Linux) and from the dashboard's own pages. Connections arriving
on any other interface must also present the access token, because they have no operating-system identity to check.
"""

import hmac
import ipaddress
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

MAX_BODY_BYTES = 16 * 1024 * 1024
TOKEN_COOKIE = "rerunly_token"  # noqa: S105 - the cookie's name
PROC_NET = Path("/proc/net")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _ip(host: str | None):
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def is_loopback_host(host: str) -> bool:
    ip = _ip(host)
    return ip.is_loopback if ip else host.lower() == "localhost"


def _parse_proc_address(value: str):
    """An address of /proc/net/tcp{,6}: hexadecimal, each 32-bit word in the kernel's little-endian order."""
    host, port = value.split(":")
    packed = bytes.fromhex(host)
    swapped = b"".join(packed[i:i + 4][::-1] for i in range(0, len(packed), 4))
    ip = ipaddress.ip_address(swapped)
    return getattr(ip, "ipv4_mapped", None) or ip, int(port, 16)


def peer_uid(client: tuple[str, int], server: tuple[str, int], proc_net: Path | None = None) -> int | None:
    """The user owning the connecting socket; None when the kernel lists no such socket.

    A client that already closed its socket is listed without its owner, so it counts as unknown."""
    wanted = (_ip(client[0]), client[1]), (_ip(server[0]), server[1])
    for name in ("tcp", "tcp6"):
        for line in ((proc_net or PROC_NET) / name).read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 7 and (_parse_proc_address(fields[1]), _parse_proc_address(fields[2])) == wanted:
                return int(fields[7])
    return None


def peer_is_this_user(scope: Mapping) -> bool:
    """Whether this user owns the connecting socket (checked on Linux only); an unknown identity is refused."""
    if not sys.platform.startswith("linux"):
        return True
    client, server = scope.get("client"), scope.get("server")
    if not client or not server or not _ip(client[0]) or not _ip(server[0]):
        return False
    try:
        return peer_uid(client, server) == os.getuid()
    except (OSError, ValueError):
        return False


def token_path(db_path: Path) -> Path:
    return db_path.parent / "access-token"


def access_token(db_path: Path) -> str:
    """The token for non-loopback access, created once and readable by this user only."""
    path = token_path(db_path)
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as stream:
        stream.write(token)
    return token


def _authorized_remote(request: Request, db_path: Path) -> bool:
    token = access_token(db_path)
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    candidates = [request.cookies.get(TOKEN_COOKIE, ""), value if scheme.lower() == "bearer" else ""]
    return any(hmac.compare_digest(c.encode(), token.encode()) for c in candidates if c)


def _reject_request(request: Request, db_path: Path):
    """The response refusing this request, or None to serve it."""
    host = request.headers.get("host", "")
    parsed_host = urlsplit("//" + host)
    hostname = parsed_host.hostname
    _ = parsed_host.port  # force validation of malformed ports
    if not hostname or parsed_host.username is not None or parsed_host.password is not None or parsed_host.path or parsed_host.query or parsed_host.fragment:
        raise ValueError("invalid host")
    listener = request.scope.get("server")
    listener_host = listener[0] if listener else ""
    if is_loopback_host(listener_host):
        if not is_loopback_host(hostname):  # DNS rebinding
            return JSONResponse({"detail": "Invalid host for a local dashboard"}, 403)
        if not peer_is_this_user(request.scope):
            return JSONResponse({"detail": "This dashboard belongs to another local user"}, 403)
    elif _ip(listener_host) and not _authorized_remote(request, db_path):
        supplied = request.query_params.get("token", "")
        if request.method == "GET" and supplied and hmac.compare_digest(supplied.encode(), access_token(db_path).encode()):
            # The access link: keep the token out of the address bar and history.
            response = RedirectResponse(request.scope.get("root_path", "") + request.url.path.removeprefix(request.scope.get("root_path", "")), 303)
            response.set_cookie(TOKEN_COOKIE, supplied, httponly=True, samesite="strict")
            return response
        return JSONResponse({"detail": "Open this dashboard with the access link printed when the server started."}, 401)
    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlsplit(origin)
        if (parsed_origin.scheme not in ("http", "https") or parsed_origin.scheme != request.url.scheme
                or parsed_origin.netloc.lower() != host.lower()
                or parsed_origin.username is not None or parsed_origin.password is not None
                or parsed_origin.path or parsed_origin.query or parsed_origin.fragment):
            return JSONResponse({"detail": "Cross-origin requests are not allowed"}, 403)
    # Another localhost port is "same-site", yet it is another application.
    if (request.headers.get("sec-fetch-site", "same-origin").lower() not in ("same-origin", "none")
            and (request.method not in ("GET", "HEAD") or request.url.path.startswith(("/api/", "/examples/api/")))):
        return JSONResponse({"detail": "Cross-site requests are not allowed"}, 403)
    return None


def protect(app: FastAPI, db_path: Path) -> None:
    """Reject other users, other sites and DNS rebinding; require the token away from loopback.

    Local CLI clients and the telemetry hook send no Origin or Fetch Metadata."""

    @app.middleware("http")
    async def protect_dashboard(request: Request, call_next):
        try:
            rejected = _reject_request(request, db_path)
        except ValueError:
            return JSONResponse({"detail": "Invalid request host or origin"}, 400)
        response = rejected or await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


class BodyLimit:
    """Refuse request bodies over `limit` bytes, whether or not they announce their length."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES):
        self.app, self.limit = app, limit

    async def _too_large(self, send) -> None:
        body = b'{"detail":"Request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return  # the client went away
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            more = message.get("more_body", False)
            if size > self.limit:
                await self._too_large(send)
                return
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)
