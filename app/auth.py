"""Password hashing, browser sessions and API keys."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import HTTPException, Request

from .userstore import UserStore

SESSION_COOKIE = "monitor_session"
API_KEY_PREFIX = "mon_"
MAX_PASSWORD_BYTES = 72  # bcrypt limit


def hash_password(password: str) -> str:
    """bcrypt in PHP's $2y$ notation, so password_verify() in PHP apps sharing the user table accepts it."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    return "$2y$" + hashed[4:]


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith(("$2y$", "$2a$", "$2b$")):
        try:
            return bcrypt.checkpw(password.encode()[:MAX_PASSWORD_BYTES], ("$2b$" + stored[4:]).encode())
        except ValueError:
            return False
    # Legacy format of earlier versions: scrypt$<salt>$<digest>
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(users: UserStore, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    users.add_session(token_hash(token), user_id, time.time() + days * 86400)
    return token


def delete_session(users: UserStore, token: str) -> None:
    users.delete_session(token_hash(token))


def create_api_key(users: UserStore, name: str) -> tuple[int, str]:
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return users.add_api_key(name, token_hash(key), key[:10]), key


def new_push_token() -> str:
    return secrets.token_urlsafe(18)


def authenticate(request: Request) -> dict:
    """FastAPI dependency: accepts a session cookie or 'Authorization: Bearer <api key>'."""
    users: UserStore = request.app.state.users
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        row = users.find_api_key(token_hash(header[7:].strip()))
        if row:
            users.touch_api_key(row["id"], time.time())
            return {"kind": "api_key", "id": row["id"], "name": row["name"]}
        raise HTTPException(401, "Ungültiger API-Key")

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = users.session_user(token_hash(token), time.time())
        if row:
            return {"kind": "user", "id": row["id"], "name": row["username"]}
    raise HTTPException(401, "Nicht angemeldet")


class LoginThrottle:
    """Simple in-memory limit on failed logins per client address."""

    def __init__(self, max_failures: int = 10, window: float = 900):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        entries = self._failures[key]
        while entries and now - entries[0] > self.window:
            entries.popleft()
        return entries

    def check(self, key: str) -> None:
        if len(self._prune(key, time.time())) >= self.max_failures:
            raise HTTPException(429, "Zu viele Fehlversuche. Bitte später erneut versuchen.")

    def fail(self, key: str) -> None:
        self._prune(key, time.time()).append(time.time())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
