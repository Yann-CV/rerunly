"""One local account with proof-based password changes, single-use recovery and revocable sessions."""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import db, security

SESSION_COOKIE = "rerunly_session"  # noqa: S105 - cookie name
SESSION_SECONDS = 12 * 60 * 60
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 1024
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
STATIC = Path(__file__).parent / "static"


def _password_bytes(password: str) -> bytes:
    try:
        value = password.encode("utf-8")
    except UnicodeError:
        raise ValueError("Password must contain valid Unicode text.") from None
    if len(value) > MAX_PASSWORD_BYTES:
        raise ValueError("Password is too long (maximum 1024 UTF-8 bytes).")
    return value


def _hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(_password_bytes(password), salt=bytes.fromhex(salt), n=2**17, r=8, p=1,
                          maxmem=256 * 1024 * 1024, dklen=32).hex()


class OwnershipError(ValueError):
    """Changing account security requires a previously established secret."""


class AuthenticationThrottled(OwnershipError):
    """Login and account recovery share the persistent account attempt limit."""


@dataclass(frozen=True, repr=False)
class PasswordChange:
    password_hash: str
    recovery_code: str


def has_account(db_path: Path) -> bool:
    with closing(db.connect(db_path)) as c:
        return c.execute("SELECT 1 FROM login_account WHERE id=1").fetchone() is not None


def _recovery_hash(code: str) -> str:
    return hashlib.sha256(_password_bytes(code.strip())).hexdigest()


def _verify_account(c: sqlite3.Connection, *, current_password: str | None = None, recovery_code: str | None = None) -> sqlite3.Row:
    """Verify ownership and commit failed attempts even when the caller refuses the operation.

    A write transaction serializes counters across the web server and separate CLI processes.
    The returned hashes also guard later writes against a concurrent password/code change.
    """
    if (current_password is None) == (recovery_code is None):
        raise OwnershipError("Provide the current password or a saved recovery code.")
    error = None
    with c:
        c.execute("BEGIN IMMEDIATE")
        account = c.execute("SELECT * FROM login_account WHERE id=1").fetchone()
        if account is None:
            error = OwnershipError("Create your local password first.")
        elif account["blocked_until"] > time.time():
            error = AuthenticationThrottled("Too many attempts. Try again in one minute.")
        else:
            if current_password is not None:
                matches = hmac.compare_digest(_hash_password(current_password, account["salt"]), account["password_hash"])
            else:
                matches = hmac.compare_digest(_recovery_hash(recovery_code or ""), account["recovery_hash"] or "")
            if matches:
                c.execute("UPDATE login_account SET failures=0,blocked_until=0 WHERE id=1")
            else:
                failures = account["failures"] + 1 if not account["blocked_until"] else 1
                c.execute("UPDATE login_account SET failures=?,blocked_until=? WHERE id=1",
                          (failures, time.time() + LOCKOUT_SECONDS if failures >= MAX_FAILURES else 0))
                error = OwnershipError("Incorrect password." if current_password is not None else "Incorrect recovery code.")
    if error:
        raise error
    if account is None:
        raise OwnershipError("Create your local password first.")
    return account


def set_password(db_path: Path, password: str, *, initial: bool = False,
                 current_password: str | None = None, recovery_code: str | None = None) -> PasswordChange:
    """Create the account, or change its password after proof of ownership; rotate recovery and revoke sessions."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use at least {MIN_PASSWORD_LENGTH} characters for your password.")
    _password_bytes(password)
    with closing(db.connect(db_path)) as c:
        account = c.execute("SELECT * FROM login_account WHERE id=1").fetchone()
        if account is not None:
            if initial:
                raise ValueError("The local account already exists. Sign in instead.")
            account = _verify_account(c, current_password=current_password, recovery_code=recovery_code)
        elif current_password is not None or recovery_code is not None:
            raise OwnershipError("Create your local password first.")
        salt = secrets.token_hex(16)
        digest = _hash_password(password, salt)
        new_code = "RR-" + secrets.token_urlsafe(32)
        with c:
            if account is None:
                try:
                    c.execute("INSERT INTO login_account(id,salt,password_hash,recovery_hash) VALUES(1,?,?,?)",
                              (salt, digest, _recovery_hash(new_code)))
                except sqlite3.IntegrityError:
                    raise OwnershipError("The local account already exists. Sign in instead.") from None
            else:
                changed = c.execute("UPDATE login_account SET salt=?,password_hash=?,recovery_hash=?,failures=0,blocked_until=0 "
                                    "WHERE id=1 AND password_hash=? AND recovery_hash IS ?",
                                    (salt, digest, _recovery_hash(new_code), account["password_hash"], account["recovery_hash"])).rowcount
                if not changed:
                    raise OwnershipError("Account credentials changed. Try again using the current password or recovery code.")
            c.execute("DELETE FROM login_sessions")
    return PasswordChange(digest, new_code)


def issue_recovery_code(db_path: Path, current_password: str) -> str:
    """Show a replacement code only after verifying the current password; never save its plaintext."""
    with closing(db.connect(db_path)) as c:
        account = _verify_account(c, current_password=current_password)
        code = "RR-" + secrets.token_urlsafe(32)
        with c:
            changed = c.execute("UPDATE login_account SET recovery_hash=? WHERE id=1 AND password_hash=? AND recovery_hash IS ?",
                                (_recovery_hash(code), account["password_hash"], account["recovery_hash"])).rowcount
            if not changed:
                raise OwnershipError("Account credentials changed. Try again using the current password.")
            c.execute("DELETE FROM login_sessions")
    return code


class LocalLogin:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        # Bound expensive password verification to one request at a time per installation.
        self.lock = threading.Lock()
        # A previous process may have crashed without running its shutdown hook.
        self.clear_sessions()

    def clear_sessions(self) -> None:
        """End every browser session when this application starts or stops; keep the local account."""
        with closing(db.connect(self.db_path)) as c, c:
            c.execute("DELETE FROM login_sessions")

    def configured(self) -> bool:
        return has_account(self.db_path)

    def signed_in(self, request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token or len(token) > 256:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(db.connect(self.db_path)) as c:
            return c.execute("SELECT 1 FROM login_sessions WHERE token_hash=? AND expires_at>?", (digest, time.time())).fetchone() is not None

    def machine_client(self, request: Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(token.encode(), security.access_token(self.db_path).encode())

    def can_setup(self, request: Request) -> bool:
        server, client = request.scope.get("server"), request.scope.get("client")
        return bool(server and client and security.is_loopback_host(server[0]) and security.is_loopback_host(client[0])
                    and security.peer_is_this_user(request.scope))

    def reject(self, request: Request):
        path = request.url.path
        if path in ("/login", "/auth/status", "/auth/setup", "/auth/login", "/auth/logout") or path.startswith("/static/"):
            return None
        if self.signed_in(request) or self.machine_client(request):
            return None
        if request.method in ("GET", "HEAD") and not path.startswith(("/api/", "/examples/api/", "/v1/")):
            return RedirectResponse("/login", 303)
        return JSONResponse({"detail": "Sign in to your local account."}, 401)

    def _session(self, request: Request, expected_hash: str, *, recovery_code: str | None = None) -> JSONResponse:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(db.connect(self.db_path)) as c, c:
            c.execute("DELETE FROM login_sessions WHERE expires_at<=?", (time.time(),))
            c.execute("DELETE FROM login_sessions WHERE token_hash IN (SELECT token_hash FROM login_sessions ORDER BY expires_at DESC LIMIT -1 OFFSET 31)")
            inserted = c.execute("INSERT INTO login_sessions(token_hash,expires_at) SELECT ?,? FROM login_account WHERE id=1 AND password_hash=?",
                                 (digest, time.time() + SESSION_SECONDS, expected_hash)).rowcount
        if not inserted:
            raise HTTPException(401, "Password changed. Sign in again.")
        data: dict = {"signed_in": True}
        if recovery_code is not None:
            data["recovery_code"] = recovery_code
        response = JSONResponse(data)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @staticmethod
    def _password(request: Request, body: dict) -> str:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Send JSON.")
        password = body.get("password")
        if not isinstance(password, str):
            raise HTTPException(400, "Enter your password.")
        try:
            _password_bytes(password)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return password

    def routes(self, app: FastAPI) -> None:
        @app.get("/login", include_in_schema=False)
        def login_page():
            return FileResponse(STATIC / "login.html")

        @app.get("/auth/status", include_in_schema=False)
        def status(request: Request):
            return {"configured": self.configured(), "signed_in": self.signed_in(request), "can_setup": self.can_setup(request)}

        @app.post("/auth/setup", include_in_schema=False)
        def setup(request: Request, body: dict = Body(...)):
            if not self.can_setup(request):
                raise HTTPException(403, "Create the password on this machine, or run `rerunly password` locally.")
            password = self._password(request, body)
            if body.get("confirm_password") != password:
                raise HTTPException(400, "The passwords do not match.")
            with self.lock:
                if self.configured():
                    raise HTTPException(409, "The local account already exists. Sign in instead.")
                try:
                    result = set_password(self.db_path, password, initial=True)
                except ValueError as error:
                    raise HTTPException(409 if self.configured() else 400, str(error)) from error
                return self._session(request, result.password_hash, recovery_code=result.recovery_code)

        @app.post("/auth/login", include_in_schema=False)
        def login(request: Request, body: dict = Body(...)):
            password = self._password(request, body)
            with self.lock, closing(db.connect(self.db_path)) as c:
                try:
                    account = _verify_account(c, current_password=password)
                except AuthenticationThrottled as error:
                    raise HTTPException(429, str(error), headers={"Retry-After": str(LOCKOUT_SECONDS)}) from error
                except OwnershipError as error:
                    raise HTTPException(401 if self.configured() else 403, str(error)) from error
                return self._session(request, account["password_hash"])

        @app.post("/auth/logout", include_in_schema=False)
        def logout(request: Request):
            token = request.cookies.get(SESSION_COOKIE, "")
            with closing(db.connect(self.db_path)) as c, c:
                c.execute("DELETE FROM login_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
            response = JSONResponse({"signed_in": False})
            response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict", secure=request.url.scheme == "https")
            return response
